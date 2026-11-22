# TradingView → Binance Demo Bridge

A small webhook service that receives alerts from the **Gold M1 Breakout Strategy v1.4.0** Pine script and executes them on **Binance USD-M Futures Demo**. TradingView makes every trading decision. The bridge only authenticates, validates and executes.

```
TradingView alert → POST /webhook → auth → parse → duplicate check → queue → Binance Demo → status record
```

No setting is hardcoded. Every value comes from environment variables, and the service refuses to start if one is missing. A built-in guard also refuses any `BINANCE_BASE_URL` that is not a Binance demo/testnet host, so live trading is impossible.

## How it reads your Pine script

The strategy sends **plain text**, not JSON, using `{{strategy.order.alert_message}}`. The bridge parses these lines:

| Alert line | Used as |
|---|---|
| `Symbol: XAUUSDT.P` | symbol (`.P` / exchange prefix stripped, then must equal `TRADE_SYMBOL`) |
| `Side: LONG` / `SHORT` | direction → BUY / SELL |
| `Leverage: Isolated 20x` | margin mode (`ISOLATED`/`CROSSED`) and leverage, applied before the entry |
| `Quantity: 1` | order quantity (validated against Binance lot rules, never altered) |
| `Entry price` | validation of SL/TP sides, notional check, optional deviation guard (market order is used) |
| `Stop Loss` / `Take Profit` | placed as `closePosition` STOP_MARKET / TAKE_PROFIT_MARKET orders |
| `Estimated margin` | balance pre-check (`CHECK_AVAILABLE_BALANCE`) |
| `Signal time` | part of the duplicate key |
| `/close_long`, `/close_short` | market close (reduce-only) of that side, then cleanup of leftover orders |

A JSON payload (`event`, `direction`, `quantity`, `entry_price`, `stop_loss`, `take_profit`, `leverage`, `margin_mode`, `signal_id`, `secret`, `force_close`) is also accepted.

The alert has no `signal_id`, so the duplicate key is a SHA-256 of the alert's content (side, symbol, quantity, prices, leverage, signal time). A retried alert has the same key and is never executed twice.

## ENTRY behaviour

1. Authenticate, parse, claim the duplicate key in SQLite, then queue and reply `202` immediately. TradingView drops webhook calls that take more than a few seconds, so execution runs on a background worker.
2. Validate: symbol equals `TRADE_SYMBOL` and is `TRADING` on the demo endpoint, One-way mode, quantity matches step and min/max/notional, SL/TP on the correct side of entry.
3. Ask Binance (the authority) for the position. If one is open, the entry is **rejected**. Also reject if an order with this signal's client id already exists.
4. Optional guards: balance versus estimated margin, mark-price deviation.
5. While flat, clear leftover orders (optional), set margin type and leverage from the alert, and confirm the leverage applied.
6. Send a MARKET order with a deterministic client order id, then confirm the fill and the position from Binance. A request that times out is checked against the exchange, not assumed.
7. Place SL and TP, then verify both exist. **If protection cannot be placed, the position is closed immediately** (`FLATTEN_ON_PROTECTION_FAILURE=true`) and the failure is recorded.

## Deploy

1. **Binance**: in Binance Demo Trading, create an API key for USD-M Futures. Sign the TradFi-Perps agreement on that account if XAUUSDT requires it. Keep the account in One-way position mode.
2. **GitHub**: create a private repo and push this folder. `.env` is git-ignored.
3. **Render**: New → Blueprint (reads `render.yaml`) or New → Web Service with
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60`
   - Health check path: `/health`
   - A **Disk** mounted at `/var/data`
4. In the Environment tab, add every variable below (see `.env.example`).
5. Open `https://<your-service>.onrender.com/health`. `symbol_ready: true` means XAUUSDT was found and is trading on the demo endpoint. If it is `false`, entries are refused until that is fixed.

**Use a paid, always-on instance.** Render's free instances sleep when idle and would miss M1 alerts. They also have no persistent disk.

## Environment variables (all required)

| Variable | Meaning |
|---|---|
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | Demo API credentials |
| `BINANCE_BASE_URL` | Demo host, e.g. `https://demo-fapi.binance.com` (any non-demo host is refused) |
| `WEBHOOK_SECRET` | 16+ char random string used in the webhook URL |
| `TRADE_SYMBOL` | The single tradable symbol, e.g. `XAUUSDT` |
| `DB_PATH` | SQLite file for the execution record, e.g. `/var/data/bridge.db` |
| `RECV_WINDOW_MS`, `HTTP_TIMEOUT_SECONDS` | Binance request tuning |
| `ORDER_ID_PREFIX` | 1–8 chars prefixed to client order ids |
| `PROTECTIVE_ORDER_MODE` | `auto` (algo endpoint, classic only if it does not exist), `algo`, or `classic` |
| `TRIGGER_WORKING_TYPE` | `MARK_PRICE` or `CONTRACT_PRICE` for SL/TP triggers |
| `FLATTEN_ON_PROTECTION_FAILURE` | Close the position if SL/TP cannot be placed |
| `CANCEL_STALE_ORDERS_BEFORE_ENTRY` | While flat, cancel all open/conditional orders for the symbol before entering |
| `CHECK_AVAILABLE_BALANCE` | Reject if available balance is below the alert's estimated margin |
| `MAX_ENTRY_DEVIATION_PCT` | Reject if mark price is further than this % from alert entry (`0` = off) |
| `ORDER_CONFIRM_RETRIES`, `ORDER_CONFIRM_DELAY_MS` | How long to wait for Binance to confirm fills and positions |
| `EXCHANGE_INFO_TTL_SECONDS` | Cache time for symbol rules |
| `STATUS_HISTORY_LIMIT` | Rows returned by `/status` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## TradingView alert setup

- Condition: the strategy, **Order fills**
- Message: exactly `{{strategy.order.alert_message}}`
- Webhook URL: `https://<your-service>.onrender.com/webhook?secret=<WEBHOOK_SECRET>`

TradingView cannot send custom headers, so the secret goes in the URL. The bridge accepts it as `?secret=`, as an `X-Webhook-Secret` header, or as a `secret` field in a JSON body. Webhooks require TradingView 2FA and a paid plan.

## Endpoints

- `POST /webhook`: `202` accepted, `200` duplicate, `400` unreadable alert, `401` bad secret
- `GET /health`: liveness and symbol readiness (no secret needed, exposes nothing sensitive)
- `GET /status?secret=...`: recent executions with status and result codes

Result statuses: `EXECUTED`, `REJECTED` (nothing sent to Binance), `FAILED` (read the `code`), `NOOP` (close with nothing to close), `INTERRUPTED` (cut off by a restart, never auto-retried, check Binance).

## Things to know

- **Breakeven and Partial modes are not mirrored.** The Pine script moves its stop internally (`moveToBE`) without sending an alert, and in `Partial` mode the alert carries only TP1. The bridge keeps the original SL and the single TP. Leave `moveToBE` off and avoid `Partial` mode.
- **Strategy exits are not alerted** (`disable_alert=true`). Binance's own SL/TP orders close the trade. TradingView's simulated fills and Binance's real fills can differ, so TradingView may consider itself flat while Binance is not. Such entries are rejected as `POSITION_OPEN`, by design.
- **Quantity units** must match the Binance contract. The bridge rejects a quantity that is not a valid multiple of the exchange step, and does not round it.
- **SL/TP prices** are rounded to the exchange tick size (formatting only).
- **Conditional orders** on Binance USD-M now go through the algo endpoint (`/fapi/v1/algoOrder`). I could not confirm from public docs that the demo environment serves it, so `auto` mode falls back to the classic endpoint only if the algo one does not exist. If SL/TP placement fails, the entry is closed and recorded.
- **Execution record**: with a Render Disk it survives restarts. Without one, duplicate protection still works through the position check and the deterministic client order id on Binance.
- **Single worker**: keep `--workers 1` so the order queue and SQLite record stay consistent.

## Local checks

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests are offline (mock Binance and a local mock HTTP server) and cover parsing, validation, entry, duplicate handling, protection failure, closes, signing, redaction and config guards.
