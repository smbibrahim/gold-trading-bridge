import os, json, re, time, hashlib, logging, threading
from typing import Any
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from binance_client import BinanceClient

app = FastAPI(title="Gold Trading Bridge - Binance Demo Execution")
BINANCE_BASE_URL = "https://demo-fapi.binance.com"  # demo-only
TARGET_SYMBOL = "XAUUSDT"
EXECUTION_ENABLED = os.getenv("EXECUTION_ENABLED", "false").lower() == "true"
RISK_LIMIT_PERCENT = float(os.getenv("RISK_LIMIT_PERCENT", "2"))
DUPLICATE_WINDOW_SECONDS = 300
SEEN_SIGNALS = {}
LOCK = threading.Lock()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gold_bridge")


def log_event(event):
    logger.info(json.dumps(event, default=str))


def clean_number(value):
    return float(str(value).replace(",", "").strip())


def normalize_symbol(symbol):
    symbol = str(symbol).upper().strip()
    return TARGET_SYMBOL if symbol in ("XAUUSDT.P", TARGET_SYMBOL) else None


def signal_fingerprint(signal):
    raw = json.dumps(signal, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def reserve_signal(signal):
    now = time.time()
    with LOCK:
        for key, timestamp in list(SEEN_SIGNALS.items()):
            if now - timestamp > DUPLICATE_WINDOW_SECONDS:
                SEEN_SIGNALS.pop(key, None)
        fp = signal_fingerprint(signal)
        if fp in SEEN_SIGNALS:
            return False, fp
        SEEN_SIGNALS[fp] = now
        return True, fp


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
                    if ":" in line:
                        key, value = line.split(":", 1)
                        data[key.strip()] = value.strip()
        if not isinstance(data, dict):
            return None, "Signal must be a JSON object or key/value text"
        normalized = {str(k).lower().strip().replace(" ", "_"): v for k, v in data.items()}
        side_value = normalized.get("side") or normalized.get("direction") or normalized.get("signal")
        if not side_value:
            return None, "Missing direction"
        side_value = str(side_value).upper()
        side = "LONG" if "LONG" in side_value else "SHORT" if "SHORT" in side_value else None
        if not side:
            return None, "Invalid direction"
        symbol = normalized.get("symbol") or normalized.get("pair")
        if not symbol:
            return None, "Missing symbol"
        lev_value = normalized.get("leverage")
        lev_match = re.search(r"\d+", str(lev_value)) if lev_value is not None else None
        if not lev_match:
            return None, "Missing or invalid leverage"
        leverage = int(lev_match.group())
        if not 1 <= leverage <= 125:
            return None, "Leverage out of range"
        quantity_value = normalized.get("quantity") or normalized.get("qty")
        entry_value = normalized.get("entry_price") or normalized.get("entry")
        tp_value = normalized.get("take_profit") or normalized.get("tp") or normalized.get("tp_price")
        sl_value = normalized.get("stop_loss") or normalized.get("sl") or normalized.get("sl_price")
        if any(v is None for v in (quantity_value, entry_value, tp_value, sl_value)):
            return None, "Missing quantity, entry, TP or SL"
        quantity, entry, tp, sl = map(clean_number, (quantity_value, entry_value, tp_value, sl_value))
        if quantity <= 0 or any(x <= 0 for x in (entry, tp, sl)):
            return None, "Quantity and prices must be positive"
        if side == "LONG" and not sl < entry < tp:
            return None, "Invalid LONG price relationship"
        if side == "SHORT" and not tp < entry < sl:
            return None, "Invalid SHORT price relationship"
        return {
            "side": side, "symbol": str(symbol).upper().strip(),
            "exchange": str(normalized.get("exchange", "BINANCE FUTURES")),
            "timeframe": str(normalized.get("timeframe") or normalized.get("interval") or "UNKNOWN"),
            "leverage": leverage, "quantity": quantity, "entry": entry, "tp": tp, "sl": sl,
        }, None
    except Exception as exc:
        return None, str(exc)


def symbol_info(client, symbol):
    for item in client.get_exchange_info().get("symbols", []):
        if item.get("symbol") == symbol:
            return item
    return None


def filters_for(data):
    return {x["filterType"]: x for x in data.get("filters", [])}


def check_step(value, step):
    step = float(step)
    return step > 0 and abs(round(value / step) * step - value) < max(1e-8, step * 1e-8)


def active_position(client, symbol):
    return client.get_open_position(symbol)


def validate_signal(signal, account_number):
    result = {"account": f"Account {account_number}", "checks": {}, "reasons": [], "ready": False}
    try:
        client = BinanceClient(account_number)
        symbol = normalize_symbol(signal["symbol"])
        result["symbol"] = symbol
        if not symbol:
            result["reasons"].append("Unsupported symbol")
            return result
        data = symbol_info(client, symbol)
        if not data:
            result["reasons"].append("Contract not found")
            return result
        f = filters_for(data)
        lot, mlot, pf = f.get("LOT_SIZE", {}), f.get("MARKET_LOT_SIZE", {}), f.get("PRICE_FILTER", {})
        nf = f.get("MIN_NOTIONAL", f.get("NOTIONAL", {}))
        qty, entry, lev = signal["quantity"], signal["entry"], signal["leverage"]
        min_notional = float(nf.get("notional", nf.get("minNotional", 0)))
        checks = {
            "contract_trading": data.get("status") == "TRADING",
            "lot_size_valid": float(lot.get("minQty", 0)) <= qty <= float(lot.get("maxQty", 0)) and check_step(qty, float(lot.get("stepSize", 0))),
            "market_lot_size_valid": float(mlot.get("minQty", 0)) <= qty <= float(mlot.get("maxQty", 0)) and check_step(qty, float(mlot.get("stepSize", 0))),
            "price_filter_valid": check_step(entry, float(pf.get("tickSize", 0))) and check_step(signal["tp"], float(pf.get("tickSize", 0))) and check_step(signal["sl"], float(pf.get("tickSize", 0))) and float(pf.get("minPrice", 0)) <= min(entry, signal["tp"], signal["sl"]) and max(entry, signal["tp"], signal["sl"]) <= float(pf.get("maxPrice", 0)),
            "minimum_notional_valid": qty * entry >= min_notional,
        }
        account = client.get_account()
        available, wallet = float(account.get("availableBalance", 0)), float(account.get("totalWalletBalance", 0))
        checks["account_can_trade"] = bool(account.get("canTrade", False))
        positions = client.get_position_risk(symbol)
        checks["no_existing_position"] = not any(p.get("symbol") == symbol and abs(float(p.get("positionAmt", 0))) > 0 for p in positions if isinstance(positions, list)) if isinstance(positions, list) else False
        mode = client.get_position_mode()
        checks["one_way_mode"] = not bool(mode.get("dualSidePosition", False))
        brackets_data = client.get_leverage_brackets(symbol)
        brackets = []
        if isinstance(brackets_data, list):
            for item in brackets_data:
                if item.get("symbol") == symbol:
                    brackets = item.get("brackets", []); break
        elif isinstance(brackets_data, dict):
            brackets = brackets_data.get("brackets", [])
        notional = qty * entry
        applicable = next((b for b in brackets if float(b.get("notionalFloor", 0)) <= notional <= float(b.get("notionalCap", 0))), None)
        maxlev = int(applicable.get("initialLeverage")) if applicable else None
        checks["bracket_leverage_valid"] = maxlev is not None and lev <= maxlev
        checks["margin_available"] = wallet > 0 and notional / lev <= available
        loss = abs(entry - signal["sl"]) * qty
        risk_pct = loss / wallet * 100 if wallet > 0 else float("inf")
        checks["risk_limit_valid"] = wallet > 0 and risk_pct <= RISK_LIMIT_PERCENT
        result.update({"checks": checks, "account_balance": {"available_balance": available, "wallet_balance": wallet}, "leverage_bracket": {"requested": lev, "maximum": maxlev}, "risk_estimate": {"estimated_loss_at_stop": loss, "risk_percent": risk_pct, "limit_percent": RISK_LIMIT_PERCENT, "fees_slippage_included": False}, "position_mode": mode})
        result["reasons"] = [k for k, v in checks.items() if not v]
        result["ready"] = all(checks.values())
        result["status"] = "READY" if result["ready"] else "REJECTED"
        return result
    except Exception as exc:
        result["reasons"].append(str(exc)); result["status"] = "ERROR"; return result


def confirm_position(client, symbol, attempts=5, delay=0.5):
    for _ in range(attempts):
        pos = client.get_open_position(symbol)
        if pos:
            return pos
        time.sleep(delay)
    return None


def verify_algo_orders(client, symbol, algo_ids):
    verified = []
    for aid in algo_ids:
        details = client.query_algo_order(symbol, aid)
        verified.append(details)
    return verified


def execute_for_account(signal, account_number):
    outcome = {"account": f"Account {account_number}", "status": "starting"}
    client = BinanceClient(account_number)
    entry_attempted = False
    try:
        pre = validate_signal(signal, account_number)
        outcome["validation"] = pre
        if not pre.get("ready"):
            outcome["status"] = "rejected_validation"; return outcome
        # Recheck just before order submission.
        if client.get_position_mode().get("dualSidePosition") or client.get_open_position(TARGET_SYMBOL):
            outcome["status"] = "rejected_final_position_or_mode_check"; return outcome
        lev = client.set_leverage(TARGET_SYMBOL, signal["leverage"])
        outcome["leverage"] = lev
        entry_side = "BUY" if signal["side"] == "LONG" else "SELL"
        closing_side = "SELL" if entry_side == "BUY" else "BUY"
        entry_attempted = True
        entry_order = client.market_entry(TARGET_SYMBOL, entry_side, signal["quantity"], "GB" + signal_fingerprint(signal)[:20])
        outcome["entry_order"] = entry_order
        position = confirm_position(client, TARGET_SYMBOL)
        if not position:
            # Entry outcome uncertain: do not submit another entry. Check order response/status.
            if entry_order.get("orderId"):
                outcome["entry_query"] = client.query_order(TARGET_SYMBOL, order_id=entry_order["orderId"])
            outcome["status"] = "CRITICAL_ENTRY_UNCONFIRMED"; return outcome
        amount = abs(float(position.get("positionAmt", 0)))
        fill_price = float(position.get("entryPrice", 0))
        if amount <= 0 or fill_price <= 0:
            outcome["status"] = "CRITICAL_POSITION_DATA_INVALID"; outcome["position"] = position; return outcome
        outcome["confirmed_position"] = position
        # Market-price drift is checked against actual fill; protective triggers must remain correctly sided.
        if signal["side"] == "LONG" and not (signal["sl"] < fill_price < signal["tp"]):
            raise RuntimeError("Actual fill is outside TP/SL price relationship; emergency close initiated")
        if signal["side"] == "SHORT" and not (signal["tp"] < fill_price < signal["sl"]):
            raise RuntimeError("Actual fill is outside TP/SL price relationship; emergency close initiated")
        sl_order = client.place_close_all_algo(TARGET_SYMBOL, closing_side, "STOP_MARKET", signal["sl"])
        outcome["sl_algo"] = sl_order
        tp_order = client.place_close_all_algo(TARGET_SYMBOL, closing_side, "TAKE_PROFIT_MARKET", signal["tp"])
        outcome["tp_algo"] = tp_order
        ids = [sl_order.get("algoId"), tp_order.get("algoId")]
        if any(x is None for x in ids):
            raise RuntimeError("Protective orders did not return both algo IDs")
        outcome["protective_order_verification"] = verify_algo_orders(client, TARGET_SYMBOL, ids)
        # Verify both orders appear in open algo orders and symbol/closePosition attributes.
        opened = client.get_open_algo_orders(TARGET_SYMBOL)
        open_ids = {str(x.get("algoId")) for x in opened} if isinstance(opened, list) else set()
        if not all(str(x) in open_ids for x in ids):
            raise RuntimeError("Protective orders not both present in open Algo orders")
        outcome["status"] = "EXECUTED_AND_PROTECTED"
        outcome["position_size"] = amount
        outcome["average_entry"] = fill_price
        return outcome
    except Exception as exc:
        outcome["error"] = str(exc)
        # If entry was attempted and exposure is confirmed, cancel algos and try reduce-only emergency close.
        if entry_attempted:
            try:
                pos = client.get_open_position(TARGET_SYMBOL)
                if pos:
                    try: client.cancel_all_open_algo_orders(TARGET_SYMBOL)
                    except Exception as cancel_exc: outcome["cancel_protection_error"] = str(cancel_exc)
                    close_result = client.close_position_market(TARGET_SYMBOL)
                    outcome["emergency_close"] = close_result
                    outcome["status"] = "EMERGENCY_CLOSE_ATTEMPTED" if close_result.get("confirmed_flat") else "CRITICAL_CLOSE_NOT_CONFIRMED"
                else:
                    outcome["status"] = "FAILED_NO_CONFIRMED_EXPOSURE"
            except Exception as recovery_exc:
                outcome["recovery_error"] = str(recovery_exc)
                outcome["status"] = "CRITICAL_RECOVERY_ERROR"
        else:
            outcome["status"] = "FAILED_BEFORE_ENTRY"
        return outcome


@app.get("/")
def home():
    return {"service": "Gold Trading Bridge", "status": "running", "mode": "BINANCE_DEMO", "execution_enabled": EXECUTION_ENABLED, "target": TARGET_SYMBOL}

@app.get("/health")
def health():
    return {"status": "healthy", "execution_enabled": EXECUTION_ENABLED, "mode": "BINANCE_DEMO"}

@app.get("/diagnostic")
def diagnostic():
    results = {}
    for name, endpoint in (("ping", "/fapi/v1/ping"), ("time", "/fapi/v1/time")):
        try:
            r = requests.get(BINANCE_BASE_URL + endpoint, timeout=10); results[name] = {"http_status": r.status_code, "success": r.status_code == 200, "response": r.text[:500]}
        except Exception as exc: results[name] = {"success": False, "error": str(exc)}
    return {"results": results, "execution_enabled": EXECUTION_ENABLED}

@app.get("/test-connections")
def test_connections():
    results=[]
    for n in (1,2):
        try: results.append(BinanceClient(n).test_connection())
        except Exception as exc: results.append({"account":f"Account {n}","status":"error","error":str(exc)})
    return {"results":results,"execution_enabled":EXECUTION_ENABLED}

@app.get("/account-config")
def account_config():
    results=[]
    for n in (1,2):
        try:
            c=BinanceClient(n); results.append({"account":f"Account {n}","position_mode":c.get_position_mode(),"positions":c.get_position_risk(TARGET_SYMBOL),"brackets":c.get_leverage_brackets(TARGET_SYMBOL)})
        except Exception as exc: results.append({"account":f"Account {n}","error":str(exc)})
    return {"results":results,"execution_enabled":EXECUTION_ENABLED}

@app.get("/validate-contract")
def validate_contract(symbol: str = TARGET_SYMBOL, quantity: float = 1, price: float = 3500):
    try:
        c=BinanceClient(1); data=symbol_info(c,symbol.upper())
        if not data: return {"status":"rejected","reason":"Symbol not found","execution_enabled":EXECUTION_ENABLED}
        f=filters_for(data); lot,mlot,pf=f.get("LOT_SIZE",{}),f.get("MARKET_LOT_SIZE",{}),f.get("PRICE_FILTER",{}); nf=f.get("MIN_NOTIONAL",f.get("NOTIONAL",{})); mn=float(nf.get("notional",nf.get("minNotional",0)))
        checks={"trading":data.get("status")=="TRADING","lot":float(lot.get("minQty",0))<=quantity<=float(lot.get("maxQty",0)) and check_step(quantity,float(lot.get("stepSize",0))),"market_lot":float(mlot.get("minQty",0))<=quantity<=float(mlot.get("maxQty",0)) and check_step(quantity,float(mlot.get("stepSize",0))),"price":check_step(price,float(pf.get("tickSize",0))),"notional":quantity*price>=mn}
        return {"status":"validated" if all(checks.values()) else "rejected","symbol":symbol,"quantity":quantity,"price":price,"checks":checks,"execution_enabled":EXECUTION_ENABLED}
    except Exception as exc: return {"status":"error","error":str(exc),"execution_enabled":EXECUTION_ENABLED}

@app.post("/validate-signal")
async def validate_only(request: Request):
    try:
        signal,error=parse_signal((await request.body()).decode())
        if error: return JSONResponse(status_code=400,content={"status":"rejected","reason":error,"execution_enabled":EXECUTION_ENABLED})
        results=[validate_signal(signal,n) for n in (1,2)]
        return {"status":"multi_account_validation","signal":signal,"accounts":results,"execution_enabled":EXECUTION_ENABLED}
    except Exception as exc: return JSONResponse(status_code=400,content={"status":"error","detail":str(exc),"execution_enabled":EXECUTION_ENABLED})

@app.post("/webhook")
async def webhook(request: Request):
    try:
        raw=(await request.body()).decode("utf-8").strip()
        try: payload=json.loads(raw)
        except json.JSONDecodeError: payload=None
        if isinstance(payload,dict) and str(payload.get("type","")).upper()=="KEEPALIVE":
            log_event({"event":"keepalive_received","source":payload.get("source","unknown")})
            return {"status":"keepalive_received","trading":False,"binance_contacted":False}
        signal,error=parse_signal(raw)
        if error: return JSONResponse(status_code=400,content={"status":"rejected","reason":error,"execution_enabled":EXECUTION_ENABLED})
        if not EXECUTION_ENABLED: return JSONResponse(status_code=503,content={"status":"execution_disabled","mode":"BINANCE_DEMO","execution_enabled":False})
        accepted,fp=reserve_signal(signal)
        if not accepted: return JSONResponse(status_code=409,content={"status":"duplicate_rejected","execution_enabled":True})
        # Preflight both accounts before any order is sent.
        preflight=[validate_signal(signal,n) for n in (1,2)]
        if not all(x.get("ready") for x in preflight):
            return JSONResponse(status_code=400,content={"status":"rejected_preflight","accounts":preflight,"execution_enabled":True})
        # Sequential execution; failures on one account do not imply success on the other.
        results=[execute_for_account(signal,n) for n in (1,2)]
        response={"status":"multi_account_execution","signal":signal,"accounts":results,"execution_enabled":True,"demo_only":True}
        log_event({"event":"execution_result","fingerprint":fp,"results":results})
        return response
    except Exception as exc:
        log_event({"event":"webhook_error","error":str(exc)})
        return JSONResponse(status_code=400,content={"status":"error","detail":str(exc),"execution_enabled":EXECUTION_ENABLED})
