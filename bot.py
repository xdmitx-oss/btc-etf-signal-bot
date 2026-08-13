import os
import logging
from datetime import datetime, timezone
import asyncio
import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MIN_SIGNAL = int(os.getenv("MIN_SIGNAL", "65"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("btc-bot")

async def fetch_text(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            return await r.text()

async def get_etf_flows():
    # Farside publishes daily US spot BTC ETF flows in USD millions.
    html = await fetch_text("https://farside.co.uk/btc/")
    tables = pd.read_html(html)
    for t in tables:
        cols = [str(c).strip() for c in t.columns]
        if "Total" in cols:
            total = pd.to_numeric(
                t["Total"].astype(str).str.replace(",", "", regex=False)
                .str.replace("(", "-", regex=False).str.replace(")", "", regex=False)
                .replace("-", "0"), errors="coerce"
            ).dropna()
            if len(total):
                return float(total.iloc[-1]), total.tail(20).tolist()
    raise RuntimeError("ETF table not found")

async def get_btc():
    url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=10) as r:
            r.raise_for_status()
            j = await r.json()
            return {
                "price": float(j["lastPrice"]),
                "change": float(j["priceChangePercent"]),
                "volume": float(j["quoteVolume"]),
            }

def score(etf_last, etf_hist, btc):
    # ETF component: current flow + short-term persistence.
    etf = max(-40, min(40, etf_last / 15))
    persistence = 0
    if len(etf_hist) >= 5:
        avg5 = sum(etf_hist[-5:]) / 5
        persistence = max(-15, min(15, avg5 / 20))

    # Price/volume confirmation, intentionally simple for MVP.
    momentum = max(-20, min(20, btc["change"] * 3))
    volume_bonus = 5 if btc["volume"] > 1_000_000_000 else 0

    raw = etf + persistence + momentum + volume_bonus
    return int(max(-100, min(100, raw)))

def signal(score_value):
    if score_value >= 65:
        return "🟢 BUY"
    if score_value <= -65:
        return "🔴 SELL"
    return "🟡 WAIT"

async def build_report():
    etf_last, hist = await get_etf_flows()
    btc = await get_btc()
    s = score(etf_last, hist, btc)
    sig = signal(s)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{sig}\n\n"
        f"BTC: ${btc['price']:,.0f}\n"
        f"24h: {btc['change']:+.2f}%\n"
        f"ETF flow: ${etf_last:+,.1f}M\n"
        f"ETF 5d avg: ${sum(hist[-5:])/min(5,len(hist)):+,.1f}M\n"
        f"Signal score: {s:+d}/100\n"
        f"Updated: {now}\n\n"
        f"⚠️ Signal only — no automatic trading."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC Signal Bot\n\n"
        "/signal — текущий сигнал\n"
        "/status — краткий статус\n\n"
        "Модель использует ETF flows + BTC momentum/volume."
    )

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(await build_report())
    except Exception as e:
        log.exception("signal error")
        await update.message.reply_text(f"Ошибка получения данных: {e}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await build_report())

async def scheduled(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    try:
        report = await build_report()
        # Notify only on strong signals.
        if "BUY" in report or "SELL" in report:
            await context.bot.send_message(chat_id=chat_id, text=report)
    except Exception:
        log.exception("scheduled error")

async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.job_queue.run_repeating(scheduled, interval=3600, first=5, data=chat_id)
    await update.message.reply_text("Автоуведомления включены: проверка раз в час.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("notify", notify))
    app.run_polling()

if __name__ == "__main__":
    main()
