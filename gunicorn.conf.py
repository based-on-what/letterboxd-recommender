# bind y workers se definen via CLI en Procfile (usa $PORT de Railway)
# Este archivo provee defaults para desarrollo local: `gunicorn --config gunicorn.conf.py`
workers = 2
worker_class = "gthread"
threads = 4
timeout = 180
keepalive = 5
graceful_timeout = 30
max_requests = 500
max_requests_jitter = 50

wsgi_app = "main:app"

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
