"""Parses TradingView alert messages into a normalised Alert.

Supported inputs
  1. Plain text produced by the Pine strategy's f_entryAlert():
         🟢 LONG ENTRY
         Symbol: XAUUSDT.P
         Side: LONG
         Leverage: Isolated 20x
         Quantity: 1
         Entry price: 3500.12
         Take Profit: 3530.00
         Stop Loss: 3480.00
         ...
  2. Force-close commands emitted by the strategy: "/close_long" or "/close_short"
  3. JSON (optional), e.g. {"event":"ENTRY","direction":"LONG","quantity":1,...}

The parser only extracts values. It never computes or changes trading parameters.
"""
import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

ENTRY = "ENTRY"
CLOSE = "CLOSE"


class AlertError(ValueError):
    """The alert could not be understood or is missing required fields."""


@dataclass(frozen=True)
class Alert:
    event: str                      # ENTRY | CLOSE
    direction: str                  # LONG | SHORT
    symbol: Optional[str] = None
    quantity: Optional[Decimal] = None
    entry_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    leverage: Optional[int] = None
    margin_mode: Optional[str] = None       # ISOLATED | CROSSED
    estimated_margin: Optional[Decimal] = None
    exchange: Optional[str] = None
    timeframe: Optional[str] = None
    signal_time: Optional[str] = None
    signal_id: Optional[str] = None

    def dedupe_key(self) -> str:
        """Stable idempotency key: identical alert content -> identical key."""
        if self.signal_id:
            basis = f"id|{self.signal_id}"
        else:
            basis = "|".join(
                str(x)
                for x in (
                    self.event, self.direction, self.symbol, self.quantity,
                    self.entry_price, self.stop_loss, self.take_profit,
                    self.leverage, self.margin_mode, self.signal_time,
                )
            )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def public_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items() if v is not None}


_CLOSE_RE = re.compile(r"^\s*/?close[_\s-]?(long|short)\b", re.IGNORECASE)
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$")
_LEV_RE = re.compile(r"(isolated|cross(?:ed)?)\s+([0-9]+(?:\.[0-9]+)?)\s*x", re.IGNORECASE)


def normalise_symbol(value: str) -> str:
    """'BINANCE:XAUUSDT.P' -> 'XAUUSDT'."""
    return value.strip().upper().split(":")[-1].split(".")[0]


def _dec(value, name: str) -> Decimal:
    text = "" if value is None else str(value).strip().replace(",", "")
    if not text:
        raise AlertError(f"Missing field: {name}")
    try:
        number = Decimal(text.split()[0])
    except InvalidOperation:
        raise AlertError(f"Invalid number for {name}: {text!r}") from None
    if not number.is_finite():
        raise AlertError(f"Invalid number for {name}: {text!r}")
    return number


def _leverage(value) -> int:
    number = _dec(value, "leverage")
    if number != number.to_integral_value() or number < 1:
        raise AlertError(f"leverage must be a whole number >= 1, got {value!r}")
    return int(number)


def _margin_mode(value) -> str:
    text = str(value or "").strip().upper()
    if text == "ISOLATED":
        return "ISOLATED"
    if text in ("CROSS", "CROSSED"):
        return "CROSSED"
    raise AlertError(f"margin mode must be Isolated or Cross, got {value!r}")


def _direction(value) -> str:
    text = str(value or "").strip().upper()
    if text in ("LONG", "BUY"):
        return "LONG"
    if text in ("SHORT", "SELL"):
        return "SHORT"
    raise AlertError(f"direction must be LONG or SHORT, got {value!r}")


def _from_text(raw: str) -> Alert:
    close = _CLOSE_RE.match(raw)
    if close:
        return Alert(event=CLOSE, direction=close.group(1).upper())

    fields = {}
    for line in raw.splitlines():
        match = _KV_RE.match(line)
        if match:
            fields[match.group(1).strip().lower().replace(" ", "_")] = match.group(2)

    if "side" not in fields:
        raise AlertError("Unrecognised alert: no 'Side:' line and not a /close_long or /close_short command")

    lev = _LEV_RE.search(fields.get("leverage", ""))
    if not lev:
        raise AlertError(f"Cannot read leverage line: {fields.get('leverage')!r}")

    estimated_margin = None
    if fields.get("estimated_margin", "").strip().lower() not in ("", "n/a"):
        estimated_margin = _dec(fields["estimated_margin"], "estimated margin")

    return Alert(
        event=ENTRY,
        direction=_direction(fields["side"]),
        symbol=normalise_symbol(fields.get("symbol", "")) or None,
        quantity=_dec(fields.get("quantity"), "quantity"),
        entry_price=_dec(fields.get("entry_price"), "entry price"),
        stop_loss=_dec(fields.get("stop_loss"), "stop loss"),
        take_profit=_dec(fields.get("take_profit"), "take profit"),
        leverage=_leverage(lev.group(2)),
        margin_mode=_margin_mode(lev.group(1)),
        estimated_margin=estimated_margin,
        exchange=fields.get("exchange") or None,
        timeframe=fields.get("timeframe") or None,
        signal_time=fields.get("signal_time") or None,
    )


def _from_json(obj: dict) -> Alert:
    def pick(*names):
        for name in names:
            if obj.get(name) not in (None, ""):
                return obj[name]
        return None

    event = str(pick("event", "action") or ENTRY).strip().upper()
    force_close = str(pick("force_close") or "").strip().lower() in ("true", "1", "yes")
    symbol = pick("symbol", "ticker")
    symbol = normalise_symbol(str(symbol)) if symbol else None
    direction = _direction(pick("direction", "side"))

    if event in ("CLOSE", "EXIT", "FORCE_CLOSE") or force_close:
        return Alert(event=CLOSE, direction=direction, symbol=symbol, signal_id=pick("signal_id", "id"))
    if event != ENTRY:
        raise AlertError(f"Unsupported event: {event!r}")

    margin_mode = pick("margin_mode")
    return Alert(
        event=ENTRY,
        direction=direction,
        symbol=symbol,
        quantity=_dec(pick("quantity", "qty"), "quantity"),
        entry_price=_dec(pick("entry_price", "entry"), "entry_price"),
        stop_loss=_dec(pick("stop_loss", "sl"), "stop_loss"),
        take_profit=_dec(pick("take_profit", "tp"), "take_profit"),
        leverage=_leverage(pick("leverage")),
        margin_mode=_margin_mode(margin_mode),
        estimated_margin=_dec(pick("estimated_margin"), "estimated_margin") if pick("estimated_margin") else None,
        exchange=pick("exchange"),
        timeframe=pick("timeframe"),
        signal_time=pick("timestamp", "signal_time"),
        signal_id=pick("signal_id", "id"),
    )


def parse_alert(raw: str, body_json: Optional[dict]) -> Tuple[Alert, Optional[str]]:
    """Returns (alert, secret_found_in_json_body)."""
    if body_json is not None:
        secret = body_json.get("secret")
        return _from_json(body_json), (str(secret) if secret is not None else None)
    if not raw or not raw.strip():
        raise AlertError("Empty alert body")
    return _from_text(raw), None
