
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = 4
timeout = 60
worker_class = "gthread"


def post_worker_init(worker):
    from app import start_executor_worker

    start_executor_worker()
