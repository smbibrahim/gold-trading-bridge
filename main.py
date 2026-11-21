import os, json, re, time, hashlib, hmac, logging, threading
from typing import Any
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from binance_client import BinanceClient

app = FastAPI(title="Gold Trading Bridge - Binance Demo Execution")
BINANCE_BASE_URL = "https://demo-fapi.binance.com"  # demo-only
TARGET_SYMBOL = "XAUUSDT"
EXECUTION_ENABLED = os.getenv("EXECUTION_ENABLED", "false").lower() == "true"
RISK_LIMIT_PERCENT = float(os.getenv("RISK_LIMIT_PERCENT", "2"))
DUPLICATE_WINDOW_SECONDS = 300
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
EXECUTION_LOCK = threading.Lock()  # serializes entry/forced-close tasks across both accounts
SIGNAL_STATUS = {}
STATUS_LOCK = threading.Lock()
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


def release_signal(fp):
    with LOCK:
        SEEN_SIGNALS.pop(fp, None)


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


def active_accounts():
    return [n for n in (1, 2) if os.getenv(f"BINANCE_API_KEY_{n}") and os.getenv(f"BINANCE_API_SECRET_{n}")]


def token_ok(request):
    if not WEBHOOK_SECRET:
        return True
    supplied = request.headers.get("X-Webhook-Token", "")
    return hmac.compare_digest(supplied.encode(), WEBHOOK_SECRET.encode())


def set_status(fp, status, detail=None):
    with STATUS_LOCK:
        SIGNAL_STATUS[fp] = {"status": status, "updated_at": time.time(), "detail": detail}


def get_status(fp):
    with STATUS_LOCK:
        return SIGNAL_STATUS.get(fp)


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
            "leverage": leverage, "margin_mode": ("ISOLATED" if "isolated" in str(lev_value).lower() else "CROSS"), "quantity": quantity, "entry": entry, "tp": tp, "sl": sl,
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


def verify_algo_orders(client, symbol, expected_orders, attempts=4, delay=1.0):
    """Verify protective orders from the open Algo-order listing, not fragile per-ID queries.
    Binance Algo responses use orderType (not type) and may vary in field naming.
    """
    expected = {str(o.get("algoId")): o for o in expected_orders if o.get("algoId") is not None}
    last_orders = []
    for attempt in range(attempts):
        listing = client.get_open_algo_orders(symbol)
        if not isinstance(listing, list):
            raise RuntimeError(f"Unexpected open Algo-order response type: {type(listing).__name__}")
        last_orders = listing
        matched = []
        for aid, submitted in expected.items():
            match = next((a for a in listing if str(a.get("algoId")) == aid), None)
            if not match:
                continue
            order_type = str(match.get("orderType", match.get("type", ""))).upper()
            valid = (match.get("symbol") == symbol
                     and order_type == str(submitted.get("orderType", submitted.get("type", ""))).upper()
                     and str(match.get("side", "")).upper() == str(submitted.get("side", "")).upper()
                     and str(match.get("closePosition", "")).lower() in ("true", "1")
                     and str(match.get("algoStatus", "NEW")).upper() in ("NEW", "WORKING", "ACTIVE"))
            matched.append({"algoId": aid, "matched": bool(valid), "details": match})
        if len(matched) == len(expected) and all(x["matched"] for x in matched):
            return {"verified": True, "attempt": attempt + 1, "orders": matched, "open_orders": listing}
        if attempt < attempts - 1:
            time.sleep(delay)
    return {"verified": False, "attempts": attempts, "orders": matched if expected else [], "open_orders": last_orders}


def execute_for_account(signal, account_number, pre=None):
    outcome = {"account": f"Account {account_number}", "status": "starting"}
    client = BinanceClient(account_number)
    entry_attempted = False
    try:
        pre = pre or validate_signal(signal, account_number)
        outcome["validation"] = pre
        if not pre.get("ready"):
            outcome["status"] = "rejected_validation"; return outcome
        # Recheck just before order submission.
        if client.get_position_mode().get("dualSidePosition") or client.get_open_position(TARGET_SYMBOL):
            outcome["status"] = "rejected_final_position_or_mode_check"; return outcome
        # Safe stale Algo cleanup only after the account is confirmed flat.
        try:
            client.cancel_all_open_algo_orders(TARGET_SYMBOL)
            outcome["stale_algo_cleanup"] = "attempted_after_flat_check"
        except Exception as cleanup_exc:
            outcome["stale_algo_cleanup_warning"] = str(cleanup_exc)
        margin_result = client.set_margin_type(TARGET_SYMBOL, signal.get("margin_mode", "CROSS"))
        outcome["margin_type"] = margin_result
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
        if sl_order.get("algoId") is None or tp_order.get("algoId") is None:
            raise RuntimeError("Protective orders did not return both algo IDs")
        outcome["protective_order_verification"] = verify_algo_orders(client, TARGET_SYMBOL, [sl_order, tp_order])
        if not outcome["protective_order_verification"].get("verified"):
            raise RuntimeError("Protective orders were not confirmed in repeated open Algo-order listings")
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





# Lifecycle monitor globals (required before load/startup)
LIFECYCLE_LOCK = threading.Lock()
LIFECYCLE_SNAPSHOT = {}
MONITOR_INTERVAL = max(5, int(os.getenv("LIFECYCLE_MONITOR_INTERVAL", "10")))
MONITOR_STARTED = False

# FINAL PATCH: lifecycle reconciliation and closure-event journaling
# State is written to LIFECYCLE_STATE_FILE (default /tmp on Render).
# /tmp is ephemeral on Render unless a persistent disk is mounted.
LIFECYCLE_STATE_FILE = os.getenv("LIFECYCLE_STATE_FILE", "/tmp/gold_bridge_lifecycle_state.json")
LIFECYCLE_EVENTS = []
LIFECYCLE_EVENT_LIMIT = 500
ACCOUNT_LAST_STATE = {}

def persist_lifecycle_state():
    try:
        with LIFECYCLE_LOCK:
            payload = {"last_check": LIFECYCLE_SNAPSHOT.get("last_check"),
                       "accounts": LIFECYCLE_SNAPSHOT.get("accounts", []),
                       "events": LIFECYCLE_EVENTS[-LIFECYCLE_EVENT_LIMIT:],
                       "account_last_state": ACCOUNT_LAST_STATE}
        tmp = LIFECYCLE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
        os.replace(tmp, LIFECYCLE_STATE_FILE)
    except Exception as exc:
        log_event({"event": "lifecycle_state_persist_error", "error": str(exc)})

def load_lifecycle_state():
    try:
        with open(LIFECYCLE_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        with LIFECYCLE_LOCK:
            LIFECYCLE_SNAPSHOT["last_check"] = payload.get("last_check")
            LIFECYCLE_SNAPSHOT["accounts"] = payload.get("accounts", [])
            LIFECYCLE_EVENTS[:] = payload.get("events", [])[-LIFECYCLE_EVENT_LIMIT:]
            ACCOUNT_LAST_STATE.update(payload.get("account_last_state", {}))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_event({"event": "lifecycle_state_load_error", "error": str(exc)})

def record_lifecycle_event(event):
    event["timestamp"] = time.time()
    with LIFECYCLE_LOCK:
        LIFECYCLE_EVENTS.append(event)
        del LIFECYCLE_EVENTS[:-LIFECYCLE_EVENT_LIMIT]
    log_event(event)

def inspect_account_lifecycle(account_number):
    client = BinanceClient(account_number)
    position = client.get_open_position(TARGET_SYMBOL)
    algos = client.get_open_algo_orders(TARGET_SYMBOL)
    algos = algos if isinstance(algos, list) else []
    account_label = f"Account {account_number}"
    if not position:
        stale = len(algos) > 0
        cleanup = None
        if stale:
            try:
                cleanup = client.cancel_all_open_algo_orders(TARGET_SYMBOL)
                algos = client.get_open_algo_orders(TARGET_SYMBOL)
                algos = algos if isinstance(algos, list) else []
            except Exception as exc:
                cleanup = {"error": str(exc)}
        return {"account": account_label, "account_number": account_number, "state": "FLAT",
                "position": None, "open_algo_count": len(algos), "stale_algo_cleanup": cleanup,
                "flat_with_stale_algos": bool(algos), "algo_orders": algos}
    amt = float(position.get("positionAmt", 0))
    direction = "LONG" if amt > 0 else "SHORT"
    close_side = "SELL" if direction == "LONG" else "BUY"
    relevant = [a for a in algos if str(a.get("side", "")).upper() == close_side
                and str(a.get("orderType", a.get("type", ""))).upper() in ("STOP_MARKET", "TAKE_PROFIT_MARKET")]
    sl_present = any(str(a.get("orderType", a.get("type", ""))).upper() == "STOP_MARKET" for a in relevant)
    tp_present = any(str(a.get("orderType", a.get("type", ""))).upper() == "TAKE_PROFIT_MARKET" for a in relevant)
    return {"account": account_label, "account_number": account_number, "state": "OPEN", "direction": direction,
            "position_amount": amt, "entry_price": position.get("entryPrice"),
            "sl_present": sl_present, "tp_present": tp_present,
            "protected": sl_present and tp_present, "open_algo_count": len(algos),
            "position": position, "algo_orders": relevant,
            "warning": None if sl_present and tp_present else "OPEN_POSITION_MISSING_PROTECTION"}

def reconcile_lifecycle(snapshots):
    now = time.time()
    for snap in snapshots:
        n = snap.get("account_number")
        if not n:
            match = re.search(r"(\\d+)$", str(snap.get("account", "")))
            n = int(match.group(1)) if match else None
        if n is None:
            continue
        previous = ACCOUNT_LAST_STATE.get(str(n))
        current = snap.get("state")
        if previous is None:
            record_lifecycle_event({"event": "LIFECYCLE_BASELINE", "account": snap.get("account"),
                                   "state": current, "timestamp": now,
                                   "note": "First observed state; no prior state available for transition attribution"})
        elif previous.get("state") == "OPEN" and current == "FLAT":
            record_lifecycle_event({"event": "POSITION_CLOSED_DETECTED", "account": snap.get("account"),
                                   "previous_direction": previous.get("direction"),
                                   "previous_position_amount": previous.get("position_amount"),
                                   "previous_entry_price": previous.get("entry_price"),
                                   "previous_protection": previous.get("algo_orders", []),
                                   "closure_reason": "EXCHANGE_POSITION_FLAT; trigger attribution not confirmed",
                                   "timestamp": now})
        elif previous.get("state") == "FLAT" and current == "OPEN":
            record_lifecycle_event({"event": "POSITION_OPEN_DETECTED", "account": snap.get("account"),
                                   "direction": snap.get("direction"), "position_amount": snap.get("position_amount"),
                                   "entry_price": snap.get("entry_price"), "timestamp": now})
        elif previous.get("state") == "OPEN" and current == "OPEN":
            changed = (previous.get("direction") != snap.get("direction") or
                       abs(float(previous.get("position_amount", 0)) - float(snap.get("position_amount", 0))) > 1e-8)
            if changed:
                record_lifecycle_event({"event": "POSITION_CHANGED_DETECTED", "account": snap.get("account"),
                                       "previous": previous, "current": snap, "timestamp": now})
        if snap.get("warning") or snap.get("flat_with_stale_algos") or current == "MONITOR_ERROR":
            record_lifecycle_event({"event": "LIFECYCLE_WARNING", "snapshot": snap, "timestamp": now})
        ACCOUNT_LAST_STATE[str(n)] = {"state": current, "direction": snap.get("direction"),
                                     "position_amount": snap.get("position_amount"),
                                     "entry_price": snap.get("entry_price"),
                                     "algo_orders": snap.get("algo_orders", []), "observed_at": now}

def lifecycle_monitor_loop():
    while True:
        try:
            accounts = active_accounts()
            snapshots = []
            for n in accounts:
                try:
                    snapshots.append(inspect_account_lifecycle(n))
                except Exception as exc:
                    snapshots.append({"account": f"Account {n}", "account_number": n,
                                     "state": "MONITOR_ERROR", "error": str(exc)})
            with LIFECYCLE_LOCK:
                LIFECYCLE_SNAPSHOT["last_check"] = time.time()
                LIFECYCLE_SNAPSHOT["accounts"] = snapshots
            reconcile_lifecycle(snapshots)
            open_states = [x for x in snapshots if x.get("state") == "OPEN"]
            if len(open_states) > 1:
                dirs = {x.get("direction") for x in open_states}
                sizes = [abs(float(x.get("position_amount", 0))) for x in open_states]
                if len(dirs) > 1 or (sizes and max(sizes)-min(sizes) > 1e-8):
                    record_lifecycle_event({"event": "ACCOUNT_POSITION_DIVERGENCE", "accounts": snapshots})
            with LIFECYCLE_LOCK:
                LIFECYCLE_SNAPSHOT["events_count"] = len(LIFECYCLE_EVENTS)
            persist_lifecycle_state()
        except Exception as exc:
            log_event({"event": "lifecycle_monitor_loop_error", "error": str(exc)})
        time.sleep(MONITOR_INTERVAL)

@app.get("/lifecycle-events")
def lifecycle_events(request: Request, limit: int = 100):
    if not token_ok(request):
        return JSONResponse(status_code=401, content={"status": "unauthorized"})
    limit = max(1, min(int(limit), LIFECYCLE_EVENT_LIMIT))
    with LIFECYCLE_LOCK:
        return {"mode": "BINANCE_DEMO", "events": LIFECYCLE_EVENTS[-limit:], "total": len(LIFECYCLE_EVENTS)}

@app.get("/reconciliation")
def reconciliation(request: Request):
    if not token_ok(request):
        return JSONResponse(status_code=401, content={"status": "unauthorized"})
    with LIFECYCLE_LOCK:
        return {"mode": "BINANCE_DEMO", "last_check": LIFECYCLE_SNAPSHOT.get("last_check"),
                "accounts": LIFECYCLE_SNAPSHOT.get("accounts", []),
                "events_count": len(LIFECYCLE_EVENTS),
                "note": "Exchange position state is authoritative. Closure trigger attribution is not guaranteed by this monitor."}

load_lifecycle_state()

# Start the lifecycle monitor in a daemon thread (one monitor per service process).
if not MONITOR_STARTED:
    MONITOR_STARTED = True
    threading.Thread(target=lifecycle_monitor_loop, daemon=True, name="lifecycle-monitor").start()
