"""
Catapult Trade — Telegram Bot (Вариант Б)
Верификация через checkReferral GraphQL API
"""

import os
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
API_URL          = os.getenv("API_URL", "https://your-backend.com")
MINIAPP_URL      = os.getenv("MINIAPP_URL", "https://your-miniapp.com")
CATAPULT_JWT     = os.getenv("CATAPULT_JWT", "")   # твой JWT из catapult.trade/settings/api-key
CATAPULT_MY_REF  = os.getenv("CATAPULT_MY_REF", "") # твой ref_code на Catapult Trade
CATAPULT_API     = "https://public-api.catapult.trade/graphql"

# ── GraphQL запросы ───────────────────────────────────────────────────────────

CHECK_REFERRAL_QUERY = """
query CheckReferral($referralCode: String!) {
  checkReferral(referralCode: $referralCode) {
    isValid
    referrer {
      id
      username
    }
  }
}
"""

# ── Тексты ────────────────────────────────────────────────────────────────────

WELCOME_NEW = """👋 Привет, {name}!

Я помогаю людям разобраться, как зарабатывать на Catapult Trade.

Расскажи немного о себе — это поможет мне дать тебе актуальную информацию.

💬 Есть ли у тебя опыт в торговле криптовалютой?"""

QUALIFY_Q2 = """Понял! А что для тебя сейчас важнее:

• Быстро заработать на разнице курсов
• Стабильный пассивный доход
• Участвовать в росте нового проекта с нуля"""

QUALIFY_Q3 = """Отлично. Последний вопрос — сколько времени в день ты готов уделять торговле?

• До 30 минут
• 1–2 часа
• Хочу автоматизировать, тратить минимум времени"""

READY_PITCH = """🚀 Есть кое-что интересное для тебя.

Catapult Trade — платформа, где ты торгуешь и зарабатываешь поинты, которые потом конвертируются в токены.

Токены выйдут на листинг — ранние участники получат максимальную долю.

👇 Посмотри подробности и зарегистрируйся по реф. ссылке:"""

ASK_USERNAME = """✅ Отлично! Как только зарегистрируешься на Catapult Trade — напиши мне свой username с платформы, и я создам твой персональный Mini App.

Твой Mini App будет работать так же как этот — люди смогут записываться к тебе на созвон и регистрироваться по твоей ссылке.

📝 Напиши свой username на Catapult Trade (например: @ivan_trader)"""

VERIFY_WAIT = """🔍 Проверяю регистрацию на Catapult Trade..."""

VERIFY_SUCCESS = """🎉 Подтверждено! Ты в системе, {name}.

Твой персональный Mini App создан — теперь ты можешь делиться им и зарабатывать на активности своих рефералов.

Последний шаг — пришли ссылку на своё расписание (Calendly, Cal.com или любую другую), чтобы люди могли записываться к тебе на созвон.

Если ещё нет — напиши /skip, пока используем твой Telegram."""

VERIFY_FAIL = """❌ Не могу найти username @{username} среди рефералов.

Убедись что:
• Ты зарегистрировался именно по реф. ссылке из этого бота
• Username написан правильно (можно без @)

Попробуй ещё раз или напиши /skip если хочешь пропустить проверку."""

CALENDLY_SAVED = """✅ Готово! Кнопка «Записаться на созвон» в твоём Mini App теперь ведёт на твоё расписание.

Твой Mini App: {miniapp_url}

Делись им и зарабатывай 💰"""

CALENDLY_SKIPPED = """Окей! Пока кнопка «Записаться» будет вести в твой Telegram.

Твой Mini App: {miniapp_url}

Когда добавишь Calendly — просто пришли ссылку сюда."""

ONBOARD_WELCOME = """🎉 Добро пожаловать в команду, {name}!

Твой персональный Mini App создан.

Чтобы люди могли записываться к тебе на созвон, пришли ссылку на своё расписание (Calendly, Cal.com и т.п.).

Если ещё нет — напиши /skip."""

# ── Catapult API ──────────────────────────────────────────────────────────────

async def check_referral_on_catapult(username: str) -> bool:
    """
    Проверяем через checkReferral — зарегистрирован ли пользователь
    с таким username среди рефералов.
    Используем твой JWT токен.
    """
    if not CATAPULT_JWT:
        logger.warning("CATAPULT_JWT не задан — пропускаем верификацию")
        return True  # если токена нет — верим на слово

    clean = username.lstrip("@").strip()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                CATAPULT_API,
                json={
                    "query": CHECK_REFERRAL_QUERY,
                    "variables": {"referralCode": clean}
                },
                headers={
                    "Authorization": f"Bearer {CATAPULT_JWT}",
                    "Content-Type": "application/json"
                },
                timeout=10
            )
            data = resp.json()
            logger.info(f"checkReferral response: {data}")

            result = data.get("data", {}).get("checkReferral", {})
            return result.get("isValid", False)

    except Exception as e:
        logger.error(f"Catapult API error: {e}")
        return False

# ── Backend API ───────────────────────────────────────────────────────────────

async def api_get_user(ref_or_tg_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{API_URL}/users/{ref_or_tg_id}", timeout=5)
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.error(f"api_get_user: {e}")
        return None

async def api_create_user(data: dict) -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{API_URL}/users", json=data, timeout=5)
            return r.json() if r.status_code in (200, 201) else None
    except Exception as e:
        logger.error(f"api_create_user: {e}")
        return None

async def api_update_user(telegram_id: int, data: dict) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.patch(
                f"{API_URL}/users/by-telegram/{telegram_id}",
                json=data, timeout=5
            )
            return r.status_code == 200
    except Exception as e:
        logger.error(f"api_update_user: {e}")
        return False

# ── Клавиатуры ────────────────────────────────────────────────────────────────

def miniapp_keyboard(ref_code: str) -> InlineKeyboardMarkup:
    url = f"{MINIAPP_URL}?ref={ref_code}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📲 Открыть приложение", web_app=WebAppInfo(url=url))
    ]])

def registered_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Я зарегистрировался!", callback_data="i_registered")
    ]])

def qualify_kb_1() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, есть опыт",      callback_data="exp_yes")],
        [InlineKeyboardButton("📖 Немного, изучаю",    callback_data="exp_some")],
        [InlineKeyboardButton("🔰 Нет, новичок",       callback_data="exp_no")],
    ])

def qualify_kb_2() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Быстрый заработок",  callback_data="goal_fast")],
        [InlineKeyboardButton("💎 Пассивный доход",    callback_data="goal_passive")],
        [InlineKeyboardButton("🚀 Ранний участник",    callback_data="goal_early")],
    ])

def qualify_kb_3() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ До 30 минут",        callback_data="time_low")],
        [InlineKeyboardButton("🕐 1–2 часа",           callback_data="time_mid")],
        [InlineKeyboardButton("🤖 Автоматизация",      callback_data="time_auto")],
    ])

# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    inviter_ref = args[0] if args else None
    context.user_data["inviter_ref"] = inviter_ref
    context.user_data["state"] = "qualify_1"

    # Уже зарегистрирован?
    existing = await api_get_user(str(user.id))
    if existing:
        await update.message.reply_text(
            f"С возвращением, {user.first_name}! Вот твой Mini App:",
            reply_markup=miniapp_keyboard(existing["ref_code"])
        )
        return

    await update.message.reply_text(
        WELCOME_NEW.format(name=user.first_name),
        reply_markup=qualify_kb_1()
    )

# ── Квалификация ──────────────────────────────────────────────────────────────

async def qualify_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("exp_"):
        context.user_data["exp"] = data
        await query.edit_message_text(QUALIFY_Q2, reply_markup=qualify_kb_2())

    elif data.startswith("goal_"):
        context.user_data["goal"] = data
        await query.edit_message_text(QUALIFY_Q3, reply_markup=qualify_kb_3())

    elif data.startswith("time_"):
        context.user_data["time"] = data
        context.user_data["state"] = "show_miniapp"
        user = update.effective_user
        inviter_ref = context.user_data.get("inviter_ref") or CATAPULT_MY_REF

        # Создаём пользователя в базе (ещё без catapult_username)
        await api_create_user({
            "telegram_id": str(user.id),
            "username": user.username or "",
            "name": user.first_name,
            "inviter_ref": inviter_ref,
            "qualify_data": {
                "exp":  context.user_data.get("exp"),
                "goal": context.user_data.get("goal"),
                "time": context.user_data.get("time"),
            }
        })

        # Показываем Mini App с реф. ссылкой + кнопку "Я зарегистрировался"
        user_data = await api_get_user(str(user.id))
        ref_code = user_data["ref_code"] if user_data else str(user.id)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📲 Открыть приложение", web_app=WebAppInfo(url=f"{MINIAPP_URL}?ref={ref_code}"))],
            [InlineKeyboardButton("✅ Я зарегистрировался на Catapult!", callback_data="i_registered")]
        ])
        await query.edit_message_text(READY_PITCH, reply_markup=kb)

# ── "Я зарегистрировался" ─────────────────────────────────────────────────────

async def i_registered_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["state"] = "awaiting_username"
    await query.edit_message_text(ASK_USERNAME)

# ── Получаем username и верифицируем ─────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    state = context.user_data.get("state", "")
    user = update.effective_user

    # ── Ждём username для верификации ──
    if state == "awaiting_username":
        await update.message.reply_text(VERIFY_WAIT)

        is_valid = await check_referral_on_catapult(text)

        if is_valid:
            # Сохраняем catapult username
            clean_username = text.lstrip("@").strip()
            await api_update_user(user.id, {"catapult_username": clean_username, "onboarded": 1})
            context.user_data["state"] = "awaiting_calendly"

            user_data = await api_get_user(str(user.id))
            ref_code = user_data["ref_code"] if user_data else str(user.id)

            kb = miniapp_keyboard(ref_code)
            await update.message.reply_text(
                VERIFY_SUCCESS.format(name=user.first_name),
                reply_markup=kb
            )
        else:
            await update.message.reply_text(VERIFY_FAIL.format(username=text))
        return

    # ── Ждём Calendly ссылку ──
    if state == "awaiting_calendly":
        if text.startswith("http"):
            await api_update_user(user.id, {"calendly_link": text})
            user_data = await api_get_user(str(user.id))
            ref_code = user_data["ref_code"] if user_data else str(user.id)
            miniapp_url = f"{MINIAPP_URL}?ref={ref_code}"
            await update.message.reply_text(
                CALENDLY_SAVED.format(miniapp_url=miniapp_url),
                reply_markup=miniapp_keyboard(ref_code)
            )
            context.user_data["state"] = "done"
        else:
            await update.message.reply_text(
                "Пришли ссылку (начинается с https://) или напиши /skip"
            )
        return

    # ── Уже зарегистрирован — обновляет Calendly ──
    existing = await api_get_user(str(user.id))
    if existing and text.startswith("http"):
        await api_update_user(user.id, {"calendly_link": text})
        miniapp_url = f"{MINIAPP_URL}?ref={existing['ref_code']}"
        await update.message.reply_text(
            CALENDLY_SAVED.format(miniapp_url=miniapp_url),
            reply_markup=miniapp_keyboard(existing["ref_code"])
        )

async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = await api_get_user(str(user.id))

    if not user_data:
        await update.message.reply_text("Сначала пройди регистрацию — напиши /start")
        return

    miniapp_url = f"{MINIAPP_URL}?ref={user_data['ref_code']}"
    context.user_data["state"] = "done"
    await update.message.reply_text(
        CALENDLY_SKIPPED.format(miniapp_url=miniapp_url),
        reply_markup=miniapp_keyboard(user_data["ref_code"])
    )

async def my_app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await api_get_user(str(update.effective_user.id))
    if user_data:
        await update.message.reply_text(
            "Твой Mini App:",
            reply_markup=miniapp_keyboard(user_data["ref_code"])
        )
    else:
        await update.message.reply_text("Сначала пройди регистрацию — напиши /start")

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("myapp",  my_app_cmd))
    app.add_handler(CommandHandler("skip",   skip_cmd))

    app.add_handler(CallbackQueryHandler(qualify_cb,       pattern="^(exp_|goal_|time_)"))
    app.add_handler(CallbackQueryHandler(i_registered_cb,  pattern="^i_registered$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started (Вариант Б — checkReferral верификация)")
    app.run_polling()

if __name__ == "__main__":
    main()
