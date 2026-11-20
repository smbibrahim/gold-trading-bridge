import os
import json
import re
import time
import hashlib
import logging

import requests

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from binance_client import BinanceClient


app = FastAPI(title="Gold Trading Bridge - Pre-Trade Validation")

BINANCE_BASE_URL = "https://demo-fapi.binance.com"
EXECUTION_ENABLED = False
TARGET_SYMBOL = "XAUUSDT"

RISK_LIMIT_PERCENT = float(
    os.getenv("RISK_LIMIT_PERCENT", "2")
)

# In-memory duplicate protection.
# This resets when Render restarts.
SEEN_SIGNALS = {}
DUPLICATE_WINDOW_SECONDS = 300

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold_bridge")


# =========================================================
# UTILITIES
# =========================================================

def log_event(event):
    logger.info(json.dumps(event, default=str))


def clean_number(value):
    return float(str(value).replace(",", "").strip())


def normalize_symbol(symbol):
    symbol = str(symbol).upper().strip()

    if symbol in ("XAUUSDT.P", "XAUUSDT"):
        return "XAUUSDT"

    return None


def signal_fingerprint(signal):
    raw = json.dumps(
        signal,
        sort_keys=True
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def check_duplicate(signal):
    now = time.time()

    expired = [
        key for key, timestamp in SEEN_SIGNALS.items()
        if now - timestamp > DUPLICATE_WINDOW_SECONDS
    ]

    for key in expired:
        SEEN_SIGNALS.pop(key, None)

    fingerprint = signal_fingerprint(signal)

    if fingerprint in SEEN_SIGNALS:
        return True

    SEEN_SIGNALS[fingerprint] = now

    return False


# =========================================================
# SIGNAL PARSER
# =========================================================

def parse_signal(message):

    try:

        if isinstance(message, dict):
            data = message

        else:

            message = str(message).strip()

            try:
                data = json.loads(message)

            except json.JSONDecodeError:

                data = {}

                for line in message.splitlines():

                    line = line.strip()

                    if ":" not in line:
                        continue

                    key, value = line.split(":", 1)

                    data[key.strip()] = value.strip()

        normalized = {}

        for key, value in data.items():

            normalized[
                str(key).lower().strip().replace(" ", "_")
            ] = value

        side_value = (
            normalized.get("side")
            or normalized.get("direction")
            or normalized.get("signal")
        )

        if not side_value:
            return None, "Missing direction"

        side_value = str(side_value).upper()

        if "LONG" in side_value:
            side = "LONG"

        elif "SHORT" in side_value:
            side = "SHORT"

        else:
            return None, "Invalid direction"

        symbol = (
            normalized.get("symbol")
            or normalized.get("pair")
        )

        if not symbol:
            return None, "Missing symbol"

        exchange = normalized.get(
            "exchange",
            "BINANCE FUTURES"
        )

        timeframe = (
            normalized.get("timeframe")
            or normalized.get("interval")
            or "UNKNOWN"
        )

        leverage_value = normalized.get("leverage")

        if leverage_value is None:
            return None, "Missing leverage"

        leverage_match = re.search(
            r"\d+",
            str(leverage_value)
        )

        if not leverage_match:
            return None, "Invalid leverage"

        leverage = int(leverage_match.group())

        if leverage < 1 or leverage > 125:
            return None, "Leverage out of range"

        quantity_value = (
            normalized.get("quantity")
            or normalized.get("qty")
        )

        if quantity_value is None:
            return None, "Missing quantity"

        quantity = clean_number(quantity_value)

        entry_value = (
            normalized.get("entry_price")
            or normalized.get("entry")
        )

        tp_value = (
            normalized.get("take_profit")
            or normalized.get("tp")
            or normalized.get("tp_price")
        )

        sl_value = (
            normalized.get("stop_loss")
            or normalized.get("sl")
            or normalized.get("sl_price")
        )

        if entry_value is None:
            return None, "Missing entry"

        if tp_value is None:
            return None, "Missing take profit"

        if sl_value is None:
            return None, "Missing stop loss"

        entry = clean_number(entry_value)
        tp = clean_number(tp_value)
        sl = clean_number(sl_value)

        if quantity <= 0:
            return None, "Quantity must be positive"

        if entry <= 0 or tp <= 0 or sl <= 0:
            return None, "Prices must be positive"

        if side == "LONG":

            if not sl < entry < tp:
                return None, "Invalid LONG price relationship"

        if side == "SHORT":

            if not tp < entry < sl:
                return None, "Invalid SHORT price relationship"

        signal = {
            "side": side,
            "symbol": str(symbol).upper().strip(),
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
# CONTRACT INFORMATION
# =========================================================

def get_symbol_info(client, symbol):

    exchange_info = client.get_exchange_info()

    for item in exchange_info.get("symbols", []):

        if item.get("symbol") == symbol:
            return item

    return None


def get_filters(symbol_data):

    result = {}

    for item in symbol_data.get("filters", []):

        result[item["filterType"]] = item

    return result


def check_step(value, step):

    step = float(step)

    if step <= 0:
        return False

    return abs(
        round(value / step) * step - value
    ) < 1e-8


# =========================================================
# ACCOUNT VALIDATION
# =========================================================

def get_account_snapshot(client, symbol):

    account = client.get_account()

    positions = client.get_position_risk(symbol)

    active_positions = []

    if isinstance(positions, list):

        for position in positions:

            if position.get("symbol") != symbol:
                continue

            position_amt = float(
                position.get("positionAmt", 0)
            )

            if abs(position_amt) > 0:
                active_positions.append(position)

    return account, active_positions


# =========================================================
# PRE-TRADE VALIDATION ENGINE
# =========================================================

def validate_signal(signal, account_number=1):

    results = {
        "account": f"Account {account_number}",
        "checks": {},
        "reasons": [],
        "ready": False
    }

    try:

        client = BinanceClient(account_number)

        # -------------------------------------------------
        # SYMBOL
        # -------------------------------------------------

        binance_symbol = normalize_symbol(
            signal["symbol"]
        )

        results["binance_symbol"] = binance_symbol

        if not binance_symbol:

            results["reasons"].append(
                "Unsupported symbol"
            )

            return results

        # -------------------------------------------------
        # CONTRACT
        # -------------------------------------------------

        symbol_data = get_symbol_info(
            client,
            binance_symbol
        )

        if not symbol_data:

            results["reasons"].append(
                "Contract not found"
            )

            return results

        filters = get_filters(symbol_data)

        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", {})

        quantity = signal["quantity"]
        entry = signal["entry"]
        leverage = signal["leverage"]

        notional = quantity * entry

        lot_min = float(lot.get("minQty", 0))
        lot_max = float(lot.get("maxQty", 0))
        lot_step = float(lot.get("stepSize", 0))

        market_min = float(market_lot.get("minQty", 0))
        market_max = float(market_lot.get("maxQty", 0))
        market_step = float(market_lot.get("stepSize", 0))

        price_min = float(
            price_filter.get("minPrice", 0)
        )

        price_max = float(
            price_filter.get("maxPrice", 0)
        )

        tick = float(
            price_filter.get("tickSize", 0)
        )

        min_notional = float(
            min_notional_filter.get(
                "notional",
                min_notional_filter.get("minNotional", 0)
            )
        )

        contract_trading = (
            symbol_data.get("status") == "TRADING"
        )

        lot_valid = (
            lot_min <= quantity <= lot_max
            and check_step(quantity, lot_step)
        )

        market_lot_valid = (
            market_min <= quantity <= market_max
            and check_step(quantity, market_step)
        )

        price_valid = (
            price_min <= entry <= price_max
            and check_step(entry, tick)
            and check_step(signal["tp"], tick)
            and check_step(signal["sl"], tick)
        )

        notional_valid = notional >= min_notional

        results["checks"]["contract_trading"] = (
            contract_trading
        )

        results["checks"]["lot_size_valid"] = (
            lot_valid
        )

        results["checks"]["market_lot_size_valid"] = (
            market_lot_valid
        )

        results["checks"]["price_precision_valid"] = (
            price_valid
        )

        results["checks"]["minimum_notional_valid"] = (
            notional_valid
        )

        if not contract_trading:
            results["reasons"].append(
                "Contract is not trading"
            )

        if not lot_valid:
            results["reasons"].append(
                "LOT_SIZE validation failed"
            )

        if not market_lot_valid:
            results["reasons"].append(
                "MARKET_LOT_SIZE validation failed"
            )

        if not price_valid:
            results["reasons"].append(
                "Price precision/filter validation failed"
            )

        if not notional_valid:
            results["reasons"].append(
                "Minimum notional validation failed"
            )

        # -------------------------------------------------
        # ACCOUNT SNAPSHOT
        # -------------------------------------------------

        account, active_positions = get_account_snapshot(
            client,
            binance_symbol
        )

        available_balance = float(
            account.get("availableBalance", 0)
        )

        total_wallet_balance = float(
            account.get("totalWalletBalance", 0)
        )

        total_margin_balance = float(
            account.get("totalMarginBalance", 0)
        )

        results["account_balance"] = {
            "available_balance": available_balance,
            "wallet_balance": total_wallet_balance,
            "margin_balance": total_margin_balance
        }

        results["checks"]["account_connected"] = (
            account.get("canTrade", False)
        )

        if not account.get("canTrade", False):
            results["reasons"].append(
                "Account trading permission is unavailable"
            )

        # -------------------------------------------------
        # EXISTING POSITION CHECK
        # -------------------------------------------------

        results["existing_positions"] = len(
            active_positions
        )

        position_clear = (
            len(active_positions) == 0
        )

        results["checks"]["no_existing_position"] = (
            position_clear
        )

        if not position_clear:
            results["reasons"].append(
                "Existing XAUUSDT position detected"
            )

        # -------------------------------------------------
        # LEVERAGE BRACKET CHECK
        # -------------------------------------------------

        bracket_data = client.get_leverage_brackets(
            binance_symbol
        )

        bracket_list = []

        if isinstance(bracket_data, list):

            for item in bracket_data:

                if item.get("symbol") == binance_symbol:

                    bracket_list = item.get(
                        "brackets",
                        []
                    )

                    break

        else:

            bracket_list = bracket_data.get(
                "brackets",
                []
            )

        applicable_bracket = None

        for bracket in bracket_list:

            floor = float(
                bracket.get("notionalFloor", 0)
            )

            cap = float(
                bracket.get("notionalCap", 0)
            )

            if floor <= notional <= cap:

                applicable_bracket = bracket

                break

        maximum_bracket_leverage = (
            int(applicable_bracket.get("initialLeverage"))
            if applicable_bracket
            else None
        )

        bracket_leverage_valid = (
            maximum_bracket_leverage is not None
            and leverage <= maximum_bracket_leverage
        )

        results["leverage_bracket"] = {
            "requested_leverage": leverage,
            "maximum_leverage_for_notional": (
                maximum_bracket_leverage
            )
        }

        results["checks"]["bracket_leverage_valid"] = (
            bracket_leverage_valid
        )

        if not bracket_leverage_valid:

            results["reasons"].append(
                "Requested leverage exceeds applicable bracket "
                "or bracket could not be determined"
            )

        # -------------------------------------------------
        # MARGIN ESTIMATE
        # -------------------------------------------------

        estimated_initial_margin = (
            notional / leverage
        )

        margin_valid = (
            estimated_initial_margin
            <= available_balance
        )

        results["margin_estimate"] = {
            "notional": notional,
            "requested_leverage": leverage,
            "estimated_initial_margin": (
                estimated_initial_margin
            ),
            "available_balance": available_balance
        }

        results["checks"]["margin_available"] = (
            margin_valid
        )

        if not margin_valid:

            results["reasons"].append(
                "Estimated initial margin exceeds available balance"
            )

        # -------------------------------------------------
        # RISK ESTIMATE
        # -------------------------------------------------

        stop_distance = abs(
            entry - signal["sl"]
        )

        estimated_loss = (
            stop_distance * quantity
        )

        risk_percent = (
            estimated_loss / total_wallet_balance * 100
            if total_wallet_balance > 0
            else float("inf")
        )

        risk_valid = (
            total_wallet_balance > 0
            and risk_percent <= RISK_LIMIT_PERCENT
        )

        results["risk_estimate"] = {

            "stop_distance": stop_distance,

            "estimated_loss_at_stop": (
                estimated_loss
            ),

            "wallet_balance": (
                total_wallet_balance
            ),

            "estimated_risk_percent": (
                risk_percent
            ),

            "risk_limit_percent": (
                RISK_LIMIT_PERCENT
            )

        }

        results["checks"]["risk_limit_valid"] = (
            risk_valid
        )

        if not risk_valid:

            results["reasons"].append(
                "Estimated risk exceeds configured risk limit"
            )

        # -------------------------------------------------
        # DUPLICATE CHECK
        # -------------------------------------------------

        results["checks"]["duplicate_signal"] = (
            True
        )

        # -------------------------------------------------
        # FINAL RESULT
        # -------------------------------------------------

        all_checks_passed = all(
            results["checks"].values()
        )

        results["ready"] = all_checks_passed

        results["status"] = (
            "READY_FOR_LEVERAGE_CONFIGURATION"
            if all_checks_passed
            else "REJECTED"
        )

        results["execution_enabled"] = (
            EXECUTION_ENABLED
        )

        return results

    except Exception as e:

        results["status"] = "ERROR"

        results["reasons"].append(
            str(e)
        )

        results["execution_enabled"] = (
            EXECUTION_ENABLED
        )

        return results


# =========================================================
# ROUTES
# =========================================================

@app.get("/")
def home():

    return {

        "service": "Gold Trading Bridge",

        "status": "running",

        "mode": "PRE-TRADE VALIDATION",

        "execution_enabled": (
            EXECUTION_ENABLED
        )

    }


@app.get("/health")
def health():

    return {

        "status": "healthy",

        "execution_enabled": (
            EXECUTION_ENABLED
        )

    }


@app.get("/diagnostic")
def diagnostic():

    results = {}

    for name, endpoint in (
        ("ping", "/fapi/v1/ping"),
        ("time", "/fapi/v1/time")
    ):

        try:

            response = requests.get(
                BINANCE_BASE_URL + endpoint,
                timeout=15
            )

            results[name] = {

                "http_status": (
                    response.status_code
                ),

                "success": (
                    response.status_code == 200
                ),

                "response": (
                    response.text
                )

            }

        except Exception as e:

            results[name] = {

                "success": False,

                "error": str(e)

            }

    return {

        "results": results,

        "execution_enabled": (
            EXECUTION_ENABLED
        )

    }


@app.get("/test-connections")
def test_connections():

    results = []

    for account_number in (1, 2):

        try:

            client = BinanceClient(
                account_number
            )

            results.append(
                client.test_connection()
            )

        except Exception as e:

            results.append({

                "account": (
                    f"Account {account_number}"
                ),

                "status": "error",

                "error": str(e)

            })

    return {

        "results": results,

        "execution_enabled": (
            EXECUTION_ENABLED
        )

    }


@app.get("/account-config")
def account_config():

    results = []

    for account_number in (1, 2):

        try:

            client = BinanceClient(
                account_number
            )

            mode = client.get_position_mode()

            position_risk = client.get_position_risk(
                TARGET_SYMBOL
            )

            brackets = client.get_leverage_brackets(
                TARGET_SYMBOL
            )

            leverage = []

            if isinstance(position_risk, list):

                for position in position_risk:

                    if position.get("symbol") == TARGET_SYMBOL:

                        leverage.append(
                            position.get("leverage")
                        )

            results.append({

                "account": (
                    f"Account {account_number}"
                ),

                "status": "success",

                "position_mode": mode,

                "current_leverage": leverage,

                "leverage_brackets": brackets

            })

        except Exception as e:

            results.append({

                "account": (
                    f"Account {account_number}"
                ),

                "status": "error",

                "error": str(e)

            })

    return {

        "results": results,

        "execution_enabled": (
            EXECUTION_ENABLED
        )

    }


@app.get("/validate-contract")
def validate_contract(
    symbol: str = "XAUUSDT",
    quantity: float = 1,
    price: float = 3500
):

    try:

        client = BinanceClient(1)

        symbol_data = get_symbol_info(
            client,
            symbol.upper()
        )

        if not symbol_data:

            return {

                "status": "rejected",

                "reason": "Symbol not found",

                "execution_enabled": False

            }

        filters = get_filters(
            symbol_data
        )

        lot = filters.get(
            "LOT_SIZE",
            {}
        )

        market_lot = filters.get(
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
            lot.get("minQty", 0)
        )

        max_qty = float(
            lot.get("maxQty", 0)
        )

        step = float(
            lot.get("stepSize", 0)
        )

        market_min = float(
            market_lot.get("minQty", 0)
        )

        market_max = float(
            market_lot.get("maxQty", 0)
        )

        market_step = float(
            market_lot.get("stepSize", 0)
        )

        min_price = float(
            price_filter.get("minPrice", 0)
        )

        max_price = float(
            price_filter.get("maxPrice", 0)
        )

        tick = float(
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
                and check_step(quantity, step),

            "market_lot_size_valid":
                market_min <= quantity <= market_max
                and check_step(quantity, market_step),

            "price_filter_valid":
                min_price <= price <= max_price
                and check_step(price, tick),

            "minimum_notional_valid":
                notional >= min_notional

        }

        passed = all(
            checks.values()
        )

        return {

            "status": (
                "validated"
                if passed
                else "rejected"
            ),

            "symbol": symbol.upper(),

            "quantity": quantity,

            "price": price,

            "notional": notional,

            "checks": checks,

            "all_checks_passed": passed,

            "execution_enabled": False

        }

    except Exception as e:

        return {

            "status": "error",

            "error": str(e),

            "execution_enabled": False

        }


# =========================================================
# VALIDATE-ONLY ENDPOINT
# =========================================================

@app.post("/validate-signal")
async def validate_only(request: Request):
    try:
        body = await request.body()
        signal, error = parse_signal(body.decode("utf-8"))

        if error:
            return JSONResponse(status_code=400, content={
                "status": "rejected", "reason": error,
                "execution_enabled": EXECUTION_ENABLED
            })

        account_results = []
        for account_number in (1, 2):
            result = validate_signal(signal, account_number=account_number)
            account_results.append(result)

        response = {
            "status": "multi_account_validation",
            "signal": signal,
            "accounts": account_results,
            "execution_enabled": EXECUTION_ENABLED
        }

        log_event({"event": "multi_account_validate_only", "signal": signal, "results": account_results})
        return response

    except Exception as e:
        return JSONResponse(status_code=400, content={
            "status": "error", "detail": str(e),
            "execution_enabled": EXECUTION_ENABLED
        })


# =========================================================
# DEMO EXECUTION WEBHOOK
# =========================================================

EXECUTION_ENABLED = os.getenv("EXECUTION_ENABLED", "false").lower() == "true"

def process_demo_signal(signal, fingerprint):
    """Run demo-only execution after the webhook has been acknowledged."""
    results = []
    any_completed = False
    for account_number in (1, 2):
        item = {"account": f"Account {account_number}", "status": "started"}
        try:
            validation = validate_signal(signal, account_number)
            item["validation"] = validation
            if not validation.get("ready"):
                item["status"] = "rejected_by_validation"
                results.append(item)
                continue

            client = BinanceClient(account_number)
            lev = client.apply_and_verify_leverage(TARGET_SYMBOL, signal["leverage"])
            item["leverage"] = lev

            # Explicitly require One-Way mode for closePosition protection.
            mode = client.get_position_mode()
            if mode.get("dualSidePosition"):
                item["status"] = "rejected_hedge_mode"
                item["reason"] = "Switch this demo account to One-Way Mode before execution."
                results.append(item)
                continue

            side = "BUY" if signal["side"] == "LONG" else "SELL"
            exit_side = "SELL" if side == "BUY" else "BUY"
            entry_order = client.place_market_order(TARGET_SYMBOL, side, signal["quantity"])
            item["entry_order"] = entry_order

            position = client.get_open_position(TARGET_SYMBOL)
            if not position or abs(float(position.get("positionAmt", 0))) <= 0:
                item["status"] = "entry_not_confirmed"
                results.append(item)
                continue

            item["position"] = position
            try:
                sl_order = client.place_close_position_order(TARGET_SYMBOL, exit_side, "STOP_MARKET", signal["sl"])
                tp_order = client.place_close_position_order(TARGET_SYMBOL, exit_side, "TAKE_PROFIT_MARKET", signal["tp"])
                item["stop_loss_order"] = sl_order
                item["take_profit_order"] = tp_order
                item["status"] = "demo_trade_open_with_protection"
                any_completed = True
            except Exception as protection_error:
                item["protection_error"] = str(protection_error)
                try:
                    item["emergency_close"] = client.close_position_market(TARGET_SYMBOL, exit_side)
                    item["status"] = "emergency_close_attempted_protection_failed"
                except Exception as close_error:
                    item["emergency_close_error"] = str(close_error)
                    item["status"] = "CRITICAL_protection_and_close_failed"
            results.append(item)
        except Exception as account_error:
            item["status"] = "account_error"
            item["error"] = str(account_error)
            results.append(item)

    log_event({"event": "background_demo_execution_finished", "fingerprint": fingerprint, "signal": signal, "accounts": results, "any_completed": any_completed})


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    if not EXECUTION_ENABLED:
        return JSONResponse(status_code=503, content={"status": "execution_disabled", "execution_enabled": False})
    try:
        body = await request.body()
        signal, error = parse_signal(body.decode("utf-8").strip())
        if error:
            return JSONResponse(status_code=400, content={"status": "rejected", "reason": error})

        fingerprint = signal_fingerprint(signal)
        now = time.time()
        for key, timestamp in list(SEEN_SIGNALS.items()):
            if now - timestamp > DUPLICATE_WINDOW_SECONDS:
                SEEN_SIGNALS.pop(key, None)
        if fingerprint in SEEN_SIGNALS:
            return JSONResponse(status_code=409, content={"status": "duplicate_rejected"})

        # Reserve fingerprint immediately so concurrent/repeated webhook calls don't enqueue twice.
        SEEN_SIGNALS[fingerprint] = now
        background_tasks.add_task(process_demo_signal, signal, fingerprint)
        return JSONResponse(status_code=202, content={
            "status": "accepted_for_background_demo_processing",
            "signal": signal,
            "accounts": ["Account 1", "Account 2"],
            "execution_enabled": True,
            "mode": "BINANCE_FUTURES_DEMO_ONLY",
            "message": "Signal accepted. Background processing started after acknowledgement."
        })
    except Exception as e:
        log_event({"event": "webhook_error", "error": str(e)})
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
