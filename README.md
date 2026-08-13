# BTC ETF + Liquidity Telegram Signal Bot

MVP-версия. Бот выдаёт BUY/SELL/WAIT и не совершает сделки.

## 1. Создать Telegram-бота
Откройте @BotFather в Telegram, выполните /newbot и получите токен.

## 2. Установка
Python 3.11+:

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Windows:

    .venv\Scripts\activate
    pip install -r requirements.txt

Скопируйте `.env.example` в `.env` и задайте `TELEGRAM_BOT_TOKEN`.
Переменные окружения можно экспортировать напрямую.

## 3. Запуск

Linux/macOS:
    export TELEGRAM_BOT_TOKEN="..."
    python bot.py

Windows PowerShell:
    $env:TELEGRAM_BOT_TOKEN="..."
    python bot.py

## Команды
/start
/signal
/status
/notify

## Важно
ETF flow берётся из публичной таблицы Farside. Это MVP: полноценный блок liquidity (stablecoins, global M2, exchange liquidity) ещё нужно подключить отдельными API.
Перед использованием реальных денег стратегию следует протестировать на истории и paper-trading.
