"""
Бэкенд для Mini App "Био" — ЦЭ/ЦТ по биологии.

Запуск локально:
    export DATABASE_URL="postgresql://user:pass@host/dbname"
    pip install fastapi uvicorn psycopg2-binary
    uvicorn main:app --reload

Проверка:
    http://127.0.0.1:8000/api/practice/topics
    http://127.0.0.1:8000/api/practice/svoystva_zhivogo
"""

import os
from contextlib import contextmanager

import json

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


DATABASE_URL = os.environ["DATABASE_URL"]

app = FastAPI(title="Био — API", default_response_class=UTF8JSONResponse)

# Разрешаем запросы с фронтенда Mini App (пока широко, сузим при деплое)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/api/practice/topics")
def list_topics():
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
def get_topic_tasks(topic_id: str):
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


@app.post("/api/answer")
def submit_answer(student_id: int, task_id: int, chosen_index: int):
    """Приём ответа ученика: сверка, запись в answers_log, возврат объяснения."""
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
