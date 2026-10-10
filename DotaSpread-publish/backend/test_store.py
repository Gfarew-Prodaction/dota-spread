"""Run the same queue contract on SQLite and an optional isolated PostgreSQL database."""

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from .app import create_app
from .providers import SourceError, parse_steam_page
from .service import MarketService
from .store import Store
from .test_market import steam_html


class QueueContract:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.stores = []
        self.services = []
        self.path = str(Path(self.directory.name) / 'shared.sqlite3')
        if self.database_url:
            import psycopg
            with psycopg.connect(self.database_url) as db:
                db.execute('DROP TABLE IF EXISTS snapshots, steam_jobs, source_control')
        self.store = self.new_store()

    def new_store(self):
        store = Store(self.path, database_url=self.database_url)
        self.stores.append(store)
        return store

    def tearDown(self):
        for service in self.services:
            service.close()
        for store in self.stores:
            store.close()
        self.directory.cleanup()

    def service(self, store=None, role='none'):
        service = MarketService(store or self.store, collector_role=role)
        self.services.append(service)
        return service

    @staticmethod
    def success(state):
        return {**state, 'state': 'ok', 'message': 'checked', 'retry_at': 0, 'next_at': 0}

    def seed(self):
        now = time.time()
        self.store.put_many({
            'catalog': [{'listing_id': 1, 'name': 'Test skin', 'price': '1', 'stock': 2},
                        {'listing_id': 2, 'name': 'Other skin', 'price': '1', 'stock': 2}],
            'catalog_updated_at': now,
            'fx': {'rub_per_usd': '85', 'usd_per_currency': {'USD': '1', 'EUR': '1.1'},
                   'date': '10.10.2026', 'checked_at': now},
        })

    def test_three_collectors_cannot_claim_the_same_request(self):
        stores = [self.store, self.new_store(), self.new_store()]
        barrier = threading.Barrier(3)
        def claim(index):
            barrier.wait()
            return stores[index].claim('steam', str(index), [('Test skin', 0)], now=100)
        with ThreadPoolExecutor(max_workers=3) as executor:
            claims = list(executor.map(claim, range(3)))
        self.assertEqual(sum(bool(claim.get('token')) for claim in claims), 1)
        self.assertEqual(self.store.read_market()['collector_state']['scan_turn'], 1)

    def test_collectors_append_quotes_without_overwriting_each_other(self):
        first = self.store.claim('steam', 'first', [('Test skin', 0)], now=100)
        quote_a = parse_steam_page(steam_html(), 'Test skin')
        self.store.complete('steam', first['token'], self.success, name='Test skin',
                            quote_data=quote_a, checked=True, now=101)
        second_store = self.new_store()
        # The stale candidate Test skin is skipped; the next unclaimed item wins.
        second = second_store.claim('steam', 'second', [('Test skin', 0), ('Other skin', 0)], now=102)
        self.assertEqual(second['name'], 'Other skin')
        quote_b = parse_steam_page(steam_html('Other skin', bid=200), 'Other skin')
        second_store.complete('steam', second['token'], self.success, name='Other skin',
                              quote_data=quote_b, checked=True, now=103)
        quotes = self.store.get('quotes')
        self.assertEqual(set(quotes), {'Test skin', 'Other skin'})
        self.assertEqual(quotes['Test skin']['bid_cents'], 138)
        self.assertEqual(quotes['Other skin']['bid_cents'], 200)

    def test_crashed_collector_job_is_reclaimed_and_late_reply_is_fenced(self):
        first = self.store.claim('steam', 'first', [('Test skin', 0)], now=100, lease_seconds=10)
        self.assertNotIn('token', self.store.claim('steam', 'second', [('Test skin', 100)], now=109))
        second = self.new_store().claim('steam', 'second', [('Test skin', 100)], now=111, lease_seconds=10)
        self.assertIn('token', second)
        newer = parse_steam_page(steam_html(bid=200), 'Test skin')
        self.store.complete('steam', second['token'], self.success, name='Test skin',
                            quote_data=newer, checked=True, now=112)
        older = parse_steam_page(steam_html(bid=100), 'Test skin')
        result = self.store.complete('steam', first['token'], self.success, name='Test skin',
                                     quote_data=older, checked=True, now=113)
        self.assertFalse(result['accepted'])
        self.assertEqual(self.store.get('quotes')['Test skin']['bid_cents'], 200)

    def test_expired_reply_is_rejected_even_before_another_claim(self):
        claim = self.store.claim('steam', 'first', [('Test skin', 0)], now=100, lease_seconds=10)
        result = self.store.complete('steam', claim['token'], self.success, name='Test skin',
                                    quote_data={'bid_cents': 200}, checked=True, now=111)
        self.assertFalse(result['accepted'])
        self.assertEqual(self.store.get('quotes'), {})

    def test_rate_limit_from_one_collector_blocks_all_and_survives_restart(self):
        self.seed()
        first = self.service(role='steam')
        second = self.service(self.new_store(), role='steam')
        with patch('backend.service.read_steam', side_effect=SourceError('limit', 900, 'rate_limit')) as read:
            first.collect_steam()
            second.collect_steam()
            self.assertEqual(read.call_count, 1)
        restored = self.service(self.new_store(), role='steam')
        self.assertGreater(restored.retry_at['steam'], time.time() + 895)
        self.assertEqual(restored.request_interval, 8)
        self.assertEqual(len(restored.attempted), 1)

    def test_common_request_spacing_is_enforced_across_collectors(self):
        self.seed()
        first = self.service(role='steam')
        second = self.service(self.new_store(), role='steam')
        with patch('backend.service.read_steam', return_value=parse_steam_page(steam_html(), 'Test skin')) as read:
            first.collect_steam()
            second.collect_steam()
            self.assertEqual(read.call_count, 1)
        self.assertGreater(second.next_steam_at, time.time() + 3)

    def test_confirmed_absence_removes_only_that_quote(self):
        quotes = {name: parse_steam_page(steam_html(name), name) for name in ('Test skin', 'Other skin')}
        self.store.put('quotes', quotes)
        claim = self.store.claim('steam', 'first', [('Test skin', 0)], now=100)
        self.store.complete('steam', claim['token'], self.success, name='Test skin', checked=True, now=101)
        self.assertEqual(set(self.store.get('quotes')), {'Other skin'})
        self.assertEqual(set(self.store.get('collector_state')['checked_names']), set(quotes))

    def test_network_failure_keeps_previous_quote(self):
        quote = parse_steam_page(steam_html(), 'Test skin')
        self.store.put('quotes', {'Test skin': quote})
        claim = self.store.claim('steam', 'first', [('Test skin', 0)], now=100)
        self.store.complete('steam', claim['token'], self.success, name='Test skin', checked=False, now=101)
        self.assertEqual(self.store.get('quotes')['Test skin'], quote)

    def test_catalog_has_one_owner_and_does_not_hold_the_steam_lock(self):
        lis = self.store.claim('lis', 'first', now=100)
        self.assertIn('token', lis)
        self.assertNotIn('token', self.new_store().claim('lis', 'second', now=101))
        self.assertIn('token', self.store.claim('steam', 'second', [('Test skin', 0)], now=101))

    def test_quote_and_backoff_update_roll_back_together(self):
        claim = self.store.claim('steam', 'first', [('Test skin', 0)], now=100)
        def fail_update(state):
            raise RuntimeError('transaction failed')
        with self.assertRaises(RuntimeError):
            self.store.complete('steam', claim['token'], fail_update, name='Test skin',
                                quote_data={'bid_cents': 200}, checked=True, now=101)
        self.assertEqual(self.store.get('quotes'), {})
        self.assertNotIn('token', self.store.claim('steam', 'second', [('Test skin', 100)], now=102))

    def test_web_only_instance_reads_results_after_collector_restart(self):
        self.seed()
        web = self.service(role='none')
        collector = self.service(self.new_store(), role='steam')
        with patch('backend.service.read_steam', return_value=parse_steam_page(steam_html(), 'Test skin')):
            collector.collect_steam()
        web.refresh_shared(force=True)
        client = create_app(web).test_client()
        result = client.get('/api/opportunities').json
        self.assertEqual(result['profitable_total'], 1)
        self.assertEqual(result['checked_total'], 1)
        self.assertEqual(result['storage']['backend'], self.store.backend)
        self.assertFalse(client.get('/api/health').json['collector_running'])
        restarted = self.service(self.new_store())
        self.assertEqual(restarted.snapshot()['items'][0]['name'], 'Test skin')

    def test_database_outage_serves_last_prices_and_stops_external_requests(self):
        self.seed()
        self.store.put('quotes', {'Test skin': parse_steam_page(steam_html(), 'Test skin')})
        service = self.service(role='steam')
        with patch.object(self.store, 'read_market', side_effect=OSError('database down')):
            self.assertFalse(service.refresh_shared(force=True))
            result = service.snapshot()
        self.assertEqual(result['profitable_total'], 1)
        self.assertEqual(result['storage']['state'], 'error')
        with patch('backend.service.read_steam') as read:
            service.collect_steam()
            read.assert_not_called()
        self.assertTrue(service.refresh_shared(force=True))
        self.assertEqual(service.snapshot()['storage']['state'], 'ok')

    def test_incremental_reads_skip_unchanged_catalog_and_include_quote_deletions(self):
        self.seed()
        self.store.put('quotes', {'Test skin': parse_steam_page(steam_html(), 'Test skin')})
        initial = self.store.read_market()
        unchanged = self.store.read_market(initial['snapshot_versions'], initial['steam_revision'])
        self.assertNotIn('catalog', unchanged)
        self.assertNotIn('fx', unchanged)
        self.assertEqual(unchanged['quotes'], {})
        claim = self.store.claim('steam', 'first', [('Test skin', 0)], now=100)
        self.store.complete('steam', claim['token'], self.success, name='Test skin', checked=True, now=101)
        delta = self.store.read_market(initial['snapshot_versions'], initial['steam_revision'])
        self.assertNotIn('catalog', delta)
        self.assertEqual(delta['quotes'], {'Test skin': None})
        catalog = self.store.get('catalog')[:1]
        self.store.put('catalog', catalog)
        next_delta = self.store.read_market(delta['snapshot_versions'], delta['steam_revision'])
        self.assertEqual(next_delta['catalog'], catalog)
        self.assertNotIn('fx', next_delta)
        self.assertEqual(next_delta['quotes'], {})


class SQLiteQueueTests(QueueContract, unittest.TestCase):
    database_url = None

    def test_legacy_cache_and_cooldown_are_migrated_without_losing_progress(self):
        path = str(Path(self.directory.name) / 'legacy.sqlite3')
        quote = parse_steam_page(steam_html(), 'Test skin')
        now = time.time()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE snapshots (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            for key, value in {'quotes': {'Test skin': quote}, 'collector_state': {
                    'checked_names': ['Test skin', 'Unavailable skin'],
                    'attempted': {'Test skin': now, 'Unavailable skin': now},
                    'retry_at': {'steam': now + 900}, 'request_interval': 16, 'scan_turn': 20}}.items():
                db.execute('INSERT INTO snapshots VALUES (?, ?)', (key, json.dumps(value)))
        migrated = Store(path)
        self.stores.append(migrated)
        service = self.service(migrated)
        self.assertEqual(service.quotes['Test skin']['bid_cents'], 138)
        self.assertEqual(service.checked_names, {'Test skin', 'Unavailable skin'})
        self.assertEqual(service.request_interval, 16)
        self.assertEqual(service.scan_turn, 20)
        self.assertGreater(service.retry_at['steam'], now + 899)


@unittest.skipUnless(os.getenv('DOTA_TEST_DATABASE_URL'), 'Set DOTA_TEST_DATABASE_URL to an isolated test PostgreSQL database')
class PostgreSQLQueueTests(QueueContract, unittest.TestCase):
    # This URL must point to a disposable test database: tests reset its tables.
    database_url = os.getenv('DOTA_TEST_DATABASE_URL')


if __name__ == '__main__':
    unittest.main()
