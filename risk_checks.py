def validate_signal(signal):
    if not signal or not signal.get("valid"):
        return False, "Invalid signal"

    required_fields = [
        "side",
        "symbol",
        "quantity",
        "entry",
        "take_profit",
        "stop_loss",
        "leverage"
    ]

    for field in required_fields:
        if not signal.get(field):
            return False, f"Missing field: {field}"

    if signal["side"] not in ("LONG", "SHORT"):
        return False, "Invalid trade direction"

    try:
        quantity = float(signal["quantity"])
        entry = float(signal["entry"])
        take_profit = float(signal["take_profit"])
        stop_loss = float(signal["stop_loss"])
        leverage_text = signal["leverage"]

        leverage = int(
            "".join(
                character for character in leverage_text
                if character.isdigit()
            )
        )

    except (ValueError, TypeError):
        return False, "Invalid numerical values"

    if quantity <= 0:
        return False, "Quantity must be positive"

    if entry <= 0 or take_profit <= 0 or stop_loss <= 0:
        return False, "Prices must be positive"

    if leverage < 1 or leverage > 125:
        return False, "Leverage outside permitted range"

    if signal["side"] == "LONG":
        if not stop_loss < entry < take_profit:
            return False, "Invalid LONG price relationship"

    if signal["side"] == "SHORT":
        if not take_profit < entry < stop_loss:
            return False, "Invalid SHORT price relationship"

    return True, "Signal passed validation"
