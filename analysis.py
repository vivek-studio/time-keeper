"""Parse Slack List JSON exports and compute working hours + leave-policy columns.

Pipeline: Dataset.add_file() (tolerant parsing, every problem recorded as an issue)
          -> compute() (per user/day rows, policy flags, per-user summary, extra health items).
"""
from __future__ import annotations
import html, json, re, time, datetime as dt
from collections import defaultdict, Counter
from zoneinfo import ZoneInfo

MENTION = re.compile(r"<@([A-Z0-9]+)>")
LABELS = {
    "import_error": "Import error", "unknown_structure": "Unrecognised file structure",
    "no_records": "File has no records", "malformed_record": "Malformed record",
    "missing_timestamp": "Missing timestamp", "invalid_timestamp": "Invalid timestamp",
    "future_timestamp": "Timestamp in the future", "duplicate_message": "Duplicate messages removed",
    "unidentified_user": "Message with no user ID", "unmapped_user": "User ID without a name",
    "project_name_missing": "Project name missing", "record_name_conflict": "Same record, different names",
    "record_no_messages": "Project with no messages", "list_imported_twice": "Same list imported twice",
    "excluded_users": "Messages from excluded IDs (left out)", "single_message_day": "Single-message day (hours unreliable)",
    "late_start_not_eligible": "Late start without late work (policy mismatch)",
    "weekend_activity": "Activity on a weekend", "timezone_invalid": "Invalid time zone setting",
}
SEV_ORDER = {"error": 0, "warn": 1, "info": 2}


def _mins(s: str, default: int) -> int:
    try:
        h, m = str(s).split(":"); return int(h) * 60 + int(m)
    except Exception:
        return default


def _clean(text: str, names: dict) -> str:
    t = MENTION.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), html.unescape(text or ""))
    return re.sub(r"\s+", " ", t).strip()


class Dataset:
    def __init__(self):
        self.messages, self.issues, self.files = [], [], []
        # Global de-dup indexes (across ALL files and ALL projects):
        #   seen_ts  : (user, timestamp)               -> (file, project)   exact same Slack message
        #   seen_txt : (user, normalised text, second) -> (ts, file, project)  same text re-exported with a rounded timestamp
        self.seen_ts, self.seen_txt, self.rec_names, self.list_ids = {}, {}, {}, {}
        self._dups = None

    def issue(self, sev, kind, file="", detail="", project="", user="", ts=None):
        self.issues.append({"sev": sev, "kind": kind, "label": LABELS.get(kind, kind), "file": file, "project": project,
                            "user": user, "detail": detail,
                            "when": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else ""})

    # ------------------------------------------------------------------ one file
    def add_file(self, name: str, raw: bytes) -> dict:
        info = {"name": name, "records": 0, "messages": 0, "added": 0, "duplicates": 0, "status": "ok", "list": ""}
        self.files.append(info)
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception as e:
            info["status"] = "error"; self.issue("error", "import_error", name, f"Not valid JSON: {e}"); return info
        records, title = None, ""
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            records = data["records"]; lst = data.get("list") or {}
            title = lst.get("title", ""); lid = lst.get("id", "")
            if lid:
                if lid in self.list_ids:
                    self.issue("warn", "list_imported_twice", name, f"List '{title}' ({lid}) was already imported from '{self.list_ids[lid]}'. Duplicate messages are removed automatically.")
                self.list_ids.setdefault(lid, name)
            meta = data.get("meta") or {}
            for k, v in meta.items():
                if k.endswith("truncated") and v is True:
                    self.issue("warn", "import_error", name, f"Export reports '{k}': some data may be missing from this file.")
            if meta.get("complete") is False:
                self.issue("warn", "import_error", name, "Export says it is not complete.")
        elif isinstance(data, list):
            records = data
        else:
            info["status"] = "error"; self.issue("error", "unknown_structure", name, "Expected a Slack List export (object with 'records') or a list of records."); return info
        info["list"] = title
        if not records:
            info["status"] = "warn"; self.issue("warn", "no_records", name, "The file contains no records."); return info
        self._dups = {"same_file_or_earlier": Counter(), "other_project": Counter()}
        for item in records:
            if not isinstance(item, dict):
                self.issue("warn", "malformed_record", name, "A record is not an object; skipped."); continue
            rec = item.get("record") if isinstance(item.get("record"), dict) else item
            thread = item.get("thread") if isinstance(item.get("thread"), list) else (item.get("messages") or [])
            rid = rec.get("id") or rec.get("thread_ts") or f"{name}#{info['records']}"
            info["records"] += 1
            pname = self._project(rec)
            if not pname:
                self.issue("warn", "project_name_missing", name, f"Record {rid} has no project name.", project=rid); pname = f"(unnamed {rid})"
            prev = self.rec_names.get(rid)
            if prev and prev[0] != pname:
                self.issue("warn", "record_name_conflict", name, f"Record {rid} is '{prev[0]}' in {prev[1]} but '{pname}' here. Latest name used.", project=pname)
            self.rec_names[rid] = (pname, name)
            if not thread:
                self.issue("info", "record_no_messages", name, "No messages in this project's thread.", project=pname)
            for m in thread:
                info["messages"] += 1
                if not isinstance(m, dict):
                    self.issue("warn", "malformed_record", name, "A message is not an object; skipped.", project=pname); continue
                self._message(info, name, rid, pname, m)
        self._report_duplicates(name)
        if info["status"] == "ok" and any(i["file"] == name and i["sev"] == "error" for i in self.issues): info["status"] = "warn"
        return info

    def _report_duplicates(self, name):
        """One summary line per file instead of one issue per duplicate message."""
        d, self._dups = self._dups or {}, None
        a, b = sum(d.get("same_file_or_earlier", {}).values()), sum(d.get("other_project", {}).values())
        if a or b:
            parts = []
            if a: parts.append(f"{a} already imported from an earlier file (or repeated inside this file)")
            if b: parts.append(f"{b} the same message appearing in several projects")
            self.issue("info", "duplicate_message", name, "Removed " + str(a + b) + " duplicate message(s): " + "; ".join(parts) + ". Only one copy of each message is used.")

    def _project(self, rec):
        for f in rec.get("fields") or []:
            if f.get("key") == "name":
                t = f.get("text")
                if not t and isinstance(f.get("value"), str):
                    try: t = "".join(e.get("text", "") for b in json.loads(f["value"]) for s in b.get("elements", []) for e in s.get("elements", []))
                    except Exception: t = f["value"]
                return (t or "").strip()
        return (rec.get("name") or "").strip()

    def _message(self, info, name, rid, pname, m):
        user = (m.get("user_id") or m.get("user") or "").strip() if isinstance(m.get("user_id") or m.get("user") or "", str) else ""
        text = m.get("text") or ""
        if not text and m.get("files"): text = "[file attachment]"
        raw_ts = m.get("ts")
        if raw_ts in (None, ""):
            self.issue("warn", "missing_timestamp", name, "Message has no timestamp; left out of hours.", project=pname, user=user); return
        try: ts = float(raw_ts)
        except Exception:
            self.issue("warn", "invalid_timestamp", name, f"Timestamp '{raw_ts}' is not a number; left out of hours.", project=pname, user=user); return
        if ts < 946684800:
            self.issue("warn", "invalid_timestamp", name, f"Timestamp {raw_ts} is before year 2000; left out.", project=pname, user=user); return
        if ts > time.time() + 86400:
            self.issue("warn", "future_timestamp", name, f"Timestamp {raw_ts} is in the future; left out.", project=pname, user=user, ts=ts); return
        if not user:
            self.issue("warn", "unidentified_user", name, "Message has no user ID (bot/system?); left out of hours.", project=pname, ts=ts); return
        # ---- de-duplication: one real message must be counted exactly once, whatever file/project it came from
        tkey = (user, round(ts, 3))
        ntext = _clean(text, {}).lower()
        hit = self.seen_ts.get(tkey)
        if not hit and ntext:
            sec = int(ts)
            for b in (sec - 1, sec, sec + 1):
                prev = self.seen_txt.get((user, ntext, b))
                if prev and abs(prev[0] - ts) <= 1.0: hit = (prev[1], prev[2]); break
        if hit:
            info["duplicates"] += 1
            if self._dups is not None:
                self._dups["other_project" if hit[1] != pname and hit[0] == name else "same_file_or_earlier"][1] += 1
            return
        self.seen_ts[tkey] = (name, pname)
        if ntext: self.seen_txt[(user, ntext, int(ts))] = (ts, name, pname)
        info["added"] += 1
        self.messages.append({"ts": ts, "user": user, "text": text, "project": pname, "file": name})


# ---------------------------------------------------------------------- one row per date
def _one_per_date(groups, user_rows, names, cfg, late_co):
    """Collapse per-user rows into ONE row per date. Hours = earliest to latest message of that date across everyone.
    Policy columns are still judged per person (original logic) and rolled up: Yes if anyone qualifies, names in the note."""
    bd = defaultdict(lambda: [[], []])
    for (u, wd), lst in groups.items(): bd[wd][0].extend(lst)
    for r in user_rows: bd[dt.date.fromisoformat(r["date"])][1].append(r)
    out = []
    for wd, (lst, urs) in bd.items():
        lst.sort(key=lambda x: x[0]); base = dt.datetime.combine(wd, dt.time())
        fm = int((lst[0][0] - base).total_seconds() // 60); lm = int((lst[-1][0] - base).total_seconds() // 60)
        first, last = lst[0][1], lst[-1][1]
        who = lambda f: ", ".join(sorted({r["user"] for r in urs if f(r)}))
        dl, a11, a12 = who(lambda r: r["delayed"] == "Yes"), who(lambda r: r["next_start"] == "At 11"), who(lambda r: r["next_start"] == "At 12")
        bad = who(lambda r: r["note"].startswith("Started"))
        starts = "; ".join(x for x in ((f"At 11 ({a11})" if a11 else ""), (f"At 12 ({a12})" if a12 else "")) if x)
        nxt_any = any(r["next_start"] != "—" for r in urs)
        notes = []
        if dl: notes.append("Checkout after 8: " + dl)
        if bad: notes.append("Policy mismatch: " + bad)
        out.append({
            "date": wd.isoformat(), "day": wd.strftime("%a"), "user_id": "*", "user": "All users",
            "first": lst[0][0].strftime("%H:%M"), "last": lst[-1][0].strftime("%H:%M") + (" (+1)" if lst[-1][0].date() > wd else ""),
            "first_by": names.get(first["user"]) or first["user"], "last_by": names.get(last["user"]) or last["user"],
            "first_min": fm, "last_min": lm, "hours": round((lm - fm) / 60, 2), "messages": len(lst),
            "projects": len({m["project"] for _, m in lst}), "last_project": last["project"], "last_message": _clean(last["text"], names)[:240],
            "delayed": "Yes" if dl else "No", "next11": "Yes" if a11 else ("No" if nxt_any else "—"), "next12": "Yes" if a12 else ("No" if nxt_any else "—"),
            "next_start": starts or ("No" if nxt_any else "—"), "note": " | ".join(notes),
        })
    return sorted(out, key=lambda r: r["date"], reverse=True)


# ---------------------------------------------------------------------- compute
def compute(ds: Dataset, cfg: dict, excluded: set, names: dict) -> dict:
    issues = list(ds.issues)
    def add(sev, kind, detail, **kw):
        issues.append({"sev": sev, "kind": kind, "label": LABELS.get(kind, kind), "file": kw.get("file", ""), "project": kw.get("project", ""),
                       "user": kw.get("user", ""), "detail": detail, "when": kw.get("when", "")})
    try: tz = ZoneInfo(cfg["tz"])
    except Exception:
        add("error", "timezone_invalid", f"Time zone '{cfg['tz']}' is not valid; using Asia/Kolkata."); tz = ZoneInfo("Asia/Kolkata")
    cutoff = int(cfg.get("day_cutoff_hour", 4) or 0)
    late_co, s2, s3 = _mins(cfg["late_checkout"], 1200), _mins(cfg["shift2_start"], 660), _mins(cfg["shift3_start"], 720)
    win = int(cfg.get("start_window_min", 60) or 60)
    el2, el3 = _mins(cfg["shift2_eligible"], 1290), _mins(cfg["shift3_eligible"], 1410)
    one = bool(cfg.get("one_row_per_date", True))
    nm = lambda u: names.get(u) or u

    groups, excl_count, uniq = defaultdict(list), Counter(), set()
    for m in ds.messages:
        if m["user"] in excluded: excl_count[m["user"]] += 1; continue
        if (m["user"], round(m["ts"], 3)) in uniq: continue   # safety net: never count one message twice
        uniq.add((m["user"], round(m["ts"], 3)))
        l = dt.datetime.fromtimestamp(m["ts"], tz).replace(tzinfo=None)
        wd = (l - dt.timedelta(hours=cutoff)).date()
        groups[(m["user"], wd)].append((l, m))

    rows = {}
    for (u, wd), lst in groups.items():
        lst.sort(key=lambda x: x[0])
        base = dt.datetime.combine(wd, dt.time())
        fm = int((lst[0][0] - base).total_seconds() // 60); lm = int((lst[-1][0] - base).total_seconds() // 60)
        last = lst[-1][1]
        projs = Counter(x[1]["project"] for x in lst)
        rows[(u, wd)] = {
            "date": wd.isoformat(), "day": wd.strftime("%a"), "user_id": u, "user": nm(u),
            "first": lst[0][0].strftime("%H:%M"), "last": lst[-1][0].strftime("%H:%M") + (" (+1)" if lst[-1][0].date() > wd else ""),
            "first_by": nm(lst[0][1]["user"]), "last_by": nm(last["user"]),
            "first_min": fm, "last_min": lm, "hours": round((lm - fm) / 60, 2), "messages": len(lst), "projects": len(projs),
            "last_project": last["project"], "last_message": _clean(last["text"], names)[:240],
            "delayed": "Yes" if lm > late_co else "No", "next11": "—", "next12": "—", "note": "",
        }
    # next working day columns
    by_user = defaultdict(dict)
    for (u, wd), r in rows.items(): by_user[u][wd] = r
    for u, days in by_user.items():
        for wd, r in days.items():
            nd = wd + dt.timedelta(days=1)
            while nd.weekday() >= 5: nd += dt.timedelta(days=1)
            n = days.get(nd)
            if not n: r["note"] = "No activity next working day"; continue
            r["next11"] = "Yes" if s2 <= n["first_min"] < s2 + win else "No"
            r["next12"] = "Yes" if s3 <= n["first_min"] < s3 + win else "No"
            if r["next11"] == "Yes" and r["last_min"] <= el2:
                r["note"] = f"Started {n['first']} but worked only until {r['last']} (needs past {cfg['shift2_eligible']})"
            if r["next12"] == "Yes" and r["last_min"] <= el3:
                r["note"] = f"Started {n['first']} but worked only until {r['last']} (needs past {cfg['shift3_eligible']})"
            if r["note"]:
                add("warn", "late_start_not_eligible", r["note"] + f" — {nd.isoformat()}", user=r["user"], when=nd.isoformat())
    def next_start(r):
        if r["next11"] == "Yes": return "At 11"
        if r["next12"] == "Yes": return "At 12"
        return "—" if r["next11"] == "—" else "No"
    for r in rows.values(): r["next_start"] = next_start(r)
    out_rows = sorted(rows.values(), key=lambda r: (r["date"], r["user"]), reverse=True)
    for r in out_rows:
        if r["messages"] == 1: add("info", "single_message_day", "Only one message that day, so span is 0 h.", user=r["user"], when=r["date"])
        if dt.date.fromisoformat(r["date"]).weekday() >= 5: add("info", "weekend_activity", f"{r['messages']} message(s) on a {r['day']}.", user=r["user"], when=r["date"])
    if excl_count:
        add("info", "excluded_users", "Left out: " + ", ".join(f"{nm(u)} ({c})" for u, c in excl_count.most_common()))
    unmapped = sorted({r["user_id"] for r in out_rows if r["user_id"] not in names})
    for u in unmapped: add("info", "unmapped_user", f"{u} has no name yet; add one in Settings > Users.", user=u)

    users = []
    for u, days in by_user.items():
        rs = sorted(days.values(), key=lambda r: r["date"]); lastr = rs[-1]
        users.append({"user_id": u, "user": nm(u), "days": len(rs), "total_hours": round(sum(r["hours"] for r in rs), 1),
                      "avg_hours": round(sum(r["hours"] for r in rs) / len(rs), 2), "delayed": sum(r["delayed"] == "Yes" for r in rs),
                      "next11": sum(r["next11"] == "Yes" for r in rs), "next12": sum(r["next12"] == "Yes" for r in rs),
                      "messages": sum(r["messages"] for r in rs), "last_date": lastr["date"], "last_project": lastr["last_project"],
                      "last_message": lastr["last_message"]})
    users.sort(key=lambda x: x["user"].lower())
    if one: out_rows = _one_per_date(groups, out_rows, names, cfg, late_co)
    issues.sort(key=lambda i: (SEV_ORDER[i["sev"]], i["label"]))
    summary = defaultdict(lambda: {"count": 0})
    for i in issues:
        s = summary[i["kind"]]; s.update(kind=i["kind"], sev=i["sev"], label=i["label"]); s["count"] += 1
    dates = [r["date"] for r in out_rows]
    return {"rows": out_rows, "users": users, "health": {"summary": sorted(summary.values(), key=lambda s: (SEV_ORDER[s["sev"]], s["label"])), "items": issues[:3000], "total": len(issues)},
            "files": ds.files, "excluded_ids": sorted(excl_count),
            "stats": {"messages": len(ds.messages), "included_messages": sum(r["messages"] for r in out_rows), "users": len(users), "rows": len(out_rows),
                      "from": min(dates) if dates else "", "to": max(dates) if dates else "", "tz": str(tz)},
            "known_ids": sorted({m["user"] for m in ds.messages})}
