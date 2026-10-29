import json
import sqlite3
from datetime import date
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
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        chat_id TEXT PRIMARY KEY, reminders INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_food_cache(
        chat_id TEXT NOT NULL, food_key TEXT NOT NULL, calories REAL,
        protein REAL, category TEXT, notes TEXT, policy_version INTEGER NOT NULL DEFAULT 3,
        PRIMARY KEY(chat_id, food_key)
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


def register_user(chat_id: str, reminders: bool = False) -> None:
    conn = _conn()
    conn.execute("INSERT OR IGNORE INTO users(chat_id,reminders) VALUES(?,?)", (chat_id, int(reminders)))
    conn.commit()
    conn.close()


def is_registered(chat_id: str) -> bool:
    conn = _conn()
    found = conn.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return bool(found)


def reminder_users() -> list[str]:
    conn = _conn()
    rows = conn.execute("SELECT chat_id FROM users WHERE reminders=1").fetchall()
    conn.close()
    return [row[0] for row in rows]


def set_reminders(chat_id: str, enabled: bool) -> None:
    conn = _conn()
    conn.execute("UPDATE users SET reminders=? WHERE chat_id=?", (int(enabled), chat_id))
    conn.commit()
    conn.close()


def reminders_enabled(chat_id: str) -> bool:
    conn = _conn()
    row = conn.execute("SELECT reminders FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return bool(row and row[0])


def get_entries_df(chat_id: str, start_date=None, end_date=None):
    import pandas as pd
    conn = _conn()
    clauses = ["chat_id=?", "status='logged'"]
    params = [chat_id]
    if start_date:
        clauses.append("date>=?")
        params.append(start_date.isoformat())
    if end_date:
        clauses.append("date<=?")
        params.append(end_date.isoformat())
    rows = conn.execute("SELECT date,meal,food_text,category,calories,protein FROM meal_status WHERE " +
                        " AND ".join(clauses) + " ORDER BY date,meal", params).fetchall()
    conn.close()
    df = pd.DataFrame(rows, columns=["Date", "Meal", "Food", "Category", "Calories", "Protein_g"])
    if not df.empty:
        df["Date"] = [date.fromisoformat(value) for value in df["Date"]]
        df["Calories"] = pd.to_numeric(df["Calories"]).fillna(0)
        df["Protein_g"] = pd.to_numeric(df["Protein_g"]).fillna(0)
    return df


# ---- food estimate cache (keeps repeat entries of the same food consistent) ----

def food_cache_key(food_text: str) -> str:
    return " ".join(food_text.strip().lower().split())


def get_cached_food(food_key: str, chat_id: str | None = None):
    conn = _conn()
    if chat_id and chat_id != get_kv("chat_id"):
        row = conn.execute("SELECT calories,protein,category,notes,policy_version FROM user_food_cache "
                           "WHERE chat_id=? AND food_key=?", (chat_id, food_key)).fetchone()
    else:
        row = conn.execute(
            "SELECT calories, protein, category, notes, policy_version FROM food_cache WHERE food_key=?",
            (food_key,),
        ).fetchone()
    conn.close()
    if not row:
        return None
    return {"calories": row[0], "protein_g": row[1], "category": row[2], "notes": row[3], "policy_version": row[4]}


def set_cached_food(food_key: str, calories: float, protein: float, category: str, notes: str,
                    chat_id: str | None = None) -> None:
    conn = _conn()
    if chat_id and chat_id != get_kv("chat_id"):
        conn.execute("""INSERT INTO user_food_cache(chat_id,food_key,calories,protein,category,notes)
            VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,food_key) DO UPDATE SET
            calories=excluded.calories,protein=excluded.protein,category=excluded.category,notes=excluded.notes,
            policy_version=3""", (chat_id, food_key, calories, protein, category, notes))
    else:
        conn.execute(
            """INSERT INTO food_cache(food_key, calories, protein, category, notes, policy_version)
            VALUES(?,?,?,?,?,3)
            ON CONFLICT(food_key) DO UPDATE SET
                calories=excluded.calories, protein=excluded.protein,
                category=excluded.category, notes=excluded.notes, policy_version=3""",
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
