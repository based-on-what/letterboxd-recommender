bind = "0.0.0.0:8080"
workers = 2
threads = 2
timeout = 300
keepalive = 5
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 50

wsgi_app = "main:app"

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
