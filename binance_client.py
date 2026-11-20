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
    BASE_URL = "https://demo-fapi.binance.com"

    def __init__(self, account_number=1):
        self.account_number = int(account_number)
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
        query = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        method = method.upper()
        if method == "GET":
            response = self.session.request(method, self.BASE_URL + endpoint, params=params, timeout=15)
        else:
            response = self.session.request(method, self.BASE_URL + endpoint, data=params, timeout=15)

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

    def get_exchange_info(self):
        response = self.session.get(self.BASE_URL + "/fapi/v1/exchangeInfo", timeout=15)
        response.raise_for_status()
        return response.json()

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

    # Order-placement and close methods are intentionally not exposed in this release.
    # This revision is diagnostic/validation-only and contains no trade execution path.
