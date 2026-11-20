
            "result": result

        })

        return result

    except Exception as e:

        log_event({

            "event": "webhook_error",

            "error": str(e)

        })

        return JSONResponse(

            status_code=400,

            content={

                "status": "error",

                "detail": str(e),

                "execution_enabled": False

            }

        )@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        message = body.decode("utf-8").strip()
        signal, error = parse_signal(message)

        if error:
            return JSONResponse(status_code=400, content={
                "status": "rejected", "reason": error,
                "execution_enabled": EXECUTION_ENABLED
            })

        fingerprint = signal_fingerprint(signal)
        now = time.time()

        expired = [
            key for key, timestamp in SEEN_SIGNALS.items()
            if now - timestamp > DUPLICATE_WINDOW_SECONDS
        ]
        for key in expired:
            SEEN_SIGNALS.pop(key, None)

        if fingerprint in SEEN_SIGNALS:
            return JSONResponse(status_code=409, content={
                "status": "duplicate_rejected",
                "reason": "Duplicate signal detected within duplicate window",
                "execution_enabled": EXECUTION_ENABLED
            })

        # Independently validate and configure leverage for each account.
        account_results = []
        any_account_processed = False

        for account_number in (1, 2):
            account_result = {
                "account": f"Account {account_number}",
                "status": "pending",
                "validation": None,
                "leverage_result": None,
                "execution_enabled": EXECUTION_ENABLED
            }

            validation = validate_signal(signal, account_number=account_number)
            account_result["validation"] = validation

            if not validation.get("ready"):
                account_result["status"] = "rejected"
                account_result["reasons"] = validation.get("reasons", [])
                account_results.append(account_result)
                continue

            try:
                client = BinanceClient(account_number)
                leverage_result = client.apply_and_verify_leverage(
                    TARGET_SYMBOL, signal["leverage"]
                )
                account_result["leverage_result"] = leverage_result
                account_result["status"] = "validated_and_leverage_verified"
                any_account_processed = True
            except Exception as account_error:
                account_result["status"] = "error"
                account_result["error"] = str(account_error)

            account_results.append(account_result)

        # Record the signal only if at least one account successfully passed
        # validation and had its requested leverage applied and verified.
        if any_account_processed:
            SEEN_SIGNALS[fingerprint] = time.time()

        response = {
            "status": "multi_account_signal_processed",
            "signal": signal,
            "accounts": account_results,
            "execution_enabled": EXECUTION_ENABLED,
            "message": "Accounts processed independently. No orders placed."
        }

        log_event({
            "event": "multi_account_webhook_processed",
            "signal": signal,
            "result": response
        })

        return response

    except Exception as e:
        log_event({"event": "webhook_error", "error": str(e)})
        return JSONResponse(status_code=400, content={
            "status": "error", "detail": str(e),
            "execution_enabled": EXECUTION_ENABLED
        })
