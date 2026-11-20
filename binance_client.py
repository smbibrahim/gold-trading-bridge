import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode


class BinanceAPIError(RuntimeError):
    def __init__(self, account_number, method, endpoint, status_code, payload, request_params):
        self.account_number = account_number
        self.method = method
        self.endpoint = endpoint
        self.status_code = status_code
        self.payload = payload
        self.request_params = request_params
        message = payload.get("msg") if isinstance(payload, dict) else str(payload)
        code = payload.get("code") if isinstance(payload, dict) else None
        super().__init__(
            f"Binance Account {account_number} API error | HTTP {status_code} | "
            f"code={code} | message={message} | endpoint={endpoint} | params={request_params}"
        )


class BinanceClient:
    # HARD-LOCKED TO BINANCE USD-M DEMO FUTURES
    BASE_URL = "https://demo-fapi.binance.com"

    def __init__(self, account_number=1):
        self.account_number = int(account_number)
        if self.account_number not in (1, 2):
            raise ValueError("Only Account 1 or Account 2 is supported")
        self.api_key = os.getenv(f"BINANCE_API_KEY_{self.account_number}")
        self.api_secret = os.getenv(f"BINANCE_API_SECRET_{self.account_number}")
        if not self.api_key or not self.api_secret:
            raise ValueError(f"Binance Account {self.account_number} credentials missing")
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})

    def _signed_request(self, method, endpoint, params=None):
        params = dict(params or {})
        safe_params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        method = method.upper()
        url = self.BASE_URL + endpoint
        if method in ("GET", "DELETE"):
            response = self.session.request(method, url, params=params, timeout=15)
        else:
            response = self.session.request(method, url, data=params, timeout=15)

        if not response.ok:
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_response": response.text[:2000]}
            raise BinanceAPIError(self.account_number, method, endpoint, response.status_code, payload, safe_params)
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"Account {self.account_number}: JSON parsing failed at {endpoint}: {exc}")

    # EXPLICIT USER-TRIGGERED DEMO AGREEMENT SIGNING
    def sign_tradfi_perps_agreement(self):
        # Binance documents POST /fapi/v1/stock/contract for signing the
        # TradFi-Perps agreement. This client remains locked to Demo base URL.
        return self._signed_request("POST", "/fapi/v1/stock/contract")

    # READ-ONLY / DIAGNOSTIC
    def test_connection(self):
        result = self.get_account()
        return {
            "status": "connected",
            "account": f"Account {self.account_number}",
            "can_trade": result.get("canTrade"),
            "available_balance": result.get("availableBalance"),
        }

    def get_account(self):
        return self._signed_request("GET", "/fapi/v2/account")

    _EXCHANGE_INFO_CACHE = {"ts": 0.0, "data": None}

    def get_exchange_info(self):
        cache = BinanceClient._EXCHANGE_INFO_CACHE
        if cache["data"] is not None and time.time() - cache["ts"] < 3600:
            return cache["data"]
        response = self.session.get(self.BASE_URL + "/fapi/v1/exchangeInfo", timeout=15)
        response.raise_for_status()
        cache["data"], cache["ts"] = response.json(), time.time()
        return cache["data"]

    def get_position_mode(self):
        return self._signed_request("GET", "/fapi/v1/positionSide/dual")

    def get_position_risk(self, symbol=None):
        return self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol} if symbol else {})

    def get_leverage_brackets(self, symbol=None):
        return self._signed_request("GET", "/fapi/v1/leverageBracket", {"symbol": symbol} if symbol else {})

    def get_open_position(self, symbol):
        positions = self.get_position_risk(symbol)
        if isinstance(positions, list):
            for position in positions:
                if position.get("symbol") == symbol and abs(float(position.get("positionAmt", 0))) > 0:
                    return position
        return None

    def get_mark_price(self, symbol):
        response = self.session.get(self.BASE_URL + "/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=15)
        response.raise_for_status()
        return response.json()

    # EXECUTION — DEMO ONLY (base URL is hard-locked above)
    def set_margin_type(self, symbol, margin_type):
        margin_type = str(margin_type).upper()
        if margin_type not in ("ISOLATED", "CROSSED", "CROSS"):
            raise ValueError("Unsupported margin type")
        api_value = "CROSSED" if margin_type in ("CROSS", "CROSSED") else "ISOLATED"
        try:
            return self._signed_request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": api_value})
        except BinanceAPIError as exc:
            # Binance -4046: margin type already set to requested type.
            if "-4046" in str(exc) or "No need to change margin type" in str(exc):
                return {"status": "already_set", "marginType": api_value}
            raise

    def set_leverage(self, symbol, leverage):
        return self._signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    def market_entry(self, symbol, side, quantity, client_order_id=None):
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": str(quantity),
            "newOrderRespType": "RESULT",
        }
        if client_order_id:
            params["newClientOrderId"] = str(client_order_id)[:36]
        return self._signed_request("POST", "/fapi/v1/order", params)

    def query_order(self, symbol, order_id=None, client_order_id=None):
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return self._signed_request("GET", "/fapi/v1/order", params)

    def place_close_all_algo(self, symbol, closing_side, order_type, trigger_price, working_type="CONTRACT_PRICE"):
        if order_type not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise ValueError("Only STOP_MARKET and TAKE_PROFIT_MARKET are supported")
        return self._signed_request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": closing_side.upper(),
            "type": order_type,
            "triggerPrice": str(trigger_price),
            "closePosition": "true",
            "workingType": working_type,
            "priceProtect": "false",
        })

    def get_open_algo_orders(self, symbol):
        return self._signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})

    def query_algo_order(self, symbol, algo_id):
        return self._signed_request("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def cancel_algo_order(self, symbol, algo_id):
        return self._signed_request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})

    def cancel_all_open_algo_orders(self, symbol):
        return self._signed_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def close_position_market(self, symbol):
        position = self.get_open_position(symbol)
        if not position:
            return {"status": "already_flat", "confirmed_flat": True}
        amount = float(position.get("positionAmt", 0))
        side = "SELL" if amount > 0 else "BUY"
        quantity = abs(amount)
        result = self._signed_request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": str(quantity),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
        })
        # Confirm position state after close request.
        time.sleep(0.5)
        confirm = self.get_open_position(symbol)
        return {
            "close_order": result,
            "confirmed_flat": confirm is None,
            "remaining_position": confirm,
        }
