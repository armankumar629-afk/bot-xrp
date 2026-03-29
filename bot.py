"""
Black Cat Scalping Bot
TradingView Webhook → Bitget Execution → Telegram Notifications
Pair: XRP/USDT | Leverage: 15x | Risk: 10% per trade
"""

import os
import json
import time
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "blackcat2024")

SYMBOL = "XRPUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
LEVERAGE = 15
RISK_PERCENT = 10  # % of balance per trade
MARGIN_MODE = "crossed"

BITGET_BASE_URL = "https://api.bitget.com"

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("BlackCatBot")

# ═══════════════════════════════════════════════════════════════
# SIGNAL DEDUPLICATION
# ═══════════════════════════════════════════════════════════════
last_signal = {"side": None, "timestamp": 0}
COOLDOWN_SECONDS = 60  # ignore duplicate signals within 60s


# ═══════════════════════════════════════════════════════════════
# BITGET API HELPERS
# ═══════════════════════════════════════════════════════════════
def get_timestamp():
    return str(int(time.time() * 1000))


def sign_request(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body
    signature = hmac.new(
        BITGET_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()
    import base64
    return base64.b64encode(signature).decode("utf-8")


def get_headers(method, request_path, body=""):
    timestamp = get_timestamp()
    signature = sign_request(timestamp, method, request_path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }


async def bitget_request(method, endpoint, body=None):
    """Make authenticated request to Bitget API v2."""
    body_str = json.dumps(body) if body else ""
    headers = get_headers(method, endpoint, body_str)
    url = BITGET_BASE_URL + endpoint

    async with httpx.AsyncClient(timeout=10) as client:
        if method == "GET":
            response = await client.get(url, headers=headers)
        else:
            response = await client.post(url, headers=headers, content=body_str)

    data = response.json()
    if data.get("code") != "00000":
        logger.error(f"Bitget API error: {data}")
        raise Exception(f"Bitget API error: {data.get('msg', 'Unknown error')}")
    return data.get("data")


# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════
async def send_telegram(message: str):
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping notification")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Telegram error: {resp.text}")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


# ═══════════════════════════════════════════════════════════════
# TRADING FUNCTIONS
# ═══════════════════════════════════════════════════════════════
async def get_account_balance():
    """Get available USDT balance for futures."""
    endpoint = "/api/v2/mix/account/account"
    params = f"?symbol={SYMBOL}&productType={PRODUCT_TYPE}&marginCoin=USDT"
    data = await bitget_request("GET", endpoint + params)
    available = float(data.get("crossedMaxAvailable", 0))
    total = float(data.get("accountEquity", 0))
    logger.info(f"Balance - Total: {total} USDT | Available: {available} USDT")
    return available, total


async def set_leverage():
    """Set leverage for the symbol."""
    endpoint = "/api/v2/mix/account/set-leverage"
    for hold_side in ["long", "short"]:
        body = {
            "symbol": SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginCoin": "USDT",
            "leverage": str(LEVERAGE),
            "holdSide": hold_side
        }
        try:
            await bitget_request("POST", endpoint, body)
            logger.info(f"Leverage set to {LEVERAGE}x for {hold_side}")
        except Exception as e:
            logger.warning(f"Set leverage {hold_side}: {e}")


async def set_margin_mode():
    """Set margin mode to crossed."""
    endpoint = "/api/v2/mix/account/set-margin-mode"
    body = {
        "symbol": SYMBOL,
        "productType": PRODUCT_TYPE,
        "marginCoin": "USDT",
        "marginMode": MARGIN_MODE
    }
    try:
        await bitget_request("POST", endpoint, body)
        logger.info(f"Margin mode set to {MARGIN_MODE}")
    except Exception as e:
        logger.warning(f"Set margin mode: {e}")


async def get_current_price():
    """Get current market price."""
    endpoint = f"/api/v2/mix/market/ticker?symbol={SYMBOL}&productType={PRODUCT_TYPE}"
    data = await bitget_request("GET", endpoint)
    if isinstance(data, list):
        data = data[0]
    price = float(data.get("lastPr", 0))
    return price


async def close_position(side: str):
    """Close any existing position on the opposite side."""
    endpoint = "/api/v2/mix/position/single-position"
    params = f"?symbol={SYMBOL}&productType={PRODUCT_TYPE}&marginCoin=USDT"
    try:
        data = await bitget_request("GET", endpoint + params)
        if not data:
            return

        positions = data if isinstance(data, list) else [data]
        for pos in positions:
            pos_side = pos.get("holdSide", "")
            pos_size = float(pos.get("total", 0))
            if pos_size > 0 and pos_side != side:
                close_body = {
                    "symbol": SYMBOL,
                    "productType": PRODUCT_TYPE,
                    "marginCoin": "USDT",
                    "side": "sell" if pos_side == "long" else "buy",
                    "tradeSide": "close",
                    "orderType": "market",
                    "size": str(pos_size)
                }
                await bitget_request("POST", "/api/v2/mix/order/place-order", close_body)
                logger.info(f"Closed {pos_side} position: {pos_size}")
                await send_telegram(
                    f"🔄 <b>Closed {pos_side.upper()} position</b>\n"
                    f"Size: {pos_size}\n"
                    f"Reason: Reversing to {side}"
                )
    except Exception as e:
        logger.error(f"Close position error: {e}")


async def place_order(side: str, stop_loss: float = None, take_profit: float = None):
    """Place a market order with optional SL/TP."""
    try:
        # Get balance and price
        available, total = await get_account_balance()
        price = await get_current_price()

        if price <= 0:
            raise Exception("Invalid price")

        # Calculate position size (10% of balance * leverage)
        margin = available * (RISK_PERCENT / 100)
        position_value = margin * LEVERAGE
        size = position_value / price

        # Round size to appropriate decimal
        size = round(size, 1)
        if size <= 0:
            raise Exception(f"Position size too small: {size}")

        # Close opposite position first
        await close_position(side)

        # Place order
        order_body = {
            "symbol": SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginCoin": "USDT",
            "side": "buy" if side == "long" else "sell",
            "tradeSide": "open",
            "orderType": "market",
            "size": str(size),
        }

        # Add SL/TP if provided
        if stop_loss and stop_loss > 0:
            order_body["presetStopLossPrice"] = str(round(stop_loss, 4))
        if take_profit and take_profit > 0:
            order_body["presetStopSurplusPrice"] = str(round(take_profit, 4))

        result = await bitget_request("POST", "/api/v2/mix/order/place-order", order_body)
        order_id = result.get("orderId", "unknown") if result else "unknown"

        logger.info(f"Order placed: {side} {size} {SYMBOL} @ ~{price} | SL: {stop_loss} | TP: {take_profit}")

        # Telegram notification
        sl_text = f"{stop_loss:.4f}" if stop_loss else "None"
        tp_text = f"{take_profit:.4f}" if take_profit else "None"
        emoji = "🟢" if side == "long" else "🔴"

        await send_telegram(
            f"{emoji} <b>BLACK CAT - {side.upper()} ENTRY</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📊 Pair: {SYMBOL}\n"
            f"💰 Price: {price:.4f}\n"
            f"📐 Size: {size}\n"
            f"⚡ Leverage: {LEVERAGE}x\n"
            f"💵 Margin: {margin:.2f} USDT\n"
            f"🛑 Stop Loss: {sl_text}\n"
            f"🎯 Take Profit: {tp_text}\n"
            f"📋 Order ID: {order_id}\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )

        return result

    except Exception as e:
        error_msg = f"Order failed: {e}"
        logger.error(error_msg)
        await send_telegram(f"❌ <b>ORDER FAILED</b>\n{error_msg}")
        raise


# ═══════════════════════════════════════════════════════════════
# WEBHOOK MODELS
# ═══════════════════════════════════════════════════════════════
class WebhookSignal(BaseModel):
    secret: str = ""
    side: str  # "buy" or "sell"
    stop_loss: float = 0
    take_profit: float = 0


# ═══════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("BLACK CAT BOT STARTING")
    logger.info(f"Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Risk: {RISK_PERCENT}%")
    logger.info("=" * 50)

    try:
        await set_leverage()
        await set_margin_mode()
        balance, total = await get_account_balance()
        startup_msg = (
            f"🐱 <b>BLACK CAT BOT ONLINE</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📊 Pair: {SYMBOL}\n"
            f"⚡ Leverage: {LEVERAGE}x\n"
            f"📐 Risk: {RISK_PERCENT}% per trade\n"
            f"💰 Balance: {total:.2f} USDT\n"
            f"✅ Available: {balance:.2f} USDT\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        await send_telegram(startup_msg)
    except Exception as e:
        logger.error(f"Startup error: {e}")
        await send_telegram(f"⚠️ <b>BOT STARTUP WARNING</b>\n{e}")

    yield

    logger.info("Black Cat Bot shutting down")
    await send_telegram("🔴 <b>BLACK CAT BOT OFFLINE</b>")


app = FastAPI(title="Black Cat Trading Bot", lifespan=lifespan)


@app.get("/")
async def health():
    return {
        "status": "alive",
        "bot": "Black Cat Scalping",
        "symbol": SYMBOL,
        "leverage": LEVERAGE,
        "time": datetime.now(timezone.utc).isoformat()
    }


@app.get("/balance")
async def check_balance():
    available, total = await get_account_balance()
    return {"total": total, "available": available}


@app.post("/webhook")
async def webhook(request: Request):
    """Receive TradingView alert and execute trade."""
    try:
        body = await request.body()
        body_text = body.decode("utf-8")
        logger.info(f"Webhook received: {body_text}")

        # Try to parse as JSON
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            # Handle plain text alerts
            text = body_text.lower().strip()
            if "buy" in text:
                data = {"side": "buy", "secret": WEBHOOK_SECRET}
            elif "sell" in text:
                data = {"side": "sell", "secret": WEBHOOK_SECRET}
            else:
                raise HTTPException(status_code=400, detail="Cannot parse signal")

        # Validate secret
        secret = data.get("secret", "")
        if secret != WEBHOOK_SECRET:
            logger.warning(f"Invalid secret: {secret}")
            raise HTTPException(status_code=401, detail="Invalid secret")

        # Parse signal
        side_raw = data.get("side", "").lower()
        if side_raw in ["buy", "long"]:
            side = "long"
        elif side_raw in ["sell", "short"]:
            side = "short"
        else:
            raise HTTPException(status_code=400, detail=f"Invalid side: {side_raw}")

        stop_loss = float(data.get("stop_loss", 0))
        take_profit = float(data.get("take_profit", 0))

        # Deduplication check
        now = time.time()
        if last_signal["side"] == side and (now - last_signal["timestamp"]) < COOLDOWN_SECONDS:
            logger.info(f"Duplicate signal ignored: {side}")
            return {"status": "ignored", "reason": "duplicate signal within cooldown"}

        last_signal["side"] = side
        last_signal["timestamp"] = now

        # Execute trade
        result = await place_order(side, stop_loss, take_profit)
        return {"status": "executed", "side": side, "result": str(result)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        await send_telegram(f"❌ <b>WEBHOOK ERROR</b>\n{str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/close")
async def close_all():
    """Emergency close all positions."""
    try:
        await close_position("none")
        await send_telegram("🚨 <b>EMERGENCY CLOSE - All positions closed</b>")
        return {"status": "closed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
