import os, logging, aiohttp
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("btc-bot")

async def get_json(url, params=None):
    async with aiohttp.ClientSession(headers={"User-Agent":"BTC-Signal-Bot/2.0"}) as s:
        async with s.get(url, params=params, timeout=20) as r:
            r.raise_for_status()
            return await r.json()

async def btc_market():
    j = await get_json("https://api.binance.com/api/v3/ticker/24hr", {"symbol":"BTCUSDT"})
    return float(j["lastPrice"]), float(j["priceChangePercent"]), float(j["quoteVolume"])

async def derivatives():
    funding = await get_json("https://fapi.binance.com/fapi/v1/fundingRate",
                             {"symbol":"BTCUSDT","limit":5})
    oi = await get_json("https://fapi.binance.com/fapi/v1/openInterest",
                        {"symbol":"BTCUSDT"})
    avg_funding = sum(float(x["fundingRate"]) for x in funding) / len(funding)
    return avg_funding, float(oi["openInterest"])

async def etf_flow():
    # Public Kote Charts endpoint. The bot degrades gracefully if unavailable.
    j = await get_json("https://kotecharts.com/api/v1/public/charts/etf-flows")
    data = j.get("data", j)
    if isinstance(data, dict):
        data = data.get("data", [])
    rows = []
    for x in data[-30:]:
        if isinstance(x, dict):
            val = x.get("net_flow", x.get("flow", x.get("value")))
        elif isinstance(x, (list, tuple)) and len(x) >= 2:
            val = x[-1]
        else:
            continue
        try:
            rows.append(float(val))
        except Exception:
            pass
    if not rows:
        raise RuntimeError("ETF data unavailable")
    return rows[-1], rows[-5:]

def liquidity_proxy(price_change, volume):
    vol_score = 5 if volume >= 1_000_000_000 else 0
    return max(-10, min(10, price_change * 1.5)) + vol_score

async def report():
    price, change, volume = await btc_market()
    funding, oi = await derivatives()

    etf = None
    etf5 = []
    try:
        etf, etf5 = await etf_flow()
    except Exception as e:
        log.warning("ETF source unavailable: %s", e)

    score = 0
    reasons = []

    if etf is not None:
        score += max(-35, min(35, etf / 20))
        reasons.append(f"ETF {etf:+.1f}")
        if etf5:
            score += max(-15, min(15, (sum(etf5)/len(etf5))/30))
    else:
        reasons.append("ETF unavailable")

    score += max(-20, min(20, change * 3))
    reasons.append("momentum +" if change > 0 else "momentum -" if change < 0 else "momentum neutral")

    liq = liquidity_proxy(change, volume)
    score += liq

    if funding > 0.0002:
        score -= 8
        reasons.append("funding high")
    elif funding < -0.0002:
        score += 8
        reasons.append("funding low")

    score = max(-100, min(100, int(round(score))))
    signal = "🟢 BUY" if score >= 65 else "🔴 SELL" if score <= -65 else "🟡 WAIT"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    etf_txt = f"${etf:+,.1f}M" if etf is not None else "N/A"

    return (
        f"{signal}\n\n"
        f"BTC: ${price:,.0f}\n"
        f"24h: {change:+.2f}%\n"
        f"ETF flow: {etf_txt}\n"
        f"Funding: {funding*100:+.4f}%\n"
        f"Open Interest: {oi:,.0f} BTC\n"
        f"Liquidity proxy: {liq:+.1f}\n"
        f"Signal: {score:+d}/100\n"
        f"Reasons: {', '.join(reasons)}\n"
        f"Updated: {now}\n\n"
        "⚠️ Market signal only; no automatic trading."
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BTC ETF + Liquidity Signal Bot\n\n"
        "/signal — текущий сигнал\n"
        "/status — текущий сигнал\n"
        "/notify — автоуведомления каждый час"
    )

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(await report())
    except Exception as e:
        log.exception("signal failed")
        await update.message.reply_text(f"Ошибка получения данных: {e}")

async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.job_queue.run_repeating(job, interval=3600, first=5,
                                    data=update.effective_chat.id)
    await update.message.reply_text("Автоуведомления включены. Проверка каждый час.")

async def job(context: ContextTypes.DEFAULT_TYPE):
    try:
        r = await report()
        if r.startswith("🟢") or r.startswith("🔴"):
            await context.bot.send_message(chat_id=context.job.data, text=r)
    except Exception:
        log.exception("scheduled job failed")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("status", signal))
    app.add_handler(CommandHandler("notify", notify))
    app.run_polling()

if __name__ == "__main__":
    main()
