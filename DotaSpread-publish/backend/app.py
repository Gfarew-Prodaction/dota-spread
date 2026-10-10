import atexit
import logging
import math
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .service import MarketService
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


def create_service(default_role='all'):
    return MarketService(
        Store(os.getenv('DOTA_DB_PATH', str(ROOT / '.cache' / 'market.sqlite3')),
              database_url=os.getenv('DATABASE_URL', '').strip() or None),
        interval=max(10, int(os.getenv('UPDATE_INTERVAL', '10'))),
        batch_size=max(1, min(10, int(os.getenv('STEAM_BATCH_SIZE', '3')))),
        quote_ttl=max(20, int(os.getenv('QUOTE_TTL', '120'))),
        catalog_interval=max(10, int(os.getenv('LIS_REFRESH_INTERVAL', '60'))),
        catalog_ttl=max(60, int(os.getenv('CATALOG_TTL', '600'))),
        pending_ttl=max(120, int(os.getenv('PENDING_TTL', '1800'))),
        steam_request_interval=max(2, float(os.getenv('STEAM_REQUEST_INTERVAL', '4'))),
        collector_role=os.getenv('COLLECTOR_ROLE', default_role),
        worker_id=os.getenv('COLLECTOR_ID') or None,
    )


def create_app(service=None, start_collector=False):
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    if service is None:
        service = create_service()
    app.extensions['market_service'] = service
    if start_collector:
        service.start()
        atexit.register(service.close)

    @app.get('/api/opportunities')
    def opportunities():
        try:
            fee = float(request.args.get('deposit_fee', '0'))
            if not math.isfinite(fee) or not 0 <= fee <= 100:
                raise ValueError
        except ValueError:
            return jsonify(error='Комиссия пополнения должна быть числом от 0 до 100.'), 400
        return jsonify(service.snapshot(fee))

    @app.get('/api/health')
    def health():
        return jsonify(status='ok', collector_running=service.collector_running(),
                       collector_role=service.collector_role,
                       storage={**service.storage_status})

    @app.get('/')
    def index():
        dist = ROOT / 'frontend' / 'dist'
        if not (dist / 'index.html').is_file():
            return jsonify(error='Сначала выполните npm run build в папке frontend.'), 503
        return send_from_directory(dist, 'index.html')

    @app.get('/assets/<path:filename>')
    def assets(filename):
        return send_from_directory(ROOT / 'frontend' / 'dist' / 'assets', filename)

    @app.after_request
    def headers(response):
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response

    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    create_app(start_collector=True).run(host='127.0.0.1', port=int(os.getenv('PORT', '5050')), threaded=True, use_reloader=False)
