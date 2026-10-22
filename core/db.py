"""Local SQLite store: settings, excluded user IDs, user-name map. One file in the app-data folder."""
import json, sqlite3, threading
from core.appdata import data_dir

_lock = threading.Lock()
DEFAULTS = {
    "tz": "Asia/Kolkata",
    "day_cutoff_hour": 4,        # messages before 04:00 belong to the previous working day
    "late_checkout": "20:00",    # checkout AFTER this = delayed
    "shift2_start": "11:00",     # next-day start at/after this (and before shift 3) = "started at 11"
    "shift3_start": "12:00",     # next-day start at/after this = "started at 12"
    "shift2_eligible": "21:30",  # policy: worked past this -> may request Shift 2
    "shift3_eligible": "23:30",  # policy: worked past this -> may request Shift 3
    "start_window_min": 60,      # "started at 11" = first message within 60 min after 11:00 (same for 12:00)
    "one_row_per_date": True,    # ONE row per date (all included users combined): earliest + latest message of the day
    "sheet_url": "",
}

def _conn():
    c = sqlite3.connect(str(data_dir() / "message_hours.db"))
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS excluded_users(user_id TEXT PRIMARY KEY, note TEXT DEFAULT '', added_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE IF NOT EXISTS user_names(user_id TEXT PRIMARY KEY, name TEXT NOT NULL);
    """)
    return c

def get_settings() -> dict:
    with _lock, _conn() as c:
        s = dict(DEFAULTS)
        for r in c.execute("SELECT k,v FROM kv"):
            try: s[r["k"]] = json.loads(r["v"])
            except Exception: pass
        return s

def save_settings(s: dict) -> None:
    with _lock, _conn() as c:
        for k, v in s.items():
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

def get_excluded() -> list:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT user_id,note,added_at FROM excluded_users ORDER BY user_id")]

def set_excluded(items: list) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM excluded_users")
        for it in items:
            c.execute("INSERT OR REPLACE INTO excluded_users(user_id,note) VALUES(?,?)", (it["user_id"], it.get("note", "")))

def get_names() -> dict:
    with _lock, _conn() as c:
        return {r["user_id"]: r["name"] for r in c.execute("SELECT user_id,name FROM user_names")}

def set_names(m: dict) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM user_names")
        for k, v in m.items():
            if v.strip(): c.execute("INSERT INTO user_names(user_id,name) VALUES(?,?)", (k, v.strip()))
