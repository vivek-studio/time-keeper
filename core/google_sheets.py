"""
core/google_sheets.py
======================
Thin wrapper around the direct Google Sheets API (v4) -- the same helper
Workload Manager uses, so both apps talk to Google the same way. Vision
Insights' shared-sheet code (core/hub.py) goes through this module, always
with the one Google sign-in in core/google_auth.py.

This intentionally does not use gspread -- gspread added a dependency
and an abstraction layer that wasn't needed once every caller already
speaks the v4 API's batchUpdate/values.* calls directly.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from core import google_auth

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class SheetsSyncError(Exception):
    pass


def extract_sheet_id(sheet_url_or_id: str) -> str:
    """Accepts either a full Google Sheets URL or a bare sheet ID."""
    s = (sheet_url_or_id or "").strip()
    m = SHEET_ID_RE.search(s)
    return m.group(1) if m else s


def open_sheet_tab_url(sheet_url_or_id: str, tab_name: str = "") -> str:
    """The URL to open for an 'Open Sheet' button: deep-links straight to
    ``tab_name`` (via its gid) whenever that tab already exists, and
    falls back to the plain spreadsheet URL otherwise -- including when
    there's no live Google connection yet, so the button still does
    something useful rather than failing outright."""
    spreadsheet_id = extract_sheet_id(sheet_url_or_id)
    base_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    if not tab_name:
        return base_url
    try:
        service = get_service()
        meta = open_spreadsheet_meta(service, spreadsheet_id, fields="sheets(properties(sheetId,title))")
        target = tab_name.strip().lower()
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if (props.get("title") or "").strip().lower() == target:
                return f"{base_url}#gid={props.get('sheetId')}"
    except Exception:
        pass
    return base_url


def get_service():
    """Returns an authorized `sheets` v4 API client using the shared
    Google connection. Raises SheetsSyncError with a plain-language
    message if nothing is connected or the connection was revoked."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SheetsSyncError(
            "Google Sheets support needs the 'google-api-python-client' package "
            "(pip install google-api-python-client)."
        ) from exc

    try:
        creds = google_auth.get_credentials()
    except google_auth.GoogleAuthError as exc:
        raise SheetsSyncError(str(exc)) from exc

    # A hung network must never freeze a comparison: every call gets a timeout.
    try:
        import httplib2
        import google_auth_httplib2
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=20))
        return build("sheets", "v4", http=http, cache_discovery=False)
    except ImportError:
        return build("sheets", "v4", credentials=creds, cache_discovery=False)


def open_spreadsheet_meta(service, spreadsheet_id: str, fields: str = "sheets(properties(sheetId,title),merges,bandedRanges,rowGroups)") -> dict:
    if not spreadsheet_id:
        raise SheetsSyncError("No target sheet set -- add the shared sheet link in Settings.")
    try:
        return service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields=fields).execute()
    except Exception as exc:
        raise SheetsSyncError(
            f"Couldn't open that sheet -- check the URL and that your connected "
            f"Google account has access to it. ({exc})"
        ) from exc


def ensure_worksheet(service, spreadsheet_id: str, title: str, rows: int = 1000, cols: int = 26) -> int:
    """Returns the sheetId for `title`, creating the tab if it doesn't
    exist yet. Never touches other tabs."""
    meta = open_spreadsheet_meta(service, spreadsheet_id, fields="sheets(properties(sheetId,title))")
    for sh in meta.get("sheets", []):
        if sh["properties"]["title"] == title:
            return sh["properties"]["sheetId"]

    try:
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {
                "title": title, "gridProperties": {"rowCount": rows, "columnCount": cols},
            }}}]},
        ).execute()
    except Exception as exc:
        raise _wrap(f"Couldn't create the tab '{title}'", exc) from exc
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def quote_tab(title: str) -> str:
    """A1-notation tab reference, with any apostrophe in the name doubled."""
    return "'" + str(title).replace("'", "''") + "'"


def _wrap(action: str, exc: Exception) -> "SheetsSyncError":
    """Turn any Google client failure (HttpError, network, quota...) into a
    plain-language SheetsSyncError so the UI always has something to show."""
    detail = str(exc)
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in (401, 403):
        detail = ("Google refused access (your connected account may not have edit rights "
                  f"on this sheet). {detail}")
    elif status == 404:
        detail = f"Sheet or tab not found. {detail}"
    elif status == 429:
        detail = f"Google is rate-limiting requests; wait a minute and retry. {detail}"
    return SheetsSyncError(f"{action}: {detail}")


def get_values(service, spreadsheet_id: str, a1_range: str, unformatted: bool = False) -> List[List[Any]]:
    """Read a range. With ``unformatted=True`` numbers/dates come back as raw
    values (a date cell is a serial number), so a date can never be misread
    because of the sheet's locale."""
    kwargs = {}
    if unformatted:
        kwargs = {"valueRenderOption": "UNFORMATTED_VALUE"}
    try:
        resp = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=a1_range, **kwargs).execute()
    except Exception as exc:
        raise _wrap(f"Couldn't read '{a1_range}'", exc) from exc
    return resp.get("values", [])


def clear_values(service, spreadsheet_id: str, a1_range: str) -> None:
    try:
        service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=a1_range, body={}).execute()
    except Exception as exc:
        raise _wrap(f"Couldn't clear '{a1_range}'", exc) from exc


def update_values(service, spreadsheet_id: str, a1_range: str, values: List[List[Any]],
                   value_input_option: str = "USER_ENTERED") -> None:
    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=a1_range,
            valueInputOption=value_input_option, body={"values": values},
        ).execute()
    except Exception as exc:
        raise _wrap(f"Couldn't write to '{a1_range}'", exc) from exc


def batch_update(service, spreadsheet_id: str, requests: List[dict]) -> Optional[dict]:
    if not requests:
        return None
    try:
        return service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    except Exception as exc:
        raise _wrap("Couldn't update the sheet's layout", exc) from exc


def ensure_grid_size(service, spreadsheet_id: str, sheet_id: int, need_rows: int, need_cols: int) -> None:
    """A values.update past the tab's current rows/columns fails with 'exceeds
    grid limits', so grow the tab first when the data won't fit."""
    meta = open_spreadsheet_meta(service, spreadsheet_id, fields="sheets(properties(sheetId,gridProperties))")
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("sheetId") != sheet_id:
            continue
        grid = props.get("gridProperties", {})
        rows, cols = int(grid.get("rowCount", 0) or 0), int(grid.get("columnCount", 0) or 0)
        if rows >= need_rows and cols >= need_cols:
            return
        batch_update(service, spreadsheet_id, [{"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {
                "rowCount": max(rows, need_rows), "columnCount": max(cols, need_cols)}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount",
        }}])
        return


def list_tab_titles(service, spreadsheet_id: str) -> List[str]:
    meta = open_spreadsheet_meta(service, spreadsheet_id, fields="sheets(properties(sheetId,title))")
    return [sh["properties"]["title"] for sh in meta.get("sheets", [])]


def col_letter(n: int) -> str:
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"
