"""
Stock Alert Agent — Cloud Edition
----------------------------------
מותאם לפריסה ב-Railway / Render.
ה-token וה-chat_id נקראים ממשתני סביבה (Environment Variables),
שאר ההגדרות נשארות ב-config.yaml.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, date
from pathlib import Path

import yaml
import yfinance as yf
import pandas as pd
import ta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.error import TelegramError

# ── לוגינג ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler()],   # בענן — stdout בלבד
)
log = logging.getLogger(__name__)

# ── קריאת קונפיגורציה ────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.yaml"

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # קריאת סודות ממשתני סביבה (מנצחים על config.yaml)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram", {}).get("bot_token")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID")   or cfg.get("telegram", {}).get("chat_id")

    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("❌ TELEGRAM_BOT_TOKEN לא מוגדר! הגדר ב-Railway → Variables")
    if not chat_id or chat_id == "YOUR_CHAT_ID_HERE":
        raise ValueError("❌ TELEGRAM_CHAT_ID לא מוגדר! הגדר ב-Railway → Variables")

    cfg.setdefault("telegram", {})
    cfg["telegram"]["bot_token"] = bot_token
    cfg["telegram"]["chat_id"]   = str(chat_id)
    return cfg

# ── שמירת מחירי פתיחה יומיים ─────────────────────────────────────────────────
_open_prices: dict[str, float] = {}
_alerted_today: dict[str, set] = {}

def _reset_daily():
    _open_prices.clear()
    _alerted_today.clear()
    log.info("Daily reset done.")

# ── שליפת נתונים ─────────────────────────────────────────────────────────────
def fetch_data(symbol: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame | None:
    for attempt in range(3):
        try:
            df = yf.Ticker(symbol).history(period=period, interval=interval)
            if df.empty:
                log.warning(f"No data for {symbol}")
                return None
            df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            log.error(f"fetch_data({symbol}) attempt {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(20 * (attempt + 1))  # 20s, 40s
    return None

def get_current_price(df: pd.DataFrame) -> float:
    return float(df["Close"].iloc[-1])

def get_open_price(symbol: str, df: pd.DataFrame) -> float:
    key = f"{symbol}_{date.today()}"
    if key not in _open_prices:
        today_df = df[df.index.date == date.today()]
        _open_prices[key] = float((today_df["Open"].iloc[0] if not today_df.empty else df["Open"].iloc[-1]))
    return _open_prices[key]

def calc_rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    try:
        rsi = ta.momentum.RSIIndicator(df["Close"], window=period).rsi()
        if rsi is not None and not rsi.empty:
            val = rsi.iloc[-1]
            return float(val) if pd.notna(val) else None
    except Exception as e:
        log.warning(f"RSI error: {e}")
    return None

def calc_volume_ratio(df: pd.DataFrame, lookback: int = 20) -> float | None:
    try:
        vol = df["Volume"]
        if len(vol) < lookback + 1:
            return None
        avg = float(vol.iloc[-lookback-1:-1].mean())
        return float(vol.iloc[-1]) / avg if avg > 0 else None
    except Exception as e:
        log.warning(f"Volume error: {e}")
    return None

# ── בדיקות ───────────────────────────────────────────────────────────────────
def check_symbol(symbol: str, rules: dict) -> list[str]:
    df = fetch_data(symbol)
    if df is None:
        return []

    alerts = []
    price = get_current_price(df)
    open_price = get_open_price(symbol, df)
    already = _alerted_today.setdefault(symbol, set())

    # 1. שינוי % מפתיחה
    if "price_change_pct" in rules and open_price:
        change_pct = ((price - open_price) / open_price) * 100
        threshold = rules["price_change_pct"]
        key = f"pct_{threshold}"
        if abs(change_pct) >= abs(threshold) and key not in already:
            direction = "📈 עלה" if change_pct > 0 else "📉 ירד"
            alerts.append(
                f"*{symbol}* — שינוי יומי: {direction} *{change_pct:+.2f}%*\n"
                f"מחיר: ${price:.2f}  |  פתיחה: ${open_price:.2f}"
            )
            already.add(key)

    # 2. מחירי יעד
    if "price_targets" in rules:
        for target in rules["price_targets"]:
            tval = target["price"]
            direction = target.get("direction", "above")
            key = f"target_{tval}_{direction}"
            hit = (direction == "above" and price >= tval) or (direction == "below" and price <= tval)
            if hit and key not in already:
                emoji = "🎯" if direction == "above" else "⚠️"
                label = "הגיע מעל" if direction == "above" else "ירד מתחת"
                alerts.append(
                    f"*{symbol}* {emoji} — {label} ${tval}\n"
                    f"מחיר נוכחי: *${price:.2f}*"
                )
                already.add(key)

    # 3. RSI
    if "rsi" in rules:
        rsi_cfg = rules["rsi"]
        rsi_val = calc_rsi(df, period=rsi_cfg.get("period", 14))
        if rsi_val is not None:
            oversold   = rsi_cfg.get("oversold", 30)
            overbought = rsi_cfg.get("overbought", 70)
            if rsi_val <= oversold:
                key = f"rsi_oversold"
                if key not in already:
                    alerts.append(
                        f"*{symbol}* 🟢 RSI נמוך — *{rsi_val:.1f}* (מתחת ל-{oversold})\n"
                        f"מחיר: ${price:.2f}"
                    )
                    already.add(key)
            elif rsi_val >= overbought:
                key = f"rsi_overbought"
                if key not in already:
                    alerts.append(
                        f"*{symbol}* 🔴 RSI גבוה — *{rsi_val:.1f}* (מעל {overbought})\n"
                        f"מחיר: ${price:.2f}"
                    )
                    already.add(key)

    # 4. נפח חריג
    if "volume_spike" in rules:
        vol_ratio = calc_volume_ratio(df, lookback=rules["volume_spike"].get("lookback", 20))
        threshold = rules["volume_spike"].get("multiplier", 3.0)
        key = f"volume_{threshold}"
        if vol_ratio is not None and vol_ratio >= threshold and key not in already:
            alerts.append(
                f"*{symbol}* 🔊 נפח חריג — *{vol_ratio:.1f}x* מהממוצע\n"
                f"מחיר: ${price:.2f}"
            )
            already.add(key)

    return alerts

# ── שליחת טלגרם ──────────────────────────────────────────────────────────────
async def send_telegram(bot: Bot, chat_id: str, text: str):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        log.info(f"Alert sent → {chat_id}")
    except TelegramError as e:
        log.error(f"Telegram error: {e}")

# ── סריקה ────────────────────────────────────────────────────────────────────
async def run_scan(bot: Bot, config: dict):
    log.info("── Scan started ──")
    now = datetime.now().strftime("%H:%M:%S")
    for item in config.get("stocks", []):
        symbol = item["symbol"].upper()
        log.info(f"Checking {symbol}...")
        try:
            for msg in check_symbol(symbol, item.get("rules", {})):
                await send_telegram(bot, config["telegram"]["chat_id"],
                                    f"🔔 *התראת מניה* — {now}\n\n{msg}")
                await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"Error on {symbol}: {e}")
        await asyncio.sleep(15)  # השהייה בין מניות למניעת rate limit
    log.info("── Scan complete ──")

# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    config = load_config()
    bot = Bot(token=config["telegram"]["bot_token"])
    interval_min = config.get("interval_minutes", 5)

    await send_telegram(bot, config["telegram"]["chat_id"],
        f"🚀 *Stock Agent פועל בענן!*\n\n"
        f"⏱ בדיקה כל {interval_min} דקות\n"
        f"📋 מניות: {', '.join(s['symbol'] for s in config['stocks'])}"
    )

    await run_scan(bot, config)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_scan, "interval", minutes=interval_min,
                      args=[bot, config], id="stock_scan")
    scheduler.add_job(_reset_daily, "cron", hour=0, minute=0, id="daily_reset")
    scheduler.start()
    log.info(f"Running — scan every {interval_min} min.")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
