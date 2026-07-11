import asyncio
import re
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
import requests

from core.database import get_db_connection
from core.trade_utils import calculate_smart_targets, format_price
from core.bot_utils import get_target_channel

logger = logging.getLogger(__name__)


def get_live_price(symbol):
    try:
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        res = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}", timeout=5)
        res.raise_for_status()
        return float(res.json()["price"])
    except Exception as e:
        logger.error(f"Error fetching des Preises für {symbol}: {e}")
        return None


async def open_command_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg or not msg.text:
        return

    text = msg.text.strip()

    match = re.match(r"(?i)^!open\s+(CMP|LIMIT)\s+(LONG|SHORT)\s+([A-Z0-9]+)\s+(\d+x?)(.*)$", text)
    if not match:
        await msg.reply_text(
            "⚠️ Usage: !open [CMP|LIMIT] [LONG|SHORT] [COIN] [LEVERAGE] [CHANNEL_ID?] [-V?]\n"
            "Example: !open LIMIT LONG BTCUSDT 20x 1 -v"
        )
        return

    order_type = match.group(1).upper()
    direction = match.group(2).upper()

    symbol = match.group(3).upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    leverage_str = match.group(4).upper().replace("X", "")
    rest_args = match.group(5).upper().strip()

    post_video = "-V" in rest_args
    channel_match = re.search(r"\b(\d+)\b", rest_args.replace("-V", ""))
    target_channel_key = channel_match.group(1) if channel_match else None

    try:
        lev = int(leverage_str)
        if not 1 <= lev <= 125: raise ValueError
    except Exception:
        await msg.reply_text("❌ Invalid leverage (1-125).")
        return

    if target_channel_key:
        target_chat_id = get_target_channel(target_channel_key)
        if not target_chat_id:
            await msg.reply_text(f"❌ Channel-Mapping '{target_channel_key}' nicht in bot_config.json gefunden!")
            return
    else:
        target_chat_id = msg.chat_id

    try:
        await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")
    except Exception:
        pass

    # P3.5: get_live_price does a blocking requests.get — run it off the event
    # loop so a slow Binance response doesn't freeze every other bot handler.
    live_price = await asyncio.to_thread(get_live_price, symbol)
    if not live_price:
        await msg.reply_text(f"❌ Could not fetch live price for {symbol}.")
        return

    try:
        conn = get_db_connection()
        setup = calculate_smart_targets(conn, symbol, direction, live_price)
        conn.close()
    except Exception as e:
        await msg.reply_text(f"❌ Error calculating targets in the database: {e}")
        return

    entry1 = live_price if order_type == "CMP" else setup['entry1']
    entry2 = setup['entry2']
    sl = setup['sl']
    targets = setup['targets']

    user = update.effective_user
    # P3.5: a user without an @username would render "@None" in the attribution
    # line — fall back to full_name, then to "Trader".
    username = (user.username or user.full_name) if user else "Trader"
    is_long = direction == "LONG"

    # 💥 CORNIX RAW TEXT (Purer Plain Text)
    cornix_msg = f"📈 Signal for {symbol} 📈\n"
    cornix_msg += f"🚨 Direction: {direction}\n"
    cornix_msg += f"🚨 Leverage: {lev}x\n"
    cornix_msg += f"🚨 Margin: Cross\n"
    # P1.4: signifikante Stellen statt :.6f, sonst kollabieren Sub-0.001-TPs
    cornix_msg += f"🏦 CMP Entry: $ {format_price(entry1)}\n"
    cornix_msg += f"🏦 Entry 2: $ {format_price(entry2)}\n"

    for i, t in enumerate(targets[:10], 1):
        cornix_msg += f"💰 TP{i}: $ {format_price(t)}\n"

    cornix_msg += f"💸 Stop Loss: $ {format_price(sl)}\n"
    cornix_msg += f"🧠 Triggered manually by @{username}"

    # --- SENDEN (OHNE parse_mode='HTML') ---
    try:
        if post_video:
            video_name = "botlong.mp4" if is_long else "botshort.mp4"
            video_path = Path(video_name)
            if video_path.exists():
                with open(video_path, 'rb') as f:
                    # Sending das Video mit dem puren Text als Caption
                    await context.bot.send_video(chat_id=target_chat_id, video=f, caption=cornix_msg)
            else:
                logger.warning(f"Video {video_name} not found, sending nur Text.")
                # Purer Text ohne Parse-Mode
                await context.bot.send_message(chat_id=target_chat_id, text=cornix_msg)
        else:
            # Purer Text ohne Parse-Mode
            await context.bot.send_message(chat_id=target_chat_id, text=cornix_msg)

        if str(target_chat_id) != str(msg.chat_id):
            await msg.reply_text(f"✅ Trade posted successfully to channel ID {target_chat_id}.")

    except Exception as e:
        logger.error(f"Post Error: {e}")
        await msg.reply_text(f"❌ Error posting: {e}")
