import os
import re
import logging
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("btc-signal-bot")


async def fetch(url, params=None, as_json=False):
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BTCSignalBot/6.0)",
               "Accept": "application/json,text/plain,text/html"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.get(url, params=params, allow_redirects=True) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")
            return await r.json() if as_json else text


async def market():
    ticker = await fetch(
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        as_json=True)
    stats = await fetch(
        "https://api.exchange.coinbase.com/products/BTC-USD/stats",
        as_json=True)
    price = float(ticker["price"])
    volume_btc = float(ticker.get("volume", 0))
    opening = float(stats["open"])
    last = float(stats["last"])
    change = ((last - opening) / opening * 100) if opening else 0
    return price, change, volume_btc * price


async def liquidity():
    book = await fetch(
        "https://api.exchange.coinbase.com/products/BTC-USD/book",
        {"level": 2}, as_json=True)
    bids = book.get("bids", [])[:50]
    asks = book.get("asks", [])[:50]
    bid_usd = sum(float(x[0]) * float(x[1]) for x in bids)
    ask_usd = sum(float(x[0]) * float(x[1]) for x in asks)
    depth = bid_usd + ask_usd
    if depth <= 0:
        raise RuntimeError("empty order book")
    return depth, (bid_usd - ask_usd) / depth


def parse_farside(text):
    # Farside all-data table ends each daily row with Total ETF flow.
    date_re = re.compile(r"^\|?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s*\|")
    rows = []
    for line in text.splitlines():
        line = line.strip()
        m = date_re.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        nums = []
        for c in cells[1:]:
            c = c.replace(",", "").replace("$", "").strip()
            if c in ("", "-", "—", "–"):
                nums.append(0.0)
                continue
            neg = c.startswith("(") and c.endswith(")")
            if neg:
                c = c[1:-1]
            try:
                v = float(c)
                nums.append(-v if neg else v)
            except ValueError:
                pass
        if nums:
            rows.append((m.group(1), nums[-1]))
    if not rows:
        raise RuntimeError("Farside table could not be parsed")
    return rows[-1]


async def etf():
    # Direct Farside can return 403 to cloud IPs, so try reader first.
    sources = [
        "https://r.jina.ai/https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "https://r.jina.ai/http://farside.co.uk/bitcoin-etf-flow-all-data/",
        "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    ]
    errors = []
    for url in sources:
        try:
            return parse_farside(await fetch(url))
        except Exception as e:
            errors.append(str(e))
            log.warning("ETF source failed: %s", e)
    raise RuntimeError("ETF unavailable: " + " | ".join(errors[-2:]))


def score(change, volume, imbalance, flow):
    etf = max(-45, min(45, flow / 20))
    mom = max(-20, min(20, change * 4))
    book = max(-20, min(20, imbalance * 20))
    vol = 15 if volume >= 2e9 else 8 if volume >= 7.5e8 else 0
    total = int(round(max(-100, min(100, etf + mom + book + vol))))

    parts = [etf, mom, book]
    denom = sum(abs(x) for x in parts)
    agreement = abs(sum(parts)) / denom if denom else 0
    quality = 1.0 if volume >= 7.5e8 else 0.8
    confidence = max(30, min(95, int(round(
        100 * (0.65 * agreement + 0.35 * quality)))))
    signal = "🟢 BUY" if total >= 65 else "🔴 SELL" if total <= -65 else "🟡 WAIT"
    return total, confidence, signal


async def report():
    price, change, volume = await market()
    depth, imbalance = await liquidity()
    etf_date, flow = await etf()

    total, confidence, signal = score(change, volume, imbalance, flow)
    now = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{signal}\n\n"
        f"BTC: ${price:,.0f}\n"
        f"24h: {change:+.2f}%\n"
        f"ETF flow: ${flow:+,.1f}M ({etf_date})\n"
        f"24h volume: ${volume/1e9:.2f}B\n"
        f"Order-book depth: ${depth/1e6:.2f}M\n"
        f"Bid/ask imbalance: {imbalance:+.2%}\n"
        f"Signal: {total:+d}/100\n"
        f"Confidence: {confidence}%\n"
        f"Updated: {now}\n\n"
        "⚠️ Market signal only; no automatic trading."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC ETF + Liquidity Signal Bot v6\n\n"
        "/signal — текущий сигнал\n"
        "/status — текущий сигнал\n"
        "/notify — уведомления каждый час\n"
        "/stop — выключить уведомления"
    )


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(await report())
    except Exception as e:
        log.exception("signal failed")
        await update.message.reply_text(
            "⚪ DATA INCOMPLETE\n\n"
            f"Причина: {e}\n\n"
            "BUY/SELL не выдаю, пока ключевые данные недоступны."
        )


async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = f"notify:{update.effective_chat.id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_repeating(
        scheduled, interval=3600, first=5,
        chat_id=update.effective_chat.id, name=name)
    await update.message.reply_text("Автоуведомления включены.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = f"notify:{update.effective_chat.id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    await update.message.reply_text("Автоуведомления выключены.")


async def scheduled(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await report()
        if text.startswith("🟢 BUY") or text.startswith("🔴 SELL"):
            await context.bot.send_message(context.job.chat_id, text)
    except Exception:
        log.exception("scheduled signal failed")


async def error_handler(update, context):
    log.error("Unhandled Telegram exception: %r", context.error)


async def post_init(application):
    # Remove webhook so this instance uses long polling only.
    await application.bot.delete_webhook(drop_pending_updates=False)
    log.info("Webhook cleared; starting long polling")


def main():
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("status", signal))
    app.add_handler(CommandHandler("notify", notify))
    app.add_handler(CommandHandler("stop", stop))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
