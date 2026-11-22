"""TradingView -> Binance Demo bridge (Flask).

Flow: POST /webhook -> authenticate -> parse -> duplicate check -> queue -> worker executes.

The webhook answers within milliseconds; execution happens on a single background
worker so entries and closes are processed one at a time, in arrival order.
"""
import hmac
import json
import logging
import queue
import re
import threading
import uuid

from flask import Flask, jsonify, request

from alert_parser import ENTRY, AlertError, parse_alert
from binance_client import BinanceClient
from config import ConfigError, load
from executor import Executor
from store import Store

cfg = load()


class _Redactor(logging.Filter):
    """Strips credentials and webhook secrets from every log line."""

    def __init__(self, secrets):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]
        self._signature = re.compile(r"signature=[0-9a-fA-F]{16,}")
        self._query_secret = re.compile(r"(secret=)[^&\\s\"']+", re.IGNORECASE)

    def filter(self, record):
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        message = self._signature.sub("signature=***", message)
        message = self._query_secret.sub(r"\\1***", message)
        record.msg, record.args = message, ()
        return True


logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_redactor = _Redactor([cfg.api_key, cfg.api_secret, cfg.webhook_secret])
for _name in ("", "gunicorn.error", "gunicorn.access"):
    for _handler in logging.getLogger(_name).handlers:
        _handler.addFilter(_redactor)
log = logging.getLogger("app")

store = Store(cfg.db_path)
client = BinanceClient(cfg)
executor = Executor(cfg, client)
jobs: "queue.Queue" = queue.Queue()

interrupted = store.mark_interrupted()
if interrupted:
    log.warning("%d alert(s) were interrupted by a restart and will NOT be retried. Check /status and Binance.", interrupted)


def _worker() -> None:
    while True:
        key, alert = jobs.get()
        try:
            store.update(key, "PROCESSING")
            result = executor.process(key, alert)
            store.update(key, result["status"], result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Worker error")
            store.update(key, "FAILED", {"status": "FAILED", "code": "WORKER_ERROR", "message": type(exc).__name__})
        finally:
            jobs.task_done()


threading.Thread(target=_worker, name="executor", daemon=True).start()

app = Flask(__name__)

# Start warmup lazily on the first app request, rather than during module import.
# This avoids import-time startup races and keeps readiness checks observable.
_warmup_lock = threading.Lock()
_warmup_started = False


def _start_warmup_once() -> None:
    global _warmup_started
    with _warmup_lock:
        if _warmup_started:
            return
        _warmup_started = True
        log.info("Warmup trigger: first application request")
        threading.Thread(target=executor.warmup, name="warmup", daemon=True).start()


@app.before_request
def _ensure_warmup_started():
    _start_warmup_once()


def _authorised(body_secret=None) -> bool:
    candidates = [request.headers.get("X-Webhook-Secret"), request.args.get("secret"), body_secret]
    expected = cfg.webhook_secret.encode("utf-8")
    return any(c and hmac.compare_digest(str(c).encode("utf-8"), expected) for c in candidates)


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        symbol=cfg.symbol,
        symbol_ready=executor.symbol_ready,
        symbol_note=executor.symbol_note,
        queue_depth=jobs.qsize(),
    )


@app.post("/webhook")
def webhook():
    raw = request.get_data(as_text=True) or ""

    body_json = None
    if raw.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw)
            body_json = parsed if isinstance(parsed, dict) else None
        except ValueError:
            body_json = None

    body_secret = body_json.get("secret") if body_json else None
    if not _authorised(body_secret):
        log.warning("Webhook rejected: bad or missing secret")
        return jsonify(status="unauthorized"), 401

    try:
        alert, _ = parse_alert(raw, body_json)
    except AlertError as exc:
        log.warning("Webhook rejected: %s", exc)
        return jsonify(status="invalid", error=str(exc)), 400

    key = alert.dedupe_key() if alert.event == ENTRY else uuid.uuid4().hex
    existing = store.claim(key, alert.event, alert.public_dict())
    if existing is not None:
        log.info("[%s] duplicate alert ignored (previous status %s)", key[:8], existing["status"])
        return jsonify(status="duplicate", previous_status=existing["status"], key=key[:12]), 200

    jobs.put((key, alert))
    log.info("[%s] accepted %s %s", key[:8], alert.event, alert.direction)
    return jsonify(status="accepted", key=key[:12], event=alert.event, direction=alert.direction), 202


@app.get("/status")
def status():
    if not _authorised():
        return jsonify(status="unauthorized"), 401
    return jsonify(
        symbol_ready=executor.symbol_ready,
        symbol_note=executor.symbol_note,
        queue_depth=jobs.qsize(),
        recent=store.recent(cfg.status_history_limit),
    )
