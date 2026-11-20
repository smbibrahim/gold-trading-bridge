
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Gold Trading Bridge")


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


@app.post("/webhook")
async def webhook(request: Request):
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
