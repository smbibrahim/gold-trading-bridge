
import os
import re
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from binance_client import BinanceClient

app = FastAPI(title="Gold Trading Bridge")


@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "Gold Trading Bridge",
        "mode": "binance-connection-test"
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


def extract_field(message, label):
    pattern = rf"{re.escape(label)}\s*:\s*([^\n]+)"
    match = re.search(pattern, message, re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_signal(message):
    message_upper = message.upper()

    if "LONG ENTRY" in message_upper:
        side = "LONG"
    elif "SHORT ENTRY" in message_upper:
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


@app.get("/test-binance")
async def test_binance():

    key = os.getenv("BINANCE_API_KEY_1")
    secret = os.getenv("BINANCE_API_SECRET_1")

    if not key or not secret:
        return JSONResponse(
            status_code=400,
            content={
                "status": "not_configured",
                "account": "Account 1"
            }
        )

    try:
        client = BinanceClient()
        result = client.test_connection()

        return result

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "detail": str(e)
            }
        )


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

        account_1_configured = bool(
            os.getenv("BINANCE_API_KEY_1")
            and os.getenv("BINANCE_API_SECRET_1")
        )

        account_2_configured = bool(
            os.getenv("BINANCE_API_KEY_2")
            and os.getenv("BINANCE_API_SECRET_2")
        )

        return JSONResponse(
            content={
                "status": "signal_validated",
                "signal": signal,
                "accounts": [
                    {
                        "account": "Account 1",
                        "status": (
                            "credentials_configured"
                            if account_1_configured
                            else "not_configured"
                        ),
                        "execution": "disabled"
                    },
                    {
                        "account": "Account 2",
                        "status": (
                            "credentials_configured"
                            if account_2_configured
                            else "not_configured"
                        ),
                        "execution": "disabled"
                    }
                ],
                "execution_enabled": False
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
