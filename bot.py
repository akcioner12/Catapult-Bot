"""
Основной пользовательский бот (@catapulttrade_guide_bot)
- /start + живой прогрев через Claude (warmup dialog)
- Квиз про Catapult Trade
- /connect, /disconnect — привязка API ключа Catapult
- Legacy-флоу квалификации (резервный, /legacy_start)
- Режим поддержки после викторины — бот продолжает отвечать, когда пользователь
  возвращается из Mini App с вопросами

ВАЖНО: этот файл слушает MAIN_BOT_TOKEN (@catapulttrade_guide_bot).
Парсер/одобрение постов живёт в отдельном файле parser.py и слушает PARSER_BOT_TOKEN.
"""

import os
import asyncio
import logging

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────
MAIN_BOT_TOKEN   = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_API_URL   = "https://api.anthropic.com/v1/messages"

BACKEND_URL      = os.getenv("BACKEND_URL", "https://web-production-9851f.up.railway.app")
MINIAPP_URL      = os.getenv("MINIAPP_URL", "https://akcioner12.github.io/Catapult-Trade/")
CATAPULT_GRAPHQL = "https://public-api.catapult.trade/graphql"

# ── Legacy bot.py config (восстановленный флоу квалификации) ─────────────────
API_URL          = os.getenv("API_URL", BACKEND_URL)
CATAPULT_JWT     = os.getenv("CATAPULT_JWT", "")
CATAPULT_MY_REF  = os.getenv("CATAPULT_MY_REF", "")

# ════════════════════════════════════════════════════════════════════════════
# ── WARMUP DIALOG — живой прогрев пользователя через Claude ─────────────────
# ════════════════════════════════════════════════════════════════════════════

WARMUP_SYSTEM_PROMPT = """Ты — живой, обаятельный собеседник в Telegram. Тема разговора — крипта, трейдинг, заработок с AI, но ты не сухой консультант, а человек с характером.

ТВОЯ ЗАДАЧА:
Веди живой, но БЫСТРЫЙ диалог — без затягивания. Структура:
1. Первое сообщение — лёгкое человеческое знакомство, без крипты (1 реплика).
2. Сразу после ответа человека на знакомство — переходи к теме крипты/трейдинга/заработка. Не задерживайся на small-talk дольше одного обмена репликами.
3. Дальше максимум 2 коротких вопроса по теме (опыт в крипте/трейдинге, и пробовал ли зарабатывать с AI или что сейчас интересно по деньгам) — и сразу переходи к предложению Catapult.

Итого: первое сообщение (знакомство) → ответ → 1-2 сообщения по теме крипты/AI → ответы → предложение. Это значит после знакомства у тебя есть максимум 2 своих сообщения с вопросами, прежде чем переходить к ШАГУ 1 предложения Catapult ниже. Не растягивай больше.

Узнать по ходу нужно по минимуму, не как анкету:
- Есть ли вообще опыт в криптовалюте/трейдинге (можно одним вопросом)
- Что сейчас интересно по деньгам/заработку (можно вторым вопросом, без отдельного вопроса про AI если не зашло органично)

СТИЛЬ И РАЗНООБРАЗИЕ:
- Каждый раз веди себя немного по-разному — то с юмором, то более вдумчиво, то с лёгким азартом. Не повторяй одни и те же фразы и конструкции от диалога к диалогу.
- Реагируй конкретно на то, что СКАЗАЛ собеседник — переспрашивай детали, удивляйся, соглашайся или мягко спорь, шути в тему.
- Можешь делиться короткими наблюдениями или мнением от первого лица — не только спрашивай.
- Короткие сообщения (1-4 предложения). Никаких лекций и списков.
- Один акцент за раз — не вываливай несколько вопросов сразу.
- Используй живую разговорную речь, можно с лёгким сленгом, эмодзи — по чуть-чуть, не через каждое слово.
- НЕ упоминай Catapult Trade и не давай ссылок, пока сам не решишь что собеседник прогрет.
- Форматирование: если нужно выделить текст, используй HTML-теги Telegram (<b>текст</b>), НИКОГДА не используй markdown со звёздочками **текст**.

ОТКРЫВАЮЩЕЕ СООБЩЕНИЕ:
Если это первое сообщение в разговоре (от тебя, до ответа пользователя) — начни с обычного человеческого знакомства, без захода в тему крипты/трейдинга сразу.
Поздоровайся естественно и задай один лёгкий, ненавязчивый вопрос о человеке — например, откуда он, как дела, чем занимается по жизни, как настроение. НЕ упоминай крипту, трейдинг, биткоин или заработок в этом первом сообщении вообще.
Тему крипты/трейдинга заводи только в следующих сообщениях, после того как человек ответит на твой простой вопрос — и тоже органично, отталкиваясь от того что он сказал, а не резким поворотом.
Каждый раз вступление должно звучать по-новому, но всегда оставайся в рамках простого, тёплого знакомства — без анкетности и без явного "продающего" тона.

КОГДА ПОДВОДИТЬ К CATAPULT (ДВА ШАГА — ВАЖНО, НЕ ПРОПУСКАЙ ШАГ СОГЛАСИЯ):

ШАГ 1 — Предложение:
Переходи к предложению быстро — после знакомства и максимум 2 содержательных ответов пользователя по теме крипты/заработка (не позже). Не жди дополнительных деталей, не углубляйся.
Когда переходишь — органично, своими словами подведи к тому, что у тебя есть интересное предложение: платформа Catapult Trade (честная математика без манипуляций, Provably Fair, поинты за каждую сделку конвертируются в будущий аирдроп).
Затем СПРОСИ РАЗРЕШЕНИЕ — своими словами предложи ответить на 3 коротких вопроса, чтобы ты мог подобрать ему более точную стратегию заработка под его профиль. Не отправляй вопросы сразу — только спроси согласие и подожди ответа.
Сформулируй это предложение каждый раз по-своему, не шаблонно.
Сразу после текста этого предложения добавь на новой строке ТОЧНО эту строку (без кавычек, без изменений):
[ASKED_PERMISSION]

ШАГ 2 — Запуск викторины (только после ШАГА 1, в следующем сообщении пользователя):
Если в истории твоё предыдущее сообщение заканчивалось на [ASKED_PERMISSION] — посмотри на ответ собеседника:
- Если он согласился (даже коротко — "да", "ок", "давай", "погнали", любое явное согласие) — напиши короткую фразу-переход ("Отлично, тогда начнём" или похожее, своими словами) и сразу после неё на новой строке добавь ТОЧНО:
[READY_FOR_QUIZ]
- Если он отказался или явно не хочет — прояви уважение, не дави, можно мягко продолжить обычный разговор или закончить его. НЕ добавляй никакой служебной строки в этом случае.
- Если ответ неоднозначный — переспроси мягко, без служебных строк.

Если собеседник в любой момент явно не интересуется темой вообще (грубит, игнорирует, пишет что ему это не нужно) — прояви уважение, мягко закончи разговор без перехода к предложению или викторине.

Отвечай только текстом следующего сообщения от своего лица — без META-комментариев, без пояснений о своей стратегии."""

CATAPULT_QUIZ = [
    {
        "question": "1️⃣ Что для тебя привычнее — спекуляция на новостях или системный подход со стратегией?",
        "options": ["Спекуляция на новостях", "Системный подход", "Пока не торговал вообще"],
    },
    {
        "question": "2️⃣ Что важнее при выборе платформы для торговли?",
        "options": ["Низкий депозит для входа", "Честность алгоритма (без манипуляций)", "Пассивный доход без усилий"],
    },
    {
        "question": "3️⃣ Готов попробовать платформу с минимальным депозитом $2 и без KYC?",
        "options": ["Да, готов попробовать", "Хочу сначала изучить детальнее", "Пока не готов"],
    },
]


async def claude_warmup_reply(history: list) -> str:
    """Отправляет историю диалога в Claude, получает следующую реплику."""
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 400,
                    "system": WARMUP_SYSTEM_PROMPT,
                    "messages": messages
                }
            )
            data = resp.json()
            if "content" in data and data["content"]:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Warmup Claude error: {e}")
    return "Расскажи чуть больше — интересно узнать про твой опыт!"


async def claude_generate_opening(first_name: str) -> str:
    """Генерирует свежее, каждый раз разное открывающее сообщение через Claude."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 250,
                    "system": WARMUP_SYSTEM_PROMPT,
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"[СИСТЕМНАЯ ИНСТРУКЦИЯ, не показывай это пользователю] "
                            f"Это начало нового разговора с пользователем по имени {first_name or 'друг'}. "
                            f"Напиши своё первое сообщение — живое, нешаблонное приветствие с заходом в тему крипты/трейдинга/заработка, "
                            f"как описано в системном промпте. Используй имя пользователя естественно, не в каждом сообщении обязательно."
                        )
                    }]
                }
            )
            data = resp.json()
            if "content" in data and data["content"]:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude opening generation error: {e}")
    return f"👋 Привет{f', {first_name}' if first_name else ''}! Расскажи — у тебя есть опыт в крипте или трейдинге?"


# ════════════════════════════════════════════════════════════════════════════
# ── SUPPORT MODE — продолжение диалога ПОСЛЕ викторины (stage = done) ───────
# ════════════════════════════════════════════════════════════════════════════

SUPPORT_MODE_SYSTEM_PROMPT = """Ты — дружелюбный помощник в Telegram-боте Catapult Trade. Человек уже прошёл знакомство и викторину, увидел ссылку на Mini App.

ТВОЯ ЗАДАЧА СЕЙЧАС:
Человек может вернуться в бота с любым вопросом — не понял что делать в приложении, не разобрался с регистрацией на платформе, хочет узнать детали, или просто продолжает общаться. Твоя цель — помочь и поддержать разговор, не быть назойливым.

ЧТО ТЫ ЗНАЕШЬ О ПЛАТФОРМЕ И MINI APP:
- Mini App показывает: бегущую строку токенов, топ роста/падения, общую статистику платформы, разделы "Заработок" и "Стратегии" с объяснениями как работает Catapult, раздел "Рефералы" с реф. ссылкой, и Личный Кабинет (открывается после привязки аккаунта Catapult через API-ключ).
- Если человек не понимает что делать в приложении — объясни простыми словами: внизу есть вкладки (Главная, Рынок, Заработок, Рефералы, Кабинет), можно просто полистать и почитать про способы заработка, либо сразу перейти на catapult.trade по кнопке "Начать торговать" чтобы зарегистрироваться.
- Чтобы открыть Личный Кабинет с балансом — нужно зарегистрироваться на catapult.trade, затем в Настройках найти API Key, и привязать его через кнопку "Подключить аккаунт Catapult" в этом боте.
- Минимальный депозит на платформе $2, без KYC.

СТИЛЬ:
Живой, короткий, по-дружески. Отвечай конкретно на то что спросили. Если не уверен в технической детали — будь честен, что лучше проверить в самом приложении или на сайте, не выдумывай факты.

ФОРМАТИРОВАНИЕ — ТОЛЬКО HTML-теги Telegram, НЕ markdown:
- жирный текст: <b>текст</b>
- курсив: <i>текст</i>
- НИКОГДА не используй звёздочки **текст** — это не сработает в Telegram и будет выглядеть как мусор.

Отвечай только текстом следующего сообщения от своего лица."""


async def claude_support_reply(history: list) -> str:
    """Генерирует ответ в режиме поддержки после прохождения викторины."""
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 350,
                    "system": SUPPORT_MODE_SYSTEM_PROMPT,
                    "messages": messages
                }
            )
            data = resp.json()
            if "content" in data and data["content"]:
                return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Support mode Claude error: {e}")
    return "Расскажи подробнее, что именно непонятно — помогу разобраться!"


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сообщения от пользователя ПОСЛЕ прохождения викторины (stage=done)."""
    tg_id = str(update.effective_user.id)
    user_text = update.message.text.strip()

    state = await get_dialog_state(tg_id)
    support_history = state.get("support_history", [])
    support_history.append({"role": "user", "content": user_text})

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await claude_support_reply(support_history)
    support_history.append({"role": "assistant", "content": reply})

    state["support_history"] = support_history
    await save_dialog_state(tg_id, state)

    keyboard = {
        "inline_keyboard": [
            [{"text": "📱 Открыть приложение", "web_app": {"url": MINIAPP_URL}}],
            [{"text": "🔑 Подключить аккаунт Catapult", "callback_data": "connect_start"}],
        ]
    }
    try:
        await update.message.reply_text(reply, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.warning(f"HTML parse failed in support reply, sending plain: {e}")
        await update.message.reply_text(reply, reply_markup=keyboard)


async def get_dialog_state(telegram_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BACKEND_URL}/dialog/{telegram_id}")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.error(f"get_dialog_state error: {e}")
    return {"history": [], "stage": "chatting", "quiz_answers": [], "quiz_step": 0, "support_history": []}


async def save_dialog_state(telegram_id: str, state: dict):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{BACKEND_URL}/dialog/{telegram_id}", json=state)
    except Exception as e:
        logger.error(f"save_dialog_state error: {e}")


async def send_quiz_question(chat_id: int, step: int):
    """Отправляет вопрос викторины №step (0-indexed) с кнопками-вариантами."""
    q = CATAPULT_QUIZ[step]
    keyboard = {
        "inline_keyboard": [
            [{"text": opt, "callback_data": f"quizans_{step}_{i}"}]
            for i, opt in enumerate(q["options"])
        ]
    }
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": q["question"],
                "reply_markup": keyboard
            }
        )


async def handle_warmup_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает свободный текст от пользователя во время прогрева.
    Вызывается из handle_text, когда не ждём API ключ.
    """
    user_id = update.effective_user.id
    tg_id = str(user_id)
    user_text = update.message.text.strip()

    state = await get_dialog_state(tg_id)

    # После викторины — отдельный режим поддержки, не игнорируем сообщение
    if state["stage"] == "done":
        await handle_support_message(update, context)
        return

    # Во время самой викторины — кнопки отвечают сами, текст игнорируем
    if state["stage"] == "quiz":
        return

    state["history"].append({"role": "user", "content": user_text})

    # Показываем "печатает..."
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    reply = await claude_warmup_reply(state["history"])

    ready_for_quiz = "[READY_FOR_QUIZ]" in reply
    asked_permission = "[ASKED_PERMISSION]" in reply

    # Сохраняем в историю ПОЛНЫЙ ответ с маркером — Claude должен видеть свой предыдущий шаг
    history_reply = reply.strip()
    # Пользователю показываем текст без служебных маркеров
    clean_reply = reply.replace("[READY_FOR_QUIZ]", "").replace("[ASKED_PERMISSION]", "").strip()

    if clean_reply:
        state["history"].append({"role": "assistant", "content": history_reply})
        try:
            await update.message.reply_text(clean_reply, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"HTML parse failed in warmup reply, sending plain: {e}")
            await update.message.reply_text(clean_reply)

    if ready_for_quiz:
        state["stage"] = "quiz"
        state["quiz_step"] = 0
        state["quiz_answers"] = []
        await save_dialog_state(tg_id, state)

        await asyncio.sleep(1.2)
        await send_quiz_question(update.effective_chat.id, 0)
    else:
        await save_dialog_state(tg_id, state)


async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия кнопки в викторине про Catapult."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    tg_id = str(user_id)

    # callback_data формат: quizans_{step}_{option_index}
    _, step_str, opt_str = query.data.split("_")
    step = int(step_str)
    opt_idx = int(opt_str)

    state = await get_dialog_state(tg_id)
    if state["stage"] != "quiz":
        return  # неактуальный квиз (например, повторное нажатие старой кнопки)

    chosen_text = CATAPULT_QUIZ[step]["options"][opt_idx]
    state["quiz_answers"].append({"q": step, "answer": chosen_text})

    await query.edit_message_text(f"{CATAPULT_QUIZ[step]['question']}\n\n✅ {chosen_text}")

    next_step = step + 1
    if next_step < len(CATAPULT_QUIZ):
        state["quiz_step"] = next_step
        await save_dialog_state(tg_id, state)
        await asyncio.sleep(0.6)
        await send_quiz_question(update.effective_chat.id, next_step)
    else:
        # Викторина завершена — выдаём ссылку на Mini App
        state["stage"] = "done"
        await save_dialog_state(tg_id, state)

        keyboard = {
            "inline_keyboard": [
                [{"text": "📱 Открыть приложение", "web_app": {"url": MINIAPP_URL}}],
                [{"text": "🔑 Подключить аккаунт Catapult", "callback_data": "connect_start"}],
            ]
        }
        await asyncio.sleep(0.6)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🎉 <b>Отлично, ты в деле!</b>\n\n"
                "Вот приложение с полной статистикой платформы — котировки, топ токенов, объёмы торгов и заработок.\n\n"
                "Когда зарегистрируешься — подключи аккаунт и увидишь свой личный кабинет прямо здесь."
            ),
            parse_mode="HTML",
            reply_markup=keyboard
        )


async def cmd_reset_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """На случай если пользователь хочет начать прогрев заново"""
    tg_id = str(update.effective_user.id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(f"{BACKEND_URL}/dialog/{tg_id}")
    except Exception as e:
        logger.error(f"reset dialog error: {e}")
    await update.message.reply_text("🔄 Окей, начнём с начала! Расскажи — какой у тебя опыт в крипте и трейдинге?")
    state = {"history": [], "stage": "chatting", "quiz_answers": [], "quiz_step": 0, "support_history": []}
    await save_dialog_state(tg_id, state)


# ════════════════════════════════════════════════════════════════════════════
# ── LEGACY FLOW (восстановлено из bot.py) — резервный кнопочный флоу ───────
# ════════════════════════════════════════════════════════════════════════════

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


async def check_referral_on_catapult(username: str) -> bool:
    if not CATAPULT_JWT:
        logger.warning("CATAPULT_JWT не задан — пропускаем верификацию")
        return True

    clean = username.lstrip("@").strip()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                CATAPULT_GRAPHQL,
                json={"query": CHECK_REFERRAL_QUERY, "variables": {"referralCode": clean}},
                headers={"Authorization": f"Bearer {CATAPULT_JWT}", "Content-Type": "application/json"},
                timeout=10
            )
            data = resp.json()
            logger.info(f"checkReferral response: {data}")
            result = data.get("data", {}).get("checkReferral", {})
            return result.get("isValid", False)
    except Exception as e:
        logger.error(f"Catapult API error: {e}")
        return False


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
            r = await client.patch(f"{API_URL}/users/by-telegram/{telegram_id}", json=data, timeout=5)
            return r.status_code == 200
    except Exception as e:
        logger.error(f"api_update_user: {e}")
        return False


def miniapp_keyboard(ref_code: str) -> InlineKeyboardMarkup:
    url = f"{MINIAPP_URL}?ref={ref_code}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📲 Открыть приложение", web_app=WebAppInfo(url=url))
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


async def cmd_legacy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Резервный кнопочный флоу квалификации, доступен через /legacy_start, не основной."""
    user = update.effective_user
    args = context.args

    if args and args[0] == "connect":
        await start_connect_flow(update, context)
        return

    inviter_ref = args[0] if args else None
    context.user_data["inviter_ref"] = inviter_ref
    context.user_data["state"] = "qualify_1"

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

        user_data = await api_get_user(str(user.id))
        ref_code = user_data["ref_code"] if user_data else str(user.id)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📲 Открыть приложение", web_app=WebAppInfo(url=f"{MINIAPP_URL}?ref={ref_code}"))],
            [InlineKeyboardButton("✅ Я зарегистрировался на Catapult!", callback_data="i_registered")]
        ])
        await query.edit_message_text(READY_PITCH, reply_markup=kb)


async def i_registered_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["state"] = "awaiting_username"
    await query.edit_message_text(ASK_USERNAME)


async def handle_legacy_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Обрабатывает текстовые сообщения для legacy-флоу (username верификация, Calendly).
    Возвращает True если сообщение было обработано здесь, False если нужно передать дальше.
    """
    text = update.message.text.strip()
    state = context.user_data.get("state", "")
    user = update.effective_user

    if state == "awaiting_username":
        await update.message.reply_text(VERIFY_WAIT)
        is_valid = await check_referral_on_catapult(text)

        if is_valid:
            clean_username = text.lstrip("@").strip()
            await api_update_user(user.id, {"catapult_username": clean_username, "onboarded": 1})
            context.user_data["state"] = "awaiting_calendly"

            user_data = await api_get_user(str(user.id))
            ref_code = user_data["ref_code"] if user_data else str(user.id)

            await update.message.reply_text(
                VERIFY_SUCCESS.format(name=user.first_name),
                reply_markup=miniapp_keyboard(ref_code)
            )
        else:
            await update.message.reply_text(VERIFY_FAIL.format(username=text))
        return True

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
            await update.message.reply_text("Пришли ссылку (начинается с https://) или напиши /skip")
        return True

    existing = await api_get_user(str(user.id))
    if existing and text.startswith("http"):
        await api_update_user(user.id, {"calendly_link": text})
        miniapp_url = f"{MINIAPP_URL}?ref={existing['ref_code']}"
        await update.message.reply_text(
            CALENDLY_SAVED.format(miniapp_url=miniapp_url),
            reply_markup=miniapp_keyboard(existing["ref_code"])
        )
        return True

    return False


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
        await update.message.reply_text("Твой Mini App:", reply_markup=miniapp_keyboard(user_data["ref_code"]))
    else:
        await update.message.reply_text("Сначала пройди регистрацию — напиши /start")


# ════════════════════════════════════════════════════════════════════════════
# ── ОСНОВНОЙ /start — живой прогрев через Claude ─────────────────────────────
# ════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Основной /start — живой диалог-прогрев через Claude.
    /start connect (deep link из Mini App) — сразу переход к привязке API ключа, минуя прогрев.
    """
    args = context.args
    if args and args[0] == "connect":
        await start_connect_flow(update, context)
        return

    tg_id = str(update.effective_user.id)
    state = await get_dialog_state(tg_id)

    if state["stage"] == "done":
        keyboard = {
            "inline_keyboard": [
                [{"text": "📱 Открыть приложение", "web_app": {"url": MINIAPP_URL}}],
                [{"text": "🔑 Подключить аккаунт Catapult", "callback_data": "connect_start"}],
            ]
        }
        await update.message.reply_text("👋 Привет снова! Вот твоё приложение:", reply_markup=keyboard)
        return

    if state["stage"] == "quiz":
        await update.message.reply_text("👋 Продолжим викторину с того места, где остановились!")
        await send_quiz_question(update.effective_chat.id, state["quiz_step"])
        return

    if state["history"]:
        await update.message.reply_text("👋 Привет снова! Продолжим разговор?")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    first_name = update.effective_user.first_name or ""
    opening = await claude_generate_opening(first_name)
    state["history"] = [{"role": "assistant", "content": opening}]
    await save_dialog_state(tg_id, state)
    try:
        await update.message.reply_text(opening, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"HTML parse failed in opening message, sending plain: {e}")
        await update.message.reply_text(opening)


async def start_connect_flow(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    """Первый экран выбора: Инструкция / Ввести ключ. Ключ пока НЕ запрашивается."""
    text = (
        "🔑 <b>Привязка аккаунта Catapult Trade</b>\n\n"
        "Чтобы открыть Личный Кабинет в Mini App, нужен твой API ключ с catapult.trade.\n\n"
        "Выбери действие:"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "📖 Инструкция", "callback_data": "connect_howto"}],
            [{"text": "🔑 Ввести ключ", "callback_data": "connect_enter_key"}],
            [{"text": "❌ Отмена", "callback_data": "connect_cancel"}],
        ]
    }
    context.user_data['awaiting_api_key'] = False

    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_connect_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Подключить аккаунт Catapult' из /start"""
    query = update.callback_query
    await query.answer()
    await start_connect_flow(query, context)


async def handle_connect_howto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Инструкция' — как найти ключ на catapult.trade"""
    query = update.callback_query
    await query.answer()

    text = (
        "📖 <b>Как найти API ключ на Catapult Trade</b>\n\n"
        "1️⃣ Открой <b>catapult.trade</b> в браузере\n\n"
        "2️⃣ Войди в свой аккаунт\n\n"
        "3️⃣ Открой меню (значок ☰ <b>Menu</b> снизу)\n\n"
        "4️⃣ Найди раздел <b>API Key</b> (значок 🔑)\n\n"
        "5️⃣ Скопируй ключ кнопкой <b>Copy</b>\n\n"
        "6️⃣ Вернись сюда и нажми «Ввести ключ»\n\n"
        "⚠️ <b>Важно:</b> никогда не отправляй этот ключ никому кроме этого бота."
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "🌐 Открыть Catapult Trade", "url": "https://catapult.trade/r/akcioner12"}],
            [{"text": "🔑 Ввести ключ", "callback_data": "connect_enter_key"}],
            [{"text": "⬅️ Назад", "callback_data": "connect_retry"}],
        ]
    }
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    context.user_data['awaiting_api_key'] = False


async def handle_connect_enter_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Ввести ключ' — теперь реально ждём сообщение с ключом"""
    query = update.callback_query
    await query.answer()

    text = (
        "🔑 <b>Вставь свой API ключ</b>\n\n"
        "Скопируй ключ из Catapult (Menu → API Key → Copy) и отправь его следующим сообщением."
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "📖 Инструкция", "callback_data": "connect_howto"}],
            [{"text": "❌ Отмена", "callback_data": "connect_cancel"}],
        ]
    }
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    context.user_data['awaiting_api_key'] = True


async def handle_connect_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Назад' — на первый экран выбора"""
    query = update.callback_query
    await query.answer()
    await start_connect_flow(query, context)


async def handle_connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_api_key'] = False
    await query.edit_message_text("❌ Привязка отменена.")


async def handle_api_key_for_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Проверяет, ждём ли мы от этого пользователя API ключ; если да — обрабатывает.
    """
    if not context.user_data.get('awaiting_api_key'):
        return  # обычное сообщение не по теме — игнорируем

    api_key = update.message.text.strip()

    if not api_key.startswith('eyJ') or len(api_key) < 50:
        await update.message.reply_text(
            "⚠️ Это не похоже на API ключ.\n\n"
            "Ключ должен начинаться с <code>eyJ</code> и быть длинной строкой.\n"
            "Попробуй ещё раз или нажми «Отмена».",
            parse_mode="HTML",
            reply_markup={"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "connect_cancel"}]]}
        )
        return

    await update.message.reply_text("⏳ Проверяю ключ...")

    profile_name = "Трейдер"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            test_resp = await client.post(
                CATAPULT_GRAPHQL,
                json={"query": "{ userMe { id profileName } }"},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            )
            test_data = test_resp.json()
            user_me = test_data.get("data", {}).get("userMe")

            if not user_me:
                await update.message.reply_text(
                    "❌ Ключ не прошёл проверку.\n\n"
                    "Убедись что скопировал ключ полностью из раздела <b>API Key</b> на catapult.trade",
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[{"text": "🔄 Попробовать снова", "callback_data": "connect_retry"}]]}
                )
                return
            profile_name = user_me.get("profileName") or user_me.get("id", "Трейдер")
    except Exception as e:
        logger.error(f"API key validation error: {e}")
        # Не блокируем сохранение — сохраним как есть, проверим позже в Mini App

    tg_id = str(update.effective_user.id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            check = await client.get(f"{BACKEND_URL}/users/{tg_id}")
            if check.status_code == 200:
                await client.patch(
                    f"{BACKEND_URL}/users/by-telegram/{tg_id}",
                    json={"catapult_jwt": api_key}
                )
            else:
                name = update.effective_user.full_name or "Трейдер"
                username = update.effective_user.username or ""
                await client.post(
                    f"{BACKEND_URL}/users",
                    json={"telegram_id": tg_id, "username": username, "name": name, "catapult_jwt": api_key}
                )
    except Exception as e:
        logger.error(f"DB save error: {e}")
        await update.message.reply_text(f"❌ Ошибка сохранения: {e}")
        context.user_data['awaiting_api_key'] = False
        return

    context.user_data['awaiting_api_key'] = False

    await update.message.reply_text(
        f"✅ <b>Аккаунт подключён!</b>\n\n"
        f"👤 Профиль: <b>{profile_name}</b>\n\n"
        f"Открой Mini App — Личный Кабинет уже разблокирован 🎉",
        parse_mode="HTML",
        reply_markup={"inline_keyboard": [[
            {"text": "📱 Открыть Mini App", "web_app": {"url": MINIAPP_URL}}
        ]]}
    )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отвязать аккаунт Catapult — доступно всем пользователям"""
    tg_id = str(update.effective_user.id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(
                f"{BACKEND_URL}/users/by-telegram/{tg_id}",
                json={"catapult_jwt": None}
            )
            if resp.status_code == 200:
                await update.message.reply_text("✅ Аккаунт Catapult отвязан.")
            else:
                await update.message.reply_text("⚠️ Пользователь не найден в базе.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запасной вариант — команда /connect делает то же самое, что кнопка"""
    await start_connect_flow(update, context)


# ── Маршрутизация обычного текста ─────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Все текстовые сообщения от пользователей этого бота.
    Порядок проверки:
    1) legacy-флоу (ожидание username/calendly из восстановленного bot.py)
    2) ожидание API ключа Catapult
    3) живой диалог-прогрев / режим поддержки после викторины (warmup)
    """
    handled = await handle_legacy_text(update, context)
    if handled:
        return
    if context.user_data.get('awaiting_api_key'):
        await handle_api_key_for_users(update, context)
    else:
        await handle_warmup_message(update, context)


# ── Запуск ────────────────────────────────────────────────────────────────────
async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)


async def main():
    if not MAIN_BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения!")

    app = Application.builder().token(MAIN_BOT_TOKEN).build()

    app.add_error_handler(global_error_handler)

    # ── Catapult Connect и пользовательский флоу ──
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("myapp", my_app_cmd))
    app.add_handler(CallbackQueryHandler(qualify_cb,      pattern="^(exp_|goal_|time_)"))
    app.add_handler(CallbackQueryHandler(i_registered_cb, pattern="^i_registered$"))
    app.add_handler(CommandHandler("legacy_start", cmd_legacy_start))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CallbackQueryHandler(handle_connect_start_button, pattern="^connect_start$"))
    app.add_handler(CallbackQueryHandler(handle_connect_howto,        pattern="^connect_howto$"))
    app.add_handler(CallbackQueryHandler(handle_connect_enter_key,    pattern="^connect_enter_key$"))
    app.add_handler(CallbackQueryHandler(handle_connect_retry,        pattern="^connect_retry$"))
    app.add_handler(CallbackQueryHandler(handle_connect_cancel,       pattern="^connect_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_quiz_answer,          pattern="^quizans_"))
    app.add_handler(CommandHandler("reset", cmd_reset_dialog))

    # ── Любой обычный текст ──
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("✅ Main user bot (BOT_TOKEN / @catapulttrade_guide_bot) запущен!")
    logger.info("🔑 Catapult Connect: /start, /connect, /disconnect")
    logger.info("💬 Support mode: продолжение диалога после викторины")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
