"""
Бэкенд для Mini App "Био" — ЦЭ/ЦТ по биологии.

Запуск локально:
    export DATABASE_URL="postgresql://user:pass@host/dbname"
    export BOT_TOKEN="1234567890:AA...твой_токен_бота..."
    pip install fastapi uvicorn psycopg2-binary
    uvicorn main:app --reload

Проверка:
    http://127.0.0.1:8000/api/practice/topics
    http://127.0.0.1:8000/api/practice/svoystva_zhivogo
"""

import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


DATABASE_URL = os.environ["DATABASE_URL"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Сколько секунд считаем initData ещё свежим (защита от replay-атак).
# Telegram обновляет initData при каждом открытии Mini App, так что
# 24 часа — разумный запас на случай долгой сессии.
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60

app = FastAPI(title="Био — API", default_response_class=UTF8JSONResponse)

# Разрешаем запросы только с фронтенда Mini App (сужено после деплоя)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://bio-bot-frontend-production.up.railway.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Проверка Telegram initData
# ---------------------------------------------------------------------------

def verify_init_data(init_data: str) -> dict:
    """
    Проверяет подпись initData от Telegram WebApp и возвращает данные
    пользователя (dict с полями id, first_name и т.д.).

    Алгоритм из официальной документации Telegram:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Отсутствует initData")

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(status_code=401, detail="Некорректный формат initData")

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="В initData нет hash")

    # Строка для проверки: все поля кроме hash, отсортированные по ключу,
    # склеенные через \n в формате "key=value"
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )

    # Секретный ключ — HMAC-SHA256 от строки "WebAppData" с ключом = токен бота
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=BOT_TOKEN.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    computed_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Неверная подпись initData")

    # Защита от replay-атак: отклоняем слишком старые initData
    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="initData устарела")

    user_raw = parsed.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="В initData нет данных пользователя")

    return json.loads(user_raw)


def get_current_user(x_telegram_init_data: str = Header(...)) -> dict:
    """
    FastAPI-зависимость: достаёт и проверяет initData из заголовка запроса.
    Фронтенд должен слать заголовок:
        X-Telegram-Init-Data: <window.Telegram.WebApp.initData>
    """
    return verify_init_data(x_telegram_init_data)


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

@contextmanager
def get_cursor():
    conn = psycopg2.connect(DATABASE_URL)
    conn.set_client_encoding("UTF8")
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.get("/api/practice/topics")
def list_topics(user: dict = Depends(get_current_user)):
    """Список всех тем с количеством вопросов в каждой — для экрана 'По теме'."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.name, COUNT(k.id) AS task_count
            FROM topics t
            LEFT JOIN tasks k ON k.topic_id = t.id
            GROUP BY t.id, t.name
            ORDER BY t.name
            """
        )
        return cur.fetchall()


@app.get("/api/practice/{topic_id}")
def get_topic_tasks(topic_id: str, user: dict = Depends(get_current_user)):
    """Все задания одной темы — для прохождения практики по теме."""
    with get_cursor() as cur:
        cur.execute("SELECT id, name FROM topics WHERE id = %s", (topic_id,))
        topic = cur.fetchone()
        if not topic:
            raise HTTPException(status_code=404, detail="Тема не найдена")

        cur.execute(
            """
            SELECT id, question_text, options, photo_file_id
            FROM tasks
            WHERE topic_id = %s
            ORDER BY id
            """,
            (topic_id,),
        )
        tasks = cur.fetchall()
        # correct_index/correct_text намеренно не отдаём на фронтенд —
        # ответ проверяется отдельным эндпоинтом (см. ниже)
        return {"topic": topic, "tasks": tasks}


class AnswerPayload(BaseModel):
    task_id: int
    chosen_index: int


@app.post("/api/answer")
def submit_answer(
    payload: AnswerPayload,
    user: dict = Depends(get_current_user),
):
    """
    Приём ответа ученика: сверка, запись в answers_log, возврат объяснения.

    student_id больше НЕ принимается от клиента — берём id из проверенной
    initData, чтобы ученик не мог отправить чужой student_id.
    """
    student_id = user["id"]
    task_id = payload.task_id
    chosen_index = payload.chosen_index

    with get_cursor() as cur:
        cur.execute(
            "SELECT correct_index, explanation FROM tasks WHERE id = %s",
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Задание не найдено")

        is_correct = chosen_index == task["correct_index"]

        cur.execute(
            """
            INSERT INTO answers_log (student_id, task_id, is_correct)
            VALUES (%s, %s, %s)
            """,
            (student_id, task_id, is_correct),
        )

        return {"is_correct": is_correct, "explanation": task["explanation"]}
