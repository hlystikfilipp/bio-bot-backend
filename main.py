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
from datetime import date, timedelta
from urllib.parse import parse_qsl

import httpx
import psycopg2
import psycopg2.extras
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
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

# XP-экономика. Бонусы за домашку (+30) и за завершённый тест (+50)
# добавим, когда появятся сами эти механики — пока реализовано только
# начисление за отдельный отвеченный вопрос.
XP_PER_ANSWER = 5
LEVEL_XP_STEP = 200  # сколько XP нужно набрать на один уровень

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


def get_or_create_student(cur, telegram_id: int, name: str) -> int:
    """
    Возвращает внутренний id ученика (students.id) по его Telegram ID.
    Если ученик заходит впервые — создаёт запись автоматически.

    Только регистрация/поиск — XP и стрик здесь не трогаем (см. award_xp),
    иначе любой GET-запрос двигал бы стрик, даже без реальной активности.
    """
    cur.execute(
        """
        INSERT INTO students (telegram_id, name)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id) DO UPDATE
            SET name = COALESCE(students.name, EXCLUDED.name)
        RETURNING id
        """,
        (telegram_id, name),
    )
    return cur.fetchone()["id"]


def award_xp(cur, student_id: int, xp_amount: int) -> None:
    """
    Начисляет XP ученику и обновляет стрик по дате последней активности:
    - уже был активен сегодня — стрик не меняется
    - был активен вчера — стрик +1
    - иначе (пропустил день или первый раз) — стрик сбрасывается на 1
    """
    cur.execute(
        "SELECT streak, last_active FROM students WHERE id = %s",
        (student_id,),
    )
    row = cur.fetchone()
    streak = row["streak"] or 0
    last_active = row["last_active"]

    today = date.today()
    if last_active == today:
        new_streak = streak or 1
    elif last_active == today - timedelta(days=1):
        new_streak = streak + 1
    else:
        new_streak = 1

    cur.execute(
        """
        UPDATE students
        SET xp = xp + %s, streak = %s, last_active = %s
        WHERE id = %s
        """,
        (xp_amount, new_streak, today, student_id),
    )


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

    student_id больше НЕ принимается от клиента — берём Telegram ID из
    проверенной initData, а внутренний students.id получаем/создаём через
    get_or_create_student (answers_log ссылается именно на него, не на
    telegram_id напрямую).
    """
    task_id = payload.task_id
    chosen_index = payload.chosen_index

    with get_cursor() as cur:
        student_id = get_or_create_student(
            cur,
            telegram_id=user["id"],
            name=user.get("first_name", ""),
        )

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

        award_xp(cur, student_id, XP_PER_ANSWER)

        return {"is_correct": is_correct, "explanation": task["explanation"]}


@app.get("/api/me")
def get_me(user: dict = Depends(get_current_user)):
    """
    Профиль ученика для главного экрана: XP, уровень (считается от XP,
    отдельно не хранится) и текущий стрик.
    """
    with get_cursor() as cur:
        student_id = get_or_create_student(
            cur,
            telegram_id=user["id"],
            name=user.get("first_name", ""),
        )
        cur.execute("SELECT xp, streak FROM students WHERE id = %s", (student_id,))
        row = cur.fetchone()

    xp = row["xp"]
    return {
        "xp": xp,
        "level": xp // LEVEL_XP_STEP + 1,
        "xp_into_level": xp % LEVEL_XP_STEP,
        "xp_per_level": LEVEL_XP_STEP,
        "streak": row["streak"],
    }


@app.get("/api/photo/{file_id}")
async def get_photo(file_id: str, user: dict = Depends(get_current_user)):
    """
    Прокси для картинок в заданиях (task.photo_file_id).

    Telegram не даёт постоянных публичных ссылок на файлы — только
    временный путь через getFile, доступный по адресу, где нужен сам
    BOT_TOKEN. Отдавать токен на фронтенд нельзя, поэтому бэкенд сам
    ходит в Telegram и пересылает байты картинки ученику.

    Примечание: getFile дергается на каждый запрос без кэширования —
    для одной темы с несколькими фото-заданиями это нормально, но если
    картинки станут часто переиспользоваться, стоит закэшировать
    file_path (или сами байты) на стороне бэкенда.
    """
    async with httpx.AsyncClient() as client:
        file_info_resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
        )
        file_info = file_info_resp.json()
        if not file_info.get("ok"):
            raise HTTPException(status_code=404, detail="Файл не найден в Telegram")

        file_path = file_info["result"]["file_path"]

        file_resp = await client.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        )
        if file_resp.status_code != 200:
            raise HTTPException(
                status_code=502, detail="Не удалось скачать файл из Telegram"
            )

    content_type = file_resp.headers.get("content-type", "image/jpeg")
    return Response(
        content=file_resp.content,
        media_type=content_type,
        # Файл под конкретным file_id не меняется — можно спокойно кэшировать
        # на стороне браузера/Telegram WebView на сутки.
        headers={"Cache-Control": "public, max-age=86400"},
    )
