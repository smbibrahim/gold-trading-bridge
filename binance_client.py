"""Minimal signed REST client for Binance USD-M Futures (Demo endpoint only).

Security notes
  * Request URLs/params are never logged and never put into exception text,
    because they contain the signature.
  * Transport errors are re-raised without the original exception, whose message
    would include the full URL (and therefore the signature).
"""
import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger("binance")

CODE_ORDER_NOT_FOUND = -2013
CODE_TIMESTAMP = -1021
CODE_NO_NEED_TO_CHANGE_MARGIN = -4046


class BinanceError(Exception):
    """Binance answered with an error."""

    def __init__(self, http_status: int, code, msg: str, path: str):
        super().__init__(f"{path}: HTTP {http_status}, code {code}, {msg}")
        self.http_status = http_status
        self.code = code
        self.msg = msg
        self.path = path

    @property
    def ambiguous(self) -> bool:
        """True when the order may or may not have been accepted."""
        return self.http_status >= 500 or self.code in (-1006, -1007)


class TransportError(Exception):
    """Network failure: the request may or may not have reached Binance."""

    ambiguous = True


class BinanceClient:
    def __init__(self, cfg):
        self._cfg = cfg
        self._secret = cfg.api_secret.encode("utf-8")
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": cfg.api_key})
        self._offset_ms = 0

    # ------------------------------------------------------------------ core
    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._offset_ms

    def sync_time(self) -> None:
        data = self._request("GET", "/fapi/v1/time", signed=False)
        self._offset_ms = int(data["serverTime"]) - int(time.time() * 1000)

    def _request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = True, _retry: bool = True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["recvWindow"] = self._cfg.recv_window_ms
            params["timestamp"] = self._timestamp()
            query = urlencode(params)
            signature = hmac.new(self._secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
        else:
            query = urlencode(params)

        url = self._cfg.base_url + path
        try:
            if method in ("GET", "DELETE"):
                resp = self._session.request(
                    method, f"{url}?{query}" if query else url, timeout=self._cfg.http_timeout
                )
            else:
                resp = self._session.request(
                    method, url, data=query, timeout=self._cfg.http_timeout,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except requests.RequestException as exc:
            raise TransportError(f"{type(exc).__name__} calling {method} {path}") from None

        try:
            data = resp.json()
        except ValueError:
            data = None

        code = data.get("code") if isinstance(data, dict) else None
        if resp.status_code >= 400 or (isinstance(code, int) and code < 0):
            msg = data.get("msg") if isinstance(data, dict) else None
            err = BinanceError(resp.status_code, code, msg or "non-JSON or empty error response", path)
            if signed and _retry and err.code == CODE_TIMESTAMP:
                self.sync_time()
                return self._request(method, path, params, signed, _retry=False)
            raise err
        return data

    # ------------------------------------------------------------ market data
    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

    def mark_price(self, symbol: str) -> Decimal:
        data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
        return Decimal(str(data["markPrice"]))

    # ---------------------------------------------------------------- account
    def position_mode_is_hedge(self) -> bool:
        data = self._request("GET", "/fapi/v1/positionSide/dual")
        return str(data.get("dualSidePosition")).lower() == "true"

    def position_risk(self, symbol: str) -> list:
        try:
            return self._request("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
        except BinanceError as exc:
            if exc.http_status != 404:
                raise
            return self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})

    def available_balance(self, asset: str) -> Decimal:
        try:
            rows = self._request("GET", "/fapi/v3/balance")
        except BinanceError as exc:
            if exc.http_status != 404:
                raise
            rows = self._request("GET", "/fapi/v2/balance")
        for row in rows:
            if row.get("asset") == asset:
                return Decimal(str(row.get("availableBalance", "0")))
        return Decimal(0)

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except BinanceError as exc:
            if exc.code != CODE_NO_NEED_TO_CHANGE_MARGIN:
                raise

    def set_leverage(self, symbol: str, leverage: int) -> int:
        data = self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
        return int(data["leverage"])

    # ----------------------------------------------------------------- orders
    def new_order(self, params: dict) -> dict:
        return self._request("POST", "/fapi/v1/order", params)

    def get_order(self, symbol: str, client_id: str) -> Optional[dict]:
        try:
            return self._request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        except BinanceError as exc:
            if exc.code == CODE_ORDER_NOT_FOUND:
                return None
            raise

    def open_orders(self, symbol: str) -> list:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol})

    def cancel_all_open_orders(self, symbol: str) -> None:
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    # ------------------------------------------- conditional (algo) orders
    def new_algo_order(self, params: dict) -> dict:
        return self._request("POST", "/fapi/v1/algoOrder", params)

    def open_algo_orders(self, symbol: str) -> list:
        return self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})

    def cancel_all_algo_orders(self, symbol: str) -> None:
        self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
