import os
import re
import logging
import aiohttp
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("btc-signal-bot")


async def get_text(url, params=None):
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {
        "User-Agent": "BTC-ETF-Liquidity-Bot/5.0",
        "Accept": "text/html,text/plain,application/json",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {text[:200]}")
            return text


async def get_json(url, params=None):
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "User-Agent": "BTC-ETF-Liquidity-Bot/5.0",
        "Accept": "application/json",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {text[:200]}")
            return await response.json()


async def btc_market():
    ticker = await get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    )
    stats = await get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/stats"
    )

    price = float(ticker["price"])
    volume_btc = float(ticker.get("volume", 0))
    open_price = float(stats["open"])
    last_price = float(stats["last"])
    change_24h = ((last_price - open_price) / open_price * 100) if open_price else 0
    volume_usd = volume_btc * price

    return price, change_24h, volume_usd


async def orderbook_liquidity():
    book = await get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/book",
        {"level": 2},
    )

    bids = book.get("bids", [])[:25]
    asks = book.get("asks", [])[:25]

    bid_usd = sum(float(x[0]) * float(x[1]) for x in bids)
    ask_usd = sum(float(x[0]) * float(x[1]) for x in asks)
    depth = bid_usd + ask_usd

    if depth <= 0:
        raise RuntimeError("Order-book depth unavailable")

    imbalance = (bid_usd - ask_usd) / depth
    return depth, imbalance


def parse_farside_latest(text):
    """
    Parses the public Farside 'all data' table.
    We intentionally use the public page through Jina Reader because
    some cloud hosts receive 403 from Farside directly.
    Expected row format:
      date | IBIT | FBTC | ... | GBTC | BTC | Total
    """
    # Normalize markdown/table text.
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    date_re = re.compile(
        r"^\|?\s*(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s*\|"
    )

    candidates = []
    for line in lines:
        m = date_re.match(line)
        if not m:
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue

        date_text = cells[0]
        # Last numeric cell is normally Total.
        numeric = []
        for c in cells[1:]:
            c = c.replace(",", "").replace("$", "").strip()
            if c in ("-", "—", ""):
                numeric.append(0.0)
                continue
            # Parentheses mean negative.
            neg = c.startswith("(") and c.endswith(")")
            if neg:
                c = c[1:-1]
            try:
                v = float(c)
                numeric.append(-v if neg else v)
            except ValueError:
                pass

        if numeric:
            candidates.append((date_text, numeric[-1]))

    if not candidates:
        raise RuntimeError("Could not parse ETF flow table")

    # Ignore Total/Average/Maximum/Minimum if they somehow match.
    latest_date, latest_flow = candidates[-1]
    return latest_date, latest_flow


async def etf_flow():
    # Public reader proxy for the public Farside table.
    # Direct Farside is used as a fallback.
    urls = [
        "https://r.jina.ai/http://farside.co.uk/bitcoin-etf-flow-all-data/",
        "https://r.jina.ai/https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    ]

    last_error = None
    for url in urls:
        try:
            text = await get_text(url)
            date_text, flow = parse_farside_latest(text)
            return date_text, flow
        except Exception as exc:
            last_error = exc
            log.warning("ETF source failed %s: %s", url, exc)

    raise RuntimeError(f"ETF source unavailable: {last_error}")


def signal_score(change_24h, volume_usd, depth_usd, imbalance, etf_flow):
    # Directional score: ETF 45, momentum 20, order-book 20, volume regime 15.
    score = 0
    reasons = []

    etf_score = max(-45, min(45, etf_flow / 20))
    score += etf_score
    reasons.append(f"ETF {etf_flow:+.0f}M")

    momentum_score = max(-20, min(20, change_24h * 4))
    score += momentum_score
    reasons.append(f"momentum {momentum_score:+.1f}")

    book_score = max(-20, min(20, imbalance * 20))
    score += book_score
    reasons.append(f"book {book_score:+.1f}")

    if volume_usd >= 2_000_000_000:
        score += 15
        reasons.append("high volume")
    elif volume_usd >= 750_000_000:
        score += 8
        reasons.append("healthy volume")
    else:
        reasons.append("low volume")

    score = int(round(max(-100, min(100, score))))

    # Confidence reflects data freshness/completeness and agreement of inputs.
    direction_components = [etf_score, momentum_score, book_score]
    abs_sum = sum(abs(x) for x in direction_components)
    directional_agreement = abs(sum(direction_components)) / abs_sum if abs_sum else 0
    data_confidence = 0.90 if volume_usd >= 750_000_000 else 0.75
    confidence = int(round(100 * (0.55 * directional_agreement + 0.45 * data_confidence)))
    confidence = max(35, min(95, confidence))

    if score >= 65:
        signal = "🟢 BUY"
    elif score <= -65:
        signal = "🔴 SELL"
    else:
        signal = "🟡 WAIT"

    return score, confidence, signal, reasons


async def build_report():
    price, change_24h, volume_usd = await btc_market()

    depth_usd, imbalance = await orderbook_liquidity()
    etf_date, etf_flow_value = await etf_flow()

    score, confidence, signal, reasons = signal_score(
        change_24h, volume_usd, depth_usd, imbalance, etf_flow_value
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{signal}\n\n"
        f"BTC: ${price:,.0f}\n"
        f"24h: {change_24h:+.2f}%\n"
        f"ETF flow: ${etf_flow_value:+,.1f}M ({etf_date})\n"
        f"24h volume: ${volume_usd/1e9:.2f}B\n"
        f"Order-book depth: ${depth_usd/1e6:.1f}M\n"
        f"Bid/ask imbalance: {imbalance:+.2%}\n"
        f"Signal: {score:+d}/100\n"
        f"Confidence: {confidence}%\n"
        f"Reasons: {', '.join(reasons)}\n"
        f"Updated: {now}\n\n"
        "⚠️ Market signal only; no automatic trading."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC ETF + Liquidity Signal Bot v5\n\n"
        "/signal — текущий сигнал\n"
        "/status — текущий сигнал\n"
        "/notify — автоуведомления каждый час"
    )


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(await build_report())
    except Exception as exc:
        log.exception("signal failed")
        await update.message.reply_text(f"Ошибка получения данных: {exc}")


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
    await update.message.reply_text("Автоуведомления включены: проверка каждый час.")


async def scheduled_signal(context: ContextTypes.DEFAULT_TYPE):
    try:
        report = await build_report()
        if report.startswith("🟢 BUY") or report.startswith("🔴 SELL"):
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text=report,
            )
    except Exception:
        log.exception("scheduled signal failed")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("status", signal))
    app.add_handler(CommandHandler("notify", notify))
    app.run_polling()


if __name__ == "__main__":
    main()
