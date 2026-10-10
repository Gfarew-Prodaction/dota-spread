import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote
from xml.etree import ElementTree

import ijson
import requests

LIS_EXPORT = 'https://lis-skins.com/market_export_json/api_dota2_full.json'
CBR_URL = 'https://www.cbr.ru/scripts/XML_daily.asp'
STEAM_CURRENCIES = {1: 'USD', 3: 'EUR', 5: 'RUB'}


class SourceError(Exception):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


def client():
    session = requests.Session()
    session.headers.update({'User-Agent': 'DotaSpread/0.1 (price comparison; no purchases)', 'Accept-Language': 'en-US,en;q=0.9'})
    return session


def checked_response(response):
    if response.status_code == 429:
        retry = response.headers.get('Retry-After', '120')
        raise SourceError('Источник ограничил частоту запросов. Сбор временно приостановлен.', int(retry) if retry.isdigit() else 120)
    if response.status_code in (401, 403):
        raise SourceError('Источник временно отклонил запросы.', 120)
    response.raise_for_status()
    return response


def read_catalog(session):
    """Stream the large official export instead of allocating 273k records."""
    grouped = {}
    updated_at = None
    skipped = 0
    now = time.time()
    with checked_response(session.get(LIS_EXPORT, stream=True, timeout=(10, 60))) as response:
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
    return [{**item, 'price': str(item['price'])} for item in grouped.values()], updated_at, skipped


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
        raise SourceError('Страница Steam не содержит стакан ордеров. Требуется обновление адаптера.')
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
