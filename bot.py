import os, logging, aiohttp
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("btc-bot")

async def get_json(url, params=None):
    async with aiohttp.ClientSession(headers={"User-Agent":"BTC-Signal-Bot/3.0","Accept":"application/json"}) as s:
        async with s.get(url, params=params, timeout=20) as r:
            r.raise_for_status()
            return await r.json()

async def market():
    # Coinbase Exchange public ticker: no API key required.
    j=await get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    price=float(j["price"])
    vol=float(j.get("volume_24h",0))
    # 24h change from stats endpoint
    st=await get_json("https://api.exchange.coinbase.com/products/BTC-USD/stats")
    openp=float(st["open"]); last=float(st["last"])
    change=(last-openp)/openp*100 if openp else 0
    return price,change,vol

async def etf():
    # Public Kote Charts endpoint documents a public spot ETF flows chart.
    j=await get_json("https://kotecharts.com/api/v1/public/charts/etf-flows")
    data=j.get("data",j)
    if isinstance(data,dict): data=data.get("data",[])
    vals=[]
    for x in data[-10:]:
        if isinstance(x,dict):
            v=x.get("net_flow",x.get("flow",x.get("value")))
        elif isinstance(x,(list,tuple)) and len(x)>1: v=x[-1]
        else: continue
        try: vals.append(float(v))
        except: pass
    if not vals: raise RuntimeError("ETF data unavailable")
    return vals[-1],vals[-5:]

async def liquidity():
    # Public MM Flow endpoints: cross-venue derivatives/liquidations and ETF flows.
    # Used as a market-liquidity stress proxy; no credentials required for these examples.
    j=await get_json("https://www.mmflow.ai/api/v1/perps/liquidations",{"coins":"BTC"})
    d=j.get("data",[])
    if isinstance(d,dict): d=[d]
    longv=float(d[0].get("long24hUsd",0)) if d else 0
    shortv=float(d[0].get("short24hUsd",0)) if d else 0
    return longv,shortv

async def report():
    price,change,volume=await market()
    score=0
    reasons=[]
    try:
        ef,ef5=await etf()
        score+=max(-35,min(35,ef/20))
        if ef5: score+=max(-15,min(15,(sum(ef5)/len(ef5))/30))
        etf_txt=f"${ef:+,.1f}M"
        reasons.append(f"ETF {ef:+.1f}M")
    except Exception as e:
        log.warning("ETF unavailable: %s",e); etf_txt="N/A"; reasons.append("ETF N/A")
    score+=max(-20,min(20,change*3))
    reasons.append("momentum +" if change>0 else "momentum -" if change<0 else "momentum neutral")
    try:
        longv,shortv=await liquidity()
        total=longv+shortv
        # More balanced liquidation flow is neutral; extreme imbalance reduces confidence.
        imbalance=(shortv-longv)/total if total else 0
        liq_score=max(-10,min(10,imbalance*10))
        score+=liq_score
        reasons.append(f"liq {liq_score:+.1f}")
    except Exception as e:
        log.warning("Liquidity unavailable: %s",e); liq_score=0; reasons.append("liq N/A")
    vol_bonus=5 if volume>=1000 else 0
    score+=vol_bonus
    score=max(-100,min(100,int(round(score))))
    sig="🟢 BUY" if score>=65 else "🔴 SELL" if score<=-65 else "🟡 WAIT"
    now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"{sig}\n\nBTC: ${price:,.0f}\n24h: {change:+.2f}%\n"
            f"ETF flow: {etf_txt}\n24h volume: ${volume:,.0f}M\n"
            f"Liquidity score: {liq_score:+.1f}\nSignal: {score:+d}/100\n"
            f"Reasons: {', '.join(reasons)}\nUpdated: {now}\n\n"
            "⚠️ Market signal only; no automatic trading.")

async def start(update,context):
    await update.message.reply_text("BTC ETF + Liquidity Signal Bot v3\n\n/signal — сигнал\n/status — сигнал\n/notify — автоуведомления каждый час")
async def signal(update,context):
    try: await update.message.reply_text(await report())
    except Exception as e:
        log.exception("signal failed"); await update.message.reply_text(f"Ошибка получения данных: {e}")
async def notify(update,context):
    context.job_queue.run_repeating(job,interval=3600,first=5,data=update.effective_chat.id)
    await update.message.reply_text("Автоуведомления включены.")
async def job(context):
    try:
        r=await report()
        if r.startswith("🟢") or r.startswith("🔴"): await context.bot.send_message(context.job.data,r)
    except Exception: log.exception("job failed")
def main():
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("signal",signal))
    app.add_handler(CommandHandler("status",signal)); app.add_handler(CommandHandler("notify",notify))
    app.run_polling()
if __name__=="__main__": main()
