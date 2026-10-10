import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from .local_worker import load_config
from . import local_worker
from .providers import SourceError
from .service import MarketService
from .store import Store


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / '.env.collector'

    def tearDown(self):
        self.directory.cleanup()

    def test_postgres_url_with_equals_and_quotes_is_kept_literal(self):
        url = 'postgresql://user:private@localhost/test?sslmode=require&options=a=b'
        self.path.write_text(f'# comment\nDATABASE_URL="{url}"\nCOLLECTOR_ROLE=all\n', encoding='utf-8')
        environ = {}
        load_config(self.path, environ)
        self.assertEqual(environ['DATABASE_URL'], url)
        self.assertEqual(environ['STEAM_PRIORITY_SIZE'], '200')

    def test_shell_code_in_password_is_never_executed(self):
        marker = Path(self.directory.name) / 'must-not-exist'
        url = f'postgresql://user:$(touch {marker})@localhost/test'
        self.path.write_text(f'DATABASE_URL={url}\n', encoding='utf-8')
        environ = {}
        load_config(self.path, environ)
        self.assertEqual(environ['DATABASE_URL'], url)
        self.assertFalse(marker.exists())

    def test_errors_do_not_print_credentials_and_do_not_partially_load(self):
        for line in ['DATABASE_URL=psql private-secret', 'DATABASE_URL=private-secret',
                     'UNKNOWN=private-secret', 'DATABASE_URL="private-secret',
                     'DATABASE_URL=postgresql://user:private-secret@localhost/test\nSTEAM_PRIORITY_SIZE=NaN']:
            self.path.write_text(line, encoding='utf-8')
            environ = {}
            with self.subTest(line=line), self.assertRaises(ValueError) as error:
                load_config(self.path, environ)
            self.assertNotIn('private-secret', str(error.exception))
            self.assertEqual(environ, {})

    def test_environment_overrides_file_and_none_role_is_rejected(self):
        self.path.write_text('DATABASE_URL=postgresql://user:file@localhost/test\nSTEAM_PRIORITY_SIZE=200\n')
        environ = {'DATABASE_URL': 'postgresql://user:env@localhost/test', 'STEAM_PRIORITY_SIZE': '100'}
        load_config(self.path, environ)
        self.assertEqual(environ['DATABASE_URL'], 'postgresql://user:env@localhost/test')
        self.assertEqual(environ['STEAM_PRIORITY_SIZE'], '100')
        with self.assertRaises(ValueError):
            load_config(self.path, {'COLLECTOR_ROLE': 'none'})

    def test_stop_does_not_signal_an_unrelated_process(self):
        root = Path(self.directory.name)
        (root / '.cache').mkdir()
        (root / '.cache/home-collector.lock').write_text('123')
        with patch.object(local_worker, 'ROOT', root), \
             patch('backend.local_worker.fcntl.flock', side_effect=BlockingIOError), \
             patch('backend.local_worker.subprocess.run') as process, \
             patch('backend.local_worker.os.kill') as kill:
            process.return_value.stdout = 'python unrelated.py\n'
            self.assertEqual(local_worker.stop_collector(), 1)
            kill.assert_not_called()

    def test_stop_signals_only_the_confirmed_worker(self):
        root = Path(self.directory.name)
        (root / '.cache').mkdir()
        (root / '.cache/home-collector.lock').write_text('123')
        with patch.object(local_worker, 'ROOT', root), \
             patch('backend.local_worker.fcntl.flock', side_effect=BlockingIOError), \
             patch('backend.local_worker.subprocess.run') as process, \
             patch('backend.local_worker.os.kill') as kill:
            process.return_value.stdout = '/path/python -m backend.local_worker\n'
            self.assertEqual(local_worker.stop_collector(), 0)
            kill.assert_called_once_with(123, local_worker.signal.SIGTERM)


class PriceDropQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.directory.name) / 'market.sqlite3'))
        self.service = MarketService(self.store, batch_size=1, priority_size=1)
        self.now = time.time()
        self.service.fx = {'checked_at': self.now, 'usd_per_currency': {'USD': '1'}, 'rub_per_usd': '85'}
        self.service.catalog = [self.item('Common', '1', 100), self.item('Discount', '2')]
        self.service.catalog_updated_at = self.now

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    @staticmethod
    def item(name, price, stock=1):
        return {'name': name, 'price': price, 'stock': stock, 'listing_id': name}

    def change(self, price='1'):
        catalog = [self.item('Common', '1', 100), self.item('Discount', price)]
        self.service.catalog_drops = self.service.find_catalog_drops(catalog, self.now)
        self.service.catalog = catalog

    def test_price_drop_jumps_ahead_of_unseen_high_stock_items(self):
        self.change()
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Discount')

    def test_small_drop_without_evidence_does_not_consume_priority(self):
        self.change('1.99')
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Common')

    def test_small_drop_that_makes_cached_order_profitable_gets_priority(self):
        self.service.catalog[1]['price'] = '1.01'
        self.service.quotes['Discount'] = {'quantity': 1, 'bid_cents': 115, 'currency': 'USD',
                                           'checked_at': self.now - 10}
        self.change('0.99')
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Discount')

    def test_recent_negative_result_is_rechecked_after_drop_but_not_after_confirmation(self):
        self.service.attempted['Discount'] = self.now - 10
        self.service.checked_names.add('Discount')
        self.service.checked_at['Discount'] = self.now - 10
        self.change()
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Discount')
        self.service.checked_at['Discount'] = self.now + .1
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Common')

    def test_hint_does_not_bypass_request_spacing_cooldown_or_create_verified_rows(self):
        self.change()
        self.service.attempted['Discount'] = time.time()
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Common')
        self.service.retry_at['steam'] = self.now + 300
        with patch('backend.service.read_steam') as read:
            self.service.collect_steam()
            read.assert_not_called()
        result = self.service.snapshot()
        self.assertEqual(result['checked_total'], 0)
        self.assertEqual(result['items'], [])

    def test_background_discovery_is_preserved_when_a_drop_waits(self):
        self.change()
        self.service.catalog.append(self.item('Outside', '1'))
        self.service.checked_names.add('Discount')
        self.service.scan_turn = 9
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Outside')

    def test_expired_removed_and_reversed_drops_are_discarded(self):
        self.change()
        self.service.catalog_drops['Discount']['changed_at'] -= 3601
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Common')
        self.service.catalog_drops['Discount']['changed_at'] = self.now
        self.assertNotIn('Discount', self.service.find_catalog_drops([self.item('Discount', '1.5')], self.now))
        self.assertNotIn('Discount', self.service.find_catalog_drops([self.item('Common', '1')], self.now))

    def test_catalog_drop_is_atomic_persistent_and_304_does_not_refresh_hint(self):
        new_catalog = [self.item('Common', '1', 100), self.item('Discount', '1')]
        with patch('backend.service.read_catalog', return_value=(new_catalog, self.now, 0, 'new')):
            self.service.collect_catalog()
        before = copy.deepcopy(self.service.catalog_drops)
        restored = MarketService(self.store, collector_role='none')
        try:
            self.assertEqual(restored.catalog_drops, before)
            self.assertEqual(restored.catalog, new_catalog)
        finally:
            restored.close()
        self.service.catalog_checked_at = 0
        self.store.mutate_control('lis', lambda state: {**state, 'next_at': 0})
        with patch('backend.service.read_catalog', return_value=None):
            self.service.collect_catalog()
        self.assertEqual(self.service.catalog_drops, before)

    def test_failed_request_keeps_drop_for_retry(self):
        self.change()
        with patch('backend.service.read_steam', side_effect=SourceError('network', kind='network')):
            self.service.collect_steam()
        self.service.attempted['Discount'] -= 10
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Discount')


if __name__ == '__main__':
    unittest.main()
