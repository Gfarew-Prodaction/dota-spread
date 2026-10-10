import copy
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from .providers import SourceError, parse_bulk_orders, read_bulk_orders
from .service import MarketService
from .store import Store
from .app import create_app
from .test_market import steam_html
from .providers import parse_steam_page


def export(now, name='Bulk winner', bid=2, age=30):
    return {'metadata': {'appId': 570, 'generatedAt': now * 1000, 'itemCount': 1}, 'items': [{
        'marketHashName': name, 'updatedAt': (now + 20) * 1000, 'flags': {'marketable': True},
        'orderBook': {'highestBuy': bid, 'buyDepth': 9999, 'histogramAgeSeconds': age,
                      'buyOrdersTop10': [{'price': bid, 'quantity': 2}, {'price': bid - .01, 'quantity': 500}]},
        'volume': {'30d': 100}, 'signals': {'stale': False},
    }]}


def parse(data, now):
    return parse_bulk_orders(io.BytesIO(json.dumps(data).encode()), now=now)


class BulkProviderTests(unittest.TestCase):
    def test_best_bid_quantity_and_histogram_age(self):
        result = parse(export(100000), 100100)
        item = result['entries']['Bulk winner']
        self.assertEqual(item['bid_cents'], 200)
        self.assertEqual(item['quantity'], 2)
        self.assertEqual(item['updated_at'], 99970)
        self.assertEqual(result['generated_at'], 100000)

    def test_old_histogram_is_not_freshened_by_entry_or_download(self):
        item = parse(export(100000, age=4000), 100100)['entries']['Bulk winner']
        self.assertEqual(item['updated_at'], 96000)

    def test_metadata_can_follow_items(self):
        data = export(100000)
        self.assertEqual(parse({'items': data['items'], 'metadata': data['metadata']}, 100100)['item_count'], 1)

    def test_wrong_app_incomplete_and_future_exports_are_rejected(self):
        for field, value in [('appId', 730), ('itemCount', 2), ('generatedAt', 200000000)]:
            data = export(100000)
            data['metadata'][field] = value
            with self.subTest(field=field), self.assertRaises(SourceError):
                parse(data, 100100)

    def test_missing_best_quantity_age_and_non_marketable_items_are_skipped(self):
        for mutate in [lambda x: x['orderBook'].pop('buyOrdersTop10'),
                       lambda x: x['orderBook'].pop('histogramAgeSeconds'),
                       lambda x: x['flags'].update(marketable=False),
                       lambda x: x['orderBook'].update(highestBuy=-1),
                       lambda x: x['orderBook'].update(highestBuy='NaN')]:
            data = export(100000)
            mutate(data['items'][0])
            self.assertEqual(parse(data, 100100)['entries'], {})

    def test_invalid_json_and_oversized_exports_are_rejected(self):
        with self.assertRaises(SourceError):
            parse_bulk_orders(io.BytesIO(b'{'))
        with self.assertRaises(SourceError):
            parse_bulk_orders(io.BytesIO(b' ' * (32 * 1024 * 1024 + 1)))

    def test_cdn_redirect_does_not_receive_key(self):
        now = time.time()
        redirect = MagicMock(status_code=302, headers={'Location': 'https://cdn.steamapis.com/dota.json'})
        file = MagicMock(status_code=200, raw=io.BytesIO(json.dumps(export(now)).encode()))
        redirect.__enter__.return_value, file.__enter__.return_value = redirect, file
        session = MagicMock()
        session.get.side_effect = [redirect, file]
        result = read_bulk_orders(session, 'test-secret')
        self.assertEqual(result['item_count'], 1)
        self.assertEqual(session.get.call_args_list[0].kwargs['headers'], {'x-api-key': 'test-secret'})
        self.assertEqual(session.get.call_args_list[1].kwargs['headers'], {})
        self.assertFalse(session.get.call_args_list[1].kwargs['allow_redirects'])

    def test_unknown_redirect_and_access_errors_do_not_expose_key(self):
        for code, headers in [(302, {'Location': 'http://localhost/private'}), (403, {})]:
            response = MagicMock(status_code=code, headers=headers)
            response.__enter__.return_value = response
            session = MagicMock()
            session.get.return_value = response
            with self.subTest(code=code), self.assertRaises(SourceError) as error:
                read_bulk_orders(session, 'test-secret')
            self.assertNotIn('test-secret', str(error.exception))
            self.assertEqual(session.get.call_count, 1)


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.directory.name) / 'market.sqlite3'))
        self.service = MarketService(self.store, bulk_key='test-secret', priority_size=2)
        self.now = time.time()
        self.service.fx = {'checked_at': self.now, 'usd_per_currency': {'USD': '1'}, 'rub_per_usd': '85'}
        self.service.catalog = [self.item('Common A', stock=100), self.item('Common B', stock=50), self.item('Bulk winner'), self.item('Rare')]
        self.service.catalog_updated_at = self.now

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def item(self, name, stock=1):
        return {'name': name, 'price': '1', 'stock': stock, 'listing_id': name}

    def add_bulk(self, name='Bulk winner', bid=2):
        self.service.bulk_orders = parse(export(self.now, name=name, bid=bid), self.now)

    def test_bulk_winner_precedes_blind_discovery(self):
        self.add_bulk()
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Bulk winner')

    def test_nonprofitable_old_or_already_verified_hints_do_not_take_priority(self):
        for kind in ('nonprofit', 'old', 'verified'):
            self.add_bulk(bid=1 if kind == 'nonprofit' else 2)
            self.service.checked_at = {}
            if kind == 'old':
                self.service.bulk_orders['entries']['Bulk winner']['updated_at'] -= 86401
            elif kind == 'verified':
                self.service.checked_at['Bulk winner'] = self.now
            with self.subTest(kind=kind):
                self.assertEqual(self.service.choose_candidates()[0]['name'], 'Common A')

    def test_focus_and_background_discovery_are_both_served(self):
        self.service.batch_size = 1
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Common A')
        self.service.scan_turn = 9
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Bulk winner')

    def test_no_key_performs_no_bulk_requests(self):
        self.service.bulk_key = ''
        with patch('backend.service.read_bulk_orders') as read:
            self.service.collect_bulk()
            read.assert_not_called()

    def test_failed_request_does_not_discard_unverified_bulk_candidate(self):
        self.add_bulk()
        self.service.attempted['Bulk winner'] = self.now - 10
        self.assertEqual(self.service.choose_candidates()[0]['name'], 'Bulk winner')

    def test_recent_negative_result_is_not_repeated_without_a_new_hint(self):
        self.service.catalog = [self.item('Common A')]
        self.service.attempted['Common A'] = self.now - 10
        self.service.checked_at['Common A'] = self.now - 10
        self.service.checked_names.add('Common A')
        self.assertEqual(self.service.choose_candidates(), [])

    def test_api_reports_mode_without_exposing_credentials(self):
        client = create_app(self.service).test_client()
        health = client.get('/api/health')
        self.assertTrue(health.json['bulk_configured'])
        self.assertEqual(health.json['scanner_mode'], 'hybrid')
        for endpoint in ['/api/health', '/api/opportunities']:
            self.assertNotIn('test-secret', client.get(endpoint).get_data(as_text=True))

    def test_bulk_does_not_add_verified_rows_or_checked_count(self):
        self.add_bulk()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot['items'], [])
        self.assertEqual(snapshot['checked_total'], 0)
        self.assertEqual(snapshot['scanner']['bulk_matched_total'], 1)

    def test_bulk_roundtrip_preserves_direct_quote_and_survives_restart(self):
        self.store.put('quotes', {'Bulk winner': parse_steam_page(steam_html('Bulk winner'), 'Bulk winner')})
        before = copy.deepcopy(self.store.get('quotes'))
        with patch('backend.service.read_bulk_orders', return_value=parse(export(self.now), self.now)):
            self.service.collect_bulk()
        self.assertEqual(self.store.get('quotes'), before)
        restored = MarketService(self.store)
        try:
            self.assertEqual(restored.bulk_orders['entries']['Bulk winner']['bid_cents'], 200)
            self.assertEqual(restored.quotes, before)
        finally:
            restored.close()

    def test_steam_cooldown_does_not_stop_independent_bulk_source(self):
        self.service.fail('steam', SourceError('limit', kind='rate_limit'))
        with patch('backend.service.read_bulk_orders', return_value=parse(export(self.now), self.now)) as read:
            self.service.collect_bulk()
        read.assert_called_once()
        self.assertTrue(self.service.bulk_orders)
        self.assertGreater(self.service.retry_at['steam'], self.now)

    def test_bulk_failure_keeps_previous_data_and_direct_source_running(self):
        self.add_bulk()
        before = copy.deepcopy(self.service.bulk_orders)
        with patch('backend.service.read_bulk_orders', side_effect=SourceError('API unavailable', kind='unavailable')):
            self.service.collect_bulk()
        self.assertEqual(self.service.bulk_orders, before)
        self.assertEqual(self.service.retry_at['steam'], 0)
        self.assertGreater(self.service.retry_at['steamapis'], self.now)

    def test_bulk_lease_and_pacing_are_shared(self):
        other = MarketService(self.store, bulk_key='test-secret')
        try:
            with patch('backend.service.read_bulk_orders', return_value=parse(export(self.now), self.now)) as read:
                self.service.collect_bulk()
                other.collect_bulk()
            read.assert_called_once()
        finally:
            other.close()

    def test_unchanged_export_does_not_rewrite_large_snapshot(self):
        self.add_bulk()
        self.store.put('bulk_orders', self.service.bulk_orders)
        before = self.store.read_market()['snapshot_versions']['bulk_orders']
        with patch('backend.service.read_bulk_orders', return_value=copy.deepcopy(self.service.bulk_orders)):
            self.service.collect_bulk()
        self.assertEqual(self.store.read_market()['snapshot_versions']['bulk_orders'], before)

    def test_older_export_cannot_replace_a_newer_snapshot(self):
        self.add_bulk()
        before = copy.deepcopy(self.service.bulk_orders)
        with patch('backend.service.read_bulk_orders', return_value=parse(export(self.now - 60), self.now)):
            self.service.collect_bulk()
        self.assertEqual(self.service.bulk_orders, before)
        self.assertGreater(self.service.retry_at['steamapis'], self.now)


if __name__ == '__main__':
    unittest.main()
