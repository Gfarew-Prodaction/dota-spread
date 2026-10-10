import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
# One process keeps the small instance lightweight; PostgreSQL coordinates collectors.
workers = 1
worker_class = 'gthread'
threads = 4
preload_app = False
timeout = 60
graceful_timeout = 30
accesslog = '-'
errorlog = '-'
