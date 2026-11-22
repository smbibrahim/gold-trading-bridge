"""Trade execution. Receives validated alerts and executes them on Binance Demo.

The bot never creates trading parameters. Quantity, leverage, margin mode, stop loss and
take profit all come from the TradingView alert. The only adjustment made is rounding
SL/TP trigger prices to the exchange tick size (formatting, not strategy).
"""
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from alert_parser import ENTRY, Alert
from binance_client import BinanceError, TransportError

log = logging.getLogger("executor")

ZERO = Decimal(0)


class Reject(Exception):
    """Alert refused before any order was sent. Nothing changed on the exchange."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Failure(Exception):
    """Something went wrong at or after order placement."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class Rules:
    status: str
    margin_asset: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal


def fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


class Executor:
    def __init__(self, cfg, client, sleep=time.sleep):
        self.cfg = cfg
        self.client = client
        self._sleep = sleep
        self._lock = threading.Lock()
        self._rules: Optional[Rules] = None
        self._rules_at = 0.0
        self.symbol_ready: Optional[bool] = None   # None = not checked yet
        self.symbol_note = "not checked yet"

    # ------------------------------------------------------------ entry point
    def process(self, key: str, alert: Alert) -> dict:
        with self._lock:
            tag = key[:8]
            try:
                if alert.event == ENTRY:
                    result = self._entry(key, alert)
                else:
                    result = self._close(alert)
            except Reject as exc:
                log.warning("[%s] REJECTED %s: %s", tag, exc.code, exc)
                return {"status": "REJECTED", "code": exc.code, "message": str(exc)}
            except Failure as exc:
                log.error("[%s] FAILED %s: %s", tag, exc.code, exc)
                return {"status": "FAILED", "code": exc.code, "message": str(exc), "details": exc.details}
            except (BinanceError, TransportError) as exc:
                log.error("[%s] FAILED EXCHANGE_ERROR: %s", tag, exc)
                return {"status": "FAILED", "code": "EXCHANGE_ERROR", "message": str(exc)}
            except Exception as exc:  # noqa: BLE001 - last line of defence, never crash the worker
                log.exception("[%s] FAILED UNEXPECTED_ERROR", tag)
                return {"status": "FAILED", "code": "UNEXPECTED_ERROR", "message": type(exc).__name__}
            log.info("[%s] %s %s", tag, result["status"], result.get("code", ""))
            return result

    def warmup(self) -> None:
        """Startup validation of Binance connectivity and symbol rules."""
        self.symbol_ready = False
        self.symbol_note = "startup check in progress"

        log.info("WARMUP STARTED")

        try:
            log.info("WARMUP: syncing Binance time")
            self.client.sync_time()
            log.info("WARMUP: time sync successful")

            log.info("WARMUP: checking symbol %s", self.cfg.symbol)
            rules = self.symbol_rules(force=True)

            self.symbol_ready = rules.status == "TRADING"
            self.symbol_note = f"status {rules.status}"

            log.info(
                "WARMUP COMPLETE: ready=%s note=%s",
                self.symbol_ready,
                self.symbol_note,
            )

        except Reject as exc:
            self.symbol_ready = False
            self.symbol_note = f"startup rejected: {exc.code}"
            log.error("WARMUP REJECTED: %s", exc)

        except Exception as exc:  # noqa: BLE001
            self.symbol_ready = False
            self.symbol_note = f"startup check error: {type(exc).__name__}"
            log.exception("WARMUP ERROR")

    # --------------------------------------------------------- exchange rules
    def symbol_rules(self, force: bool = False) -> Rules:
        now = time.time()
        if not force and self._rules and now - self._rules_at < self.cfg.exchange_info_ttl:
            return self._rules

        info = self.client.exchange_info()
        match = next((s for s in info.get("symbols", []) if s.get("symbol") == self.cfg.symbol), None)
        if match is None:
            self._rules = None
            self.symbol_ready = False
            self.symbol_note = "symbol not listed on the configured endpoint"
            raise Reject("SYMBOL_NOT_SUPPORTED", f"{self.cfg.symbol} is not listed on the configured Binance endpoint")

        filters = {f.get("filterType"): f for f in match.get("filters", [])}
        price = filters.get("PRICE_FILTER")
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
        if not price or not lot:
            self.symbol_ready = False
            self.symbol_note = "exchange filters missing"
            raise Reject("SYMBOL_RULES_MISSING", "PRICE_FILTER / LOT_SIZE missing in exchange info")
        notional = filters.get("MIN_NOTIONAL")

        rules = Rules(
            status=str(match.get("status")),
            margin_asset=str(match.get("marginAsset", "USDT")),
            tick=Decimal(price["tickSize"]),
            step=Decimal(lot["stepSize"]),
            min_qty=Decimal(lot["minQty"]),
            max_qty=Decimal(lot["maxQty"]),
            min_notional=Decimal(notional["notional"]) if notional and "notional" in notional else ZERO,
        )
        self._rules, self._rules_at = rules, now
        self.symbol_ready = rules.status == "TRADING"
        self.symbol_note = f"status {rules.status}"
        return rules

    # ------------------------------------------------------------------ ENTRY
    def _entry(self, key: str, alert: Alert) -> dict:
        cfg, client = self.cfg, self.client
        symbol = cfg.symbol

        if alert.symbol != symbol:
            raise Reject("SYMBOL_MISMATCH", f"Alert symbol {alert.symbol!r} does not match configured symbol {symbol!r}")

        rules = self.symbol_rules()
        if rules.status != "TRADING":
            raise Reject("SYMBOL_NOT_TRADING", f"{symbol} status is {rules.status}")

        if client.position_mode_is_hedge():
            raise Reject("HEDGE_MODE", "Account is in Hedge mode; this bot requires One-way mode")

        qty = self._validate_quantity(alert, rules)
        sl = self._to_tick(alert.stop_loss, rules.tick)
        tp = self._to_tick(alert.take_profit, rules.tick)
        self._validate_levels(alert, sl, tp)

        if self._position_amt() != ZERO:
            raise Reject("POSITION_OPEN", "A position is already open for this symbol")

        client_id = self._client_id(key)
        if client.get_order(symbol, client_id) is not None:
            raise Reject("DUPLICATE_ON_EXCHANGE", "An order with this signal's client id already exists on Binance")

        self._check_deviation(alert)
        self._check_balance(alert, qty, rules)

        if cfg.cancel_stale_orders:
            self._cancel_all_orders(strict=True)

        client.set_margin_type(symbol, alert.margin_mode)
        applied = client.set_leverage(symbol, alert.leverage)
        if applied != alert.leverage:
            raise Reject("LEVERAGE_NOT_APPLIED", f"Requested leverage {alert.leverage}x but Binance applied {applied}x")

        side = "BUY" if alert.direction == "LONG" else "SELL"
        order = self._send_entry(symbol, side, qty, client_id)
        try:
            order = self._confirm_order(client_id, order)
        except Failure:
            if self._position_amt() == ZERO:
                raise
            log.warning("[%s] entry order status unclear but a position exists; continuing to protect it", key[:8])

        position = self._wait_for_position(alert.direction)
        details = {
            "order_id": order.get("orderId"),
            "client_order_id": client_id,
            "side": side,
            "requested_qty": fmt(qty),
            "executed_qty": order.get("executedQty"),
            "avg_price": order.get("avgPrice"),
            "position_amt": fmt(position),
            "leverage": applied,
            "margin_mode": alert.margin_mode,
            "stop_loss": fmt(sl),
            "take_profit": fmt(tp),
        }

        try:
            protection = self._place_protection(alert.direction, sl, tp, key)
            if self._position_amt() == ZERO:
                details["note"] = "position already closed by a protective order before verification"
                details["protection"] = protection
                return {"status": "EXECUTED", "code": "OPENED_AND_CLOSED", "message": "Entry filled and already closed", "details": details}
            self._verify_protection(protection)
            details["protection"] = protection
        except Exception as exc:  # noqa: BLE001 - any failure here leaves an unprotected position
            return self._protection_failed(exc, details)

        return {"status": "EXECUTED", "code": "OPENED", "message": "Entry filled and protected", "details": details}

    # ------------------------------------------------------------- validation
    def _validate_quantity(self, alert: Alert, rules: Rules) -> Decimal:
        qty = alert.quantity
        if qty <= ZERO:
            raise Reject("INVALID_QUANTITY", "Quantity must be greater than zero")
        if qty % rules.step != ZERO:
            raise Reject("INVALID_QUANTITY", f"Quantity {fmt(qty)} is not a multiple of step size {fmt(rules.step)}")
        if qty < rules.min_qty or qty > rules.max_qty:
            raise Reject("INVALID_QUANTITY", f"Quantity {fmt(qty)} outside allowed range {fmt(rules.min_qty)}..{fmt(rules.max_qty)}")
        if rules.min_notional > ZERO and qty * alert.entry_price < rules.min_notional:
            raise Reject("INVALID_QUANTITY", f"Notional below exchange minimum {fmt(rules.min_notional)}")
        return qty

    @staticmethod
    def _to_tick(price: Decimal, tick: Decimal) -> Decimal:
        rounded = (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
        if rounded <= ZERO:
            raise Reject("INVALID_LEVELS", f"Price {fmt(price)} is not valid for tick size {fmt(tick)}")
        return rounded

    @staticmethod
    def _validate_levels(alert: Alert, sl: Decimal, tp: Decimal) -> None:
        entry = alert.entry_price
        if alert.direction == "LONG":
            ok = sl < entry < tp
        else:
            ok = tp < entry < sl
        if not ok:
            raise Reject("INVALID_LEVELS", f"Stop loss / take profit are on the wrong side of entry for a {alert.direction}")

    def _check_deviation(self, alert: Alert) -> None:
        limit = self.cfg.max_entry_deviation_pct
        if limit <= 0:
            return
        mark = self.client.mark_price(self.cfg.symbol)
        deviation = abs(mark - alert.entry_price) / alert.entry_price * 100
        if deviation > Decimal(str(limit)):
            raise Reject("PRICE_DEVIATION", f"Mark price {fmt(mark)} is {deviation:.3f}% from alert entry, limit {limit}%")

    def _check_balance(self, alert: Alert, qty: Decimal, rules: Rules) -> None:
        if not self.cfg.check_balance:
            return
        needed = alert.estimated_margin or (qty * alert.entry_price / alert.leverage)
        available = self.client.available_balance(rules.margin_asset)
        if available < needed:
            raise Reject("INSUFFICIENT_BALANCE", f"Available {fmt(available)} {rules.margin_asset} < required margin {fmt(needed)}")

    # ------------------------------------------------------------- order flow
    def _client_id(self, key: str, suffix: str = "") -> str:
        prefix = self.cfg.order_id_prefix
        return f"{prefix}{key[: 36 - len(prefix) - len(suffix)]}{suffix}"

    def _send_entry(self, symbol: str, side: str, qty: Decimal, client_id: str) -> dict:
        params = {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": fmt(qty),
            "newClientOrderId": client_id, "newOrderRespType": "RESULT",
        }
        try:
            return self.client.new_order(params)
        except (BinanceError, TransportError) as exc:
            if not exc.ambiguous:
                raise Failure("ENTRY_REJECTED", f"Binance rejected the entry order: {exc}") from None
        # Ambiguous (timeout / 5xx): ask the exchange what really happened.
        for _ in range(self.cfg.confirm_retries + 1):
            found = self.client.get_order(symbol, client_id)
            if found:
                return found
            self._sleep(self.cfg.confirm_delay_s)
        if self._position_amt() == ZERO:
            raise Failure("ENTRY_NOT_PLACED", "Entry request outcome was unclear; no order and no position found on Binance")
        return {"status": "UNKNOWN", "executedQty": None, "avgPrice": None, "orderId": None}

    def _confirm_order(self, client_id: str, order: dict) -> dict:
        retries = self.cfg.confirm_retries
        for attempt in range(retries + 1):
            status = order.get("status")
            if status == "FILLED":
                return order
            executed = Decimal(str(order.get("executedQty") or "0"))
            if status in ("CANCELED", "EXPIRED", "REJECTED") and executed == ZERO:
                raise Failure("ENTRY_NOT_FILLED", f"Entry order ended with status {status}")
            if attempt == retries:
                break
            self._sleep(self.cfg.confirm_delay_s)
            fresh = self.client.get_order(self.cfg.symbol, client_id)
            if fresh:
                order = fresh
        if Decimal(str(order.get("executedQty") or "0")) > ZERO:
            return order
        raise Failure("ENTRY_NOT_CONFIRMED", "Could not confirm the entry order as filled")

    def _wait_for_position(self, direction: str) -> Decimal:
        for attempt in range(self.cfg.confirm_retries + 1):
            amount = self._position_amt()
            if amount != ZERO:
                if (amount > ZERO) != (direction == "LONG"):
                    raise Failure("POSITION_DIRECTION_MISMATCH", f"Position on Binance ({fmt(amount)}) does not match {direction}")
                return amount
            if attempt < self.cfg.confirm_retries:
                self._sleep(self.cfg.confirm_delay_s)
        raise Failure("POSITION_NOT_CONFIRMED", "Entry order reported filled but no position was found on Binance")

    def _position_amt(self) -> Decimal:
        total = ZERO
        for row in self.client.position_risk(self.cfg.symbol):
            if row.get("symbol") == self.cfg.symbol:
                total += Decimal(str(row.get("positionAmt", "0")))
        return total

    # -------------------------------------------------- protective SL / TP
    def _place_protection(self, direction: str, sl: Decimal, tp: Decimal, key: str) -> dict:
        exit_side = "SELL" if direction == "LONG" else "BUY"
        sl_order = self._conditional(exit_side, "STOP_MARKET", sl, self._client_id(key, "-sl"))
        tp_order = self._conditional(exit_side, "TAKE_PROFIT_MARKET", tp, self._client_id(key, "-tp"))
        return {"stop_loss": sl_order, "take_profit": tp_order}

    def _conditional(self, side: str, order_type: str, trigger: Decimal, client_id: str) -> dict:
        symbol, mode = self.cfg.symbol, self.cfg.protective_mode

        def via_algo() -> dict:
            resp = self.client.new_algo_order({
                "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
                "triggerPrice": fmt(trigger), "closePosition": "true",
                "workingType": self.cfg.working_type, "clientAlgoId": client_id,
            })
            return {"api": "algo", "client_id": client_id, "id": resp.get("algoId"), "trigger": fmt(trigger)}

        def via_classic() -> dict:
            resp = self.client.new_order({
                "symbol": symbol, "side": side, "type": order_type, "stopPrice": fmt(trigger),
                "closePosition": "true", "workingType": self.cfg.working_type, "newClientOrderId": client_id,
            })
            return {"api": "classic", "client_id": client_id, "id": resp.get("orderId"), "trigger": fmt(trigger)}

        if mode == "algo":
            return via_algo()
        if mode == "classic":
            return via_classic()
        try:  # auto: algo endpoint first, classic only if the endpoint does not exist
            return via_algo()
        except BinanceError as exc:
            if exc.http_status in (404, 405):
                return via_classic()
            raise

    def _verify_protection(self, protection: dict) -> None:
        wanted = {p["client_id"]: p["api"] for p in protection.values()}
        found = set()
        if "algo" in wanted.values():
            for row in self.client.open_algo_orders(self.cfg.symbol):
                found.add(row.get("clientAlgoId"))
        if "classic" in wanted.values():
            for row in self.client.open_orders(self.cfg.symbol):
                found.add(row.get("clientOrderId"))
        missing = [cid for cid in wanted if cid not in found]
        if missing:
            raise Failure("PROTECTION_NOT_FOUND", "Protective order(s) not found on Binance after placement")

    def _protection_failed(self, exc: Exception, details: dict) -> dict:
        reason = f"{type(exc).__name__}: {exc}"
        log.critical("Position opened but protection failed (%s)", reason)
        details["protection_error"] = reason
        if not self.cfg.flatten_on_protection_failure:
            return {"status": "FAILED", "code": "PROTECTION_FAILED_POSITION_OPEN",
                    "message": "Position is OPEN WITHOUT full SL/TP protection. Manual action required.", "details": details}
        try:
            self._flatten()
        except Exception as flat_exc:  # noqa: BLE001
            log.critical("FLATTEN FAILED (%s)", type(flat_exc).__name__)
            details["flatten_error"] = f"{type(flat_exc).__name__}: {flat_exc}"
            return {"status": "FAILED", "code": "PROTECTION_FAILED_NOT_FLATTENED",
                    "message": "SL/TP failed AND the emergency close failed. Position may be OPEN. Manual action required.", "details": details}
        return {"status": "FAILED", "code": "PROTECTION_FAILED_FLATTENED",
                "message": "SL/TP could not be placed; position was closed for safety.", "details": details}

    # ---------------------------------------------------------- close / flatten
    def _close(self, alert: Alert) -> dict:
        if alert.symbol and alert.symbol != self.cfg.symbol:
            raise Reject("SYMBOL_MISMATCH", f"Alert symbol {alert.symbol!r} does not match configured symbol")
        amount = self._position_amt()
        if amount == ZERO:
            self._cancel_all_orders(strict=False)
            return {"status": "NOOP", "code": "ALREADY_FLAT", "message": "No open position; leftover orders cleaned up"}
        held = "LONG" if amount > ZERO else "SHORT"
        if held != alert.direction:
            return {"status": "NOOP", "code": "DIRECTION_MISMATCH",
                    "message": f"Close {alert.direction} ignored: open position is {held}"}
        self._flatten()
        return {"status": "EXECUTED", "code": "CLOSED", "message": f"{held} position closed"}

    def _flatten(self) -> None:
        """Market-close the whole position (reduce-only), confirm flat, then remove leftover orders."""
        amount = self._position_amt()
        if amount != ZERO:
            self.client.new_order({
                "symbol": self.cfg.symbol, "side": "SELL" if amount > ZERO else "BUY", "type": "MARKET",
                "quantity": fmt(abs(amount)), "reduceOnly": "true", "newOrderRespType": "RESULT",
            })
            for attempt in range(self.cfg.confirm_retries + 1):
                if self._position_amt() == ZERO:
                    break
                if attempt < self.cfg.confirm_retries:
                    self._sleep(self.cfg.confirm_delay_s)
            else:
                raise Failure("FLATTEN_NOT_CONFIRMED", "Close order sent but position is still open on Binance")
        self._cancel_all_orders(strict=False)

    def _cancel_all_orders(self, strict: bool) -> None:
        """Remove open regular and conditional orders for the symbol."""
        symbol = self.cfg.symbol
        steps = [lambda: self.client.cancel_all_open_orders(symbol)]
        if self.cfg.protective_mode != "classic":
            steps.append(lambda: self.client.cancel_all_algo_orders(symbol))
        for step in steps:
            try:
                step()
            except BinanceError as exc:
                if exc.http_status == 404:
                    continue  # endpoint not available on this environment
                if strict:
                    raise Reject("STALE_ORDER_CLEANUP_FAILED", f"Could not clear old orders: {exc}") from None
                log.warning("Order cleanup failed: %s", exc)
            except TransportError as exc:
                if strict:
                    raise Reject("STALE_ORDER_CLEANUP_FAILED", f"Could not clear old orders: {exc}") from None
                log.warning("Order cleanup failed: %s", exc)
