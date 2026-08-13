import os
import logging
import aiohttp
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("btc-signal-bot")


async def get_json(url, params=None):
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "User-Agent": "BTC-ETF-Liquidity-Bot/4.0",
        "Accept": "application/json",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {text[:200]}")
            try:
                return await response.json()
            except Exception:
                raise RuntimeError(f"Invalid JSON from {url}")


async def btc_market():
    """Coinbase public market data. No API key required."""
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

    # Coinbase ticker volume is BTC, not USD.
    volume_usd = volume_btc * price
    return price, change_24h, volume_btc, volume_usd


async def orderbook_liquidity():
    """Simple spot liquidity proxy from Coinbase Level 2 order book."""
    book = await get_json(
        "https://api.exchange.coinbase.com/products/BTC-USD/book",
        {"level": 2},
    )

    bids = book.get("bids", [])[:25]
    asks = book.get("asks", [])[:25]

    bid_usd = sum(float(x[0]) * float(x[1]) for x in bids)
    ask_usd = sum(float(x[0]) * float(x[1]) for x in asks)
    total_depth = bid_usd + ask_usd

    if total_depth <= 0:
        raise RuntimeError("Order book depth unavailable")

    imbalance = (bid_usd - ask_usd) / total_depth
    return total_depth, imbalance


def extract_etf_values(payload):
    """
    Kote Charts documents /api/v1/public/charts/etf-flows.
    The parser accepts several possible response shapes so a minor
    API schema change does not crash the bot.
    """
    data = payload.get("data", payload) if isinstance(payload, dict) else payload

    if isinstance(data, dict):
        for key in ("data", "rows", "results", "values"):
            if key in data:
                data = data[key]
                break

    if not isinstance(data, list):
        return []

    values = []
    for row in data[-30:]:
        value = None

        if isinstance(row, dict):
            for key in (
                "net_flow",
                "netFlow",
                "daily_net_flow",
                "flow",
                "value",
                "etf_flow",
            ):
                if key in row:
                    value = row[key]
                    break

        elif isinstance(row, (list, tuple)):
            # Kote/financial chart responses commonly use [date, value, ...].
            # Prefer the second item, then scan numeric items from the end.
            if len(row) >= 2:
                value = row[1]
            if isinstance(value, str) and not value.strip():
                value = None

        try:
            if value is not None:
                values.append(float(value))
        except (TypeError, ValueError):
            pass

    return values


async def etf_flow():
    payload = await get_json(
        "https://kotecharts.com/api/v1/public/charts/etf-flows"
    )
    values = extract_etf_values(payload)

    if not values:
        raise RuntimeError("ETF flow data unavailable")

    latest = values[-1]
    recent = values[-5:]
    return latest, recent


def score_signal(change_24h, volume_usd, depth_usd, imbalance, etf_latest, etf_recent):
    """
    Conservative scoring:
      ETF flow      40 points
      Momentum      20 points
      Liquidity     20 points
      Volume        20 points
    """
    score = 0
    reasons = []

    # ETF: strongest input.
    if etf_latest is not None:
        score += max(-40, min(40, etf_latest / 25))
        avg5 = sum(etf_recent) / len(etf_recent) if etf_recent else etf_latest
        score += max(-10, min(10, avg5 / 50))
        reasons.append(f"ETF {etf_latest:+,.0f}M")
    else:
        return None, ["ETF data unavailable"]

    # Momentum.
    momentum = max(-20, min(20, change_24h * 4))
    score += momentum
    reasons.append(f"momentum {momentum:+.1f}")

    # Order-book imbalance.
    liq_score = max(-20, min(20, imbalance * 20))
    score += liq_score
    reasons.append(f"book {liq_score:+.1f}")

    # USD volume: high volume gives confidence, but never dominates direction.
    if volume_usd >= 2_000_000_000:
        score += 10
        reasons.append("high volume")
    elif volume_usd >= 750_000_000:
        score += 5
        reasons.append("healthy volume")
    else:
        reasons.append("low volume")

    score = int(round(max(-100, min(100, score))))

    # Require stronger confirmation for BUY/SELL.
    signal = "🟢 BUY" if score >= 65 else "🔴 SELL" if score <= -65 else "🟡 WAIT"
    return (score, signal), reasons


async def build_report():
    price, change_24h, volume_btc, volume_usd = await btc_market()

    try:
        depth_usd, imbalance = await orderbook_liquidity()
    except Exception as exc:
        log.warning("Order book unavailable: %s", exc)
        depth_usd, imbalance = None, None

    try:
        etf_latest, etf_recent = await etf_flow()
    except Exception as exc:
        log.warning("ETF unavailable: %s", exc)
        etf_latest, etf_recent = None, []

    if etf_latest is None or depth_usd is None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        missing = []
        if etf_latest is None:
            missing.append("ETF")
        if depth_usd is None:
            missing.append("liquidity")

        return (
            "⚪ DATA INCOMPLETE\n\n"
            f"BTC: ${price:,.0f}\n"
            f"24h: {change_24h:+.2f}%\n"
            f"24h volume: ${volume_usd/1e9:.2f}B\n"
            f"ETF flow: {'N/A' if etf_latest is None else f'${etf_latest:+,.0f}M'}\n"
            f"Order-book depth: {'N/A' if depth_usd is None else f'${depth_usd/1e6:.1f}M'}\n"
            f"Missing: {', '.join(missing)}\n"
            f"Updated: {now}\n\n"
            "Signal is withheld until key data is available."
        )

    result, reasons = score_signal(
        change_24h,
        volume_usd,
        depth_usd,
        imbalance,
        etf_latest,
        etf_recent,
    )
    score, signal = result

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{signal}\n\n"
        f"BTC: ${price:,.0f}\n"
        f"24h: {change_24h:+.2f}%\n"
        f"ETF flow: ${etf_latest:+,.0f}M\n"
        f"24h volume: ${volume_usd/1e9:.2f}B\n"
        f"Order-book depth: ${depth_usd/1e6:.1f}M\n"
        f"Bid/ask imbalance: {imbalance:+.2%}\n"
        f"Signal: {score:+d}/100\n"
        f"Reasons: {', '.join(reasons)}\n"
        f"Updated: {now}\n\n"
        "⚠️ Market signal only; no automatic trading."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC ETF + Liquidity Signal Bot v4\n\n"
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
    # Avoid duplicate hourly jobs for the same chat.
    for job in context.job_queue.get_jobs_by_name(f"notify:{update.effective_chat.id}"):
        job.schedule_removal()

    context.job_queue.run_repeating(
        scheduled_signal,
        interval=3600,
        first=5,
        chat_id=update.effective_chat.id,
        name=f"notify:{update.effective_chat.id}",
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
