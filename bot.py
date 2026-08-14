import os
import re
import logging
from datetime import datetime, timezone

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("btc-signal-bot")


async def fetch(url, params=None, as_json=False):
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BTCSignalBot/7.0)",
        "Accept": "application/json,text/plain,text/html",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
        async with s.get(url, params=params, allow_redirects=True) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {text[:200]}")
            return await r.json() if as_json else text


async def get_market():
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


async def get_liquidity():
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

    imbalance = (bid_usd - ask_usd) / depth
    return depth, imbalance


def parse_farside(text):
    # Farside all-data table: each daily row ends with Total flow (US$m).
    date_re = re.compile(r"^\|?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s*\|")
    rows = []

    for line in text.splitlines():
        line = line.strip()
        match = date_re.match(line)
        if not match:
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        nums = []

        for cell in cells[1:]:
            cell = cell.replace(",", "").replace("$", "").strip()

            if cell in ("", "-", "—", "–"):
                nums.append(0.0)
                continue

            negative = cell.startswith("(") and cell.endswith(")")
            if negative:
                cell = cell[1:-1]

            try:
                value = float(cell)
                nums.append(-value if negative else value)
            except ValueError:
                pass

        if nums:
            rows.append((match.group(1), nums[-1]))

    if not rows:
        raise RuntimeError("Farside table could not be parsed")

    return rows


async def get_etf_history():
    sources = [
        "https://r.jina.ai/https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "https://r.jina.ai/http://farside.co.uk/bitcoin-etf-flow-all-data/",
        "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    ]

    errors = []

    for url in sources:
        try:
            text = await fetch(url)
            rows = parse_farside(text)
            if len(rows) >= 5:
                return rows[-5:]
        except Exception as exc:
            errors.append(str(exc))
            log.warning("ETF source failed: %s", exc)

    raise RuntimeError("ETF unavailable: " + " | ".join(errors[-2:]))


def money(x):
    return f"${x:+,.1f}M"


def classify_flow(value):
    if value >= 100:
        return "strong inflow"
    if value >= 25:
        return "inflow"
    if value <= -100:
        return "strong outflow"
    if value <= -25:
        return "outflow"
    return "neutral"


def build_signal(change, volume, imbalance, flows):
    # ETF trend is intentionally more important than a single ETF day.
    f1 = flows[-1][1]
    f3 = sum(x[1] for x in flows[-3:])
    f5 = sum(x[1] for x in flows[-5:])

    # Directional score, normalized in USD millions.
    # 1D: 20 pts, 3D: 30 pts, 5D: 35 pts.
    etf_1d = max(-20, min(20, f1 / 20))
    etf_3d = max(-30, min(30, f3 / 35))
    etf_5d = max(-35, min(35, f5 / 60))

    # Momentum: 15 pts.
    momentum = max(-15, min(15, change * 3))

    # Order-book: 15 pts.
    book = max(-15, min(15, imbalance * 15))

    # Volume is a confidence modifier, not a directional signal.
    if volume >= 2e9:
        volume_quality = "HIGH"
    elif volume >= 7.5e8:
        volume_quality = "NORMAL"
    else:
        volume_quality = "LOW"

    directional = etf_1d + etf_3d + etf_5d + momentum + book
    score = int(round(max(-100, min(100, directional))))

    # Market bias is more readable than "93% confidence".
    if score >= 55:
        bias = "Bullish"
        signal = "🟢 BUY"
    elif score >= 20:
        bias = "Slightly bullish"
        signal = "🟡 WAIT"
    elif score <= -55:
        bias = "Bearish"
        signal = "🔴 SELL"
    elif score <= -20:
        bias = "Slightly bearish"
        signal = "🟡 WAIT"
    else:
        bias = "Neutral"
        signal = "🟡 WAIT"

    # Confidence = data completeness + trend agreement, NOT win probability.
    etf_direction = [etf_1d, etf_3d, etf_5d]
    nonzero = [x for x in etf_direction if abs(x) > 0.01]
    same_sign = 1.0
    if nonzero:
        signs = [x > 0 for x in nonzero]
        same_sign = max(sum(signs), len(signs) - sum(signs)) / len(signs)

    agreement = same_sign
    quality = 1.0 if volume_quality != "LOW" else 0.75
    confidence = int(round(100 * (0.7 * agreement + 0.3 * quality)))
    confidence = max(40, min(95, confidence))

    return {
        "f1": f1, "f3": f3, "f5": f5,
        "score": score, "bias": bias, "signal": signal,
        "volume_quality": volume_quality,
        "confidence": confidence,
    }


async def make_report():
    price, change, volume = await get_market()
    depth, imbalance = await get_liquidity()
    rows = await get_etf_history()

    result = build_signal(change, volume, imbalance, rows)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    flow_lines = (
        f"ETF 1D: {money(result['f1'])} ({classify_flow(result['f1'])})\n"
        f"ETF 3D: {money(result['f3'])}\n"
        f"ETF 5D: {money(result['f5'])}"
    )

    return (
        f"{result['signal']}\n\n"
        f"BTC: ${price:,.0f}\n"
        f"24h: {change:+.2f}%\n\n"
        f"{flow_lines}\n"
        f"Volume: ${volume/1e9:.2f}B ({result['volume_quality']})\n"
        f"Liquidity depth: ${depth/1e6:.2f}M\n"
        f"Bid/ask imbalance: {imbalance:+.2%}\n\n"
        f"Signal: {result['score']:+d}/100\n"
        f"Market bias: {result['bias']}\n"
        f"Data confidence: {result['confidence']}%\n"
        f"Updated: {now}\n\n"
        "⚠️ Market signal only; no automatic trading.\n"
        "Data confidence is not probability of profit."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC ETF + Liquidity Signal Bot v7\n\n"
        "/signal — сигнал\n"
        "/status — сигнал\n"
        "/notify — BUY/SELL уведомления каждый час\n"
        "/stop — выключить уведомления"
    )


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(await make_report())
    except Exception as exc:
        log.exception("signal failed")
        await update.message.reply_text(
            "⚪ DATA INCOMPLETE\n\n"
            f"Причина: {exc}\n\n"
            "Сигнал не выдаю."
        )


async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = f"notify:{update.effective_chat.id}"

    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    context.job_queue.run_repeating(
        scheduled_signal,
        interval=3600,
        first=5,
        chat_id=update.effective_chat.id,
        name=name,
    )

    await update.message.reply_text("Автоуведомления включены.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = f"notify:{update.effective_chat.id}"

    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    await update.message.reply_text("Автоуведомления выключены.")


async def scheduled_signal(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await make_report()

        if text.startswith("🟢 BUY") or text.startswith("🔴 SELL"):
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text=text,
            )
    except Exception:
        log.exception("scheduled signal failed")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram exception: %r", context.error)


async def post_init(application: Application):
    await application.bot.delete_webhook(drop_pending_updates=False)
    log.info("Webhook cleared; starting polling")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("status", signal))
    app.add_handler(CommandHandler("notify", notify))
    app.add_handler(CommandHandler("stop", stop))
    app.add_error_handler(error_handler)

    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
