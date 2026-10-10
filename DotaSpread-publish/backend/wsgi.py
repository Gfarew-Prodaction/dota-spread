"""Production entry point: imported once by the single Gunicorn worker."""

import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = create_app(start_collector=True)
