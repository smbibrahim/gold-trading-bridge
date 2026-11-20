import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Gold Trading Bridge")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")


@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "Gold Trading Bridge",
        "mode": "development"
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):

    if not WEBHOOK_SECRET:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Webhook secret not configured"}
        )

    if secret != WEBHOOK_SECRET:
        return JSONResponse(
            status_code=401,
            content={"status": "error", "detail": "Unauthorized"}
        )

    try:
        body = await request.body()
        message = body.decode("utf-8")

        print("Webhook received:", message)

        return JSONResponse(
            content={
                "status": "received",
                "message": "Webhook received successfully"
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
