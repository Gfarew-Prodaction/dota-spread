import logging
import threading
import time
from decimal import Decimal
from urllib.parse import quote

from .pricing import profitability
from .providers import SourceError, client, read_catalog, read_fx, read_steam

logger = logging.getLogger(__name__)


class MarketService:
    def __init__(self, store, interval=10, batch_size=3, quote_ttl=120,
                 catalog_interval=60, catalog_ttl=600, pending_ttl=1800,
                 steam_request_interval=4):
        self.store = store
        self.interval = interval
        self.batch_size = batch_size
        self.quote_ttl = quote_ttl
        self.catalog_interval = catalog_interval
        self.catalog_ttl = catalog_ttl
        self.pending_ttl = max(pending_ttl, quote_ttl, catalog_ttl)
        self.base_request_interval = steam_request_interval
        self.lock = threading.RLock()
        self.stop = threading.Event()
        # Independently scheduled sources own separate requests.Session objects.
        self.session = client()
        self.catalog_session = client()
        self.fx_session = client()
        self.catalog = store.get('catalog', [])
        self.quotes = store.get('quotes', {})
        self.fx = store.get('fx')
        self.catalog_updated_at = store.get('catalog_updated_at', 0)
        self.catalog_checked_at = store.get('catalog_checked_at', 0)
        self.catalog_etag = store.get('catalog_etag')
        self.skipped = store.get('skipped', 0)
        state = store.get('collector_state', {})
        self.attempted = state.get('attempted', {})
        self.checked_names = set(state.get('checked_names', [])) | set(self.quotes)
        self.scan_turn = state.get('scan_turn', 0)
        self.retry_at = {source: state.get('retry_at', {}).get(source, 0) for source in ('lis', 'steam', 'fx')}
        self.failures = {source: state.get('failures', {}).get(source, 0) for source in self.retry_at}
        self.request_interval = max(steam_request_interval, state.get('request_interval', steam_request_interval))
        self.success_streak = 0
        self.next_steam_at = 0
        self.status = {
            'lis': {'state': 'ok' if self.catalog else 'loading', 'message': 'Каталог доступен' if self.catalog else 'Загружаем каталог'},
            'steam': {'state': 'loading', 'message': 'Проверяем ордера'},
            'fx': {'state': 'ok' if self.fx else 'loading', 'message': 'Курс Банка России' if self.fx else 'Получаем курс'},
        }
        for source, retry_at in self.retry_at.items():
            if retry_at > time.time():
                self.status[source] = {'state': 'cooldown', 'message': 'Ожидаем следующую разрешённую проверку'}
        self.last_cycle = None
        self.cycle_running = False
        self.thread = None
        self.source_thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.source_thread = threading.Thread(target=self.run_sources, name='catalog-collector', daemon=True)
        self.thread = threading.Thread(target=self.run, name='steam-collector', daemon=True)
        self.source_thread.start()
        self.thread.start()

    def close(self):
        self.stop.set()
        for session in (self.session, self.catalog_session, self.fx_session):
            session.close()

    def save_state(self):
        with self.lock:
            state = {
                'attempted': dict(self.attempted), 'checked_names': sorted(self.checked_names),
                'scan_turn': self.scan_turn, 'retry_at': dict(self.retry_at),
                'failures': dict(self.failures), 'request_interval': self.request_interval,
            }
        self.store.put('collector_state', state)

    def fail(self, source, error):
        with self.lock:
            self.failures[source] += 1
            kind = getattr(error, 'kind', 'network')
            base = 300 if kind in ('rate_limit', 'access') else 60
            wait = max(getattr(error, 'retry_after', 60), min(3600, base * 2 ** min(self.failures[source] - 1, 6)))
            self.status[source] = {
                'state': 'cooldown',
                'message': str(error) if isinstance(error, SourceError) else 'Не удалось получить данные. Повторим запрос позже.',
            }
            self.retry_at[source] = time.time() + wait
            if source == 'steam':
                self.success_streak = 0
                if kind in ('rate_limit', 'access'):
                    self.request_interval = min(120, self.request_interval * 2)
        self.save_state()
        logger.warning('%s collection paused for %ss: %s', source, wait, error)

    def succeed(self, source, message):
        with self.lock:
            self.status[source] = {'state': 'ok', 'message': message}
            self.retry_at[source] = 0
            if source == 'steam':
                self.success_streak += 1
                # One successful request does not undo a rate-limit recovery.
                if self.success_streak >= 20:
                    self.failures[source] = 0
                    self.request_interval = max(self.base_request_interval, self.request_interval * .9)
                    self.success_streak = 0
            else:
                self.failures[source] = 0

    def collect_catalog(self):
        now = time.time()
        if now < self.retry_at['lis'] or now - self.catalog_checked_at < self.catalog_interval:
            return
        try:
            result = read_catalog(self.catalog_session, self.catalog_etag)
            with self.lock:
                if result is not None:
                    self.catalog, self.catalog_updated_at, self.skipped, self.catalog_etag = result
                # A 304 checks availability without changing the source price time.
                self.catalog_checked_at = time.time()
                saved = {'catalog_checked_at': self.catalog_checked_at, 'catalog_etag': self.catalog_etag}
                if result is not None:
                    saved.update(catalog=self.catalog, catalog_updated_at=self.catalog_updated_at, skipped=self.skipped)
            self.succeed('lis', 'Каталог доступен')
            for key, value in saved.items():
                self.store.put(key, value)
        except Exception as error:
            self.fail('lis', error)

    def collect_fx(self):
        if time.time() < self.retry_at['fx'] or (self.fx and time.time() - self.fx['checked_at'] < 3600):
            return
        try:
            rates = read_fx(self.fx_session)
            with self.lock:
                self.fx = rates
            self.succeed('fx', 'Курс Банка России')
            self.store.put('fx', rates)
        except Exception as error:
            self.fail('fx', error)

    def choose_candidates(self):
        """Reserve two discovery slots per refresh and leave fresh winners cached."""
        now = time.time()
        with self.lock:
            catalog = list(self.catalog)
            quotes = dict(self.quotes)
            attempted = dict(self.attempted)
            checked = set(self.checked_names) | set(quotes)
            fx = self.fx
            turn = self.scan_turn
        rank = lambda item: (attempted.get(item['name'], quotes.get(item['name'], {}).get('checked_at', 0)), not (.5 <= float(item['price']) <= 50), -min(item['stock'], 100))
        unseen = sorted((item for item in catalog if item['name'] not in checked), key=rank)
        rotating = sorted(catalog, key=rank)
        due = []
        for item in catalog:
            quote_data = quotes.get(item['name'])
            if not fx or not quote_data or not quote_data['quantity']:
                continue
            if now - quote_data['checked_at'] < max(30, self.quote_ttl * .6):
                continue
            rate = fx['usd_per_currency'].get(quote_data['currency'])
            if rate and profitability(item['price'], quote_data['bid_cents'], rate)['profit'] > 0:
                due.append(item)
        due.sort(key=lambda item: quotes[item['name']]['checked_at'])
        selected, picked = [], set()
        for offset in range(self.batch_size):
            queues = (due, unseen, rotating) if (turn + offset) % 3 == 2 else (unseen, due, rotating)
            item = next((entry for queue in queues for entry in queue
                         if entry['name'] not in picked and now - attempted.get(entry['name'], 0) >= self.request_interval), None)
            if item is None:
                break
            selected.append(item)
            picked.add(item['name'])
        return selected

    def collect_steam(self, limit=None):
        if not self.catalog or time.time() < max(self.retry_at['steam'], self.next_steam_at):
            return
        candidates = self.choose_candidates()
        if limit is not None:
            candidates = candidates[:limit]
        for index, item in enumerate(candidates):
            if self.stop.is_set():
                break
            name = item['name']
            with self.lock:
                self.attempted[name] = time.time()
                self.scan_turn += 1
            try:
                result = read_steam(self.session, name)
                with self.lock:
                    self.quotes[name] = result
                    self.checked_names.add(name)
                self.succeed('steam', 'Проверяем новые предметы и обновляем найденные ордера')
                self.store.put('quotes', dict(self.quotes))
            except Exception as error:
                if not isinstance(error, SourceError) or error.kind != 'item':
                    self.fail('steam', error)
                    break
                with self.lock:
                    self.checked_names.add(name)
                    # A newer unavailable response invalidates the old quote.
                    self.quotes.pop(name, None)
                self.store.put('quotes', dict(self.quotes))
                self.succeed('steam', 'Проверяем каталог; часть предметов не имеет подходящего ордера')
                logger.info('No usable Steam quote for %s: %s', name, error)
            finally:
                with self.lock:
                    self.next_steam_at = time.time() + self.request_interval
                self.save_state()
            if index + 1 < len(candidates) and self.stop.wait(self.request_interval):
                break

    def run_sources(self):
        while not self.stop.is_set():
            try:
                self.collect_catalog()
                self.collect_fx()
            except Exception:
                logger.exception('Catalog collection failed')
            self.stop.wait(self.interval)

    def run(self):
        while not self.stop.is_set():
            with self.lock:
                self.cycle_running = True
            try:
                # Steam scanning continues while the catalog downloads separately.
                self.collect_steam(limit=1)
            except Exception:
                logger.exception('Steam collection failed')
            finally:
                with self.lock:
                    self.last_cycle = time.time()
                    self.cycle_running = False
            self.stop.wait(1)

    def snapshot(self, deposit_fee_percent=0):
        now = time.time()
        with self.lock:
            catalog = list(self.catalog)
            quotes = dict(self.quotes)
            fx = dict(self.fx) if self.fx else None
            status = {key: {**value, 'retry_at': self.retry_at[key] or None} for key, value in self.status.items()}
            catalog_updated, catalog_checked = self.catalog_updated_at, self.catalog_checked_at
            running, cycle = self.cycle_running, self.last_cycle
            checked_names = set(self.checked_names) | set(quotes)
            attempted_names = set(self.attempted)
            request_interval = self.request_interval
        items, pending_items = [], []
        fresh_quotes, stale_quotes = 0, 0
        catalog_fresh = catalog_updated > 0 and now - catalog_updated <= self.catalog_ttl
        fx_fresh = fx and now - fx['checked_at'] <= 86400 * 3
        if catalog_updated and not catalog_fresh and status['lis']['state'] == 'ok':
            status['lis'] = {**status['lis'], 'state': 'stale', 'message': 'Ждём новую выгрузку Lis-Skins; последние цены помечены как устаревшие'}
        if fx and not fx_fresh:
            status['fx'] = {**status['fx'], 'state': 'stale', 'message': 'Курс устарел; требуется перепроверка'}
        for item in catalog:
            quote_data = quotes.get(item['name'])
            if not quote_data:
                continue
            quote_fresh = now - quote_data['updated_at'] <= self.quote_ttl
            if quote_fresh:
                fresh_quotes += 1
            else:
                stale_quotes += 1
            if not fx or not quote_data['quantity'] or not quote_data['bid_cents']:
                continue
            if now - quote_data['updated_at'] > self.pending_ttl or now - catalog_updated > self.pending_ttl:
                continue
            rate = fx['usd_per_currency'].get(quote_data['currency'])
            if not rate:
                continue
            calc = profitability(item['price'], quote_data['bid_cents'], rate, deposit_fee_percent)
            if calc['profit'] <= 0:
                continue
            fresh = bool(quote_fresh and catalog_fresh and fx_fresh)
            waiting_for = []
            if not quote_fresh:
                waiting_for.append('ордер Steam')
            if not catalog_fresh:
                waiting_for.append('цена Lis-Skins')
            if not fx_fresh:
                waiting_for.append('курс валют')
            row = {
                'id': item['listing_id'], 'name': item['name'], 'image': quote_data['image'], 'stock': item['stock'],
                'lis_price_usd': float(Decimal(item['price'])), 'cost_usd': float(calc['cost']),
                'steam_bid_usd': float(calc['gross']), 'steam_net_usd': float(calc['net']), 'profit_usd': float(calc['profit']), 'roi': float(calc['roi']),
                'source_currency': quote_data['currency'], 'source_bid': quote_data['bid_cents'] / 100,
                'buy_order_quantity': quote_data['quantity'], 'total_orders': quote_data['total_orders'],
                'updated_at': quote_data['updated_at'], 'checked_at': quote_data['checked_at'],
                'fresh': fresh, 'waiting_reason': ', '.join(waiting_for) if waiting_for else None,
                'lis_url': 'https://lis-skins.com/ru/market/dota2/?query=' + quote(item['name'], safe=''),
                'steam_url': 'https://steamcommunity.com/market/listings/570/' + quote(item['name'], safe=''),
            }
            (items if fresh else pending_items).append(row)
        items.sort(key=lambda item: item['roi'], reverse=True)
        pending_items.sort(key=lambda item: item['roi'], reverse=True)
        names = {item['name'] for item in catalog}
        return {
            'items': items, 'pending_items': pending_items, 'server_time': now, 'interval_seconds': self.interval,
            'quote_ttl_seconds': self.quote_ttl, 'catalog_ttl_seconds': self.catalog_ttl,
            'pending_ttl_seconds': self.pending_ttl, 'steam_request_interval_seconds': request_interval,
            'catalog_total': len(catalog), 'checked_total': len(checked_names & names),
            'attempted_total': len(attempted_names & names), 'fresh_quotes': fresh_quotes, 'stale_quotes': stale_quotes,
            'profitable_total': len(items), 'pending_total': len(pending_items), 'best_roi': max((item['roi'] for item in items), default=None),
            'catalog_updated_at': catalog_updated or None, 'catalog_checked_at': catalog_checked or None,
            'last_cycle_at': cycle, 'collecting': running, 'sources': status, 'fx': fx,
            'skipped_variants': self.skipped, 'deposit_fee_percent': deposit_fee_percent,
        }
