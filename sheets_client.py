import json
from datetime import datetime

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["Date", "Meal", "Food", "Category", "Calories", "Protein_g", "LoggedAt"]


def _client() -> gspread.Client:
    info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _sheet():
    gc = _client()
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(config.GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=config.GOOGLE_SHEET_TAB, rows=2000, cols=8)
        ws.append_row(HEADER)
    return ws


def append_entry(date_str, meal, food_text, category, calories, protein_g) -> None:
    ws = _sheet()
    ws.append_row(
        [date_str, meal, food_text, category, calories, protein_g,
         datetime.now(config.TIMEZONE).isoformat(timespec="seconds")]
    )


def update_entry(date_str, meal, food_text, category, calories, protein_g) -> bool:
    """Update the existing row for (date, meal) if found, else append a new one."""
    ws = _sheet()
    records = ws.get_all_values()
    for i, row in enumerate(records[1:], start=2):
        if len(row) >= 2 and row[0] == date_str and row[1] == meal:
            ws.update(
                range_name=f"A{i}:G{i}",
                values=[[date_str, meal, food_text, category, calories, protein_g,
                  datetime.now(config.TIMEZONE).isoformat(timespec="seconds")]],
            )
            return True
    append_entry(date_str, meal, food_text, category, calories, protein_g)
    return False


def get_entries_df(start_date=None, end_date=None) -> pd.DataFrame:
    ws = _sheet()
    records = ws.get_all_records()
    df = pd.DataFrame(records, columns=HEADER)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    df["Calories"] = pd.to_numeric(df["Calories"], errors="coerce").fillna(0)
    df["Protein_g"] = pd.to_numeric(df["Protein_g"], errors="coerce").fillna(0)
    if start_date:
        df = df[df["Date"] >= start_date]
    if end_date:
        df = df[df["Date"] <= end_date]
    return df.reset_index(drop=True)
