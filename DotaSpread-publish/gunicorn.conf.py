import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
# The collector lives in the web worker. Multiple workers would duplicate requests.
workers = 1
worker_class = 'gthread'
threads = 4
preload_app = False
timeout = 60
graceful_timeout = 30
accesslog = '-'
errorlog = '-'
