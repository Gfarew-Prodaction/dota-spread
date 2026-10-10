import logging
import threading
import time
from decimal import Decimal
from urllib.parse import quote

from .pricing import profitability
from .providers import SourceError, client, read_catalog, read_fx, read_steam

logger = logging.getLogger(__name__)


class MarketService:
    def __init__(self, store, interval=10, batch_size=3, quote_ttl=120, catalog_interval=60):
        self.store = store
        self.interval = interval
        self.batch_size = batch_size
        self.quote_ttl = quote_ttl
        self.catalog_interval = catalog_interval
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.session = client()
        self.catalog = store.get('catalog', [])
        self.quotes = store.get('quotes', {})
        self.fx = store.get('fx')
        self.catalog_updated_at = store.get('catalog_updated_at', 0)
        self.catalog_checked_at = store.get('catalog_checked_at', 0)
        self.skipped = store.get('skipped', 0)
        self.attempted = {}
        self.status = {
            'lis': {'state': 'ok' if self.catalog else 'loading', 'message': 'Каталог доступен' if self.catalog else 'Загружаем каталог'},
            'steam': {'state': 'loading', 'message': 'Проверяем ордера'},
            'fx': {'state': 'ok' if self.fx else 'loading', 'message': 'Курс Банка России' if self.fx else 'Получаем курс'},
        }
        self.retry_at = {'lis': 0, 'steam': 0, 'fx': 0}
        self.last_cycle = None
        self.cycle_running = False
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, name='market-collector', daemon=True)
        self.thread.start()

    def fail(self, source, error):
        wait = getattr(error, 'retry_after', 60)
        with self.lock:
            self.status[source] = {'state': 'error', 'message': str(error) if isinstance(error, SourceError) else 'Не удалось получить данные. Повторим запрос позже.'}
            self.retry_at[source] = time.time() + wait
        logger.warning('%s collection failed: %s', source, error)

    def collect_catalog(self):
        now = time.time()
        if now < self.retry_at['lis'] or now - self.catalog_checked_at < self.catalog_interval:
            return
        try:
            catalog, updated, skipped = read_catalog(self.session)
            with self.lock:
                self.catalog, self.catalog_updated_at, self.catalog_checked_at = catalog, updated, time.time()
                self.skipped = skipped
                self.status['lis'] = {'state': 'ok', 'message': 'Каталог доступен'}
            for key, value in [('catalog', catalog), ('catalog_updated_at', updated), ('catalog_checked_at', self.catalog_checked_at), ('skipped', skipped)]:
                self.store.put(key, value)
        except Exception as error:
            self.fail('lis', error)

    def collect_fx(self):
        if time.time() < self.retry_at['fx'] or (self.fx and time.time() - self.fx['checked_at'] < 3600):
            return
        try:
            rates = read_fx(self.session)
            with self.lock:
                self.fx = rates
                self.status['fx'] = {'state': 'ok', 'message': 'Курс Банка России'}
            self.store.put('fx', rates)
        except Exception as error:
            self.fail('fx', error)

    def choose_candidates(self):
        """Keep checking profitable items while gradually covering the catalog."""
        now = time.time()
        with self.lock:
            catalog = list(self.catalog)
            quotes = dict(self.quotes)
        winners = []
        if self.fx:
            for item in catalog:
                quote_data = quotes.get(item['name'])
                if not quote_data or not quote_data['quantity']:
                    continue
                rate = self.fx['usd_per_currency'].get(quote_data['currency'])
                if rate and profitability(item['price'], quote_data['bid_cents'], rate)['profit'] > 0:
                    winners.append(item)
        winners.sort(key=lambda x: quotes[x['name']]['checked_at'])
        # Rotate all items; rank common mid-price items first only within never-seen items.
        catalog.sort(key=lambda x: (self.attempted.get(x['name'], quotes.get(x['name'], {}).get('checked_at', 0)), not (0.5 <= float(x['price']) <= 50), -min(x['stock'], 100)))
        picked = []
        refresh_slots = min(max(1, (len(winners) * self.interval + int(self.quote_ttl * .75) - 1) // int(self.quote_ttl * .75)), max(1, self.batch_size - 1))
        for item in winners[:refresh_slots]:
            if now - quotes[item['name']]['checked_at'] >= self.interval:
                picked.append(item)
        for item in catalog:
            if len(picked) >= self.batch_size:
                break
            if item['name'] not in {x['name'] for x in picked} and now - self.attempted.get(item['name'], 0) >= self.interval:
                picked.append(item)
                if len(picked) >= self.batch_size:
                    break
        return picked

    def collect_steam(self):
        if not self.catalog or time.time() < self.retry_at['steam']:
            return
        successes = 0
        for item in self.choose_candidates():
            if self.stop.is_set():
                break
            name = item['name']
            self.attempted[name] = time.time()
            try:
                result = read_steam(self.session, name)
                with self.lock:
                    self.quotes[name] = result
                    self.status['steam'] = {'state': 'ok', 'message': 'Ордера проверяются постепенно'}
                successes += 1
            except Exception as error:
                # Honour throttling/auth failures globally. An absent item doesn't stop the batch.
                if not isinstance(error, SourceError) or error.retry_after >= 120:
                    self.fail('steam', error)
                    break
                logger.info('No usable Steam quote for %s: %s', name, error)
                if not successes:
                    with self.lock:
                        self.status['steam'] = {'state': 'error', 'message': str(error)}
            if self.stop.wait(1.5):
                break
        if successes:
            with self.lock:
                quotes = dict(self.quotes)
            self.store.put('quotes', quotes)

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            with self.lock:
                self.cycle_running = True
            try:
                self.collect_fx()
                self.collect_catalog()
                self.collect_steam()
            except Exception:
                logger.exception('Collector cycle failed')
            finally:
                with self.lock:
                    self.last_cycle = time.time()
                    self.cycle_running = False
            self.stop.wait(max(0.5, self.interval - (time.monotonic() - started)))

    def snapshot(self, deposit_fee_percent=0):
        now = time.time()
        with self.lock:
            catalog = list(self.catalog)
            quotes = dict(self.quotes)
            fx = dict(self.fx) if self.fx else None
            status = {key: dict(value) for key, value in self.status.items()}
            catalog_updated = self.catalog_updated_at
            catalog_checked = self.catalog_checked_at
            running, cycle = self.cycle_running, self.last_cycle
        items = []
        fresh_quotes = 0
        stale_quotes = 0
        catalog_fresh = catalog_updated > 0 and now - catalog_updated <= max(180, self.catalog_interval * 3)
        fx_fresh = fx and now - fx['checked_at'] <= 86400 * 3
        if catalog_updated and not catalog_fresh and status['lis']['state'] != 'error':
            status['lis'] = {'state': 'stale', 'message': 'Выгрузка устарела; сделки скрыты'}
        if fx and not fx_fresh:
            status['fx'] = {'state': 'stale', 'message': 'Курс устарел; сделки скрыты'}
        for item in catalog:
            quote_data = quotes.get(item['name'])
            if not quote_data:
                continue
            if now - quote_data['updated_at'] > self.quote_ttl:
                stale_quotes += 1
                continue
            fresh_quotes += 1
            if not catalog_fresh or not fx_fresh or not quote_data['quantity'] or not quote_data['bid_cents']:
                continue
            rate = fx['usd_per_currency'].get(quote_data['currency'])
            if not rate:
                continue
            calc = profitability(item['price'], quote_data['bid_cents'], rate, deposit_fee_percent)
            if calc['profit'] <= 0:
                continue
            items.append({
                'id': item['listing_id'], 'name': item['name'], 'image': quote_data['image'], 'stock': item['stock'],
                'lis_price_usd': float(Decimal(item['price'])), 'cost_usd': float(calc['cost']),
                'steam_bid_usd': float(calc['gross']), 'steam_net_usd': float(calc['net']), 'profit_usd': float(calc['profit']), 'roi': float(calc['roi']),
                'source_currency': quote_data['currency'], 'source_bid': quote_data['bid_cents'] / 100,
                'buy_order_quantity': quote_data['quantity'], 'total_orders': quote_data['total_orders'],
                'updated_at': quote_data['updated_at'], 'checked_at': quote_data['checked_at'],
                'lis_url': 'https://lis-skins.com/ru/market/dota2/?query=' + quote(item['name'], safe=''),
                'steam_url': 'https://steamcommunity.com/market/listings/570/' + quote(item['name'], safe=''),
            })
        items.sort(key=lambda item: item['roi'], reverse=True)
        return {
            'items': items, 'server_time': now, 'interval_seconds': self.interval, 'quote_ttl_seconds': self.quote_ttl,
            'catalog_total': len(catalog), 'checked_total': len(quotes), 'fresh_quotes': fresh_quotes, 'stale_quotes': stale_quotes,
            'profitable_total': len(items), 'best_roi': max((item['roi'] for item in items), default=None),
            'catalog_updated_at': catalog_updated or None, 'catalog_checked_at': catalog_checked or None,
            'last_cycle_at': cycle, 'collecting': running, 'sources': status, 'fx': fx,
            'skipped_variants': self.skipped, 'deposit_fee_percent': deposit_fee_percent,
        }
