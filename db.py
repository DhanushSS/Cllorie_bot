import json
import sqlite3
from pathlib import Path

import config


def _conn() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS food_cache(
            food_key TEXT PRIMARY KEY, calories REAL, protein REAL,
            category TEXT, notes TEXT
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(food_cache)")}
    if "policy_version" not in columns:
        conn.execute("ALTER TABLE food_cache ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1")
    conn.execute("""CREATE TABLE IF NOT EXISTS meal_aliases(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
        name TEXT NOT NULL, food_text TEXT NOT NULL, UNIQUE(chat_id, name)
    )""")
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

def set_pending_clarification(chat_id: str, meal: str, question: str, food_text: str,
                              date_str: str | None = None, is_edit: bool = False,
                              options=None, question_id=None) -> None:
    set_kv(
        f"pending_clar:{chat_id}",
        json.dumps({"meal": meal, "question": question, "food_text": food_text,
                    "date": date_str, "is_edit": is_edit,
                    "options": options or [], "question_id": question_id}),
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


# ---- food estimate cache (keeps repeat entries of the same food consistent) ----

def food_cache_key(food_text: str) -> str:
    return " ".join(food_text.strip().lower().split())


def get_cached_food(food_key: str):
    conn = _conn()
    row = conn.execute(
        "SELECT calories, protein, category, notes, policy_version FROM food_cache WHERE food_key=?",
        (food_key,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"calories": row[0], "protein_g": row[1], "category": row[2], "notes": row[3], "policy_version": row[4]}


def set_cached_food(food_key: str, calories: float, protein: float, category: str, notes: str) -> None:
    conn = _conn()
    conn.execute(
        """INSERT INTO food_cache(food_key, calories, protein, category, notes, policy_version)
        VALUES(?,?,?,?,?,2)
        ON CONFLICT(food_key) DO UPDATE SET
            calories=excluded.calories, protein=excluded.protein,
            category=excluded.category, notes=excluded.notes, policy_version=2""",
        (food_key, calories, protein, category, notes),
    )
    conn.commit()
    conn.close()


# Explicit, user-scoped memory. Never infer personal habits from guesses.
def remember_meal(chat_id, name, food_text):
    conn = _conn()
    conn.execute("""INSERT INTO meal_aliases(chat_id,name,food_text) VALUES(?,?,?)
        ON CONFLICT(chat_id,name) DO UPDATE SET food_text=excluded.food_text""", (chat_id,name,food_text))
    conn.commit()
    conn.close()


def list_usuals(chat_id):
    conn = _conn()
    rows = conn.execute("SELECT id,name,food_text FROM meal_aliases WHERE chat_id=? ORDER BY name", (chat_id,)).fetchall()
    conn.close()
    return [dict(id=r[0],name=r[1],food_text=r[2]) for r in rows]


def forget_meal(chat_id, name):
    conn = _conn()
    removed = conn.execute("DELETE FROM meal_aliases WHERE chat_id=? AND name=?", (chat_id,name)).rowcount
    conn.commit()
    conn.close()
    return bool(removed)
