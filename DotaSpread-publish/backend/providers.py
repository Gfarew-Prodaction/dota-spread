import json
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree

import ijson
import requests

LIS_EXPORT = 'https://lis-skins.com/market_export_json/api_dota2_full.json'
CBR_URL = 'https://www.cbr.ru/scripts/XML_daily.asp'
STEAM_CURRENCIES = {1: 'USD', 3: 'EUR', 5: 'RUB'}
STEAMAPIS_LIST = 'https://api.steamapis.com/v2/steam/items/570/list'


class SourceError(Exception):
    def __init__(self, message, retry_after=60, kind='item'):
        super().__init__(message)
        self.retry_after = retry_after
        self.kind = kind


def client():
    session = requests.Session()
    session.headers.update({'User-Agent': 'DotaSpread/0.1 (price comparison; no purchases)', 'Accept-Language': 'en-US,en;q=0.9'})
    return session


def retry_delay(value, fallback=120):
    try:
        return max(1, int(value))
    except (ValueError, TypeError):
        try:
            return max(1, math.ceil(parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            return fallback


def checked_response(response):
    if response.status_code == 429:
        retry = response.headers.get('Retry-After', '120')
        raise SourceError('Источник ограничил частоту запросов. Ожидаем повторную проверку.', retry_delay(retry), kind='rate_limit')
    if response.status_code in (401, 403):
        raise SourceError('Источник временно отклонил запросы.', 300, kind='access')
    if response.status_code >= 500:
        raise SourceError('Источник временно недоступен.', retry_delay(response.headers.get('Retry-After'), 60), kind='unavailable')
    if response.status_code in (404, 410):
        raise SourceError('Предмет или ресурс больше не доступен.')
    response.raise_for_status()
    return response


def read_catalog(session, etag=None):
    """Stream the large official export instead of allocating 273k records."""
    grouped = {}
    updated_at = None
    skipped = 0
    now = time.time()
    headers = {'If-None-Match': etag} if etag else {}
    with checked_response(session.get(LIS_EXPORT, headers=headers, stream=True, timeout=(10, 60))) as response:
        if response.status_code == 304:
            return None
        response_etag = response.headers.get('ETag')
        response.raw.decode_content = True
        builder = None
        for prefix, event, value in ijson.parse(response.raw):
            if prefix == 'last_update' and event == 'number':
                updated_at = float(value)
            if prefix == 'items.item' and event == 'start_map':
                builder = ijson.ObjectBuilder()
            if builder is not None:
                builder.event(event, value)
                if prefix == 'items.item' and event == 'end_map':
                    item = builder.value
                    builder = None
                    name = item.get('name')
                    price = item.get('price')
                    # Extra gems/styles and future unlocks require more than a name match.
                    if not name or price is None or Decimal(str(price)) <= 0 or item.get('gems') or item.get('styles'):
                        skipped += 1
                        continue
                    unlock = item.get('unlock_at')
                    if unlock:
                        try:
                            if isinstance(unlock, (int, float, Decimal)):
                                stamp = float(unlock)
                            else:
                                moment = datetime.fromisoformat(str(unlock).replace('Z', '+00:00'))
                                stamp = (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()
                            if stamp > now:
                                skipped += 1
                                continue
                        except (ValueError, TypeError):
                            skipped += 1
                            continue
                    entry = grouped.setdefault(name, {'name': name, 'price': Decimal(str(price)), 'stock': 0, 'listing_id': item.get('id')})
                    entry['stock'] += 1
                    if Decimal(str(price)) < entry['price']:
                        entry['price'] = Decimal(str(price))
                        entry['listing_id'] = item.get('id')
    if not grouped or updated_at is None:
        raise SourceError('Выгрузка Lis-Skins не содержит ожидаемых цен или времени обновления.')
    return [{**item, 'price': str(item['price'])} for item in grouped.values()], updated_at, skipped, response_etag


def read_fx(session):
    response = checked_response(session.get(CBR_URL, timeout=(8, 20)))
    root = ElementTree.fromstring(response.content)
    rub_values = {'RUB': Decimal(1)}
    for item in root:
        code = item.findtext('CharCode')
        if code in ('USD', 'EUR'):
            rub_values[code] = Decimal(item.findtext('Value').replace(',', '.')) / Decimal(item.findtext('Nominal'))
    if 'USD' not in rub_values or 'EUR' not in rub_values:
        raise SourceError('Не удалось получить курс USD и EUR Банка России.')
    return {'rub_per_usd': str(rub_values['USD']), 'usd_per_currency': {code: str(value / rub_values['USD']) for code, value in rub_values.items()}, 'date': root.get('Date'), 'checked_at': time.time()}


def parse_steam_page(html, name, checked_at=None):
    """Read the public order book in Steam's current SSR page, checking its identity."""
    match = re.search(r'<script\b[^>]*\bid=[\"\']valve-ssr-data[\"\'][^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise SourceError('Steam не вернул страницу с ордерами. Повторим проверку позже.', kind='unavailable')
    root = json.loads(match.group(1))
    query_data = root.get('renderContext', {}).get('queryData', {})
    if isinstance(query_data, str):
        query_data = json.loads(query_data)
    order = None
    description = None
    stamp = None
    for query in query_data.get('queries', []):
        key = query.get('queryKey', [])
        state = query.get('state', {})
        data = state.get('data')
        if key == ['market', 'orderbook', 570, name] and isinstance(data, dict):
            order = data
            stamp = state.get('dataUpdatedAt', 0) / 1000
        if isinstance(data, dict) and data.get('market_hash_name') == name and data.get('appid', 570) == 570:
            description = data
        if isinstance(data, dict):
            for page in data.get('pages', []):
                for listing in page.get('listings', []):
                    candidate = listing.get('description', {})
                    if candidate.get('market_hash_name') == name:
                        description = candidate
    if not order or not stamp or not description or description.get('marketable') not in (True, 1):
        raise SourceError('Предмет не найден, не продаётся на Steam или нет времени обновления ордера.')
    currency = STEAM_CURRENCIES.get(order.get('eCurrency'))
    # This fee implementation is verified for USD/EUR. Do not pretend RUB fee rules match.
    if currency not in ('USD', 'EUR'):
        raise SourceError('Steam вернул валюту, для которой расчёт комиссии пока не поддерживается.')
    bid = int(order.get('amtMaxBuyOrder') or 0)
    depth = order.get('rgCompactBuyOrders', [])
    quantity = int(depth[1]) if len(depth) >= 2 and int(depth[0]) == bid else 0
    icon = description.get('icon_url', '')
    return {'name': name, 'bid_cents': bid, 'currency': currency, 'quantity': quantity, 'total_orders': int(order.get('cBuyOrders') or 0), 'updated_at': min(stamp, checked_at or time.time()), 'checked_at': checked_at or time.time(), 'image': 'https://community.steamstatic.com/economy/image/' + icon + '/128fx128f' if icon else None}


def read_steam(session, name):
    url = 'https://steamcommunity.com/market/listings/570/' + quote(name, safe='')
    response = checked_response(session.get(url, params={'l': 'english', 'currency': 1}, timeout=(8, 18)))
    return parse_steam_page(response.text, name)


class LimitedReader:
    """Bound the decompressed export, including responses without Content-Length."""
    def __init__(self, stream, limit=32 * 1024 * 1024):
        self.stream, self.remaining = stream, limit

    def read(self, size=-1):
        data = self.stream.read(min(size, self.remaining + 1) if size >= 0 else self.remaining + 1)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise SourceError('Выгрузка ордеров превышает допустимый размер.', kind='unavailable')
        return data


def parse_bulk_orders(stream, currency='USD', now=None):
    """Bulk data is a discovery hint, never a verified Steam quote."""
    if currency not in ('USD', 'EUR'):
        raise ValueError('STEAMAPIS_CURRENCY: USD или EUR.')
    now = time.time() if now is None else now
    metadata, entries, builder, building = {}, {}, None, None
    count = 0
    try:
        for prefix, event, value in ijson.parse(LimitedReader(stream)):
            if event == 'start_map' and prefix in ('metadata', 'items.item'):
                builder, building = ijson.ObjectBuilder(), prefix
            if builder is None:
                continue
            builder.event(event, value)
            if prefix != building or event != 'end_map':
                continue
            item, section = builder.value, building
            builder = building = None
            if section == 'metadata':
                metadata = item
                continue
            count += 1
            name = item.get('marketHashName')
            book = item.get('orderBook') or {}
            if not isinstance(name, str) or not name or item.get('flags', {}).get('marketable') is False:
                continue
            try:
                bid = Decimal(str(book.get('highestBuy')))
                age = float(book.get('histogramAgeSeconds'))
                stamp = float(item['updatedAt']) / 1000
                quantity = sum(int(level['quantity']) for level in book.get('buyOrdersTop10', [])
                               if Decimal(str(level['price'])) == bid and int(level['quantity']) > 0)
                volume = max(0, int((item.get('volume') or {}).get('30d') or 0))
                if not bid.is_finite() or bid <= 0 or not quantity or not math.isfinite(age) or age < 0 or not math.isfinite(stamp) or stamp <= 0:
                    continue
                entry = {'bid_cents': int(bid * 100), 'currency': currency, 'quantity': quantity,
                         'updated_at': min(stamp, now) - age, 'volume_30d': volume, 'age': age}
                if name not in entries or entry['updated_at'] > entries[name]['updated_at']:
                    entries[name] = entry
            except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
                continue
        generated = float(metadata.get('generatedAt', 0)) / 1000
        if metadata.get('appId') != 570 or not math.isfinite(generated) or generated <= 0 or generated > now + 300:
            raise ValueError('Invalid app or snapshot timestamp')
        if count == 0 or int(metadata.get('itemCount', count)) != count:
            raise ValueError('Incomplete item list')
    except (ijson.JSONError, ValueError, TypeError, AttributeError, InvalidOperation) as error:
        raise SourceError('Выгрузка ордеров имеет неожиданный формат. Сохраняем прошлые данные.', kind='unavailable') from error
    for entry in entries.values():
        entry['updated_at'] = min(entry['updated_at'], generated - entry.pop('age'))
    return {'entries': entries, 'generated_at': min(generated, now), 'downloaded_at': now,
            'currency': currency, 'item_count': count}


def read_bulk_orders(session, api_key, currency='USD'):
    # Authenticate only the API request. A cross-host CDN redirect must never
    # receive the API key, including on subsequent redirects.
    url = STEAMAPIS_LIST
    for step in range(4):
        headers = {'x-api-key': api_key} if step == 0 else {}
        with session.get(url, headers=headers, stream=True, allow_redirects=False, timeout=(8, 25)) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get('Location', ''))
                parsed = urlsplit(target)
                host = parsed.hostname or ''
                if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443) or not (host == 'steamapis.com' or host.endswith('.steamapis.com')):
                    raise SourceError('SteamApis вернул неизвестный адрес выгрузки.', kind='unavailable')
                url = target
                continue
            if response.status_code in (400, 401, 403):
                raise SourceError('SteamApis: проверь ключ, баланс и доступ к App Price List.', 3600, kind='access')
            checked_response(response)
            response.raw.decode_content = True
            return parse_bulk_orders(response.raw, currency)
    raise SourceError('SteamApis не вернул файл ордеров.', kind='unavailable')
