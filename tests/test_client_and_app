"""Client signing/redaction tests against a local mock server, plus Flask app smoke tests."""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import binance_client  # noqa: E402
from binance_client import BinanceClient, BinanceError, TransportError  # noqa: E402

SECRET = "test-secret-value-123"


class MockBinance(BaseHTTPRequestHandler):
    seen = []
    script = []          # list of (status, json) to return in order; empty -> default success

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        query = urlparse(self.path).query
        MockBinance.seen.append((self.command, urlparse(self.path).path, body or query, dict(self.headers)))
        if urlparse(self.path).path == "/fapi/v1/time":
            status, payload = 200, {"serverTime": int(time.time() * 1000)}
        elif MockBinance.script:
            status, payload = MockBinance.script.pop(0)
        else:
            status, payload = 200, {"orderId": 1}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _handle


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockBinance)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.cfg = SimpleNamespace(api_key="KEY123", api_secret=SECRET, base_url=f"http://127.0.0.1:{cls.server.server_port}",
                                  recv_window_ms=5000, http_timeout=2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        MockBinance.seen.clear()
        MockBinance.script.clear()

    def test_post_is_signed_correctly_and_key_header_sent(self):
        BinanceClient(self.cfg).new_order({"symbol": "XAUUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"})
        method, path, body, headers = MockBinance.seen[-1]
        self.assertEqual((method, path), ("POST", "/fapi/v1/order"))
        pairs = parse_qsl(body)
        signature = dict(pairs)["signature"]
        unsigned = "&".join(f"{k}={v}" for k, v in pairs if k != "signature")
        expected = hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(headers.get("X-MBX-APIKEY"), "KEY123")
        self.assertIn("timestamp", dict(pairs))
        self.assertIn("recvWindow", dict(pairs))

    def test_binance_error_mapped(self):
        MockBinance.script.append((400, {"code": -2021, "msg": "Order would immediately trigger."}))
        with self.assertRaises(BinanceError) as ctx:
            BinanceClient(self.cfg).new_order({"symbol": "X"})
        self.assertEqual(ctx.exception.code, -2021)
        self.assertFalse(ctx.exception.ambiguous)

    def test_5xx_is_ambiguous(self):
        MockBinance.script.append((503, {"code": -1007, "msg": "Timeout waiting for response"}))
        with self.assertRaises(BinanceError) as ctx:
            BinanceClient(self.cfg).new_order({"symbol": "X"})
        self.assertTrue(ctx.exception.ambiguous)

    def test_timestamp_error_resyncs_and_retries_once(self):
        MockBinance.script.append((400, {"code": -1021, "msg": "Timestamp outside of recvWindow."}))
        result = BinanceClient(self.cfg).new_order({"symbol": "X"})
        self.assertEqual(result, {"orderId": 1})
        paths = [s[1] for s in MockBinance.seen]
        self.assertEqual(paths, ["/fapi/v1/order", "/fapi/v1/time", "/fapi/v1/order"])

    def test_margin_type_no_change_is_ok(self):
        MockBinance.script.append((400, {"code": -4046, "msg": "No need to change margin type."}))
        BinanceClient(self.cfg).set_margin_type("XAUUSDT", "ISOLATED")

    def test_get_order_not_found_returns_none(self):
        MockBinance.script.append((400, {"code": -2013, "msg": "Order does not exist."}))
        self.assertIsNone(BinanceClient(self.cfg).get_order("XAUUSDT", "abc"))

    def test_transport_error_never_leaks_signature_or_url(self):
        cfg = SimpleNamespace(**{**self.cfg.__dict__, "base_url": "http://127.0.0.1:9"})
        with self.assertRaises(TransportError) as ctx:
            BinanceClient(cfg).new_order({"symbol": "X"})
        text = str(ctx.exception) + repr(ctx.exception.__cause__)
        self.assertNotIn("signature", text.lower())
        self.assertNotIn("timestamp", text.lower())


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_bridge import FakeClient, ConfigTests
        cls.tmp = tempfile.TemporaryDirectory()
        env = dict(ConfigTests.BASE, DB_PATH=os.path.join(cls.tmp.name, "bridge.db"), WEBHOOK_SECRET="hook-secret-xyz-1234567",
                   ORDER_CONFIRM_DELAY_MS="0")
        os.environ.update(env)
        cls.fake = FakeClient()
        binance_client.BinanceClient = lambda cfg: cls.fake    # app imports the name from the module below
        import importlib
        import app as app_module
        importlib.reload(app_module)
        cls.app_module = app_module
        cls.http = app_module.app.test_client()

    def _wait_idle(self):
        self.app_module.jobs.join()

    def test_flow(self):
        from test_bridge import PINE_LONG
        http = self.http
        self.assertEqual(http.get("/health").status_code, 200)

        self.assertEqual(http.post("/webhook", data=PINE_LONG, content_type="text/plain").status_code, 401)
        self.assertEqual(http.post("/webhook?secret=wrong", data=PINE_LONG, content_type="text/plain").status_code, 401)
        self.assertEqual(http.post("/webhook?secret=hook-secret-xyz-1234567", data="nonsense", content_type="text/plain").status_code, 400)

        r = http.post("/webhook?secret=hook-secret-xyz-1234567", data=PINE_LONG.encode("utf-8"), content_type="text/plain; charset=utf-8")
        self.assertEqual(r.status_code, 202, r.get_data(as_text=True))
        self._wait_idle()

        r = http.post("/webhook?secret=hook-secret-xyz-1234567", data=PINE_LONG.encode("utf-8"), content_type="text/plain")
        self.assertEqual((r.status_code, r.get_json()["status"]), (200, "duplicate"))
        self._wait_idle()

        st = http.get("/status?secret=hook-secret-xyz-1234567").get_json()
        self.assertEqual(http.get("/status").status_code, 401)
        self.assertEqual(st["recent"][0]["status"], "EXECUTED")
        self.assertEqual(self.fake.position, 1)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "order" and c[1] == "MARKET"]), 1)

        r = http.post("/webhook", data="/close_long", content_type="text/plain", headers={"X-Webhook-Secret": "hook-secret-xyz-1234567"})
        self.assertEqual(r.status_code, 202)
        self._wait_idle()
        self.assertEqual(self.fake.position, 0)
        st = http.get("/status?secret=hook-secret-xyz-1234567").get_json()
        self.assertEqual(st["recent"][0]["result"]["code"], "CLOSED")

    def test_redactor_masks_secrets(self):
        import logging
        rec = logging.LogRecord("x", logging.INFO, "f", 1, "GET /w?secret=hook-secret-xyz-1234567&a=1 signature=" + "a" * 64 + " key=KEY", (), None)
        self.app_module._redactor.filter(rec)
        self.assertNotIn("hook-secret-xyz-1234567", rec.msg)
        self.assertNotIn("a" * 64, rec.msg)


if __name__ == "__main__":
    unittest.main()
