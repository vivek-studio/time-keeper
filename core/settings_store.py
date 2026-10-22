"""Shim so the copied google_auth module finds its settings (backed by SQLite)."""
from core import db
def get_settings() -> dict: return {"google_account": db.get_settings().get("google_account", {})}
def save_settings(s: dict) -> None: db.save_settings({"google_account": s.get("google_account", {})})
