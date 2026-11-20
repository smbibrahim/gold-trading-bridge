
import re
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from binance_client import BinanceClient

app = FastAPI(title="Gold Trading Bridge")

BINANCE_DEMO_URL = "https://demo-fapi.binance.com"


@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "Gold Trading Bridge",
        "mode": "binance-demo-testing"
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/diagnostic")
async def diagnostic():
    endpoints = {
        "ping": "/fapi/v1/ping",
        "time": "/fapi/v1/time"
    }

    results = {}

    for name, endpoint in endpoints.items():
        try:
            response = requests.get(
                BINANCE_DEMO_URL + endpoint,
                timeout=15
            )

            results[name] = {
                "http_status": response.status_code,
                "success": response.ok,
                "response": response.text[:500]
            }

        except Exception as e:
            results[name] = {
                "success": False,
                "error": str(e)
            }

    return {
        "service": "Binance Futures Demo Diagnostic",
        "endpoint": BINANCE_DEMO_URL,
        "results": results,
        "execution_enabled": False
    }


@app.get("/test-connections")
async def test_connections():
    results = []

    for account_number in (1, 2):
        try:
            client = BinanceClient(account_number)
            result = client.test_connection()
            results.append(result)

        except Exception as e:
            results.append({
                "account": f"Account {account_number}",
                "status": "error",
                "error": str(e)
            })

    return {
        "accounts": results,
        "execution_enabled": False
    }


@app.get("/account-config")
async def account_config():
    results = []

    for account_number in (1, 2):
        try:
            client = BinanceClient(account_number)

            position_mode = client.get_position_mode()
            position_risk = client.get_position_risk("XAUUSDT")
            leverage_brackets = client.get_leverage_brackets("XAUUSDT")

            # Return limited account configuration information.
            # Do not expose full position details publicly.

            current_leverage = []

            if isinstance(position_risk, list):
                for position in position_risk:
                    if position.get("symbol") == "XAUUSDT":
                        current_leverage.append(
                            position.get("leverage")
                        )

            results.append({
                "account": f"Account {account_number}",
                "status": "success",
                "position_mode": position_mode,
                "current_leverage": current_leverage,
                "leverage_brackets": leverage_brackets
            })

        except Exception as e:
            results.append({
                "account": f"Account {account_number}",
                "status": "error",
                "error": str(e)
            })

    return {
        "results": results,
        "execution_enabled": False
    }


@app.get("/validate-contract")
async def validate_contract(
    symbol: str = "XAUUSDT",
    quantity: float = 1,
    price: float = 3500
):
    try:
        if quantity <= 0 or price <= 0:
            return {
                "status": "rejected",
                "reason": "Quantity and price must be positive",
                "execution_enabled": False
            }

        response = requests.get(
            BINANCE_DEMO_URL + "/fapi/v1/exchangeInfo",
            timeout=20
        )

        response.raise_for_status()
        exchange_info = response.json()

        contract = next(
            (
                item for item in exchange_info.get("symbols", [])
                if item.get("symbol") == symbol.upper()
            ),
            None
        )

        if not contract:
            return {
                "status": "rejected",
                "symbol": symbol,
                "reason": "Symbol not found",
                "execution_enabled": False
            }

        filters = {
            item["filterType"]: item
            for item in contract.get("filters", [])
        }

        lot = filters.get("LOT_SIZE")
        market_lot = filters.get("MARKET_LOT_SIZE")
        price_filter = filters.get("PRICE_FILTER")
        min_notional_filter = filters.get("MIN_NOTIONAL")

        notional = quantity * price

        def validate_quantity_filter(filter_data):
            if not filter_data:
                return False

            min_qty = float(filter_data["minQty"])
            max_qty = float(filter_data["maxQty"])
            step_size = float(filter_data["stepSize"])

            if quantity < min_qty or quantity > max_qty:
                return False

            if step_size <= 0:
                return False

            step_count = quantity / step_size

            return abs(step_count - round(step_count)) < 1e-8

        lot_size_valid = validate_quantity_filter(lot)
        market_lot_size_valid = validate_quantity_filter(market_lot)

        price_filter_valid = False

        if price_filter:
            min_price = float(price_filter["minPrice"])
            max_price = float(price_filter["maxPrice"])
            tick_size = float(price_filter["tickSize"])

            if (
                min_price <= price <= max_price
                and tick_size > 0
            ):
                tick_count = price / tick_size

                price_filter_valid = (
                    abs(tick_count - round(tick_count)) < 1e-8
                )

        minimum_notional = (
            float(min_notional_filter.get("notional", 0))
            if min_notional_filter
            else 0
        )

        minimum_notional_valid = (
            notional >= minimum_notional
            if min_notional_filter
            else False
        )

        checks = {
            "symbol_trading": contract.get("status") == "TRADING",
            "lot_size_valid": lot_size_valid,
            "market_lot_size_valid": market_lot_size_valid,
            "price_filter_valid": price_filter_valid,
            "minimum_notional_valid": minimum_notional_valid
        }

        return {
            "status": "validated",
            "symbol": contract["symbol"],
            "contract_status": contract["status"],
            "quantity": quantity,
            "price": price,
            "notional": round(notional, 8),
            "filters": {
                "lot_size": lot,
                "market_lot_size": market_lot,
                "price_filter": price_filter,
                "minimum_notional": min_notional_filter
            },
            "checks": checks,
            "all_checks_passed": all(checks.values()),
            "execution_enabled": False
        }

    except Exception as e:
        return {
            "status": "error",
            "detail": str(e),
            "execution_enabled": False
        }


def extract_field(message, label):
    pattern = rf"{re.escape(label)}\s*:\s*([^\n]+)"
    match = re.search(pattern, message, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_signal(message):
    if "LONG ENTRY" in message.upper():
        side = "LONG"
    elif "SHORT ENTRY" in message.upper():
        side = "SHORT"
    else:
        return None, "Entry signal not detected"

    signal = {
        "side": side,
        "symbol": extract_field(message, "Symbol"),
        "exchange": extract_field(message, "Exchange"),
        "timeframe": extract_field(message, "Timeframe"),
        "leverage": extract_field(message, "Leverage"),
        "quantity": extract_field(message, "Quantity"),
        "entry": extract_field(message, "Entry price"),
        "take_profit": extract_field(message, "Take Profit"),
        "stop_loss": extract_field(message, "Stop Loss"),
    }

    required = [
        "symbol",
        "leverage",
        "quantity",
        "entry",
        "take_profit",
        "stop_loss",
    ]

    for field in required:
        if not signal.get(field):
            return None, f"Missing field: {field}"

    try:
        quantity = float(signal["quantity"])
        entry = float(signal["entry"])
        tp = float(signal["take_profit"])
        sl = float(signal["stop_loss"])

        leverage = int(
            "".join(
                c for c in signal["leverage"]
                if c.isdigit()
            )
        )

    except (ValueError, TypeError):
        return None, "Invalid numerical values"

    if quantity <= 0 or entry <= 0 or tp <= 0 or sl <= 0:
        return None, "Invalid numerical values"

    if leverage < 1 or leverage > 125:
        return None, "Invalid leverage"

    if side == "LONG" and not sl < entry < tp:
        return None, "Invalid LONG price relationship"

    if side == "SHORT" and not tp < entry < sl:
        return None, "Invalid SHORT price relationship"

    signal["quantity"] = quantity
    signal["entry"] = entry
    signal["take_profit"] = tp
    signal["stop_loss"] = sl
    signal["leverage"] = leverage

    return signal, None


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        message = body.decode("utf-8").strip()

        print("Incoming signal:", message)

        signal, error = parse_signal(message)

        if error:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "rejected",
                    "reason": error
                }
            )

        return JSONResponse(
            content={
                "status": "signal_validated",
                "signal": signal,
                "execution_enabled": False,
                "message": "Signal validated. No orders placed."
            }
        )

    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "detail": str(e)
            }
        )
