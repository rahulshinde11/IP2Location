"""Gunicorn configuration for the IP2Location API.

The previous CMD ran gunicorn with its defaults: a single sync worker, i.e. one
request served at a time. This config adds real concurrency while staying light
on memory for the in-RAM proxy CSV.

Key choices:
  * preload_app = True  -> the app (and the proxy CSV) is loaded ONCE in the
    master process and inherited by workers via copy-on-write, instead of each
    worker loading its own multi-GB copy. This is the main memory win when
    ENABLE_PROXY_DETECTION is on with a large IPv6 CSV.
  * gthread workers      -> threads within a worker share that loaded data, so
    you get concurrency without multiplying memory. Lookups are short and
    release the GIL during mmap/file access.

Tunables (env vars):
  WEB_CONCURRENCY   number of worker processes      (default 2)
  GUNICORN_THREADS  threads per worker              (default 4)
  GUNICORN_TIMEOUT  worker timeout in seconds       (default 60)

Memory-constrained + large proxy CSV? Set WEB_CONCURRENCY=1 and raise
GUNICORN_THREADS to keep a single in-memory copy.
"""

import os

bind = "0.0.0.0:8080"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
worker_class = "gthread"
preload_app = True
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5


def post_fork(server, worker):
    """Start the background DB hot-reload watcher in each worker.

    With preload_app=True the app is imported in the master and forked; threads
    do not survive fork, so the watcher must be (re)started here, per worker.
    """
    try:
        from app import start_db_watcher
        start_db_watcher()
    except Exception as e:  # never let a hook failure crash the worker
        worker.log.warning("could not start db watcher: %s", e)
