"""Offline tests: no network, no Binance. Run with:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alert_parser import AlertError, CLOSE, ENTRY, parse_alert  # noqa: E402
from binance_client import BinanceError, TransportError  # noqa: E402
import config as config_module  # noqa: E402
from executor import Executor  # noqa: E402
from store import Store  # noqa: E402

PINE_LONG = """🟢 LONG ENTRY
━━━━━━━━
Symbol: XAUUSDT.P
Exchange: BINANCE FUTURES
Timeframe: 1M
Side: LONG

Leverage: Isolated 20x
Quantity: 1

Entry price: 4000.5
Take Profit: 4030.5
Stop Loss: 3980.5

SL distance: 20 USD
Estimated notional: 4000.5 USDT
Estimated margin: 200.03 USDT
SL method: Opposite Stop
Signal time: 2026-11-22 10:15:00"""

PINE_SHORT = PINE_LONG.replace("🟢 LONG", "🔴 SHORT").replace("Side: LONG", "Side: SHORT") \
    .replace("Entry price: 4000.5", "Entry price: 4000.5").replace("Take Profit: 4030.5", "Take Profit: 3970.5") \
    .replace("Stop Loss: 3980.5", "Stop Loss: 4020.5").replace("Isolated 20x", "Cross 10x")


class Cfg:
    symbol = "XAUUSDT"
    base_url = "https://demo-fapi.binance.com"
    order_id_prefix = "tv"
    protective_mode = "auto"
    working_type = "CONTRACT_PRICE"
    flatten_on_protection_failure = True
    cancel_stale_orders = True
    check_balance = True
    max_entry_deviation_pct = 0.0
    confirm_retries = 2
    confirm_delay_s = 0.0
    exchange_info_ttl = 300


class FakeClient:
    """In-memory stand-in for BinanceClient."""

    def __init__(self):
        self.position = Decimal(0)
        self.orders = {}
        self.algo = []
        self.calls = []
        self.hedge = False
        self.balance = Decimal(10000)
        self.fail_algo = None          # exception to raise when placing algo orders
        self.new_order_error = None    # exception for the first entry order
        self.leverage_applied = None
        self.symbols = [{
            "symbol": "XAUUSDT", "status": "TRADING", "marginAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]

    def sync_time(self): pass
    def exchange_info(self): return {"symbols": self.symbols}
    def mark_price(self, symbol): return Decimal("4000.5")
    def position_mode_is_hedge(self): return self.hedge
    def available_balance(self, asset): return self.balance
    def set_margin_type(self, symbol, mt): self.calls.append(("margin", mt))

    def set_leverage(self, symbol, lev):
        self.calls.append(("leverage", lev))
        return self.leverage_applied or lev

    def position_risk(self, symbol):
        return [{"symbol": symbol, "positionAmt": str(self.position), "positionSide": "BOTH"}]

    def new_order(self, p):
        self.calls.append(("order", p.get("type"), p.get("side"), p.get("quantity"), p.get("reduceOnly")))
        if p["type"] == "MARKET":
            if self.new_order_error and not p.get("reduceOnly"):
                err, self.new_order_error = self.new_order_error, None
                if getattr(err, "fill", False):
                    self.position += Decimal(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
                    self.orders[p["newClientOrderId"]] = {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": "4000.6", "orderId": 1}
                raise err
            qty = Decimal(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
            self.position = Decimal(0) if p.get("reduceOnly") else self.position + qty
            order = {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": "4000.6", "orderId": 7}
            if "newClientOrderId" in p:
                self.orders[p["newClientOrderId"]] = order
            return order
        return {"orderId": 9}

    def get_order(self, symbol, cid): return self.orders.get(cid)
    def open_orders(self, symbol): return []
    def cancel_all_open_orders(self, symbol): self.calls.append(("cancel_all",))

    def new_algo_order(self, p):
        if self.fail_algo:
            raise self.fail_algo
        self.calls.append(("algo", p["type"], p["side"], p["triggerPrice"], p["closePosition"]))
        self.algo.append({"clientAlgoId": p["clientAlgoId"], "algoStatus": "NEW"})
        return {"algoId": len(self.algo)}

    def open_algo_orders(self, symbol): return list(self.algo)
    def cancel_all_algo_orders(self, symbol): self.calls.append(("cancel_algo",)); self.algo = []


def make(alert_text):
    alert, _ = parse_alert(alert_text, None)
    return alert, alert.dedupe_key()


class ParserTests(unittest.TestCase):
    def test_long_entry(self):
        a, _ = parse_alert(PINE_LONG, None)
        self.assertEqual((a.event, a.direction, a.symbol), (ENTRY, "LONG", "XAUUSDT"))
        self.assertEqual((a.quantity, a.entry_price, a.take_profit, a.stop_loss),
                         (Decimal("1"), Decimal("4000.5"), Decimal("4030.5"), Decimal("3980.5")))
        self.assertEqual((a.leverage, a.margin_mode), (20, "ISOLATED"))
        self.assertEqual(a.estimated_margin, Decimal("200.03"))
        self.assertEqual(a.signal_time, "2026-11-22 10:15:00")

    def test_short_cross(self):
        a, _ = parse_alert(PINE_SHORT, None)
        self.assertEqual((a.direction, a.leverage, a.margin_mode), ("SHORT", 10, "CROSSED"))

    def test_close_commands(self):
        for text, side in (("/close_long", "LONG"), ("/close_short", "SHORT"), (" /CLOSE_LONG\n", "LONG")):
            a, _ = parse_alert(text, None)
            self.assertEqual((a.event, a.direction), (CLOSE, side))

    def test_na_rejected(self):
        with self.assertRaises(AlertError):
            parse_alert(PINE_LONG.replace("Stop Loss: 3980.5", "Stop Loss: n/a"), None)

    def test_garbage_rejected(self):
        for bad in ("hello", "", "   "):
            with self.assertRaises(AlertError):
                parse_alert(bad, None)

    def test_json_entry_and_secret(self):
        body = {"secret": "s", "event": "ENTRY", "symbol": "BINANCE:XAUUSDT.P", "direction": "SHORT", "quantity": 2,
                "entry_price": 4000, "stop_loss": 4020, "take_profit": 3970, "leverage": 5, "margin_mode": "ISOLATED",
                "signal_id": "abc"}
        a, secret = parse_alert("{}", body)
        self.assertEqual((a.symbol, a.direction, a.leverage, secret, a.signal_id), ("XAUUSDT", "SHORT", 5, "s", "abc"))

    def test_json_force_close(self):
        a, _ = parse_alert("{}", {"event": "ENTRY", "direction": "LONG", "force_close": True})
        self.assertEqual(a.event, CLOSE)

    def test_dedupe_key_stable_and_distinct(self):
        a1, _ = parse_alert(PINE_LONG, None)
        a2, _ = parse_alert(PINE_LONG, None)
        a3, _ = parse_alert(PINE_LONG.replace("10:15:00", "10:16:00"), None)
        self.assertEqual(a1.dedupe_key(), a2.dedupe_key())
        self.assertNotEqual(a1.dedupe_key(), a3.dedupe_key())


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.ex = Executor(Cfg(), self.client, sleep=lambda s: None)

    def test_happy_long(self):
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual((r["status"], r["code"]), ("EXECUTED", "OPENED"))
        self.assertEqual(self.client.position, Decimal(1))
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds[:5], ["cancel_all", "cancel_algo", "margin", "leverage", "order"])
        self.assertIn(("margin", "ISOLATED"), self.client.calls)
        self.assertIn(("leverage", 20), self.client.calls)
        self.assertIn(("algo", "STOP_MARKET", "SELL", "3980.5", "true"), self.client.calls)
        self.assertIn(("algo", "TAKE_PROFIT_MARKET", "SELL", "4030.5", "true"), self.client.calls)

    def test_happy_short_uses_buy_exits(self):
        a, k = make(PINE_SHORT)
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "OPENED")
        self.assertIn(("margin", "CROSSED"), self.client.calls)
        self.assertIn(("algo", "STOP_MARKET", "BUY", "4020.5", "true"), self.client.calls)

    def test_position_open_rejected_and_no_orders(self):
        self.client.position = Decimal(1)
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual((r["status"], r["code"]), ("REJECTED", "POSITION_OPEN"))
        self.assertFalse([c for c in self.client.calls if c[0] == "order"])

    def test_duplicate_on_exchange(self):
        a, k = make(PINE_LONG)
        self.client.orders[self.ex._client_id(k)] = {"status": "FILLED"}
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "DUPLICATE_ON_EXCHANGE")

    def test_symbol_mismatch(self):
        a, k = make(PINE_LONG.replace("XAUUSDT.P", "BTCUSDT.P"))
        self.assertEqual(self.ex.process(k, a)["code"], "SYMBOL_MISMATCH")

    def test_symbol_not_supported_fails_safely(self):
        self.client.symbols = []
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "SYMBOL_NOT_SUPPORTED")
        self.assertFalse([c for c in self.client.calls if c[0] == "order"])

    def test_bad_step_rejected(self):
        a, k = make(PINE_LONG.replace("Quantity: 1", "Quantity: 1.0005"))
        self.assertEqual(self.ex.process(k, a)["code"], "INVALID_QUANTITY")

    def test_wrong_side_levels_rejected(self):
        a, k = make(PINE_LONG.replace("Stop Loss: 3980.5", "Stop Loss: 4100"))
        self.assertEqual(self.ex.process(k, a)["code"], "INVALID_LEVELS")

    def test_hedge_mode_rejected(self):
        self.client.hedge = True
        a, k = make(PINE_LONG)
        self.assertEqual(self.ex.process(k, a)["code"], "HEDGE_MODE")

    def test_insufficient_balance(self):
        self.client.balance = Decimal(10)
        a, k = make(PINE_LONG)
        self.assertEqual(self.ex.process(k, a)["code"], "INSUFFICIENT_BALANCE")

    def test_leverage_not_applied(self):
        self.client.leverage_applied = 10
        a, k = make(PINE_LONG)
        self.assertEqual(self.ex.process(k, a)["code"], "LEVERAGE_NOT_APPLIED")
        self.assertFalse([c for c in self.client.calls if c[0] == "order"])

    def test_protection_failure_flattens(self):
        self.client.fail_algo = BinanceError(400, -2021, "Order would immediately trigger.", "/fapi/v1/algoOrder")
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual((r["status"], r["code"]), ("FAILED", "PROTECTION_FAILED_FLATTENED"))
        self.assertEqual(self.client.position, Decimal(0))
        self.assertIn(("order", "MARKET", "SELL", "1", "true"), self.client.calls)

    def test_protection_failure_without_flatten_flag_reports_open(self):
        cfg = Cfg()
        cfg.flatten_on_protection_failure = False
        ex = Executor(cfg, self.client, sleep=lambda s: None)
        self.client.fail_algo = BinanceError(400, -2021, "x", "/fapi/v1/algoOrder")
        a, k = make(PINE_LONG)
        r = ex.process(k, a)
        self.assertEqual(r["code"], "PROTECTION_FAILED_POSITION_OPEN")
        self.assertEqual(self.client.position, Decimal(1))

    def test_auto_mode_falls_back_to_classic_only_on_404(self):
        self.client.fail_algo = BinanceError(404, None, "Not Found", "/fapi/v1/algoOrder")
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        # classic orders are accepted by the fake but never listed as open -> verification fails -> flatten
        self.assertEqual(r["code"], "PROTECTION_FAILED_FLATTENED")
        types = [c[1] for c in self.client.calls if c[0] == "order"]
        self.assertIn("STOP_MARKET", types)

    def test_timeout_but_order_filled_continues_and_protects(self):
        err = TransportError("timeout")
        err.fill = True
        self.client.new_order_error = err
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "OPENED")
        self.assertEqual(self.client.position, Decimal(1))

    def test_timeout_and_nothing_placed(self):
        self.client.new_order_error = TransportError("timeout")
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "ENTRY_NOT_PLACED")

    def test_rejected_entry_is_not_ambiguous(self):
        self.client.new_order_error = BinanceError(400, -4411, "TradFi-Perps agreement not signed", "/fapi/v1/order")
        a, k = make(PINE_LONG)
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "ENTRY_REJECTED")
        self.assertIn("-4411", r["message"])

    def test_close_long(self):
        self.client.position = Decimal(1)
        a, k = make("/close_long")
        r = self.ex.process(k, a)
        self.assertEqual((r["status"], r["code"]), ("EXECUTED", "CLOSED"))
        self.assertEqual(self.client.position, Decimal(0))

    def test_close_wrong_direction_is_noop(self):
        self.client.position = Decimal(-1)
        a, k = make("/close_long")
        r = self.ex.process(k, a)
        self.assertEqual(r["code"], "DIRECTION_MISMATCH")
        self.assertEqual(self.client.position, Decimal(-1))

    def test_close_when_flat_is_noop(self):
        a, k = make("/close_short")
        self.assertEqual(self.ex.process(k, a)["code"], "ALREADY_FLAT")


class StoreTests(unittest.TestCase):
    def test_duplicate_claim_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "bridge.db")
            s = Store(path)
            self.assertIsNone(s.claim("k1", "ENTRY", {"a": 1}))
            dup = s.claim("k1", "ENTRY", {"a": 1})
            self.assertEqual(dup["status"], "RECEIVED")
            s.update("k1", "PROCESSING")
            s2 = Store(path)                       # simulated restart
            self.assertEqual(s2.mark_interrupted(), 1)
            self.assertEqual(s2.claim("k1", "ENTRY", {})["status"], "INTERRUPTED")
            self.assertEqual(s2.recent(5)[0]["key"], "k1")


class ConfigTests(unittest.TestCase):
    BASE = {
        "BINANCE_API_KEY": "demo-key-1234567890", "BINANCE_API_SECRET": "demo-secret-1234567890", "BINANCE_BASE_URL": "https://demo-fapi.binance.com",
        "WEBHOOK_SECRET": "webhook-secret-1234567890", "TRADE_SYMBOL": "xauusdt", "DB_PATH": "/tmp/x.db", "RECV_WINDOW_MS": "5000",
        "HTTP_TIMEOUT_SECONDS": "10", "ORDER_ID_PREFIX": "tv", "PROTECTIVE_ORDER_MODE": "auto",
        "TRIGGER_WORKING_TYPE": "CONTRACT_PRICE", "FLATTEN_ON_PROTECTION_FAILURE": "true",
        "CANCEL_STALE_ORDERS_BEFORE_ENTRY": "true", "CHECK_AVAILABLE_BALANCE": "true",
        "MAX_ENTRY_DEVIATION_PCT": "0", "ORDER_CONFIRM_RETRIES": "5", "ORDER_CONFIRM_DELAY_MS": "400",
        "EXCHANGE_INFO_TTL_SECONDS": "300", "STATUS_HISTORY_LIMIT": "20", "LOG_LEVEL": "INFO",
    }

    def _load(self, **override):
        env = dict(self.BASE, **override)
        old = dict(os.environ)
        try:
            for k in list(self.BASE):
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in env.items() if v is not None})
            return config_module.load()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_valid(self):
        cfg = self._load()
        self.assertEqual(cfg.symbol, "XAUUSDT")

    def test_mainnet_blocked(self):
        for url in ("https://fapi.binance.com", "http://demo-fapi.binance.com", "https://demo-fapi.evil.com"):
            with self.assertRaises(config_module.ConfigError):
                self._load(BINANCE_BASE_URL=url)

    def test_short_webhook_secret_rejected(self):
        with self.assertRaises(config_module.ConfigError):
            self._load(WEBHOOK_SECRET="short")

    def test_missing_var_fails(self):
        with self.assertRaises(config_module.ConfigError):
            self._load(WEBHOOK_SECRET=None)


if __name__ == "__main__":
    unittest.main()
