import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from pybit.unified_trading import HTTP
import pandas as pd

# --- НАСТРОЙКИ ---
TELEGRAM_TOKEN = 'ВАШ_ТЕЛЕГРАМ_ТОКЕН'
CHAT_ID = 'ВАШ_CHAT_ID'  # Кому отправлять уведомления
THRESHOLD_PERCENT = 2.0    # Порог аномальной активности (в %)
CHECK_INTERVAL = 60    # Интервал проверки (в секутндах, 3600 = 1 час)

# Инициализация
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
session = HTTP(testnet=False)

logging.basicConfig(level=logging.INFO)

async def check_bybit_activity():
    """Функция для анализа цен на Bybit"""
    try:
        # 1. Получаем тикеры всех USDT пар
        tickers = session.get_tickers(category="spot")
        all_tickers = tickers['result']['list']
        
        # Фильтруем только USDT пары и сортируем по объему (условно берем топ)
        # Для полноценного ТОП-50 лучше использовать данные по 24h Volume
        usdt_pairs = [t for t in all_tickers if t['symbol'].endswith('USDT')]
        
        # Сортируем по объему за 24ч (desc)
        top_50 = sorted(usdt_pairs, key=lambda x: float(x['turnover24h']), reverse=True)[:50]

        anomalies = []

        for ticker in top_50:
            symbol = ticker['symbol']
            price_change = float(ticker['priceChangePercent'])
            
            # Проверяем на аномальное движение (в обе стороны)
            if abs(price_change) >= THRESHOLD_PERCENT:
                direction = "📈 РОСТ" if price_change > 0 else "📉 ПАДЕНИЕ"
                anomalies.append(f"{direction} {symbol}: {price_change}%")

        if anomalies:
            message = "⚠️ **Обнаружена аномальная активность на Bybit:**\n\n" + "\n".join(anomalies)
            await bot.send_message(CHAT_ID, message, parse_mode="Markdown")
        else:
            logging.info("Аномалий не обнаружено.")

    except Exception as e:
        logging.error(f"Ошибка при проверке: {e}")
        await bot.send_message(CHAT_ID, f"❌ Ошибка мониторинга: {e}")

async def scheduler():
    """Цикл планировщика"""
    while True:
        await check_bybit_activity()
        await asyncio.sleep(CHECK_INTERVAL)

async def main():
    # Запуск планировщика в фоновом режиме
    asyncio.create_task(scheduler())
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")