import re


def parse_signal(message: str):
    message = message.strip()

    side = None

    if "LONG ENTRY" in message:
        side = "LONG"
    elif "SHORT ENTRY" in message:
        side = "SHORT"

    if side is None:
        return {
            "valid": False,
            "reason": "Entry signal not detected"
        }

    def extract(label):
        pattern = rf"{label}\s*:\s*([^\n]+)"
        match = re.search(pattern, message, re.IGNORECASE)
        return match.group(1).strip() if match else None

    signal = {
        "valid": True,
        "side": side,
        "symbol": extract("Symbol"),
        "exchange": extract("Exchange"),
        "timeframe": extract("Timeframe"),
        "leverage": extract("Leverage"),
        "quantity": extract("Quantity"),
        "entry": extract("Entry price"),
        "take_profit": extract("Take Profit"),
        "stop_loss": extract("Stop Loss"),
        "raw_message": message
    }

    return signal
