import os
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode


class BinanceClient:
    BASE_URL = "https://demo-fapi.binance.com"

    def __init__(self, account_number=1):
        self.account_number = account_number

        self.api_key = os.getenv(
            f"BINANCE_API_KEY_{account_number}"
        )
        self.api_secret = os.getenv(
            f"BINANCE_API_SECRET_{account_number}"
        )

        self.connected = False

        if not self.api_key or not self.api_secret:
            raise ValueError(
                f"Binance Account {account_number} credentials missing"
            )

        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key
        })

    def _signed_request(self, method, endpoint, params=None):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)

        query = urlencode(params)

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        params["signature"] = signature

        response = self.session.request(
            method,
            self.BASE_URL + endpoint,
            params=params if method == "GET" else None,
            data=params if method != "GET" else None,
            timeout=15
        )

        response.raise_for_status()
        return response.json()

    def test_connection(self):
        try:
            result = self._signed_request(
                "GET",
                "/fapi/v2/account"
            )

            self.connected = True

            return {
                "status": "connected",
                "account": f"Account {self.account_number}",
                "can_trade": result.get("canTrade"),
                "available_balance": result.get(
                    "availableBalance"
                )
            }

        except Exception as e:
            self.connected = False

            return {
                "status": "connection_failed",
                "account": f"Account {self.account_number}",
                "error": str(e)
            }

    def get_exchange_info(self):
        response = self.session.get(
            self.BASE_URL + "/fapi/v1/exchangeInfo",
            timeout=15
        )

        response.raise_for_status()
        return response.json()

    def set_leverage(self, symbol, leverage):
        return self._signed_request(
            "POST",
            "/fapi/v1/leverage",
            {
                "symbol": symbol,
                "leverage": leverage
            }
        )

    def market_order(self, symbol, side, quantity):
        return self._signed_request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
                "newOrderRespType": "RESULT"
            }
        )

    def protective_order(
        self,
        symbol,
        side,
        order_type,
        trigger_price
    ):
        return self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "triggerPrice": trigger_price,
                "closePosition": "true",
                "workingType": "CONTRACT_PRICE"
            }
        )
