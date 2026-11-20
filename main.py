import os
import json
import re
import time
import hashlib
import logging
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from binance_client import BinanceClient

app = FastAPI(title="Gold Trading Bridge - Safety Revision")

BINANCE_BASE_URL = "https://demo-fapi.binance.com"
TARGET_SYMBOL = "XAUUSDT"
EXECUTION_ENABLED = False  # HARD DISABLED: this revision is validation/diagnostic only.
RISK_LIMIT_PERCENT = float(os.getenv("RISK_LIMIT_PERCENT", "2"))
DUPLICATE_WINDOW_SECONDS = 300
SEEN_SIGNALS = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold_bridge")


def log_event(event):
    logger.info(json.dumps(event, default=str))


def clean_number(value):
    return float(str(value).replace(",", "").strip())


def normalize_symbol(symbol):
    symbol = str(symbol).upper().strip()
    return "XAUUSDT" if symbol in ("XAUUSDT.P", "XAUUSDT") else None


def signal_fingerprint(signal):
    raw = json.dumps(signal, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_duplicate(signal):
    now = time.time()
    for key, timestamp in list(SEEN_SIGNALS.items()):
        if now - timestamp > DUPLICATE_WINDOW_SECONDS:
            SEEN_SIGNALS.pop(key, None)
    return signal_fingerprint(signal) in SEEN_SIGNALS


def reserve_signal(signal):
    now = time.time()
    for key, timestamp in list(SEEN_SIGNALS.items()):
        if now - timestamp > DUPLICATE_WINDOW_SECONDS:
            SEEN_SIGNALS.pop(key, None)
    fingerprint = signal_fingerprint(signal)
    if fingerprint in SEEN_SIGNALS:
        return False
    SEEN_SIGNALS[fingerprint] = now
    return True


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
                    if ":" in line:
                        key, value = line.split(":", 1)
                        data[key.strip()] = value.strip()

        normalized = {
            str(key).lower().strip().replace(" ", "_"): value
            for key, value in data.items()
        }

        side_value = normalized.get("side") or normalized.get("direction") or normalized.get("signal")
        if not side_value:
            return None, "Missing direction"
        side_value = str(side_value).upper()
        if "LONG" in side_value:
            side = "LONG"
        elif "SHORT" in side_value:
            side = "SHORT"
        else:
            return None, "Invalid direction"

        symbol = normalized.get("symbol") or normalized.get("pair")
        if not symbol:
            return None, "Missing symbol"

        leverage_value = normalized.get("leverage")
        if leverage_value is None:
            return None, "Missing leverage"
        leverage_match = re.search(r"\d+", str(leverage_value))
        if not leverage_match:
            return None, "Invalid leverage"
        leverage = int(leverage_match.group())
        if not 1 <= leverage <= 125:
            return None, "Leverage out of range"

        quantity_value = normalized.get("quantity") or normalized.get("qty")
        entry_value = normalized.get("entry_price") or normalized.get("entry")
        tp_value = normalized.get("take_profit") or normalized.get("tp") or normalized.get("tp_price")
        sl_value = normalized.get("stop_loss") or normalized.get("sl") or normalized.get("sl_price")

        if quantity_value is None:
            return None, "Missing quantity"
        if entry_value is None:
            return None, "Missing entry"
        if tp_value is None:
            return None, "Missing take profit"
        if sl_value is None:
            return None, "Missing stop loss"

        quantity = clean_number(quantity_value)
        entry = clean_number(entry_value)
        tp = clean_number(tp_value)
        sl = clean_number(sl_value)

        if quantity <= 0:
            return None, "Quantity must be positive"
        if any(x <= 0 for x in (entry, tp, sl)):
            return None, "Prices must be positive"
        if side == "LONG" and not sl < entry < tp:
            return None, "Invalid LONG price relationship"
        if side == "SHORT" and not tp < entry < sl:
            return None, "Invalid SHORT price relationship"

        signal = {
            "side": side,
            "symbol": str(symbol).upper().strip(),
            "exchange": str(normalized.get("exchange", "BINANCE FUTURES")),
            "timeframe": str(normalized.get("timeframe") or normalized.get("interval") or "UNKNOWN"),
            "leverage": leverage,
            "quantity": quantity,
            "entry": entry,
            "tp": tp,
            "sl": sl,
        }
        return signal, None
    except Exception as exc:
        return None, str(exc)


def get_symbol_info(client, symbol):
    exchange_info = client.get_exchange_info()
    for item in exchange_info.get("symbols", []):
        if item.get("symbol") == symbol:
            return item
    return None


def get_filters(symbol_data):
    return {item["filterType"]: item for item in symbol_data.get("filters", [])}


def check_step(value, step):
    step = float(step)
    if step <= 0:
        return False
    # Use tolerance to avoid binary floating-point artifacts.
    return abs(round(value / step) * step - value) < max(1e-8, step * 1e-8)


def get_account_snapshot(client, symbol):
    account = client.get_account()
    positions = client.get_position_risk(symbol)
    active_positions = []
    if isinstance(positions, list):
        for position in positions:
            if position.get("symbol") == symbol and abs(float(position.get("positionAmt", 0))) > 0:
                active_positions.append(position)
    return account, active_positions


def validate_signal(signal, account_number=1):
    results = {"account": f"Account {account_number}", "checks": {}, "reasons": [], "ready": False}
    try:
        client = BinanceClient(account_number)
        symbol = normalize_symbol(signal["symbol"])
        results["binance_symbol"] = symbol
        if not symbol:
            results["status"] = "REJECTED"
            results["reasons"].append("Unsupported symbol")
            return results

        symbol_data = get_symbol_info(client, symbol)
        if not symbol_data:
            results["status"] = "REJECTED"
            results["reasons"].append("Contract not found")
            return results

        filters = get_filters(symbol_data)
        lot = filters.get("LOT_SIZE", {})
        market_lot = filters.get("MARKET_LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))

        quantity, entry, leverage = signal["quantity"], signal["entry"], signal["leverage"]
        notional = quantity * entry
        lot_min, lot_max, lot_step = float(lot.get("minQty", 0)), float(lot.get("maxQty", 0)), float(lot.get("stepSize", 0))
        market_min, market_max, market_step = float(market_lot.get("minQty", 0)), float(market_lot.get("maxQty", 0)), float(market_lot.get("stepSize", 0))
        price_min, price_max, tick = float(price_filter.get("minPrice", 0)), float(price_filter.get("maxPrice", 0)), float(price_filter.get("tickSize", 0))
        min_notional = float(min_notional_filter.get("notional", min_notional_filter.get("minNotional", 0)))

        contract_trading = symbol_data.get("status") == "TRADING"
        lot_valid = lot_min <= quantity <= lot_max and check_step(quantity, lot_step)
        market_lot_valid = market_min <= quantity <= market_max and check_step(quantity, market_step)
        price_valid = (
            price_min <= entry <= price_max
            and check_step(entry, tick)
            and check_step(signal["tp"], tick)
            and check_step(signal["sl"], tick)
        )
        notional_valid = notional >= min_notional

        results["checks"].update({
            "contract_trading": contract_trading,
            "lot_size_valid": lot_valid,
            "market_lot_size_valid": market_lot_valid,
            "price_precision_valid": price_valid,
            "minimum_notional_valid": notional_valid,
        })
        for check, valid, reason in [
            ("contract_trading", contract_trading, "Contract is not trading"),
            ("lot_size_valid", lot_valid, "LOT_SIZE validation failed"),
            ("market_lot_size_valid", market_lot_valid, "MARKET_LOT_SIZE validation failed"),
            ("price_precision_valid", price_valid, "Price precision/filter validation failed"),
            ("minimum_notional_valid", notional_valid, "Minimum notional validation failed"),
        ]:
            if not valid:
                results["reasons"].append(reason)

        account, active_positions = get_account_snapshot(client, symbol)
        available_balance = float(account.get("availableBalance", 0))
        wallet = float(account.get("totalWalletBalance", 0))
        margin_balance = float(account.get("totalMarginBalance", 0))
        results["account_balance"] = {"available_balance": available_balance, "wallet_balance": wallet, "margin_balance": margin_balance}
        account_connected = bool(account.get("canTrade", False))
        results["checks"]["account_connected"] = account_connected
        if not account_connected:
            results["reasons"].append("Account trading permission is unavailable")

        position_clear = len(active_positions) == 0
        results["existing_positions"] = len(active_positions)
        results["checks"]["no_existing_position"] = position_clear
        if not position_clear:
            results["reasons"].append("Existing XAUUSDT position detected")

        position_mode = client.get_position_mode()
        one_way_valid = not bool(position_mode.get("dualSidePosition", False))
        results["position_mode"] = position_mode
        results["checks"]["one_way_mode_valid"] = one_way_valid
        if not one_way_valid:
            results["reasons"].append("Hedge Mode detected; One-Way Mode is required")

        bracket_data = client.get_leverage_brackets(symbol)
        brackets = []
        if isinstance(bracket_data, list):
            for item in bracket_data:
                if item.get("symbol") == symbol:
                    brackets = item.get("brackets", [])
                    break
        elif isinstance(bracket_data, dict):
            brackets = bracket_data.get("brackets", [])

        applicable = None
        for bracket in brackets:
            floor = float(bracket.get("notionalFloor", 0))
            cap = float(bracket.get("notionalCap", 0))
            if floor <= notional <= cap:
                applicable = bracket
                break
        max_leverage = int(applicable.get("initialLeverage")) if applicable else None
        bracket_valid = max_leverage is not None and leverage <= max_leverage
        results["leverage_bracket"] = {"requested_leverage": leverage, "maximum_leverage_for_notional": max_leverage}
        results["checks"]["bracket_leverage_valid"] = bracket_valid
        if not bracket_valid:
            results["reasons"].append("Requested leverage exceeds bracket or bracket could not be determined")

        estimated_margin = notional / leverage
        margin_valid = estimated_margin <= available_balance
        results["margin_estimate"] = {"notional": notional, "requested_leverage": leverage, "estimated_initial_margin": estimated_margin, "available_balance": available_balance}
        results["checks"]["margin_available"] = margin_valid
        if not margin_valid:
            results["reasons"].append("Estimated initial margin exceeds available balance")

        stop_distance = abs(entry - signal["sl"])
        estimated_loss = stop_distance * quantity
        risk_percent = estimated_loss / wallet * 100 if wallet > 0 else float("inf")
        risk_valid = wallet > 0 and risk_percent <= RISK_LIMIT_PERCENT
        results["risk_estimate"] = {
            "stop_distance": stop_distance,
            "estimated_loss_at_stop": estimated_loss,
            "wallet_balance": wallet,
            "estimated_risk_percent": risk_percent,
            "risk_limit_percent": RISK_LIMIT_PERCENT,
            "fees_and_slippage_included": False,
        }
        results["checks"]["risk_limit_valid"] = risk_valid
        if not risk_valid:
            results["reasons"].append("Estimated risk exceeds configured limit")

        # Validation does NOT reserve or consume a fingerprint.
        duplicate = is_duplicate(signal)
        results["checks"]["duplicate_signal"] = not duplicate
        if duplicate:
            results["reasons"].append("Signal fingerprint was already accepted recently")

        results["ready"] = all(results["checks"].values())
        results["status"] = "VALIDATION_PASS_EXECUTION_DISABLED" if results["ready"] else "REJECTED"
        results["execution_enabled"] = EXECUTION_ENABLED
        return results
    except Exception as exc:
        results["status"] = "ERROR"
        results["reasons"].append(str(exc))
        results["execution_enabled"] = EXECUTION_ENABLED
        return results


@app.get("/")
def home():
    return {"service": "Gold Trading Bridge", "status": "running", "mode": "VALIDATION_ONLY", "execution_enabled": EXECUTION_ENABLED}


@app.get("/health")
def health():
    return {"status": "healthy", "execution_enabled": EXECUTION_ENABLED, "mode": "VALIDATION_ONLY"}


@app.get("/diagnostic")
def diagnostic():
    results = {}
    for name, endpoint in (("ping", "/fapi/v1/ping"), ("time", "/fapi/v1/time")):
        try:
            response = requests.get(BINANCE_BASE_URL + endpoint, timeout=10)
            results[name] = {"http_status": response.status_code, "success": response.status_code == 200, "response": response.text[:1000]}
        except Exception as exc:
            results[name] = {"success": False, "error": str(exc)}
    return {"results": results, "execution_enabled": EXECUTION_ENABLED}


@app.get("/test-connections")
def test_connections():
    results = []
    for account_number in (1, 2):
        try:
            results.append(BinanceClient(account_number).test_connection())
        except Exception as exc:
            results.append({"account": f"Account {account_number}", "status": "error", "error": str(exc)})
    return {"results": results, "execution_enabled": EXECUTION_ENABLED}


@app.get("/account-config")
def account_config():
    results = []
    for account_number in (1, 2):
        try:
            client = BinanceClient(account_number)
            mode = client.get_position_mode()
            positions = client.get_position_risk(TARGET_SYMBOL)
            brackets = client.get_leverage_brackets(TARGET_SYMBOL)
            leverage = [p.get("leverage") for p in positions if p.get("symbol") == TARGET_SYMBOL] if isinstance(positions, list) else []
            results.append({"account": f"Account {account_number}", "status": "success", "position_mode": mode, "current_leverage": leverage, "leverage_brackets": brackets})
        except Exception as exc:
            results.append({"account": f"Account {account_number}", "status": "error", "error": str(exc)})
    return {"results": results, "execution_enabled": EXECUTION_ENABLED}


@app.get("/validate-contract")
def validate_contract(symbol: str = "XAUUSDT", quantity: float = 1, price: float = 3500):
    try:
        client = BinanceClient(1)
        symbol = symbol.upper()
        data = get_symbol_info(client, symbol)
        if not data:
            return {"status": "rejected", "reason": "Symbol not found", "execution_enabled": False}
        filters = get_filters(data)
        lot, market_lot, price_filter = filters.get("LOT_SIZE", {}), filters.get("MARKET_LOT_SIZE", {}), filters.get("PRICE_FILTER", {})
        notional_filter = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
        min_notional = float(notional_filter.get("notional", notional_filter.get("minNotional", 0)))
        checks = {
            "symbol_trading": data.get("status") == "TRADING",
            "lot_size_valid": float(lot.get("minQty", 0)) <= quantity <= float(lot.get("maxQty", 0)) and check_step(quantity, float(lot.get("stepSize", 0))),
            "market_lot_size_valid": float(market_lot.get("minQty", 0)) <= quantity <= float(market_lot.get("maxQty", 0)) and check_step(quantity, float(market_lot.get("stepSize", 0))),
            "price_filter_valid": float(price_filter.get("minPrice", 0)) <= price <= float(price_filter.get("maxPrice", 0)) and check_step(price, float(price_filter.get("tickSize", 0))),
            "minimum_notional_valid": quantity * price >= min_notional,
        }
        return {"status": "validated" if all(checks.values()) else "rejected", "symbol": symbol, "quantity": quantity, "price": price, "notional": quantity * price, "checks": checks, "all_checks_passed": all(checks.values()), "execution_enabled": False}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "execution_enabled": False}


@app.post("/validate-signal")
async def validate_only(request: Request):
    try:
        body = await request.body()
        signal, error = parse_signal(body.decode("utf-8"))
        if error:
            return JSONResponse(status_code=400, content={"status": "rejected", "reason": error, "execution_enabled": False})
        results = [validate_signal(signal, account_number=n) for n in (1, 2)]
        return {"status": "multi_account_validation", "signal": signal, "accounts": results, "execution_enabled": False}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(exc), "execution_enabled": False})


@app.post("/webhook")
async def webhook(request: Request):
    try:
        raw_body = (await request.body()).decode("utf-8").strip()
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict) and str(payload.get("type", "")).upper() == "KEEPALIVE":
            log_event({"event": "keepalive_received", "source": payload.get("source", "unknown")})
            return JSONResponse(status_code=200, content={"status": "keepalive_received", "trading": False, "binance_contacted": False})

        # Intentionally non-executable in this release.
        return JSONResponse(status_code=503, content={
            "status": "execution_disabled",
            "execution_enabled": False,
            "mode": "VALIDATION_ONLY",
            "message": "Trade webhooks are disabled in this safety revision."
        })
    except Exception as exc:
        log_event({"event": "webhook_error", "error": str(exc)})
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(exc), "execution_enabled": False})
