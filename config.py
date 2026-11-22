"""Configuration for the TradingView -> Binance Demo bridge.

Every value comes from an environment variable. Nothing is defaulted in code:
if a variable is missing or invalid the service refuses to start and says which one.
"""
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    pass


def _raw(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"Missing required environment variable: {name}")
    return value.strip()


def _int(name: str, minimum: int = None) -> int:
    raw = _raw(name)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, minimum: float = None) -> float:
    raw = _raw(name)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _bool(name: str) -> bool:
    raw = _raw(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _choice(name: str, choices) -> str:
    raw = _raw(name)
    for choice in choices:
        if raw.lower() == choice.lower():
            return choice
    raise ConfigError(f"{name} must be one of: {', '.join(choices)}")


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    base_url: str
    webhook_secret: str
    symbol: str
    db_path: str
    recv_window_ms: int
    http_timeout: float
    order_id_prefix: str
    protective_mode: str          # auto | algo | classic
    working_type: str             # MARK_PRICE | CONTRACT_PRICE
    flatten_on_protection_failure: bool
    cancel_stale_orders: bool
    check_balance: bool
    max_entry_deviation_pct: float  # 0 disables the check
    confirm_retries: int
    confirm_delay_s: float
    exchange_info_ttl: int
    status_history_limit: int
    log_level: str


def load() -> Config:
    base_url = _raw("BINANCE_BASE_URL").rstrip("/")
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    # Safety guard (not a setting): this service must never talk to a live exchange.
    if (
        parsed.scheme != "https"
        or not host.endswith(("binance.com", "binancefuture.com"))
        or not any(token in host for token in ("demo", "testnet"))
    ):
        raise ConfigError(
            "BINANCE_BASE_URL must be an https Binance Demo/testnet host. "
            "Live trading is blocked by design."
        )

    webhook_secret = _raw("WEBHOOK_SECRET")
    if len(webhook_secret) < 16:
        raise ConfigError("WEBHOOK_SECRET must be at least 16 characters (use a long random string)")

    prefix = _raw("ORDER_ID_PREFIX")
    if len(prefix) > 8 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", prefix):
        raise ConfigError("ORDER_ID_PREFIX must be 1-8 chars of letters, digits or _ . : / -")

    return Config(
        api_key=_raw("BINANCE_API_KEY"),
        api_secret=_raw("BINANCE_API_SECRET"),
        base_url=base_url,
        webhook_secret=webhook_secret,
        symbol=_raw("TRADE_SYMBOL").upper(),
        db_path=_raw("DB_PATH"),
        recv_window_ms=_int("RECV_WINDOW_MS", 1),
        http_timeout=_float("HTTP_TIMEOUT_SECONDS", 1),
        order_id_prefix=prefix,
        protective_mode=_choice("PROTECTIVE_ORDER_MODE", ("auto", "algo", "classic")),
        working_type=_choice("TRIGGER_WORKING_TYPE", ("MARK_PRICE", "CONTRACT_PRICE")),
        flatten_on_protection_failure=_bool("FLATTEN_ON_PROTECTION_FAILURE"),
        cancel_stale_orders=_bool("CANCEL_STALE_ORDERS_BEFORE_ENTRY"),
        check_balance=_bool("CHECK_AVAILABLE_BALANCE"),
        max_entry_deviation_pct=_float("MAX_ENTRY_DEVIATION_PCT", 0),
        confirm_retries=_int("ORDER_CONFIRM_RETRIES", 0),
        confirm_delay_s=_int("ORDER_CONFIRM_DELAY_MS", 0) / 1000.0,
        exchange_info_ttl=_int("EXCHANGE_INFO_TTL_SECONDS", 0),
        status_history_limit=_int("STATUS_HISTORY_LIMIT", 1),
        log_level=_choice("LOG_LEVEL", ("DEBUG", "INFO", "WARNING", "ERROR")),
    )
