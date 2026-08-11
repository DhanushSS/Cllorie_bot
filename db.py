import json
import sqlite3

import config


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meal_status(
            chat_id TEXT, date TEXT, meal TEXT, status TEXT,
            food_text TEXT, category TEXT, calories REAL, protein REAL,
            PRIMARY KEY(chat_id, date, meal)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kv(
            key TEXT PRIMARY KEY, value TEXT
        )"""
    )
    conn.commit()
    return conn


def set_kv(key: str, value: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_kv(key: str, default=None):
    conn = _conn()
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None or row[0] == "":
        return default
    return row[0]


def delete_kv(key: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM kv WHERE key=?", (key,))
    conn.commit()
    conn.close()


# ---- pending clarification (e.g. "is it veg or non-veg?") ----

def set_pending_clarification(chat_id: str, meal: str, question: str, food_text: str) -> None:
    set_kv(
        f"pending_clar:{chat_id}",
        json.dumps({"meal": meal, "question": question, "food_text": food_text}),
    )


def get_pending_clarification(chat_id: str):
    raw = get_kv(f"pending_clar:{chat_id}")
    return json.loads(raw) if raw else None


def clear_pending_clarification(chat_id: str) -> None:
    delete_kv(f"pending_clar:{chat_id}")


# ---- meal status (per day, per meal) ----

def upsert_meal_status(
    chat_id: str,
    date_str: str,
    meal: str,
    status: str,
    food_text: str | None = None,
    category: str | None = None,
    calories: float | None = None,
    protein: float | None = None,
) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO meal_status(chat_id,date,meal,status,food_text,category,calories,protein)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(chat_id,date,meal) DO UPDATE SET
            status=excluded.status, food_text=excluded.food_text, category=excluded.category,
            calories=excluded.calories, protein=excluded.protein""",
        (chat_id, date_str, meal, status, food_text, category, calories, protein),
    )
    conn.commit()
    conn.close()


def get_day_status(chat_id: str, date_str: str) -> dict:
    conn = _conn()
    rows = conn.execute(
        "SELECT meal, status, food_text, category, calories, protein "
        "FROM meal_status WHERE chat_id=? AND date=?",
        (chat_id, date_str),
    ).fetchall()
    conn.close()
    return {
        r[0]: {
            "status": r[1],
            "food_text": r[2],
            "category": r[3],
            "calories": r[4],
            "protein": r[5],
        }
        for r in rows
    }


def earliest_pending_meal(chat_id: str, date_str: str) -> str | None:
    status = get_day_status(chat_id, date_str)
    for meal in config.MEALS:
        st = status.get(meal, {}).get("status")
        if st not in ("logged", "skipped"):
            return meal
    return None
