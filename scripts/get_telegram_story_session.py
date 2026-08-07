"""
Разовый локальный скрипт: логинится под личным Telegram-аккаунтом через Telethon
и печатает session string для TELEGRAM_SESSION_STRING (используется для публикации
Stories в канал — обычный Bot API этого не умеет).
Запускать один раз локально (НЕ в Railway).
Перед запуском: pip install telethon

API_ID/API_HASH берутся на https://my.telegram.org (Login → API development tools),
привязаны к номеру телефона того же аккаунта, что должен логиниться.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("TELEGRAM_API_ID: ").strip())
API_HASH = input("TELEGRAM_API_HASH: ").strip()

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\nTELEGRAM_SESSION_STRING=" + client.session.save())
