import io
import json
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

from .app import create_app
from .pricing import profitability, steam_net, steam_total
from .providers import SourceError, checked_response, parse_steam_page, read_catalog
from .service import MarketService
from .store import Store


def steam_html(name='Test skin', bid=138, currency=1, quantity=2):
    data = {'queries': [
        {'queryKey': ['market', 'orderbook', 570, name], 'state': {'data': {
            'amtMaxBuyOrder': bid, 'eCurrency': currency, 'rgCompactBuyOrders': [bid, quantity], 'cBuyOrders': 50,
        }, 'dataUpdatedAt': time.time() * 1000}},
        {'queryKey': ['description'], 'state': {'data': {
            'appid': 570, 'market_hash_name': name, 'marketable': True, 'icon_url': 'test-icon',
        }}},
    ]}
    return '<script type="application/json" id="valve-ssr-data">' + json.dumps({'renderContext': {'queryData': json.dumps(data)}}) + '</script>'


class PricingTests(unittest.TestCase):
    def test_known_fee_examples(self):
        self.assertEqual(steam_net(138000), 120000)
        self.assertEqual(steam_net(115), 100)
        self.assertEqual(steam_net(116), 101)
        self.assertEqual(steam_net(3), 1)
        self.assertEqual(steam_net(2), 0)
        self.assertEqual(steam_net(0), 0)

    def test_rounding_does_not_overstate_wallet_credit(self):
        for gross in range(3, 2500):
            net = steam_net(gross)
            self.assertLessEqual(steam_total(net), gross)
            self.assertGreater(steam_total(net + 1), gross)

    def test_profit_uses_net_proceeds_and_payment_fee(self):
        result = profitability('1000', 138000, '1')
        self.assertEqual(result['profit'], Decimal('200'))
        self.assertEqual(result['roi'], Decimal('20'))
        self.assertEqual(profitability('1000', 138000, '1', 20)['profit'], 0)
        self.assertLess(profitability('0.02', 3, '1')['profit'], 0)

    def test_source_currency_is_converted_after_fee_rounding(self):
        result = profitability('1', 115, '1.1')
        self.assertEqual(result['net'], Decimal('1.1'))
        self.assertEqual(result['roi'], Decimal('10'))


class ProviderTests(unittest.TestCase):
    def test_current_ssr_orderbook(self):
        quote = parse_steam_page(steam_html(), 'Test skin')
        self.assertEqual(quote['bid_cents'], 138)
        self.assertEqual(quote['currency'], 'USD')
        self.assertEqual(quote['quantity'], 2)

    def test_wrong_item_cannot_be_matched(self):
        with self.assertRaises(SourceError):
            parse_steam_page(steam_html('Inscribed Test skin'), 'Test skin')

    def test_unknown_fee_currency_is_not_silently_treated_as_usd(self):
        with self.assertRaises(SourceError):
            parse_steam_page(steam_html(currency=5), 'Test skin')

    def test_lis_cheapest_price_stock_and_variant_exclusions(self):
        payload = {'last_update': time.time(), 'items': [
            {'id': 1, 'name': 'A', 'price': 3}, {'id': 2, 'name': 'A', 'price': 2},
            {'id': 3, 'name': 'A', 'price': 1, 'gems': [{'name': 'special'}]},
            {'id': 4, 'name': 'B', 'price': 1, 'unlock_at': '2999-01-01T00:00:00Z'},
            {'id': 5, 'name': 'C', 'price': 1, 'styles': {'unlocked': True}},
        ]}
        class Response:
            status_code = 200
            headers = {'ETag': '"test-export"'}
            def __init__(self): self.raw = io.BytesIO(json.dumps(payload).encode())
            def raise_for_status(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Session:
            def get(self, *args, **kwargs): return Response()
        catalog, _, skipped, etag = read_catalog(Session())
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]['price'], '2')
        self.assertEqual(catalog[0]['listing_id'], 2)
        self.assertEqual(catalog[0]['stock'], 2)
        self.assertEqual(skipped, 3)
        self.assertEqual(etag, '"test-export"')

    def test_source_throttling_is_honoured(self):
        class Response:
            status_code = 429
            headers = {'Retry-After': '180'}
        with self.assertRaises(SourceError) as error:
            checked_response(Response())
        self.assertEqual(error.exception.retry_after, 180)
        self.assertEqual(error.exception.kind, 'rate_limit')

    def test_retry_after_http_date_is_honoured(self):
        class Response:
            status_code = 429
            headers = {'Retry-After': formatdate(time.time() + 600, usegmt=True)}
        with self.assertRaises(SourceError) as error:
            checked_response(Response())
        self.assertGreaterEqual(error.exception.retry_after, 599)

    def test_unchanged_export_avoids_download_and_parsing(self):
        class Response:
            status_code = 304
            def raise_for_status(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
        from unittest.mock import Mock
        session = Mock()
        session.get.return_value = Response()
        self.assertIsNone(read_catalog(session, '"export-version"'))
        self.assertEqual(session.get.call_args.kwargs['headers']['If-None-Match'], '"export-version"')

    def test_missing_steam_page_is_a_source_error_not_a_removed_item(self):
        with self.assertRaises(SourceError) as error:
            parse_steam_page('<html>Temporarily unavailable</html>', 'Test skin')
        self.assertEqual(error.exception.kind, 'unavailable')


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.directory.name) / 'test.sqlite3'))
        self.service = MarketService(self.store)
        now = time.time()
        self.service.catalog = [{'listing_id': 1, 'name': 'Test skin', 'price': '1', 'stock': 2}]
        self.service.catalog_updated_at = now
        self.service.fx = {'rub_per_usd': '85', 'usd_per_currency': {'USD': '1', 'EUR': '1.1'}, 'date': '10.10.2026', 'checked_at': now}
        self.service.quotes = {'Test skin': parse_steam_page(steam_html(), 'Test skin')}

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def test_profitable_live_quote_is_displayed(self):
        self.assertEqual(self.service.snapshot()['profitable_total'], 1)
        item = self.service.snapshot()['items'][0]
        self.assertAlmostEqual(item['roi'], 20)
        self.assertEqual(item['lis_url'], 'https://lis-skins.com/ru/market/dota2/?query=Test%20skin')

    def test_fee_can_remove_apparent_profit(self):
        self.assertEqual(self.service.snapshot(20)['items'], [])

    def test_no_liquidity_is_not_an_instant_sale(self):
        self.service.quotes['Test skin']['quantity'] = 0
        self.assertEqual(self.service.snapshot()['items'], [])

    def test_stale_steam_quote_is_hidden(self):
        self.service.quotes['Test skin']['updated_at'] -= 121
        result = self.service.snapshot()
        self.assertEqual(result['items'], [])
        self.assertEqual(result['stale_quotes'], 1)
        self.assertEqual(result['pending_total'], 1)
        self.assertFalse(result['pending_items'][0]['fresh'])
        self.assertEqual(result['pending_items'][0]['waiting_reason'], 'ордер Steam')

    def test_stale_lis_price_is_hidden(self):
        self.service.catalog_updated_at -= 601
        self.assertEqual(self.service.snapshot()['items'], [])
        self.assertEqual(self.service.snapshot()['pending_items'][0]['waiting_reason'], 'цена Lis-Skins')

    def test_normal_export_lag_does_not_remove_fresh_orders(self):
        self.service.catalog_updated_at -= 199
        result = self.service.snapshot()
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['catalog_ttl_seconds'], 600)

    def test_old_pending_rows_eventually_expire(self):
        self.service.quotes['Test skin']['updated_at'] -= 1801
        result = self.service.snapshot()
        self.assertEqual(result['pending_items'], [])
        self.assertEqual(result['items'], [])

    def test_order_refresh_restores_pending_row(self):
        self.service.quotes['Test skin']['updated_at'] -= 121
        self.assertEqual(self.service.snapshot()['pending_total'], 1)
        self.service.quotes['Test skin'] = parse_steam_page(steam_html(), 'Test skin')
        self.assertEqual(self.service.snapshot()['profitable_total'], 1)
        self.assertEqual(self.service.snapshot()['pending_total'], 0)

    def test_stale_exchange_rate_is_hidden(self):
        self.service.fx['checked_at'] -= 86400 * 4
        self.assertEqual(self.service.snapshot()['items'], [])

    def test_removed_listing_is_not_displayed(self):
        self.service.catalog = []
        self.assertEqual(self.service.snapshot()['items'], [])

    def test_api_rejects_invalid_deposit_fees(self):
        client = create_app(self.service).test_client()
        for value in ['nan', 'inf', '-1', '101', 'abc', '1e999']:
            self.assertEqual(client.get('/api/opportunities?deposit_fee=' + value).status_code, 400)
        result = client.get('/api/opportunities?deposit_fee=5')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['deposit_fee_percent'], 5)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')

    def test_quotes_survive_process_restart(self):
        self.store.put('quotes', self.service.quotes)
        service = MarketService(self.store)
        self.assertEqual(service.quotes['Test skin']['bid_cents'], 138)
        service.close()

    def test_collector_does_not_retry_throttled_source(self):
        self.service.retry_at['steam'] = time.time() + 60
        with patch('backend.service.read_steam') as read:
            self.service.collect_steam()
            read.assert_not_called()

    def test_refresh_queue_respects_single_item_budget(self):
        self.service.batch_size = 1
        self.service.quotes['Test skin']['checked_at'] -= 20
        self.service.catalog.append({'name': 'Other skin', 'price': '1', 'stock': 1, 'listing_id': 2})
        self.assertEqual(len(self.service.choose_candidates()), 1)

    def test_discovery_gets_two_slots_before_due_winner(self):
        self.service.quotes['Test skin']['checked_at'] -= 80
        for index in range(3):
            self.service.catalog.append({'name': f'New skin {index}', 'price': '1', 'stock': 2, 'listing_id': index + 2})
        selected = [item['name'] for item in self.service.choose_candidates()]
        self.assertTrue(selected[0].startswith('New skin'))
        self.assertTrue(selected[1].startswith('New skin'))
        self.assertEqual(selected[2], 'Test skin')

    def test_fresh_winner_is_not_repeated_ahead_of_new_items(self):
        for index in range(3):
            self.service.catalog.append({'name': f'New skin {index}', 'price': '1', 'stock': 2, 'listing_id': index + 2})
        self.assertNotIn('Test skin', [item['name'] for item in self.service.choose_candidates()])

    def test_requests_are_spaced_even_when_api_is_polled_often(self):
        with patch('backend.service.read_steam', return_value=self.service.quotes['Test skin']) as read:
            self.service.collect_steam(limit=1)
            self.service.collect_steam(limit=1)
            self.assertEqual(read.call_count, 1)

    def test_short_retry_after_still_stops_collection_globally(self):
        error = SourceError('Too many requests', retry_after=10, kind='rate_limit')
        with patch('backend.service.read_steam', side_effect=error) as read:
            self.service.collect_steam()
            self.assertEqual(read.call_count, 1)
        self.assertGreater(self.service.retry_at['steam'], time.time() + 290)
        self.assertIn('Test skin', self.service.quotes)

    def test_rate_limit_backoff_and_progress_survive_restart(self):
        self.service.fail('steam', SourceError('limit', 20, kind='rate_limit'))
        self.assertEqual(self.service.request_interval, 8)
        self.service.succeed('steam', 'One request succeeded')
        self.service.fail('steam', SourceError('limit again', 20, kind='rate_limit'))
        self.assertEqual(self.service.request_interval, 16)
        self.assertGreater(self.service.retry_at['steam'], time.time() + 590)
        restored = MarketService(self.store)
        try:
            self.assertEqual(restored.request_interval, 16)
            self.assertEqual(restored.retry_at['steam'], self.service.retry_at['steam'])
        finally:
            restored.close()

    def test_304_check_keeps_original_price_timestamp(self):
        stamp = self.service.catalog_updated_at - 200
        self.service.catalog_updated_at = stamp
        with patch('backend.service.read_catalog', return_value=None):
            self.service.collect_catalog()
        self.assertEqual(self.service.catalog_updated_at, stamp)
        self.assertGreater(self.service.catalog_checked_at, stamp)

    def test_confirmed_unavailable_item_invalidates_old_quote(self):
        with patch('backend.service.read_steam', side_effect=SourceError('Removed item')):
            self.service.collect_steam(limit=1)
        result = self.service.snapshot()
        self.assertEqual(result['items'], [])
        self.assertEqual(result['pending_items'], [])
        self.assertEqual(result['checked_total'], 1)

    def test_catalog_download_does_not_block_steam_scanning(self):
        catalog_started, release_catalog, steam_called = threading.Event(), threading.Event(), threading.Event()
        def slow_catalog():
            catalog_started.set()
            release_catalog.wait(3)
        with patch.object(self.service, 'collect_catalog', side_effect=slow_catalog), \
             patch.object(self.service, 'collect_fx'), \
             patch.object(self.service, 'collect_steam', side_effect=lambda **kwargs: steam_called.set()):
            self.service.start()
            try:
                self.assertTrue(catalog_started.wait(1))
                self.assertTrue(steam_called.wait(1))
            finally:
                self.service.stop.set()
                release_catalog.set()
                self.service.thread.join(3)
                self.service.source_thread.join(3)


if __name__ == '__main__':
    unittest.main()
