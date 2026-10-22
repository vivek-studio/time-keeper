"""Message Hours -- local server. Run: python server.py  (opens http://127.0.0.1:8765)"""
from __future__ import annotations
import os, sys, threading, webbrowser
from typing import List
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import db, google_auth, google_sheets as gs
from analysis import Dataset, compute

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Message Hours")
STATE = {"ds": Dataset()}
GREEN, NAVY = {"red": 1.0, "green": 0.92, "blue": 0.62}, {"red": 0.14, "green": 0.12, "blue": 0.31}


def result() -> dict:
    cfg, ex, names = db.get_settings(), {e["user_id"] for e in db.get_excluded()}, db.get_names()
    r = compute(STATE["ds"], cfg, ex, names)
    r["version"] = VERSION
    return r


@app.get("/api/state")
def state(): return {**result(), "settings": db.get_settings(), "excluded": db.get_excluded(), "names": db.get_names()}


@app.post("/api/import")
async def import_files(files: List[UploadFile] = File(...), replace: bool = True):
    if replace: STATE["ds"] = Dataset()
    for f in files:
        STATE["ds"].add_file(f.filename or "file.json", await f.read())
    return state()


@app.post("/api/clear")
def clear():
    STATE["ds"] = Dataset(); return state()


class SettingsIn(BaseModel):
    settings: dict = {}
    excluded: list | None = None
    names: dict | None = None


@app.put("/api/settings")
def put_settings(b: SettingsIn):
    if b.settings:
        cur = db.get_settings(); cur.update({k: v for k, v in b.settings.items() if k in db.DEFAULTS}); db.save_settings(cur)
    if b.excluded is not None:
        clean, seen = [], set()
        for e in b.excluded:
            uid = str(e.get("user_id", "")).strip().upper()
            if uid and uid not in seen: seen.add(uid); clean.append({"user_id": uid, "note": str(e.get("note", ""))[:80]})
        db.set_excluded(clean)
    if b.names is not None: db.set_names({k.strip().upper(): str(v) for k, v in b.names.items() if k.strip()})
    return state()


# ------------------------------------------------------------------ Google
@app.get("/api/google")
def g_status(): return {**google_auth.status(), "client": google_auth.client_status()}


@app.post("/api/google/connect")
def g_connect():
    try: return google_auth.connect()
    except Exception as e: raise HTTPException(400, str(e))


@app.post("/api/google/disconnect")
def g_disc(): return google_auth.reset()


class PushIn(BaseModel):
    sheet_url: str


def _tables(r: dict):
    one = bool(r["rows"]) and r["rows"][0]["user_id"] == "*"
    H = ["Date", "Day"] + ([] if one else ["User", "User ID"]) + ["First message", "Last message", "Span (hrs)", "Messages",
         "Delayed checkout (after 8 PM)", "Next day start (11 / 12)", "Policy note"] + (["First message by", "Last message by"] if one else []) + \
        ["Projects", "Last project", "Last message text"]
    hours = [[x["date"], x["day"]] + ([] if one else [x["user"], x["user_id"]]) + [x["first"], x["last"], x["hours"], x["messages"], x["delayed"],
              x["next_start"], x["note"]] + ([x["first_by"], x["last_by"]] if one else []) + [x["projects"], x["last_project"], x["last_message"]] for x in r["rows"]]
    d = 2 if one else 4          # column index of "First message"
    yes_cols = {d + 4: "Yes", d + 5: "At"}
    U = ["User", "User ID", "Active days", "Total hrs", "Avg hrs/day", "Delayed checkouts", "Started at 11", "Started at 12", "Messages",
         "Last active", "Last project", "Last message text"]
    users = [[u["user"], u["user_id"], u["days"], u["total_hours"], u["avg_hours"], u["delayed"], u["next11"], u["next12"], u["messages"],
              u["last_date"], u["last_project"], u["last_message"]] for u in r["users"]]
    D = ["Severity", "Type", "File", "Project", "User", "When", "Detail"]
    health = [[i["sev"], i["label"], i["file"], i["project"], i["user"], i["when"], i["detail"]] for i in r["health"]["items"]]
    return [("Daily Hours", H, hours, yes_cols), ("User Summary", U, users, {}), ("Data Health", D, health, {})]


@app.post("/api/push")
def push(b: PushIn):
    r = result()
    if not r["rows"]: raise HTTPException(400, "Nothing to push: import a file first.")
    try:
        svc = gs.get_service(); sid = gs.extract_sheet_id(b.sheet_url)
        if not sid: raise HTTPException(400, "That does not look like a Google Sheet link.")
        reqs, gids = [], {}
        for title, head, body, yes_cols in _tables(r):
            gid = gs.ensure_worksheet(svc, sid, title, rows=len(body) + 50, cols=len(head) + 2); gids[title] = gid
            gs.ensure_grid_size(svc, sid, gid, len(body) + 2, len(head))
            tab = gs.quote_tab(title)
            gs.clear_values(svc, sid, tab)
            gs.update_values(svc, sid, f"{tab}!A1", [head] + body, "RAW")
            n, c = len(body) + 1, len(head)
            reqs += [
                {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {
                    "backgroundColor": NAVY, "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE",
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}}, "fields": "userEnteredFormat"}},
                {"setBasicFilter": {"filter": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": c}}}},
                {"autoResizeDimensions": {"dimensions": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": c - 1}}},
                {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": c - 1, "endIndex": c},
                                               "properties": {"pixelSize": 420}, "fields": "pixelSize"}},
            ]
            for col, txt in (yes_cols.items() if isinstance(yes_cols, dict) else []):
                reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {"ranges": [{"sheetId": gid, "startRowIndex": 1, "endRowIndex": n, "startColumnIndex": col, "endColumnIndex": col + 1}],
                             "booleanRule": {"condition": {"type": "TEXT_EQ" if txt == "Yes" else "TEXT_STARTS_WITH", "values": [{"userEnteredValue": txt}]}, "format": {"backgroundColor": GREEN, "textFormat": {"bold": True}}}}}})
        # one chart: avg hours per user, on the summary tab
        ug, nu = gids["User Summary"], len(r["users"]) + 1
        reqs.append({"addChart": {"chart": {"spec": {"title": "Average hours per active day, by user", "basicChart": {
            "chartType": "COLUMN", "legendPosition": "NO_LEGEND",
            "axis": [{"position": "BOTTOM_AXIS", "title": "User"}, {"position": "LEFT_AXIS", "title": "Hours"}],
            "domains": [{"domain": {"sourceRange": {"sources": [{"sheetId": ug, "startRowIndex": 0, "endRowIndex": nu, "startColumnIndex": 0, "endColumnIndex": 1}]}}}],
            "series": [{"series": {"sourceRange": {"sources": [{"sheetId": ug, "startRowIndex": 0, "endRowIndex": nu, "startColumnIndex": 4, "endColumnIndex": 5}]}}}],
            "headerCount": 1}}, "position": {"overlayPosition": {"anchorCell": {"sheetId": ug, "rowIndex": 1, "columnIndex": 13}, "widthPixels": 620, "heightPixels": 340}}}}})
        gs.batch_update(svc, sid, reqs)
        db.save_settings({**db.get_settings(), "sheet_url": b.sheet_url})
        return {"ok": True, "url": gs.open_sheet_tab_url(b.sheet_url, "Daily Hours"), "rows": len(r["rows"])}
    except HTTPException: raise
    except gs.SheetsSyncError as e: raise HTTPException(400, str(e))
    except Exception as e: raise HTTPException(500, f"Push failed: {e}")


@app.get("/")
def index(): return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
