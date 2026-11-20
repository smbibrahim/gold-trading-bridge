
import os
import json
import re
import requests

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from binance_client import BinanceClient


app = FastAPI(title="Gold Trading Bridge")


BINANCE_BASE_URL = "https://demo-fapi.binance.com"
EXECUTION_ENABLED = False


# =========================================================
# BASIC ROUTES
# =========================================================

@app.get("/")
def home():
    return {
        "service": "Gold Trading Bridge",
        "status": "running",
        "execution_enabled": EXECUTION_ENABLED
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "execution_enabled": EXECUTION_ENABLED
    }


# =========================================================
# PUBLIC BINANCE DIAGNOSTIC
# =========================================================

@app.get("/diagnostic")
def diagnostic():
    results = {}

    try:
        response = requests.get(
            BINANCE_BASE_URL + "/fapi/v1/ping",
            timeout=15
        )

        results["ping"] = {
            "http_status": response.status_code,
            "success": response.status_code == 200,
            "response": response.text
        }

    except Exception as e:
        results["ping"] = {
            "success": False,
            "error": str(e)
        }

    try:
        response = requests.get(
            BINANCE_BASE_URL + "/fapi/v1/time",
            timeout=15
        )

        results["time"] = {
            "http_status": response.status_code,
            "success": response.status_code == 200,
            "response": response.text
        }

    except Exception as e:
        results["time"] = {
            "success": False,
            "error": str(e)
        }

    return {
        "service": "Binance Futures Demo Diagnostic",
        "endpoint": BINANCE_BASE_URL,
        "results": results,
        "execution_enabled": EXECUTION_ENABLED
    }


# =========================================================
# ACCOUNT CONNECTION TEST
# =========================================================

@app.get("/test-connections")
def test_connections():

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
        "results": results,
        "execution_enabled": EXECUTION_ENABLED
    }


# =========================================================
# ACCOUNT CONFIGURATION
# READ ONLY
# =========================================================

@app.get("/account-config")
def account_config():

    results = []

    for account_number in (1, 2):

        try:

            client = BinanceClient(account_number)

            position_mode = client.get_position_mode()

            position_risk = client.get_position_risk(
                "XAUUSDT"
            )

            leverage_brackets = client.get_leverage_brackets(
                "XAUUSDT"
            )

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
        "execution_enabled": EXECUTION_ENABLED
    }


# =========================================================
# SIGNAL PARSER
# =========================================================

def parse_signal(message):

    try:

        # JSON format support
        if isinstance(message, dict):

            data = message

        else:

            message = str(message).strip()

            try:

                data = json.loads(message)

            except json.JSONDecodeError:

                data = {}

                lines = message.splitlines()

                for line in lines:

                    line = line.strip()

                    if not line:
                        continue

                    if ":" in line:

                        key, value = line.split(
                            ":",
                            1
                        )

                        data[key.strip()] = value.strip()

        if not data:

            return None, "Unable to parse signal"

        # Normalize keys
        normalized = {}

        for key, value in data.items():

            normalized[
                str(key).strip().lower().replace(" ", "_")
            ] = value

        # Direction
        side_value = (
            normalized.get("side")
            or normalized.get("direction")
            or normalized.get("signal")
        )

        if side_value is None:

            return None, "Missing direction"

        side_value = str(side_value).upper()

        if "LONG" in side_value:

            side = "LONG"

        elif "SHORT" in side_value:

            side = "SHORT"

        else:

            return None, "Invalid direction"

        # Symbol
        symbol = (
            normalized.get("symbol")
            or normalized.get("pair")
        )

        if not symbol:

            return None, "Missing symbol"

        symbol = str(symbol).upper().strip()

        # Exchange
        exchange = normalized.get(
            "exchange",
            "BINANCE FUTURES"
        )

        # Timeframe
        timeframe = (
            normalized.get("timeframe")
            or normalized.get("interval")
            or "UNKNOWN"
        )

        # Leverage
        leverage_value = normalized.get("leverage")

        if leverage_value is None:

            return None, "Missing leverage"

        leverage_text = str(leverage_value)

        leverage_match = re.search(
            r"\d+",
            leverage_text
        )

        if not leverage_match:

            return None, "Invalid leverage"

        leverage = int(
            leverage_match.group()
        )

        if leverage < 1 or leverage > 125:

            return None, "Leverage must be between 1 and 125"

        # Quantity
        quantity_value = (
            normalized.get("quantity")
            or normalized.get("qty")
        )

        if quantity_value is None:

            return None, "Missing quantity"

        quantity = float(quantity_value)

        if quantity <= 0:

            return None, "Quantity must be greater than zero"

        # Entry
        entry_value = (
            normalized.get("entry_price")
            or normalized.get("entry")
        )

        if entry_value is None:

            return None, "Missing entry price"

        entry = float(entry_value)

        # TP
        tp_value = (
            normalized.get("take_profit")
            or normalized.get("tp")
            or normalized.get("tp_price")
        )

        if tp_value is None:

            return None, "Missing take profit"

        tp = float(tp_value)

        # SL
        sl_value = (
            normalized.get("stop_loss")
            or normalized.get("sl")
            or normalized.get("sl_price")
        )

        if sl_value is None:

            return None, "Missing stop loss"

        sl = float(sl_value)

        # Basic numeric validation
        if entry <= 0 or tp <= 0 or sl <= 0:

            return None, "Prices must be greater than zero"

        # Directional validation
        if side == "LONG":

            if not sl < entry < tp:

                return None, (
                    "Invalid LONG price relationship: "
                    "SL < Entry < TP required"
                )

        if side == "SHORT":

            if not tp < entry < sl:

                return None, (
                    "Invalid SHORT price relationship: "
                    "TP < Entry < SL required"
                )

        signal = {

            "side": side,

            "symbol": symbol,

            "exchange": str(exchange),

            "timeframe": str(timeframe),

            "leverage": leverage,

            "quantity": quantity,

            "entry": entry,

            "tp": tp,

            "sl": sl

        }

        return signal, None

    except Exception as e:

        return None, str(e)


# =========================================================
# CONTRACT VALIDATION
# =========================================================

@app.get("/validate-contract")
def validate_contract(
    symbol: str = "XAUUSDT",
    quantity: float = 1,
    price: float = 3500
):

    try:

        symbol = symbol.upper()

        client = BinanceClient(1)

        exchange_info = client.get_exchange_info()

        symbol_data = None

        for item in exchange_info.get("symbols", []):

            if item.get("symbol") == symbol:

                symbol_data = item

                break

        if not symbol_data:

            return {
                "status": "rejected",
                "reason": "Symbol not found",
                "execution_enabled": EXECUTION_ENABLED
            }

        filters = {}

        for item in symbol_data.get("filters", []):

            filters[item["filterType"]] = item

        lot_size = filters.get("LOT_SIZE", {})

        market_lot_size = filters.get(
            "MARKET_LOT_SIZE",
            {}
        )

        price_filter = filters.get(
            "PRICE_FILTER",
            {}
        )

        minimum_notional = filters.get(
            "MIN_NOTIONAL",
            {}
        )

        min_qty = float(
            lot_size.get("minQty", 0)
        )

        max_qty = float(
            lot_size.get("maxQty", 0)
        )

        lot_step = float(
            lot_size.get("stepSize", 0)
        )

        market_min_qty = float(
            market_lot_size.get("minQty", 0)
        )

        market_max_qty = float(
            market_lot_size.get("maxQty", 0)
        )

        market_step = float(
            market_lot_size.get("stepSize", 0)
        )

        min_price = float(
            price_filter.get("minPrice", 0)
        )

        max_price = float(
            price_filter.get("maxPrice", 0)
        )

        tick_size = float(
            price_filter.get("tickSize", 0)
        )

        min_notional = float(
            minimum_notional.get(
                "notional",
                minimum_notional.get("minNotional", 0)
            )
        )

        notional = quantity * price

        checks = {

            "symbol_trading":
                symbol_data.get("status") == "TRADING",

            "lot_size_valid":
                min_qty <= quantity <= max_qty
                and lot_step > 0
                and abs(
                    round(quantity / lot_step)
                    * lot_step - quantity
                ) < 1e-8,

            "market_lot_size_valid":
                market_min_qty <= quantity <= market_max_qty
                and market_step > 0
                and abs(
                    round(quantity / market_step)
                    * market_step - quantity
                ) < 1e-8,

            "price_filter_valid":
                min_price <= price <= max_price
                and tick_size > 0
                and abs(
                    round(price / tick_size)
                    * tick_size - price
                ) < 1e-8,

            "minimum_notional_valid":
                notional >= min_notional

        }

        all_checks_passed = all(
            checks.values()
        )

        return {

            "status": (
                "validated"
                if all_checks_passed
                else "rejected"
            ),

            "symbol": symbol,

            "contract_status":
                symbol_data.get("status"),

            "quantity": quantity,

            "price": price,

            "notional": notional,

            "filters": {

                "lot_size": lot_size,

                "market_lot_size":
                    market_lot_size,

                "price_filter":
                    price_filter,

                "minimum_notional":
                    minimum_notional

            },

            "checks": checks,

            "all_checks_passed":
                all_checks_passed,

            "execution_enabled":
                EXECUTION_ENABLED

        }

    except Exception as e:

        return {

            "status": "error",

            "error": str(e),

            "execution_enabled":
                EXECUTION_ENABLED

        }


# =========================================================
# WEBHOOK
# APPLY SIGNAL LEVERAGE
# NO ORDER EXECUTION
# =========================================================

@app.post("/webhook")
async def webhook(request: Request):

    try:

        body = await request.body()

        message = body.decode(
            "utf-8"
        ).strip()

        print(
            "Incoming signal:",
            message
        )

        signal, error = parse_signal(
            message
        )

        if error:

            return JSONResponse(

                status_code=400,

                content={

                    "status": "rejected",

                    "reason": error,

                    "execution_enabled":
                        EXECUTION_ENABLED

                }

            )

        # -------------------------------------------------
        # SYMBOL MAPPING
        # -------------------------------------------------

        tv_symbol = signal[
            "symbol"
        ].upper()

        if tv_symbol in (
            "XAUUSDT.P",
            "XAUUSDT"
        ):

            binance_symbol = "XAUUSDT"

        else:

            return JSONResponse(

                status_code=400,

                content={

                    "status": "rejected",

                    "reason": (
                        f"Unsupported symbol: "
                        f"{tv_symbol}"
                    ),

                    "execution_enabled":
                        EXECUTION_ENABLED

                }

            )

        # -------------------------------------------------
        # CONTRACT VALIDATION
        # -------------------------------------------------

        client = BinanceClient(1)

        exchange_info = client.get_exchange_info()

        symbol_data = None

        for item in exchange_info.get("symbols", []):

            if item.get("symbol") == binance_symbol:

                symbol_data = item

                break

        if not symbol_data:

            return JSONResponse(

                status_code=400,

                content={

                    "status": "rejected",

                    "reason": (
                        "Binance symbol not found"
                    ),

                    "execution_enabled":
                        EXECUTION_ENABLED

                }

            )

        if symbol_data.get("status") != "TRADING":

            return JSONResponse(

                status_code=400,

                content={

                    "status": "rejected",

                    "reason": (
                        "Binance contract is not trading"
                    ),

                    "execution_enabled":
                        EXECUTION_ENABLED

                }

            )

        # -------------------------------------------------
        # APPLY LEVERAGE FROM SIGNAL
        # -------------------------------------------------

        leverage_result = (
            client.apply_and_verify_leverage(
                binance_symbol,
                signal["leverage"]
            )
        )

        # -------------------------------------------------
        # RESPONSE
        # -------------------------------------------------

        return JSONResponse(

            content={

                "status":
                    "signal_validated",

                "signal":
                    signal,

                "binance_symbol":
                    binance_symbol,

                "account":
                    "Account 1",

                "leverage_result":
                    leverage_result,

                "execution_enabled":
                    EXECUTION_ENABLED,

                "message":
                    "Signal validated and leverage applied. "
                    "No orders placed."

            }

        )

    except Exception as e:

        print(
            "Webhook error:",
            str(e)
        )

        return JSONResponse(

            status_code=400,

            content={

                "status": "error",

                "detail": str(e),

                "execution_enabled":
                    EXECUTION_ENABLED

            }

        )
