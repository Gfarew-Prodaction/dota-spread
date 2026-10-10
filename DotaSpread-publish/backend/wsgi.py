"""Production entry point; COLLECTOR_ROLE controls background collection."""

import logging

from .app import create_app

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = create_app(start_collector=True)
