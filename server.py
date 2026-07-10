"""
Catapult Trade — Backend API
Стек: FastAPI + SQLite (для старта) / легко переключить на Supabase/PostgreSQL
"""

import os
import uuid
import string
import random
import logging
import json
import httpx
import shutil
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi import UploadFile, File
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
import subagents.yt_publisher as yt_publisher

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Catapult Trade Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH          = os.getenv("DB_PATH", "catapult.db")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "change_this_secret")
MINIAPP_URL      = os.getenv("MINIAPP_URL", "https://your-miniapp.com")
BOT_API_URL      = os.getenv("BOT_API_URL", "http://localhost:8001")
CATAPULT_JWT     = os.getenv("CATAPULT_JWT", "")
CATAPULT_API     = "https://public-api.catapult.trade/graphql"
MEDIA_SERVE_TOKEN = os.getenv("MEDIA_SERVE_TOKEN", "")
MEDIA_DIRS = {"photos": "/data/photos", "audio": "/data/audio", "videos": "/data/videos"}

VIDEOS_DIR = "/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

UPLOAD_FORM_HTML = """<!doctype html>
<html><body>
<h3>Загрузка ролика</h3>
<form action="" method="post" enctype="multipart/form-data">
<input type="file" name="video" accept="video/*" required>
<button type="submit">Загрузить</button>
</form>
</body></html>"""

# ── База данных ───────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     TEXT    UNIQUE NOT NULL,
            username        TEXT    DEFAULT '',
            name            TEXT    NOT NULL,
            ref_code        TEXT    UNIQUE NOT NULL,
            inviter_ref     TEXT    DEFAULT NULL,
            calendly_link   TEXT    DEFAULT NULL,
            catapult_ref    TEXT    DEFAULT NULL,
            catapult_jwt    TEXT    DEFAULT NULL,
            qualify_exp     TEXT    DEFAULT NULL,
            qualify_goal    TEXT    DEFAULT NULL,
            qualify_time    TEXT    DEFAULT NULL,
            onboarded       INTEGER DEFAULT 0,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_ref_code    ON users(ref_code);
        CREATE INDEX IF NOT EXISTS idx_telegram_id ON users(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_inviter_ref ON users(inviter_ref);

        CREATE TABLE IF NOT EXISTS dialogs (
            telegram_id     TEXT PRIMARY KEY,
            history         TEXT DEFAULT '[]',
            stage           TEXT DEFAULT 'chatting',
            quiz_answers    TEXT DEFAULT '[]',
            quiz_step       INTEGER DEFAULT 0,
            support_history TEXT DEFAULT '[]',
            updated_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

def generate_ref_code(name: str) -> str:
    clean = "".join(c.upper() for c in name if c.isalpha())[:4].ljust(4, "X")
    suffix = "".join(random.choices(string.digits + string.ascii_uppercase, k=4))
    return f"{clean}{suffix}"

# ── Pydantic модели ───────────────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    telegram_id:  str
    username:     str = ""
    name:         str
    inviter_ref:  Optional[str] = None
    qualify_data: Optional[dict] = None
    catapult_jwt: Optional[str] = None

class UpdateUserRequest(BaseModel):
    calendly_link: Optional[str] = None
    catapult_ref:  Optional[str] = None
    catapult_jwt:  Optional[str] = None

    class Config:
        extra = "allow"

class CatapultWebhookPayload(BaseModel):
    event:       str
    ref_code:    str
    user_id:     str
    email:       Optional[str] = None

class GraphQLRequest(BaseModel):
    query: str
    variables: Optional[dict] = None

class UserGraphQLRequest(BaseModel):
    query: str
    variables: Optional[dict] = None
    jwt: str

class DialogState(BaseModel):
    history: list = []
    stage: str = "chatting"          # chatting | quiz | done
    quiz_answers: list = []
    quiz_step: int = 0
    support_history: list = []       # история общения после прохождения викторины

# ── Запуск ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    # Миграция — добавляем новые поля если их нет (для существующих БД)
    conn = get_db()
    try:
        conn.execute("ALTER TABLE users ADD COLUMN catapult_jwt TEXT DEFAULT NULL")
        conn.commit()
        logger.info("Migration: added catapult_jwt column")
    except Exception:
        pass  # Поле уже существует
    try:
        conn.execute("ALTER TABLE dialogs ADD COLUMN support_history TEXT DEFAULT '[]'")
        conn.commit()
        logger.info("Migration: added support_history column")
    except Exception:
        pass  # Поле уже существует
    conn.close()
    logger.info("DB initialized")

# ── ПРОКСИ для Catapult GraphQL API (публичные запросы, общий JWT) ───────────

@app.post("/api/catapult")
async def catapult_proxy(payload: GraphQLRequest):
    """
    Прокси для публичных запросов к Catapult Trade GraphQL API.
    Mini App вызывает этот эндпоинт для общей статистики платформы.
    """
    if not CATAPULT_JWT:
        raise HTTPException(500, "CATAPULT_JWT not configured")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                CATAPULT_API,
                json={
                    "query": payload.query,
                    "variables": payload.variables or {}
                },
                headers={
                    "Authorization": f"Bearer {CATAPULT_JWT}",
                    "Content-Type": "application/json"
                }
            )
            return resp.json()
    except Exception as e:
        logger.error(f"Catapult proxy error: {e}")
        raise HTTPException(502, f"Catapult API error: {str(e)}")

# ── ПРОКСИ для личных запросов (JWT конкретного пользователя) ────────────────

@app.post("/api/catapult/user")
async def catapult_user_proxy(payload: UserGraphQLRequest):
    """
    Прокси для личных запросов — использует JWT конкретного пользователя.
    Используется Личным Кабинетом в Mini App.
    """
    if not payload.jwt:
        raise HTTPException(400, "JWT required")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                CATAPULT_API,
                json={
                    "query": payload.query,
                    "variables": payload.variables or {}
                },
                headers={
                    "Authorization": f"Bearer {payload.jwt}",
                    "Content-Type": "application/json"
                }
            )
            return resp.json()
    except Exception as e:
        logger.error(f"Catapult user proxy error: {e}")
        raise HTTPException(502, f"Catapult API error: {str(e)}")

# ── Эндпоинты — пользователи ──────────────────────────────────────────────────

@app.post("/users", status_code=201)
async def create_user(data: CreateUserRequest):
    conn = get_db()
    try:
        for _ in range(10):
            ref_code = generate_ref_code(data.name)
            existing = conn.execute("SELECT id FROM users WHERE ref_code=?", (ref_code,)).fetchone()
            if not existing:
                break

        q = data.qualify_data or {}
        conn.execute("""
            INSERT INTO users
                (telegram_id, username, name, ref_code, inviter_ref,
                 qualify_exp, qualify_goal, qualify_time, catapult_jwt)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            data.telegram_id, data.username, data.name, ref_code,
            data.inviter_ref,
            q.get("exp"), q.get("goal"), q.get("time"),
            data.catapult_jwt
        ))
        conn.commit()

        user = conn.execute("SELECT * FROM users WHERE ref_code=?", (ref_code,)).fetchone()
        return dict(user)

    except sqlite3.IntegrityError:
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id=?", (data.telegram_id,)
        ).fetchone()
        if user:
            # Если пользователь уже есть, но прислали jwt — обновим его
            if data.catapult_jwt:
                conn.execute(
                    "UPDATE users SET catapult_jwt=? WHERE telegram_id=?",
                    (data.catapult_jwt, data.telegram_id)
                )
                conn.commit()
                user = conn.execute(
                    "SELECT * FROM users WHERE telegram_id=?", (data.telegram_id,)
                ).fetchone()
            return dict(user)
        raise HTTPException(400, "User already exists")
    finally:
        conn.close()

@app.get("/users/{ref_code_or_id}")
async def get_user(ref_code_or_id: str):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE ref_code=? OR telegram_id=?",
        (ref_code_or_id, ref_code_or_id)
    ).fetchone()
    conn.close()
    if not user:
        raise HTTPException(404, "User not found")
    return dict(user)

@app.patch("/users/by-telegram/{telegram_id}")
async def update_user(telegram_id: str, data: UpdateUserRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    if not user:
        conn.close()
        raise HTTPException(404, "User not found")

    updates, params = [], []
    if data.calendly_link is not None:
        updates.append("calendly_link=?")
        params.append(data.calendly_link)
    if data.catapult_ref is not None:
        updates.append("catapult_ref=?")
        params.append(data.catapult_ref)
    if data.catapult_jwt is not None:
        updates.append("catapult_jwt=?")
        params.append(data.catapult_jwt)

    extra = data.model_extra or {}
    for key, val in extra.items():
        if key in ("catapult_username", "onboarded", "catapult_jwt"):
            updates.append(f"{key}=?")
            params.append(val)

    if updates:
        params.append(telegram_id)
        conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE telegram_id=?", params
        )
        conn.commit()

    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    conn.close()
    return dict(user)

@app.get("/users/{ref_code}/referrals")
async def get_referrals(ref_code: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT telegram_id, name, username, ref_code, created_at FROM users WHERE inviter_ref=?",
        (ref_code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Диалог-прогрев (история чата + результаты викторины) ─────────────────────

@app.get("/dialog/{telegram_id}")
async def get_dialog(telegram_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM dialogs WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    if not row:
        return {"history": [], "stage": "chatting", "quiz_answers": [], "quiz_step": 0, "support_history": []}
    d = dict(row)
    d["history"] = json.loads(d["history"])
    d["quiz_answers"] = json.loads(d["quiz_answers"])
    d["support_history"] = json.loads(d.get("support_history") or "[]")
    return d

@app.post("/dialog/{telegram_id}")
async def save_dialog(telegram_id: str, data: DialogState):
    conn = get_db()
    existing = conn.execute("SELECT telegram_id FROM dialogs WHERE telegram_id=?", (telegram_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE dialogs SET history=?, stage=?, quiz_answers=?, quiz_step=?, support_history=?, updated_at=datetime('now') WHERE telegram_id=?",
            (json.dumps(data.history, ensure_ascii=False), data.stage,
             json.dumps(data.quiz_answers, ensure_ascii=False), data.quiz_step,
             json.dumps(data.support_history, ensure_ascii=False), telegram_id)
        )
    else:
        conn.execute(
            "INSERT INTO dialogs (telegram_id, history, stage, quiz_answers, quiz_step, support_history) VALUES (?,?,?,?,?,?)",
            (telegram_id, json.dumps(data.history, ensure_ascii=False), data.stage,
             json.dumps(data.quiz_answers, ensure_ascii=False), data.quiz_step,
             json.dumps(data.support_history, ensure_ascii=False))
        )
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/dialog/{telegram_id}")
async def reset_dialog(telegram_id: str):
    conn = get_db()
    conn.execute("DELETE FROM dialogs WHERE telegram_id=?", (telegram_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── Вебхук от Catapult Trade ──────────────────────────────────────────────────

@app.post("/webhook/catapult")
async def catapult_webhook(
    payload: CatapultWebhookPayload,
    request: Request,
    x_webhook_secret: str = Header(default="")
):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid webhook secret")

    if payload.event != "user.registered":
        return {"status": "ignored", "event": payload.event}

    logger.info(f"Webhook: new user via ref={payload.ref_code}")

    conn = get_db()
    inviter = conn.execute(
        "SELECT * FROM users WHERE ref_code=? OR catapult_ref=?",
        (payload.ref_code, payload.ref_code)
    ).fetchone()

    if not inviter:
        conn.close()
        return {"status": "inviter_not_found"}

    conn.execute("UPDATE users SET onboarded=1 WHERE id=?", (inviter["id"],))
    conn.commit()
    conn.close()

    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{BOT_API_URL}/internal/onboard", json={
                "telegram_id": inviter["telegram_id"],
                "name":        inviter["name"],
                "ref_code":    inviter["ref_code"],
            }, timeout=5)
    except Exception as e:
        logger.error(f"Failed to notify bot: {e}")

    return {"status": "ok"}

# ── Mini App данные ───────────────────────────────────────────────────────────

@app.get("/miniapp-data/{ref_code}")
async def miniapp_data(ref_code: str):
    conn = get_db()
    user = conn.execute(
        "SELECT name, username, ref_code, calendly_link FROM users WHERE ref_code=?",
        (ref_code,)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(404, "Not found")

    user = dict(user)
    if user.get("calendly_link"):
        booking_url = user["calendly_link"]
    elif user.get("username"):
        booking_url = f"https://t.me/{user['username']}"
    else:
        booking_url = None

    return {
        "name":              user["name"],
        "ref_code":          user["ref_code"],
        "booking_url":       booking_url,
        "miniapp_url":       f"{MINIAPP_URL}?ref={user['ref_code']}",
        "catapult_ref_link": f"https://catapulttrade.io/register?ref={user['ref_code']}",
    }

@app.api_route("/media/{kind}/{filename}", methods=["GET", "HEAD"])
def get_media(kind: str, filename: str, token: str = ""):
    if not MEDIA_SERVE_TOKEN or token != MEDIA_SERVE_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    directory = MEDIA_DIRS.get(kind)
    if not directory:
        raise HTTPException(status_code=404, detail="Not found")
    safe_name = os.path.basename(filename)
    path = os.path.join(directory, safe_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)

@app.post("/media/{kind}/{filename}")
async def put_media(kind: str, filename: str, token: str = "", file: UploadFile = File(...)):
    """Принимает файл от Catapult-Bot (у него своё, не общее с web хранилище) для раздачи через /media."""
    if not MEDIA_SERVE_TOKEN or token != MEDIA_SERVE_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    directory = MEDIA_DIRS.get(kind)
    if not directory:
        raise HTTPException(status_code=404, detail="Not found")
    os.makedirs(directory, exist_ok=True)
    safe_name = os.path.basename(filename)
    path = os.path.join(directory, safe_name)
    with open(path, "wb") as f:
        f.write(await file.read())
    return {"ok": True}

@app.get("/upload/{token}", response_class=HTMLResponse)
def upload_form(token: str):
    info = yt_publisher.get_upload_token_info(token)
    if not info:
        return HTMLResponse("<h3>Ссылка недействительна или уже использована.</h3>", status_code=404)
    return HTMLResponse(UPLOAD_FORM_HTML)

@app.post("/upload/{token}", response_class=HTMLResponse)
async def upload_submit(token: str, video: UploadFile = File(...)):
    info = yt_publisher.get_upload_token_info(token)
    if not info:
        return HTMLResponse("<h3>Ссылка недействительна или уже использована.</h3>", status_code=404)

    local_path = f"{VIDEOS_DIR}/self_{int(time.time())}.mp4"
    with open(local_path, "wb") as f:
        shutil.copyfileobj(video.file, f)

    if not yt_publisher.consume_upload_token(token, local_path):
        return HTMLResponse("<h3>Ссылка уже использована.</h3>", status_code=409)

    return HTMLResponse("<h3>Готово! Видео загружено и обрабатывается.</h3>")

# ── Запуск ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
