import logging
import os
import threading
import time
import uuid
from decimal import Decimal
from urllib.parse import quote

from .pricing import profitability
from .providers import SourceError, client, read_catalog, read_fx, read_steam, read_bulk_orders

logger = logging.getLogger(__name__)


class MarketService:
    def __init__(self, store, interval=10, batch_size=3, quote_ttl=120,
                 catalog_interval=60, catalog_ttl=600, pending_ttl=1800,
                 steam_request_interval=4, collector_role='all', worker_id=None,
                 priority_size=200, bulk_key='', bulk_interval=60, bulk_currency='USD'):
        if collector_role not in ('all', 'sources', 'steam', 'none'):
            raise ValueError('COLLECTOR_ROLE: all, sources, steam или none.')
        if bulk_currency not in ('USD', 'EUR'):
            raise ValueError('STEAMAPIS_CURRENCY: USD или EUR.')
        self.store = store
        self.interval = interval
        self.batch_size = batch_size
        self.quote_ttl = quote_ttl
        self.catalog_interval = catalog_interval
        self.catalog_ttl = catalog_ttl
        self.pending_ttl = max(pending_ttl, quote_ttl, catalog_ttl)
        self.base_request_interval = steam_request_interval
        self.collector_role = collector_role
        self.worker_id = worker_id or f'{os.getpid()}-{uuid.uuid4().hex[:8]}'
        self.priority_size = max(1, priority_size)
        self.bulk_key, self.bulk_interval, self.bulk_currency = bulk_key, bulk_interval, bulk_currency
        self.lock = threading.RLock()
        self.sync_lock = threading.Lock()
        self.stop = threading.Event()
        self.session = client()
        self.catalog_session = client()
        self.fx_session = client()
        self.bulk_session = client()
        self.bulk_orders = None
        self.catalog, self.quotes, self.fx = [], {}, None
        self.catalog_drops = {}
        self.catalog_updated_at = self.catalog_checked_at = 0
        self.catalog_etag = None
        self.skipped = 0
        self.attempted, self.checked_names = {}, set()
        self.checked_at = {}
        self.scan_turn = 0
        self.retry_at = {source: 0 for source in ('lis', 'steam', 'fx')}
        self.failures = dict(self.retry_at)
        self.request_interval = steam_request_interval
        self.success_streak = 0
        self.next_steam_at = 0
        self.status = {
            'lis': {'state': 'loading', 'message': 'Загружаем каталог'},
            'steam': {'state': 'loading', 'message': 'Проверяем ордера'},
            'fx': {'state': 'loading', 'message': 'Получаем курс'},
        }
        if bulk_key:
            self.retry_at['steamapis'] = self.failures['steamapis'] = 0
            self.status['steamapis'] = {'state': 'loading', 'message': 'Загружаем список ордеров для поиска сделок'}
        self.controls = {}
        self.last_cycle = None
        self.cycle_running = False
        self.thread = self.source_thread = self.bulk_thread = None
        self.next_sync_at = 0
        self.snapshot_versions = self.steam_revision = None
        self.storage_retry_at = 0
        self.storage_status = {'backend': store.backend, 'persistent': store.persistent,
                               'state': 'ok', 'last_synced_at': None}
        self.refresh_shared(force=True)

    def start(self):
        if self.collector_role == 'none' or self.collector_running():
            return
        if self.collector_role in ('all', 'sources'):
            self.source_thread = threading.Thread(target=self.run_sources, name='catalog-collector', daemon=True)
            self.source_thread.start()
            if self.bulk_key:
                self.bulk_thread = threading.Thread(target=self.run_bulk, name='bulk-collector', daemon=True)
                self.bulk_thread.start()
        if self.collector_role in ('all', 'steam'):
            self.thread = threading.Thread(target=self.run, name='steam-collector', daemon=True)
            self.thread.start()

    def collector_running(self):
        required = []
        if self.collector_role in ('all', 'sources'):
            required.append(self.source_thread)
            if self.bulk_key:
                required.append(self.bulk_thread)
        if self.collector_role in ('all', 'steam'):
            required.append(self.thread)
        return bool(required) and all(thread and thread.is_alive() for thread in required)

    def close(self):
        self.stop.set()
        for thread in (self.thread, self.source_thread, self.bulk_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)
        for session in (self.session, self.catalog_session, self.fx_session, self.bulk_session):
            session.close()
        self.store.close()

    def _apply_control(self, source, state):
        with self.lock:
            self.controls[source] = dict(state)
            self.retry_at[source] = state.get('retry_at', 0)
            self.failures[source] = state.get('failures', 0)
            if state.get('state'):
                self.status[source] = {'state': state['state'], 'message': state.get('message', '')}
            elif self.retry_at[source] > time.time():
                self.status[source] = {'state': 'cooldown', 'message': 'Ожидаем следующую разрешённую проверку'}
            if source == 'steam':
                self.scan_turn = state.get('scan_turn', 0)
                self.request_interval = max(self.base_request_interval, state.get('request_interval', self.base_request_interval))
                self.success_streak = state.get('success_streak', 0)
                self.next_steam_at = max(state.get('next_at', 0), state.get('lease_until', 0))
                self.last_cycle = state.get('last_cycle_at', self.last_cycle)

    def _storage_error(self):
        with self.lock:
            first_failure = self.storage_status['state'] != 'error'
            self.storage_status.update(state='error', message='База временно недоступна. Показаны последние полученные цены.')
            self.storage_retry_at = time.time() + 10
        if first_failure:
            logger.warning('Shared database unavailable; retaining last prices and pausing collection')

    def refresh_shared(self, force=False):
        with self.sync_lock:
            if not force and time.monotonic() < self.next_sync_at:
                return self.storage_status['state'] == 'ok'
            self.next_sync_at = time.monotonic() + self.interval
            try:
                data = self.store.read_market(known_versions=self.snapshot_versions, since_revision=self.steam_revision)
            except Exception:
                self._storage_error()
                return False
            with self.lock:
                for key in ('catalog', 'catalog_drops', 'catalog_updated_at', 'catalog_checked_at', 'catalog_etag', 'skipped', 'fx', 'bulk_orders'):
                    if key in data:
                        setattr(self, key, data[key])
                if data['incremental']:
                    for name, quote_data in data['quotes'].items():
                        if quote_data is None:
                            self.quotes.pop(name, None)
                        else:
                            self.quotes[name] = quote_data
                else:
                    self.quotes = data['quotes']
                state = data['collector_state']
                self.attempted.update(state['attempted'])
                self.checked_names.update(state['checked_names'])
                self.checked_at.update(state['checked_at'])
                self.checked_names.update(self.quotes)
                for source, control in data['controls'].items():
                    self._apply_control(source, control)
                if self.catalog and self.status['lis']['state'] == 'loading':
                    self.status['lis'] = {'state': 'ok', 'message': 'Каталог доступен'}
                if self.fx and self.status['fx']['state'] == 'loading':
                    self.status['fx'] = {'state': 'ok', 'message': 'Курс Банка России'}
                self.storage_retry_at = 0
                self.snapshot_versions = data['snapshot_versions']
                self.steam_revision = data['steam_revision']
                self.storage_status = {'backend': self.store.backend, 'persistent': self.store.persistent,
                                       'state': 'ok', 'last_synced_at': time.time()}
            return True

    def _failure_state(self, source, state, error):
        state = dict(state)
        failures = state.get('failures', 0) + 1
        kind = getattr(error, 'kind', 'network')
        base = 300 if kind in ('rate_limit', 'access') else 60
        wait = max(getattr(error, 'retry_after', 60), min(3600, base * 2 ** min(failures - 1, 6)))
        state.update(failures=failures, retry_at=time.time() + wait, state='cooldown',
                     message=str(error) if isinstance(error, SourceError) else 'Не удалось получить данные. Повторим запрос позже.')
        if source == 'steam':
            state['success_streak'] = 0
            interval = max(self.base_request_interval, state.get('request_interval', self.base_request_interval))
            state['request_interval'] = min(120, interval * 2) if kind in ('rate_limit', 'access') else interval
            state['next_at'] = time.time() + state['request_interval']
        return state

    def _success_state(self, source, state, message):
        state = dict(state)
        state.update(state='ok', message=message, retry_at=0)
        if source == 'steam':
            streak = state.get('success_streak', 0) + 1
            interval = max(self.base_request_interval, state.get('request_interval', self.base_request_interval))
            if streak >= 20:
                state.update(failures=0)
                interval = max(self.base_request_interval, interval * .9)
                streak = 0
            state.update(success_streak=streak, request_interval=interval, next_at=time.time() + interval)
        else:
            delay = self.catalog_interval if source == 'lis' else self.bulk_interval if source == 'steamapis' else 3600
            state.update(failures=0, next_at=time.time() + delay)
        return state

    def fail(self, source, error):
        state = self.store.mutate_control(source, lambda current: self._failure_state(source, current, error))
        self._apply_control(source, state)

    def succeed(self, source, message):
        state = self.store.mutate_control(source, lambda current: self._success_state(source, current, message))
        self._apply_control(source, state)

    def _claim(self, source, candidates=None):
        if time.time() < self.storage_retry_at:
            return None
        claim = self.store.claim(source, self.worker_id, candidates=candidates,
                                 lease_seconds=600 if source == 'lis' else 180,
                                 request_interval=self.base_request_interval)
        self._apply_control(source, claim['control'])
        return claim if claim.get('token') else None

    def _complete(self, source, claim, message=None, error=None, values=None, quote_data=None, checked=False):
        update = (lambda current: self._failure_state(source, current, error)) if error else \
                 (lambda current: self._success_state(source, current, message))
        result = self.store.complete(source, claim['token'], update, values=values,
                                     name=claim.get('name'), quote_data=quote_data, checked=checked)
        self._apply_control(source, result['control'])
        if result['accepted']:
            with self.lock:
                for key, value in (values or {}).items():
                    setattr(self, key, value)
                if source == 'steam' and checked:
                    name = claim['name']
                    self.checked_names.add(name)
                    self.checked_at[name] = result['control']['last_cycle_at']
                    if quote_data is None:
                        self.quotes.pop(name, None)
                    else:
                        self.quotes[name] = quote_data
        return result['accepted']

    def collect_catalog(self):
        now = time.time()
        if now < self.retry_at['lis'] or now - self.catalog_checked_at < self.catalog_interval:
            return
        claim = self._claim('lis')
        if not claim:
            return
        try:
            result = read_catalog(self.catalog_session, self.catalog_etag)
        except Exception as error:
            self._complete('lis', claim, error=error)
            return
        saved = {'catalog_checked_at': time.time(), 'catalog_etag': self.catalog_etag}
        if result is not None:
            catalog, updated, skipped, etag = result
            saved.update(catalog=catalog, catalog_drops=self.find_catalog_drops(catalog, saved['catalog_checked_at']),
                         catalog_updated_at=updated, skipped=skipped, catalog_etag=etag)
        # A 304 checks availability without changing the source's price timestamp.
        self._complete('lis', claim, 'Каталог доступен', values=saved)

    def find_catalog_drops(self, catalog, now):
        """Keep one-hour price-drop hints, separate from verified Steam quotes."""
        with self.lock:
            previous = {item['name']: Decimal(item['price']) for item in self.catalog}
            existing = dict(self.catalog_drops)
        drops = {}
        for item in catalog:
            name, price = item['name'], Decimal(item['price'])
            old_price = previous.get(name)
            if old_price and price < old_price:
                drops[name] = {'price': item['price'], 'changed_at': now,
                               'percent': float((old_price - price) / old_price * 100)}
            elif name in existing and Decimal(existing[name]['price']) == price \
                    and now - existing[name]['changed_at'] <= 3600:
                drops[name] = existing[name]
        return drops

    def collect_fx(self):
        if time.time() < self.retry_at['fx'] or (self.fx and time.time() - self.fx['checked_at'] < 3600):
            return
        claim = self._claim('fx')
        if not claim:
            return
        try:
            rates = read_fx(self.fx_session)
        except Exception as error:
            self._complete('fx', claim, error=error)
            return
        self._complete('fx', claim, 'Курс Банка России', values={'fx': rates})

    def choose_candidates(self):
        """Focus scarce requests on likely deals; reserve discovery and refresh slots."""
        now = time.time()
        with self.lock:
            catalog = list(self.catalog)
            quotes = dict(self.quotes)
            attempted = dict(self.attempted)
            checked = set(self.checked_names) | set(quotes)
            checked_at = dict(self.checked_at)
            fx = self.fx
            turn = self.scan_turn
            bulk = self.bulk_orders
            catalog_drops = dict(self.catalog_drops)
        rank = lambda item: (attempted.get(item['name'], quotes.get(item['name'], {}).get('checked_at', 0)), not (.5 <= float(item['price']) <= 50), -min(item['stock'], 100))
        entries = bulk['entries'] if bulk and now - bulk['generated_at'] <= 86400 else {}
        focus = sorted(catalog, key=lambda item: (not (.5 <= float(item['price']) <= 50),
                       -entries.get(item['name'], {}).get('volume_30d', 0), -item['stock'], float(item['price']), item['name']))[:self.priority_size]
        focused_names = {item['name'] for item in focus}
        unseen = sorted((item for item in catalog if item['name'] not in checked), key=rank)
        focused_unseen = [item for item in unseen if item['name'] in focused_names]
        outside_unseen = [item for item in unseen if item['name'] not in focused_names]
        focused_rotating = sorted(focus, key=rank)
        rotating = sorted(catalog, key=rank)
        due, potential, profitable_names = [], [], set()
        for item in catalog:
            hint = entries.get(item['name'])
            if fx and hint and now - hint['updated_at'] <= 86400 and hint['quantity'] > 0:
                rate = fx['usd_per_currency'].get(hint['currency'])
                # A direct check newer than this histogram already tested the hint.
                verified_at = checked_at.get(item['name'], quotes.get(item['name'], {}).get('checked_at', 0))
                if rate and verified_at < hint['updated_at']:
                    calc = profitability(item['price'], hint['bid_cents'], rate)
                    if calc['profit'] > 0:
                        potential.append((item, calc['roi'], calc['profit']))
            quote_data = quotes.get(item['name'])
            if not fx or not quote_data or not quote_data['quantity']:
                continue
            rate = fx['usd_per_currency'].get(quote_data['currency'])
            if rate and profitability(item['price'], quote_data['bid_cents'], rate)['profit'] > 0:
                profitable_names.add(item['name'])
                if now - quote_data['checked_at'] >= max(30, self.quote_ttl * .6):
                    due.append(item)
        due.sort(key=lambda item: quotes[item['name']]['checked_at'])
        potential.sort(key=lambda entry: (-entry[1], -entry[2], rank(entry[0])))
        suggested = [entry[0] for entry in potential]
        suggested_names = {item['name'] for item in suggested}
        dropped = []
        for item in catalog:
            name = item['name']
            drop = catalog_drops.get(name)
            last_check = checked_at.get(name, quotes.get(name, {}).get('checked_at', 0))
            if drop and Decimal(drop['price']) == Decimal(item['price']) \
                    and 0 <= now - drop['changed_at'] <= 3600 and last_check < drop['changed_at'] \
                    and (drop['percent'] >= 5 or name in profitable_names):
                dropped.append(item)
        dropped.sort(key=lambda item: (item['name'] not in profitable_names,
                                      -catalog_drops[item['name']]['percent'], rank(item)))
        dropped_names = {item['name'] for item in dropped}
        def eligible(item):
            name = item['name']
            if now - attempted.get(name, 0) < self.request_interval:
                return False
            last_check = checked_at.get(name, quotes.get(name, {}).get('checked_at', 0))
            repeat_after = max(30, self.quote_ttl * .6) if name in profitable_names else 900
            return name in suggested_names or name in dropped_names or name not in attempted or now - last_check >= repeat_after
        selected, picked = [], set()
        for offset in range(self.batch_size):
            slot = turn + offset
            if slot % 10 == 9:
                queues = (outside_unseen, unseen, rotating)
            elif slot % 3 == 2:
                queues = (due, suggested, dropped, focused_unseen, focused_rotating, unseen, rotating)
            else:
                queues = (suggested, dropped, focused_unseen, due, focused_rotating, unseen, rotating)
            # Discovery falls back to rotating checks when the catalog is covered.
            # Avoid rechecking an otherwise fresh quote just because it is in focus.
            item = next((entry for queue in queues for entry in queue
                         if entry['name'] not in picked and eligible(entry)), None)
            if item is None:
                break
            selected.append(item)
            picked.add(item['name'])
        return selected

    def collect_bulk(self):
        if not self.bulk_key or time.time() < max(self.retry_at.get('steamapis', 0), self.storage_retry_at):
            return
        state = self.controls.get('steamapis', {})
        if time.time() < max(state.get('next_at', 0), state.get('lease_until', 0)):
            return
        claim = self._claim('steamapis')
        if not claim:
            return
        try:
            result = read_bulk_orders(self.bulk_session, self.bulk_key, self.bulk_currency)
            if self.bulk_orders and result['generated_at'] < self.bulk_orders['generated_at']:
                raise SourceError('SteamApis вернул более старый список. Сохраняем предыдущий.', kind='unavailable')
        except Exception as error:
            self._complete('steamapis', claim, error=error)
            return
        # This separate snapshot never overwrites direct quotes or their timestamps.
        changed = not self.bulk_orders or result['generated_at'] != self.bulk_orders['generated_at'] or result['entries'] != self.bulk_orders['entries']
        self._complete('steamapis', claim, 'Список ордеров получен; перепроверяем лучшие предложения',
                       values={'bulk_orders': result} if changed else None)

    def run_bulk(self):
        while not self.stop.is_set():
            try:
                if self.refresh_shared():
                    self.collect_bulk()
            except Exception:
                self._storage_error()
            self.stop.wait(5)

    def collect_steam(self, limit=None):
        if not self.catalog or time.time() < max(self.retry_at['steam'], self.next_steam_at, self.storage_retry_at):
            return
        # The shared gate permits one request at a time across all collectors.
        # Selection is cached locally; claim() rejects stale selections atomically.
        candidates = self.choose_candidates()
        if not candidates:
            return
        claim = self._claim('steam', [(item['name'], self.attempted.get(item['name'], 0)) for item in candidates])
        if not claim:
            self.refresh_shared(force=True)
            return
        name = claim['name']
        with self.lock:
            self.attempted[name] = claim['claimed_at']
        try:
            result = read_steam(self.session, name)
        except Exception as error:
            unavailable = isinstance(error, SourceError) and error.kind == 'item'
            self._complete('steam', claim, error=None if unavailable else error,
                           message='Проверяем каталог; часть предметов не имеет подходящего ордера' if unavailable else None,
                           checked=unavailable)
            return
        self._complete('steam', claim, 'Проверяем новые предметы и обновляем найденные ордера',
                       quote_data=result, checked=True)

    def run_sources(self):
        while not self.stop.is_set():
            try:
                if self.refresh_shared():
                    self.collect_catalog()
                    self.collect_fx()
            except Exception:
                self._storage_error()
            self.stop.wait(self.interval)

    def run(self):
        while not self.stop.is_set():
            with self.lock:
                self.cycle_running = True
            try:
                if self.refresh_shared():
                    self.collect_steam(limit=1)
            except Exception:
                self._storage_error()
            finally:
                with self.lock:
                    self.cycle_running = False
            self.stop.wait(1)

    def snapshot(self, deposit_fee_percent=0):
        self.refresh_shared()
        now = time.time()
        with self.lock:
            catalog = list(self.catalog)
            quotes = dict(self.quotes)
            fx = dict(self.fx) if self.fx else None
            status = {key: {**value, 'retry_at': self.retry_at[key] or None} for key, value in self.status.items()}
            catalog_updated, catalog_checked = self.catalog_updated_at, self.catalog_checked_at
            running = self.controls.get('steam', {}).get('lease_until', 0) > now
            cycle = self.last_cycle
            storage = dict(self.storage_status)
            checked_names = set(self.checked_names) | set(quotes)
            attempted_names = set(self.attempted)
            request_interval = self.request_interval
            bulk = self.bulk_orders
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
            'storage': storage,
            'scanner': {'mode': 'hybrid' if self.bulk_key or bulk else 'priority',
                        'bulk_configured': bool(self.bulk_key), 'priority_size': min(self.priority_size, len(catalog)),
                        'bulk_matched_total': len(set(bulk['entries']) & names) if bulk else 0,
                        'bulk_generated_at': bulk['generated_at'] if bulk else None},
            'quote_ttl_seconds': self.quote_ttl, 'catalog_ttl_seconds': self.catalog_ttl,
            'pending_ttl_seconds': self.pending_ttl, 'steam_request_interval_seconds': request_interval,
            'catalog_total': len(catalog), 'checked_total': len(checked_names & names),
            'attempted_total': len(attempted_names & names), 'fresh_quotes': fresh_quotes, 'stale_quotes': stale_quotes,
            'profitable_total': len(items), 'pending_total': len(pending_items), 'best_roi': max((item['roi'] for item in items), default=None),
            'catalog_updated_at': catalog_updated or None, 'catalog_checked_at': catalog_checked or None,
            'last_cycle_at': cycle, 'collecting': running, 'sources': status, 'fx': fx,
            'skipped_variants': self.skipped, 'deposit_fee_percent': deposit_fee_percent,
        }
