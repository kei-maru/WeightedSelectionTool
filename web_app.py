import datetime
import json
import os
import random
import sqlite3
import tempfile
import threading
import time
import webbrowser
from cgi import FieldStorage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pandas as pd

from main import (
    APP_TITLE_PRO,
    FREE_BUILD,
    calc_weights,
    db_path,
    fuzzy_find_participant,
    init_db,
    load_csv_or_excel,
)


HOST = "127.0.0.1"
START_PORT = 8765

STATE = {
    "records": [],
    "source_columns": [],
    "id_column": None,
    "display_columns": [],
    "special_rules": [],
    "excluded_indices": set(),
    "last_winner_indices": set(),
    "latest_session_id": None,
    "csv_file": "",
}


def safe_int(value, default=0):
    if pd.isna(value):
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def raw_cell_text(row, column):
    value = row.get(column, "")
    if pd.isna(value):
        return ""
    return str(value).strip()


def record_display_name(record):
    return (
        record.get("draw_id")
        or record.get("display_name")
        or record.get("vrc_id")
        or record.get("x_id")
        or "unknown"
    )


def identity_key(*values):
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def participant_stats_by_name(conn, display_name):
    key = str(display_name or "").strip()
    if not key:
        return None
    rows = conn.execute("""
        SELECT id, display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count
        FROM participants
        WHERE lower(trim(display_name))=lower(trim(?))
        ORDER BY id
    """, (key,)).fetchall()
    if not rows:
        return None
    col_names = [d[0] for d in conn.execute(
        "SELECT id, display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count FROM participants LIMIT 0"
    ).description]
    dict_rows = [dict(zip(col_names, row)) for row in rows]
    first = dict_rows[0]
    first["join_count"] = sum(safe_int(row.get("join_count", 0)) for row in dict_rows)
    first["win_count"] = sum(safe_int(row.get("win_count", 0)) for row in dict_rows)
    return first


def dedupe_user_rows(rows):
    grouped = {}
    order = []
    for row in rows:
        key = identity_key(
            row.get("drawId"),
            row.get("displayFields", {}).get("VRC ID"),
            row.get("displayFields", {}).get("X ID"),
        )
        if not key:
            key = f"row-{len(order)}"
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["join_count"] = safe_int(row.get("join_count", 0))
            grouped[key]["win_count"] = safe_int(row.get("win_count", 0))
            order.append(key)
            continue
        target = grouped[key]
        target["join_count"] += safe_int(row.get("join_count", 0))
        target["win_count"] += safe_int(row.get("win_count", 0))
        target["winner"] = bool(target.get("winner")) or bool(row.get("winner"))
        for field, value in row.get("displayFields", {}).items():
            if value and not target.get("displayFields", {}).get(field):
                target.setdefault("displayFields", {})[field] = value
    return [grouped[key] for key in order]


def apply_column_roles():
    conn = sqlite3.connect(db_path())
    for record in STATE["records"]:
        raw = record.get("raw_values", {})
        id_column = STATE["id_column"]
        draw_id = raw.get(id_column, "") if id_column else ""
        record["draw_id"] = draw_id
        record["display_fields"] = {
            col: raw.get(col, "") for col in STATE["display_columns"]
        }
        if draw_id:
            record["display_name"] = draw_id
            participant = participant_stats_by_name(conn, draw_id)
            if participant:
                record["participant_id"] = participant["id"]
                record["matched"] = True
                record["join_count"] = participant.get("join_count", record.get("join_count", 0))
                record["win_count"] = participant.get("win_count", record.get("win_count", 0))
        else:
            record["display_name"] = (
                record.get("base_display_name")
                or record.get("vrc_id")
                or record.get("x_id")
                or "unknown"
            )
        record["raw_data"] = json.dumps({
            "raw_values": raw,
            "draw_id_column": STATE["id_column"],
            "display_columns": STATE["display_columns"],
            "special_rules": STATE.get("special_rules", []),
        }, ensure_ascii=False)
    conn.close()


def recalculate_probabilities():
    records = STATE["records"]
    if not records:
        return []
    active_records = [
        record for idx, record in enumerate(records)
        if idx not in STATE.get("excluded_indices", set())
    ]
    if not active_records:
        for record in records:
            record["weight"] = 0.0
            record["current_probability"] = "除外"
        return []
    mode = "equal" if FREE_BUILD else STATE.get("mode", "linear")
    if mode == "equal":
        active_weights = [1.0] * len(active_records)
    else:
        active_weights = calc_weights(active_records, mode, len(active_records))
    active_weights = apply_special_rules_to_weights(active_records, active_weights)
    total_weight = sum(active_weights) or 1.0
    weight_iter = iter(active_weights)
    all_weights = []
    for idx, record in enumerate(records):
        if idx in STATE.get("excluded_indices", set()):
            record["weight"] = 0.0
            record["current_probability"] = "除外"
            all_weights.append(0.0)
        else:
            weight = next(weight_iter)
            record["weight"] = weight
            record["current_probability"] = f"{(weight / total_weight) * 100:.2f}%"
            all_weights.append(weight)
    return all_weights


def mode_label(mode):
    return {
        "equal": "均等抽選",
        "linear": "線形加重",
        "double": "指数加重",
    }.get(mode, mode)


def calculation_summary(mode=None, special_rules=None):
    mode = mode or STATE.get("mode", "linear")
    special_rules = special_rules if special_rules is not None else STATE.get("special_rules", [])
    if mode == "equal":
        parts = ["基本重み: 全員 1"]
    elif mode == "linear":
        parts = ["基本重み: (1 + 参加回数 ÷ 人数) ÷ (1 + 当選回数)"]
    else:
        parts = ["基本重み: 2 ^ max(参加回数 - 当選回数, 0)"]
    if special_rules:
        rule_text = " / ".join(
            f"【{r.get('value')}】 倍率 ×{float(r.get('multiplier', 2.0)):g}"
            for r in special_rules
        )
        parts.append(f"特別条件 = {rule_text}")
    parts.append("最終確率: 自分の重み ÷ 全員の重み合計")
    return "。".join(parts)


def apply_special_rules_to_weights(records, weights):
    adjusted = list(weights)
    for idx, record in enumerate(records):
        multiplier = 1.0
        raw = record.get("raw_values", {})
        for rule in STATE.get("special_rules", []):
            if raw.get(rule.get("column"), "") == rule.get("value"):
                multiplier *= float(rule.get("multiplier", 2.0))
        record["special_multiplier"] = multiplier
        adjusted[idx] *= multiplier
    return adjusted


def build_records(df):
    conn = sqlite3.connect(db_path())
    records = []
    has_join_count = "join_count" in df.columns
    has_win_count = "win_count" in df.columns
    for _, row in df.iterrows():
        raw_values = {col: raw_cell_text(row, col) for col in df.columns}
        record = {
            "display_name": str(row.get("display_name", "") or "").strip(),
            "base_display_name": str(row.get("display_name", "") or "").strip(),
            "vrc_id": str(row.get("vrc_id", "") or "").strip(),
            "vrc_url": str(row.get("vrc_url", "") or "").strip(),
            "x_id": str(row.get("x_id", "") or "").strip(),
            "x_url": str(row.get("x_url", "") or "").strip(),
            "join_count": safe_int(row.get("join_count", 0)),
            "win_count": safe_int(row.get("win_count", 0)),
            "current_probability": str(row.get("current_probability", "") or "").strip(),
            "raw_values": raw_values,
            "draw_id": "",
            "display_fields": {},
            "raw_data": json.dumps(raw_values, ensure_ascii=False),
            "participant_id": None,
            "matched": False,
        }
        if not any(raw_values.values()):
            continue

        participant, _ = fuzzy_find_participant(
            conn, record["vrc_id"], record["vrc_url"], record["x_id"], record["x_url"])
        if participant:
            record["participant_id"] = participant["id"]
            record["matched"] = True
            if not record["display_name"]:
                record["display_name"] = participant.get("display_name", "")
            if not record["base_display_name"]:
                record["base_display_name"] = record["display_name"]
            if not has_join_count:
                record["join_count"] = participant.get("join_count", 0)
            if not has_win_count:
                record["win_count"] = participant.get("win_count", 0)

        record["display_name"] = record_display_name(record)
        if not record["base_display_name"]:
            record["base_display_name"] = record["display_name"]
        records.append(record)
    conn.close()
    return records


def save_participant_from_record(conn, record):
    now = datetime.datetime.now().isoformat()
    participant_id = record.get("participant_id")
    values = (
        record.get("vrc_id", ""),
        record.get("vrc_url", ""),
        record.get("x_id", ""),
        record.get("x_url", ""),
        record_display_name(record),
        record.get("join_count", 0),
        record.get("win_count", 0),
    )
    if participant_id:
        conn.execute(
            "UPDATE participants SET vrc_id=?,vrc_url=?,x_id=?,x_url=?,"
            "display_name=?,join_count=?,win_count=? WHERE id=?",
            (*values, participant_id))
        return participant_id

    existing = participant_stats_by_name(conn, record_display_name(record))
    if existing:
        participant_id = existing["id"]
        conn.execute(
            "UPDATE participants SET vrc_id=?,vrc_url=?,x_id=?,x_url=?,"
            "display_name=?,join_count=?,win_count=? WHERE id=?",
            (*values, participant_id))
        record["participant_id"] = participant_id
        record["matched"] = True
        return participant_id

    cur = conn.execute(
        "INSERT INTO participants "
        "(vrc_id,vrc_url,x_id,x_url,display_name,join_count,win_count,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (*values, now))
    record["participant_id"] = cur.lastrowid
    record["matched"] = False
    return cur.lastrowid


def db_snapshot():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row

    sessions = [dict(row) for row in conn.execute("""
        SELECT id, session_name, csv_file, mode, draw_count, created_at, notes
        FROM raffle_sessions
        ORDER BY id DESC
        LIMIT 50
    """).fetchall()]

    latest_session_id = sessions[0]["id"] if sessions else None
    latest_payload = db_session_results(conn, latest_session_id) if latest_session_id else {
        "results": [],
        "displayColumns": [],
    }

    participant_rows = [dict(row) for row in conn.execute("""
        SELECT id, display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count
        FROM participants
        ORDER BY id DESC
    """).fetchall()]
    conn.close()

    users = []
    for row in participant_rows:
        users.append({
            "drawId": row.get("display_name") or row.get("vrc_id") or row.get("x_id") or f"User #{row.get('id')}",
            "displayFields": {},
            "join_count": row.get("join_count", 0),
            "win_count": row.get("win_count", 0),
            "current_probability": "",
            "matched": "保存済み",
            "status": "記録",
            "winner": False,
        })
    users = dedupe_user_rows(users)
    total = len(users)
    mode = "equal" if FREE_BUILD else STATE.get("mode", "linear")
    weights = [1.0] * total if mode == "equal" else calc_weights(users, mode, max(total, 1)) if total else []
    total_weight = sum(weights) or 1.0
    for row, weight in zip(users, weights):
        row["weight"] = f"{weight:.4g}"
        row["current_probability"] = f"{(weight / total_weight) * 100:.2f}%" if total else ""

    return {
        "sessions": sessions,
        "latestSessionId": latest_session_id,
        "latestResults": latest_payload["results"],
        "resultDisplayColumns": latest_payload["displayColumns"],
        "savedUsers": users,
        "savedUserDisplayColumns": [],
    }


def db_session_results(conn, session_id):
    if not session_id:
        return {"sessionId": None, "results": [], "displayColumns": []}
    rows = conn.execute("""
        SELECT display_name, vrc_id, vrc_url, x_id, x_url, is_winner, extra_display_json
        FROM raffle_results
        WHERE session_id=?
        ORDER BY id
    """, (session_id,)).fetchall()
    display_columns = []
    results = []
    for row in rows:
        extra = {}
        if row["extra_display_json"]:
            try:
                extra = json.loads(row["extra_display_json"])
            except (TypeError, json.JSONDecodeError):
                extra = {}
        for key in extra:
            if key not in display_columns:
                display_columns.append(key)
        results.append({
            "drawId": row["display_name"] or row["vrc_id"] or row["x_id"] or "unknown",
            "displayFields": extra,
            "vrc_id": row["vrc_id"] or "",
            "vrc_url": row["vrc_url"] or "",
            "x_id": row["x_id"] or "",
            "x_url": row["x_url"] or "",
            "winner": bool(row["is_winner"]),
        })
    return {
        "sessionId": session_id,
        "results": results,
        "displayColumns": display_columns,
    }


def public_state(message=""):
    snapshot = db_snapshot()
    columns = STATE["source_columns"]
    selection_rows = []
    user_rows = []
    result_rows = []
    for idx, record in enumerate(STATE["records"]):
        raw = record.get("raw_values", {})
        base = {
            "index": idx,
            "status": "除外" if idx in STATE.get("excluded_indices", set()) else "当選" if idx in STATE["last_winner_indices"] else "待機",
            "raw": {col: raw.get(col, "") for col in columns},
            "drawId": record.get("draw_id") or record_display_name(record),
            "displayFields": record.get("display_fields", {}),
            "join_count": record.get("join_count", 0),
            "win_count": record.get("win_count", 0),
            "weight": f"{record.get('weight', 1.0):.4g}" if isinstance(record.get("weight", 1.0), (int, float)) else record.get("weight", ""),
            "special_multiplier": record.get("special_multiplier", 1.0),
            "current_probability": record.get("current_probability", ""),
            "matched": "既存" if record.get("matched") else "新規",
            "winner": idx in STATE["last_winner_indices"],
            "excluded": idx in STATE.get("excluded_indices", set()),
        }
        selection_rows.append(base)
        user_rows.append(base)
        if idx in STATE["last_winner_indices"]:
            result_rows.append(base)
    deduped_user_rows = dedupe_user_rows(user_rows)
    if deduped_user_rows:
        mode = "equal" if FREE_BUILD else STATE.get("mode", "linear")
        weights = [1.0] * len(deduped_user_rows) if mode == "equal" else calc_weights(
            deduped_user_rows, mode, len(deduped_user_rows))
        total_weight = sum(weights) or 1.0
        for row, weight in zip(deduped_user_rows, weights):
            row["weight"] = f"{weight:.4g}"
            row["current_probability"] = f"{(weight / total_weight) * 100:.2f}%"
    return {
        "ok": True,
        "message": message,
        "csvFile": STATE["csv_file"],
        "columns": columns,
        "idColumn": STATE["id_column"],
        "displayColumns": STATE["display_columns"],
        "specialRules": STATE.get("special_rules", []),
        "excludedIndices": sorted(STATE.get("excluded_indices", set())),
        "columnValues": {
            col: sorted({
                record.get("raw_values", {}).get(col, "")
                for record in STATE["records"]
                if record.get("raw_values", {}).get(col, "")
            })
            for col in columns
        },
        "rows": selection_rows,
        "users": deduped_user_rows,
        "results": result_rows,
        "latestSessionId": STATE["latest_session_id"],
        "mode": STATE.get("mode", "linear"),
        "modeLabel": mode_label(STATE.get("mode", "linear")),
        "calculationSummary": calculation_summary(),
        "savedResults": snapshot["latestResults"],
        "savedUsers": snapshot["savedUsers"],
        "sessions": snapshot["sessions"],
        "savedLatestSessionId": snapshot["latestSessionId"],
        "resultDisplayColumns": snapshot["resultDisplayColumns"],
        "savedUserDisplayColumns": snapshot["savedUserDisplayColumns"],
        "summary": {
            "total": len(STATE["records"]),
            "winners": len(STATE["last_winner_indices"]),
            "idReady": bool(STATE["id_column"]),
            "displayReady": bool(STATE["display_columns"]),
            "savedUsers": len(snapshot["savedUsers"]),
            "savedResults": len(snapshot["latestResults"]),
        },
    }


def run_raffle(payload):
    if not STATE["records"]:
        raise ValueError("先にCSV/Excelを読み込んでください。")
    if not STATE["id_column"]:
        raise ValueError("抽選ID列を指定してください。")

    records = STATE["records"]
    active_indices = [
        idx for idx in range(len(records))
        if idx not in STATE.get("excluded_indices", set())
    ]
    if not active_indices:
        raise ValueError("抽選対象がありません。除外を解除してください。")
    draw_count = int(payload.get("drawCount", 1) or 1)
    mode = "equal" if FREE_BUILD else payload.get("mode", "linear")
    STATE["mode"] = mode
    allow_repeat = bool(payload.get("allowRepeat", False))
    session_name = payload.get("sessionName") or datetime.datetime.now().strftime("抽選_%Y%m%d_%H%M")
    notes = payload.get("notes", "")
    calc_note = calculation_summary(mode, STATE.get("special_rules", []))
    saved_notes = f"{notes}\n\n[計算] {calc_note}".strip() if notes else f"[計算] {calc_note}"

    total = len(active_indices)
    if draw_count > total and not allow_repeat:
        draw_count = total

    active_records = [records[idx] for idx in active_indices]
    weights = [1.0] * total if mode == "equal" else calc_weights(active_records, mode, total)
    weights = apply_special_rules_to_weights(active_records, weights)
    total_weight = sum(weights) or 1.0
    recalculate_probabilities()
    for record, weight in zip(active_records, weights):
        record["weight"] = weight
        record["current_probability"] = f"{(weight / total_weight) * 100:.2f}%"

    pool_idx = list(range(total))
    winners_idx = []
    for _ in range(draw_count):
        if not pool_idx:
            break
        total_w = sum(weights[i] for i in pool_idx)
        needle = random.uniform(0, total_w)
        cumul = 0.0
        chosen = pool_idx[0]
        for idx in pool_idx:
            cumul += weights[idx]
            if cumul >= needle:
                chosen = idx
                break
        winners_idx.append(active_indices[chosen])
        if not allow_repeat:
            pool_idx.remove(chosen)

    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(db_path())
    cur = conn.execute(
        "INSERT INTO raffle_sessions "
        "(event_id,session_name,csv_file,mode,draw_count,created_at,notes)"
        " VALUES (?,?,?,?,?,?,?)",
        (None, session_name, STATE["csv_file"], mode, draw_count, now, saved_notes))
    session_id = cur.lastrowid

    participant_ids = {}
    for record_idx in active_indices:
        record = records[record_idx]
        participant_id = save_participant_from_record(conn, record)
        participant_ids[record_idx] = participant_id
        record["join_count"] = record.get("join_count", 0) + 1
        conn.execute("UPDATE participants SET join_count=join_count+1 WHERE id=?", (participant_id,))
        conn.execute(
            "INSERT INTO submission_records "
            "(session_id,raw_data,matched_participant_id,created_at)"
            " VALUES (?,?,?,?)",
            (session_id, record.get("raw_data", json.dumps(record, ensure_ascii=False)),
             participant_id, now))

    for winner_idx in winners_idx:
        record = records[winner_idx]
        participant_id = participant_ids[winner_idx]
        conn.execute(
            "INSERT INTO raffle_results "
            "(session_id,participant_id,display_name,vrc_id,vrc_url,x_id,x_url,is_winner,extra_display_json)"
            " VALUES (?,?,?,?,?,?,?,1,?)",
            (session_id, participant_id, record_display_name(record),
             record.get("vrc_id", ""), record.get("vrc_url", ""),
             record.get("x_id", ""), record.get("x_url", ""),
             json.dumps(record.get("display_fields", {}), ensure_ascii=False)))
        record["win_count"] = record.get("win_count", 0) + 1
        conn.execute("UPDATE participants SET win_count=win_count+1 WHERE id=?", (participant_id,))

    conn.commit()
    conn.close()
    STATE["last_winner_indices"] = set(winners_idx)
    STATE["latest_session_id"] = session_id
    recalculate_probabilities()
    return public_state(f"抽選完了: Session #{session_id} | {total}人中 {len(winners_idx)}名当選")


HTML = r"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VRC 抽選ツール Local</title>
  <style>
    :root {
      --bg: #f6f9fc;
      --panel: #ffffff;
      --text: #172554;
      --muted: #64748b;
      --line: #bfdbfe;
      --blue: #2563eb;
      --blue-soft: #dbeafe;
      --green: #16a34a;
      --green-soft: #dcfce7;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Yu Gothic UI", "Hiragino Sans", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    header {
      height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 20;
    }
    h1 { font-size: 18px; margin: 0; }
    main {
      display: grid;
      grid-template-columns: 330px minmax(0, 1fr);
      gap: 12px;
      padding: 12px;
      height: calc(100vh - 58px);
    }
    aside, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    aside {
      padding: 16px;
      overflow: auto;
    }
    section {
      min-width: 0;
      display: grid;
      grid-template-rows: auto 1fr auto;
      overflow: hidden;
    }
    label { display: block; font-size: 13px; font-weight: 700; margin: 14px 0 6px; }
    input, select, textarea, button {
      font: inherit;
      border-radius: 7px;
      border: 1px solid var(--line);
    }
    input, select, textarea {
      width: 100%;
      padding: 8px 10px;
      background: #fff;
      color: var(--text);
    }
    textarea { min-height: 74px; resize: vertical; }
    button {
      padding: 9px 12px;
      background: #eaf2ff;
      color: var(--blue);
      font-weight: 700;
      cursor: pointer;
    }
    button.primary { background: var(--blue); color: #fff; border-color: var(--blue); }
    button.danger { background: #fee2e2; color: #b91c1c; border-color: #fecaca; }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row > * { flex: 1; }
    .checks { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
    .checks input { width: auto; }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .hint { color: var(--muted); font-size: 13px; }
    .status {
      padding: 10px 12px;
      color: var(--blue);
      font-weight: 700;
      border-top: 1px solid var(--line);
      min-height: 42px;
    }
    .tableWrap { overflow: auto; min-width: 0; }
    table {
      width: max-content;
      min-width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-right: 1px solid #e2e8f0;
      border-bottom: 1px solid #e2e8f0;
      padding: 8px 10px;
      min-width: 120px;
      max-width: 260px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-align: left;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #eaf2ff;
      cursor: pointer;
      user-select: none;
    }
    th.idCol, td.idCol { background: var(--blue-soft); }
    th.idCol { background: var(--blue); color: #fff; }
    th.displayCol, td.displayCol { background: var(--green-soft); }
    th.displayCol { background: var(--green); color: #fff; }
    tr.winner td { font-weight: 700; }
    tr.winner td:first-child { color: var(--blue); }
    .empty {
      height: 100%;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 30px;
    }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; height: auto; }
      section { min-height: 520px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>VRC 抽選ツール <span class="hint">Local Web</span></h1>
    <button class="danger exitAction" type="button">終了</button>
  </header>
  <main>
    <aside>
      <form id="uploadForm">
        <label>CSV / Excel ファイル</label>
        <input name="file" type="file" accept=".csv,.xls,.xlsx" required>
        <button class="primary" style="width:100%; margin-top:10px;">読み込み</button>
      </form>

      <div class="row">
        <div>
          <label>抽選人数</label>
          <input id="drawCount" type="number" min="1" value="1">
        </div>
        <div>
          <label>確率モード</label>
          <select id="mode">
            <option value="linear">線形加重 - 当たってない人を少し優先</option>
            <option value="double">指数加重 - 当たってない人を強く優先</option>
            <option value="equal">均等抽選 - 履歴を見ず全員同じ</option>
          </select>
        </div>
      </div>

      <label class="checks">
        <input id="allowRepeat" type="checkbox">
        <span>重複当選を許可</span>
      </label>

      <label>備考</label>
      <textarea id="notes"></textarea>

      <button id="raffleBtn" class="primary" style="width:100%; margin-top:14px;" disabled>抽選開始</button>
      <button class="danger exitAction" type="button" style="width:100%; margin-top:10px;">アプリを終了</button>
    </aside>
    <section>
      <div class="toolbar">
        <div>
          <strong>応募者一覧</strong>
          <div class="hint">左クリック：先に青色の抽選ID列を指定し、その後に緑色の表示列を指定します。右クリック：列指定を解除します。</div>
        </div>
        <div class="hint" id="fileName"></div>
      </div>
      <div class="tableWrap" id="tableWrap">
        <div class="empty">CSV / Excel を読み込んでください。</div>
      </div>
      <div class="status" id="status">待機中</div>
    </section>
  </main>
  <script>
    const state = { columns: [], rows: [], idColumn: null, displayColumns: [] };
    const $ = (id) => document.getElementById(id);

    function setStatus(text) { $("status").textContent = text || "待機中"; }

    function mergeState(data) {
      Object.assign(state, {
        columns: data.columns || [],
        rows: data.rows || [],
        idColumn: data.idColumn || null,
        displayColumns: data.displayColumns || []
      });
      $("fileName").textContent = data.csvFile || "";
      $("raffleBtn").disabled = !state.rows.length;
      renderTable();
      setStatus(data.message);
    }

    function colClass(col) {
      if (col === state.idColumn) return "idCol";
      if (state.displayColumns.includes(col)) return "displayCol";
      return "";
    }

    function renderTable() {
      if (!state.rows.length) {
        $("tableWrap").innerHTML = '<div class="empty">CSV / Excel を読み込んでください。</div>';
        return;
      }
      const headers = ["状態", ...state.columns, "参加回数", "当選回数", "現在確率", "照合"];
      let html = "<table><thead><tr>";
      for (const h of headers) {
        const isSource = state.columns.includes(h);
        const badge = h === state.idColumn ? " [抽選ID]" : state.displayColumns.includes(h) ? " [展示列]" : "";
        html += `<th class="${colClass(h)}" data-col="${isSource ? escapeHtml(h) : ""}">${escapeHtml(h + badge)}</th>`;
      }
      html += "</tr></thead><tbody>";
      for (const row of state.rows) {
        html += `<tr class="${row.winner ? "winner" : ""}">`;
        html += `<td>${escapeHtml(row.status)}</td>`;
        for (const col of state.columns) {
          html += `<td class="${colClass(col)}">${escapeHtml(row.raw[col] || "")}</td>`;
        }
        html += `<td>${row.join_count}</td><td>${row.win_count}</td><td>${escapeHtml(row.current_probability)}</td><td>${escapeHtml(row.matched)}</td>`;
        html += "</tr>";
      }
      html += "</tbody></table>";
      $("tableWrap").innerHTML = html;
      document.querySelectorAll("th[data-col]").forEach(th => {
        th.addEventListener("click", () => chooseColumn(th.dataset.col));
        th.addEventListener("contextmenu", (event) => {
          event.preventDefault();
          cancelColumn(th.dataset.col);
        });
      });
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "操作に失敗しました");
      return data;
    }

    async function chooseColumn(col) {
      if (!col) return;
      let idColumn = state.idColumn;
      let displayColumns = [...state.displayColumns];
      if (!idColumn || col === idColumn) {
        idColumn = col;
        displayColumns = displayColumns.filter(c => c !== col);
      } else if (!displayColumns.includes(col)) {
        displayColumns.push(col);
      }
      const data = await postJson("/api/roles", { idColumn, displayColumns });
      mergeState(data);
    }

    async function cancelColumn(col) {
      if (!col) return;
      const idColumn = state.idColumn === col ? null : state.idColumn;
      const displayColumns = state.displayColumns.filter(c => c !== col);
      const data = await postJson("/api/roles", { idColumn, displayColumns });
      mergeState(data);
    }

    $("uploadForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      setStatus("読み込み中...");
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const data = await res.json();
      if (!data.ok) { setStatus(data.error); return; }
      mergeState(data);
    });

    $("raffleBtn").addEventListener("click", async () => {
      try {
        const data = await postJson("/api/raffle", {
          drawCount: $("drawCount").value,
          mode: $("mode").value,
          allowRepeat: $("allowRepeat").checked,
          notes: $("notes").value
        });
        mergeState(data);
      } catch (err) {
        setStatus(err.message);
      }
    });

    document.querySelectorAll(".exitAction").forEach(button => {
      button.addEventListener("click", async () => {
        document.querySelectorAll(".exitAction").forEach(btn => btn.disabled = true);
        setStatus("サーバーを終了しています...");
        try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
        document.body.innerHTML = '<div class="empty">ローカルサーバーを終了しました。このタブは閉じて大丈夫です。</div>';
      });
    });
  </script>
</body>
</html>
"""

MODERN_HTML = r"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VRC 抽選ツール Local Web</title>
  <style>
    :root {
      --bg: #f7f9fd;
      --panel: #ffffff;
      --panel2: #f1f6ff;
      --text: #15285f;
      --muted: #6b7895;
      --line: #c7dbff;
      --blue: #2f66ee;
      --blue2: #174cc7;
      --blueSoft: #e5efff;
      --green: #13a361;
      --greenSoft: #e3f8ed;
      --red: #dc2626;
      --redSoft: #fee2e2;
      --shadow: 0 14px 34px rgba(37, 99, 235, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Yu Gothic UI", "Hiragino Sans", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, #eaf2ff 0, transparent 320px),
        linear-gradient(180deg, #fbfdff 0%, var(--bg) 100%);
    }
    header {
      position: sticky;
      top: 0;
      z-index: 30;
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 66px;
      padding: 0 22px;
      background: rgba(255,255,255,.88);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(16px);
    }
    h1 { margin: 0; font-size: 22px; letter-spacing: .01em; }
    .shell {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      gap: 18px;
      padding: 18px;
      height: calc(100vh - 66px);
    }
    .card {
      background: rgba(255,255,255,.95);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
    }
    aside {
      padding: 18px;
      overflow: auto;
    }
    .content {
      min-width: 0;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      overflow: hidden;
    }
    .topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 16px 18px 10px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 0 18px 12px;
      border-bottom: 1px solid var(--line);
    }
    .tab {
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--muted);
      padding: 10px 14px;
      border-radius: 999px;
      font-weight: 800;
      cursor: pointer;
    }
    .tab.active {
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
    }
    label { display: block; margin: 14px 0 7px; font-size: 13px; font-weight: 800; }
    input, select, textarea, button {
      font: inherit;
      border-radius: 10px;
      border: 1px solid var(--line);
    }
    input, select, textarea {
      width: 100%;
      padding: 10px 12px;
      background: #fff;
      color: var(--text);
      outline: none;
    }
    input:focus, select:focus, textarea:focus {
      border-color: var(--blue);
      box-shadow: 0 0 0 3px rgba(47,102,238,.12);
    }
    textarea { min-height: 84px; resize: vertical; }
    button {
      padding: 10px 14px;
      font-weight: 900;
      cursor: pointer;
    }
    button.primary {
      color: #fff;
      background: linear-gradient(135deg, var(--blue), var(--blue2));
      border-color: var(--blue);
    }
    button.soft { background: var(--blueSoft); color: var(--blue2); }
    button.danger { background: var(--redSoft); color: var(--red); border-color: #fecaca; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 13px;
      font-weight: 800;
    }
    .check input { width: auto; }
    .hint {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .guide {
      margin: 0 18px 12px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: linear-gradient(135deg, #eef5ff, #f8fbff);
    }
    .guide strong { display: block; margin-bottom: 6px; }
    .badges { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      background: #eef2ff;
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
    }
    .badge.blue { background: var(--blue); color: #fff; }
    .badge.green { background: var(--green); color: #fff; }
    .status {
      min-height: 46px;
      padding: 12px 18px;
      color: var(--blue2);
      font-weight: 900;
      border-top: 1px solid var(--line);
      background: #fff;
    }
    .tableWrap { min-width: 0; overflow: auto; background: #fff; }
    .resultStack {
      display: grid;
      gap: 16px;
      padding: 16px;
      min-width: 100%;
    }
    .resultBlock {
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: auto;
      background: #fff;
    }
    .sectionTitle {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      background: var(--panel2);
      border-bottom: 1px solid var(--line);
      font-weight: 900;
    }
    .tinyButton {
      padding: 6px 10px;
      border-radius: 8px;
      background: var(--blueSoft);
      color: var(--blue2);
      border-color: var(--line);
      font-size: 12px;
    }
    tr.activeSession td {
      background: var(--blueSoft);
      font-weight: 900;
    }
    table {
      width: max-content;
      min-width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 13px;
    }
    th, td {
      border-right: 1px solid #e5edf9;
      border-bottom: 1px solid #e5edf9;
      padding: 10px 12px;
      min-width: 136px;
      max-width: 320px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      text-align: left;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 2;
      background: #f1f6ff;
      color: var(--text);
      user-select: none;
    }
    .selectable th[data-col] { cursor: pointer; }
    th.idCol, td.idCol {
      background: linear-gradient(135deg, rgba(47,102,238,.22), rgba(255,255,255,.58));
      box-shadow: inset 0 0 0 1px rgba(47,102,238,.18);
      backdrop-filter: blur(10px);
    }
    th.idCol {
      background: linear-gradient(135deg, rgba(47,102,238,.94), rgba(89,139,255,.76));
      color: #fff;
    }
    th.displayCol, td.displayCol {
      background: linear-gradient(135deg, rgba(19,163,97,.22), rgba(255,255,255,.62));
      box-shadow: inset 0 0 0 1px rgba(19,163,97,.18);
      backdrop-filter: blur(10px);
    }
    th.displayCol {
      background: linear-gradient(135deg, rgba(19,163,97,.94), rgba(69,206,135,.76));
      color: #fff;
    }
    th.specialCol, td.specialCol {
      background: linear-gradient(135deg, rgba(217,119,6,.22), rgba(255,255,255,.62));
      box-shadow: inset 0 0 0 1px rgba(217,119,6,.18);
      backdrop-filter: blur(10px);
    }
    th.specialCol {
      background: linear-gradient(135deg, rgba(217,119,6,.94), rgba(251,191,36,.76));
      color: #fff;
    }
    tr.excludedRow td {
      background: rgba(254, 226, 226, .92) !important;
      color: #991b1b;
      text-decoration: line-through;
    }
    .selectable tbody tr { cursor: pointer; }
    .selectable tbody tr:hover td {
      box-shadow: inset 0 0 0 9999px rgba(47,102,238,.045);
    }
    tr.winner td { background: #fff7ed; font-weight: 900; }
    .empty {
      height: 100%;
      min-height: 320px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 30px;
      font-weight: 800;
    }
    .miniStats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }
    .mini {
      padding: 12px;
      border-radius: 12px;
      background: var(--panel2);
      border: 1px solid var(--line);
    }
    .mini span { display: block; color: var(--muted); font-size: 12px; font-weight: 800; }
    .mini strong { display: block; margin-top: 4px; font-size: 20px; }
    .modalOverlay {
      position: fixed;
      inset: 0;
      z-index: 100;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: rgba(15, 23, 42, .30);
      backdrop-filter: blur(10px);
    }
    .modalOverlay.open { display: flex; }
    .modal {
      width: min(520px, 100%);
      padding: 20px;
      border-radius: 16px;
      background: #fff;
      border: 1px solid var(--line);
      box-shadow: 0 24px 70px rgba(15, 23, 42, .22);
    }
    .modal h2 { margin: 0 0 8px; font-size: 20px; }
    .modalActions { display: flex; gap: 10px; margin-top: 16px; }
    .modalActions button { flex: 1; }
    @media (max-width: 980px) {
      .shell { grid-template-columns: 1fr; height: auto; }
      .content { min-height: 640px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>VRC 抽選ツール <span class="hint">Local Web</span></h1>
    <button class="danger exitAction" type="button">終了</button>
  </header>
  <main class="shell">
    <aside class="card">
      <form id="uploadForm">
        <label>CSV / Excel ファイル</label>
        <input name="file" type="file" accept=".csv,.xls,.xlsx" required>
        <button class="primary" style="width:100%; margin-top:10px;">読み込み</button>
      </form>

      <div class="row">
        <div>
          <label>抽選人数</label>
          <input id="drawCount" type="number" min="1" value="1">
        </div>
        <div>
          <label>確率モード</label>
          <select id="mode">
            <option value="linear">線形加重 - 当たってない人を少し優先</option>
            <option value="double">指数加重 - 当たってない人を強く優先</option>
            <option value="equal">均等抽選 - 履歴を見ず全員同じ</option>
          </select>
        </div>
      </div>

      <label class="check">
        <input id="allowRepeat" type="checkbox">
        <span>重複当選を許可</span>
      </label>

      <label>備考</label>
      <textarea id="notes"></textarea>

      <button id="raffleBtn" class="primary" style="width:100%; margin-top:14px;" disabled>抽選開始</button>
      <button class="danger exitAction" type="button" style="width:100%; margin-top:10px;">アプリを終了</button>

      <div class="miniStats">
        <div class="mini"><span>応募者</span><strong id="statTotal">0</strong></div>
        <div class="mini"><span>ID列</span><strong id="statId">未</strong></div>
        <div class="mini"><span>表示列</span><strong id="statDisplay">0</strong></div>
      </div>
    </aside>

    <section class="card content">
      <div class="topline">
        <div>
          <strong id="panelTitle">列設定</strong>
          <div class="hint" id="fileName">CSV / Excel を読み込んでください。</div>
        </div>
      </div>
      <nav class="tabs">
        <button class="tab active" type="button" data-tab="select">列設定</button>
        <button class="tab" type="button" data-tab="results">抽選結果</button>
        <button class="tab" type="button" data-tab="users">ユーザー一覧</button>
      </nav>
      <div class="guide" id="guide"></div>
      <div class="tableWrap" id="tableWrap">
        <div class="empty">CSV / Excel を読み込んでください。</div>
      </div>
      <div class="status" id="status">待機中</div>
    </section>
  </main>
  <div class="modalOverlay" id="specialModal">
    <div class="modal">
      <h2>特別条件を設定</h2>
      <div class="hint" id="specialColumnText"></div>
      <label>この値の応募者を優先します</label>
      <select id="specialValue"></select>
      <label>倍率</label>
      <input id="specialMultiplier" type="number" min="1" step="0.1" value="2">
      <div class="hint" style="margin-top:8px;">選んだ値に一致する応募者の重みに、この倍率を掛けます。</div>
      <div class="modalActions">
        <button class="primary" type="button" id="specialSave">設定</button>
        <button class="soft" type="button" id="specialClear">解除</button>
        <button class="danger" type="button" id="specialCancel">キャンセル</button>
      </div>
    </div>
  </div>

  <script>
    const state = {
      columns: [], rows: [], users: [], results: [],
      idColumn: null, displayColumns: [], latestSessionId: null,
      savedUsers: [], savedResults: [], sessions: [], savedLatestSessionId: null,
      resultDisplayColumns: [], savedUserDisplayColumns: [],
      selectedSessionId: null, selectedResults: [], selectedResultDisplayColumns: [],
      mode: "linear", modeLabel: "線形加重",
      specialRules: [], columnValues: {},
      excludedIndices: [],
      calculationSummary: "", selectedCalculationSummary: "",
      summary: { total: 0, winners: 0, idReady: false, displayReady: false }
    };
    let activeTab = "select";
    let specialColumn = null;
    const $ = (id) => document.getElementById(id);
    function setStatus(text) { $("status").textContent = text || "待機中"; }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }
    function colClass(col) {
      if (col === state.idColumn) return "idCol";
      if (state.displayColumns.includes(col)) return "displayCol";
      if (state.specialRules.some(rule => rule.column === col)) return "specialCol";
      return "";
    }
    function setTab(tab) {
      activeTab = tab;
      document.querySelectorAll(".tab").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === tab);
      });
      render();
    }
    function updateStats() {
      $("statTotal").textContent = state.summary.total || state.summary.savedUsers || 0;
      $("statId").textContent = state.idColumn ? "済" : "未";
      $("statDisplay").textContent = state.displayColumns.length;
      $("raffleBtn").disabled = !state.rows.length || !state.idColumn;
      $("fileName").textContent = state.csvFile || "CSV / Excel を読み込んでください。";
      $("mode").value = state.mode || $("mode").value;
    }
    function mergeState(data, nextTab) {
      Object.assign(state, {
        columns: data.columns || [],
        rows: data.rows || [],
        users: data.users || [],
        results: data.results || [],
        idColumn: data.idColumn || null,
        displayColumns: data.displayColumns || [],
        latestSessionId: data.latestSessionId || null,
        savedUsers: data.savedUsers || [],
        savedResults: data.savedResults || [],
        sessions: data.sessions || [],
        savedLatestSessionId: data.savedLatestSessionId || null,
        resultDisplayColumns: data.resultDisplayColumns || [],
        savedUserDisplayColumns: data.savedUserDisplayColumns || [],
        selectedSessionId: data.latestSessionId || data.savedLatestSessionId || state.selectedSessionId,
        selectedResults: [],
        selectedResultDisplayColumns: [],
        mode: data.mode || state.mode || "linear",
        modeLabel: data.modeLabel || state.modeLabel || "線形加重",
        specialRules: data.specialRules || [],
        excludedIndices: data.excludedIndices || [],
        columnValues: data.columnValues || {},
        calculationSummary: data.calculationSummary || state.calculationSummary || "",
        selectedCalculationSummary: "",
        csvFile: data.csvFile || "",
        summary: data.summary || { total: 0, winners: 0, idReady: false, displayReady: false }
      });
      updateStats();
      if (nextTab) activeTab = nextTab;
      render();
      setStatus(data.message);
    }
    function renderGuide() {
      if (activeTab === "select") {
        $("guide").style.display = "";
        $("panelTitle").textContent = "列設定";
        $("guide").innerHTML = `<strong>列を選択してください</strong>
          <div>1. 抽選に使うID列を左クリックしてください。選択された列は青色になります。</div>
          <div>2. 結果に表示したい列を左クリックしてください。表示列は緑色になります。</div>
          <div>3. 未選択の列を右クリックすると、特別条件を設定できます。条件に合う応募者の確率が上がります。</div>
          <div>4. 行をクリックすると、その応募者を抽選から除外できます。除外行は赤色になります。</div>
          <div>5. 青色・緑色・オレンジ色の列を右クリックすると指定を解除できます。</div>`;
      } else if (activeTab === "results") {
        $("guide").style.display = "";
        $("panelTitle").textContent = "抽選結果一覧";
        const sessionId = state.latestSessionId || state.savedLatestSessionId;
        const session = sessionId ? `Session #${sessionId}` : "記録なし";
        $("guide").innerHTML = `<strong>最新の抽選結果</strong>
          <div>${escapeHtml(session)} の当選者を表示します。CSVを読み込んでいなくても保存済み記録を確認できます。</div>`;
      } else {
        $("guide").style.display = "none";
        $("panelTitle").textContent = "ユーザー一覧";
        $("guide").innerHTML = "";
      }
    }
    function table(headers, body, className = "") {
      return `<table class="${className}"><thead><tr>${headers.join("")}</tr></thead><tbody>${body}</tbody></table>`;
    }
    function renderSelection() {
      if (!state.rows.length) return '<div class="empty">CSV / Excel を読み込んでください。</div>';
      const headers = state.columns.map(col => {
        const special = state.specialRules.find(rule => rule.column === col);
        const badge = col === state.idColumn
          ? " [抽選ID]"
            : state.displayColumns.includes(col)
              ? " [表示列]"
              : special
              ? ` [特別: ${special.value} ×${special.multiplier}]`
              : "";
        return `<th class="${colClass(col)}" data-col="${escapeHtml(col)}">${escapeHtml(col + badge)}</th>`;
      });
      const body = state.rows.map(row => `<tr class="${row.excluded ? "excludedRow" : ""}" data-row="${row.index}">${state.columns.map(col =>
        `<td class="${colClass(col)}">${escapeHtml(row.raw[col])}</td>`).join("")}</tr>`).join("");
      return table(headers, body, "selectable");
    }
    function renderResults() {
      const currentSessionId = state.latestSessionId || state.selectedSessionId || state.savedLatestSessionId;
      const resultRows = state.results.length
        ? state.results
        : state.selectedResults.length
          ? state.selectedResults
          : state.savedResults;
      const displayColumns = state.results.length
        ? state.displayColumns
        : state.selectedResults.length
          ? state.selectedResultDisplayColumns
          : state.resultDisplayColumns;
      const resultTitle = currentSessionId ? `抽選結果: Session #${currentSessionId}` : "抽選結果";
      const calcText = state.results.length
        ? state.calculationSummary
        : state.selectedCalculationSummary || state.calculationSummary;
      const resultTable = resultRows.length
        ? table(
            ["抽選ID", ...displayColumns].map(c => `<th>${escapeHtml(c)}</th>`),
            resultRows.map(row => `<tr class="winner"><td>${escapeHtml(row.drawId)}</td>${
              displayColumns.map(col => `<td>${escapeHtml(row.displayFields[col])}</td>`).join("")
            }</tr>`).join("")
          )
        : '<div class="empty">まだ抽選結果がありません。</div>';

      const sessionHeaders = ["Session", "セッション名", "CSV", "モード", "抽選数", "日時", "操作"]
        .map(c => `<th>${escapeHtml(c)}</th>`);
      const sessionBody = state.sessions.length
        ? state.sessions.map(session => `<tr class="${session.id === currentSessionId ? "activeSession" : ""}">
            <td>#${escapeHtml(session.id)}</td>
            <td>${escapeHtml(session.session_name)}</td>
            <td>${escapeHtml(session.csv_file)}</td>
            <td>${escapeHtml(session.mode)}</td>
            <td>${escapeHtml(session.draw_count)}</td>
            <td>${escapeHtml(session.created_at)}</td>
            <td><button class="tinyButton sessionOpen" type="button" data-session="${escapeHtml(session.id)}">表示</button></td>
          </tr>`).join("")
        : `<tr><td colspan="7">保存済みセッションがありません。</td></tr>`;
      return `<div class="resultStack">
        <div class="resultBlock">
          <div class="sectionTitle"><span>${escapeHtml(resultTitle)}</span><span class="hint">当選者を最上部に表示しています。</span></div>
          <div style="padding:12px 14px; border-bottom:1px solid var(--line); background:#fff;">
            <strong>計算方法</strong>
            <div class="hint" style="margin-top:4px;">${escapeHtml(calcText || "計算情報がありません。")}</div>
          </div>
          ${resultTable}
        </div>
        <div class="resultBlock">
          <div class="sectionTitle"><span>保存済みセッション</span><span class="hint">過去の抽選結果をここから開けます。</span></div>
          ${table(sessionHeaders, sessionBody)}
        </div>
      </div>`;
    }
    function renderUsers() {
      const rows = state.savedUsers;
      const displayColumns = state.savedUserDisplayColumns;
      if (!rows.length) return '<div class="empty">保存済みユーザーがありません。抽選を実行するとグローバルユーザー一覧に追加されます。</div>';
      const headers = ["抽選ID", ...displayColumns, "参加回数", "当選回数", "重み", "現在確率"]
        .map(c => `<th>${escapeHtml(c)}</th>`);
      const body = rows.map(row => `<tr class="${row.winner ? "winner" : ""}">
        <td>${escapeHtml(row.drawId)}</td>
        ${displayColumns.map(col => `<td>${escapeHtml(row.displayFields[col])}</td>`).join("")}
        <td>${escapeHtml(row.join_count)}</td>
        <td>${escapeHtml(row.win_count)}</td>
        <td>${escapeHtml(row.weight)}</td>
        <td>${escapeHtml(row.current_probability)}</td>
      </tr>`).join("");
      return table(headers, body);
    }
    function bindColumnEvents() {
      document.querySelectorAll("th[data-col]").forEach(th => {
        th.addEventListener("click", () => chooseColumn(th.dataset.col));
        th.addEventListener("contextmenu", event => {
          event.preventDefault();
          handleColumnContext(th.dataset.col);
        });
      });
    }
    function bindSessionEvents() {
      document.querySelectorAll(".sessionOpen").forEach(button => {
        button.addEventListener("click", () => loadSession(button.dataset.session));
      });
    }
    function bindRowEvents() {
      document.querySelectorAll("tbody tr[data-row]").forEach(row => {
        row.addEventListener("click", event => {
          if (event.target.closest("button")) return;
          toggleExclude(row.dataset.row);
        });
      });
    }
    function render() {
      renderGuide();
      document.querySelectorAll(".tab").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === activeTab);
      });
      if (activeTab === "select") $("tableWrap").innerHTML = renderSelection();
      if (activeTab === "results") $("tableWrap").innerHTML = renderResults();
      if (activeTab === "users") $("tableWrap").innerHTML = renderUsers();
      bindColumnEvents();
      bindRowEvents();
      bindSessionEvents();
    }
    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "操作に失敗しました");
      return data;
    }
    async function chooseColumn(col) {
      if (!col) return;
      let idColumn = state.idColumn;
      let displayColumns = [...state.displayColumns];
      if (!idColumn || col === idColumn) {
        idColumn = col;
        displayColumns = displayColumns.filter(c => c !== col);
      } else if (!displayColumns.includes(col)) {
        displayColumns.push(col);
      }
      mergeState(await postJson("/api/roles", { idColumn, displayColumns }), "select");
    }
    async function cancelColumn(col) {
      if (!col) return;
      const idColumn = state.idColumn === col ? null : state.idColumn;
      const displayColumns = state.displayColumns.filter(c => c !== col);
      mergeState(await postJson("/api/roles", { idColumn, displayColumns }), "select");
    }
    async function handleColumnContext(col) {
      if (!col) return;
      if (col === state.idColumn || state.displayColumns.includes(col)) {
        await cancelColumn(col);
        return;
      }
      if (state.specialRules.some(rule => rule.column === col)) {
        mergeState(await postJson("/api/special", { column: col, action: "clear" }), "select");
        return;
      }
      openSpecialModal(col);
    }
    function openSpecialModal(col) {
      specialColumn = col;
      $("specialColumnText").textContent = `対象列: ${col}`;
      const values = state.columnValues[col] || [];
      $("specialValue").innerHTML = values.length
        ? values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")
        : '<option value="">選択できる値がありません</option>';
      const existing = state.specialRules.find(rule => rule.column === col);
      $("specialMultiplier").value = existing ? existing.multiplier : 2;
      $("specialSave").disabled = !values.length;
      $("specialModal").classList.add("open");
    }
    function closeSpecialModal() {
      specialColumn = null;
      $("specialModal").classList.remove("open");
    }
    async function toggleExclude(index) {
      try {
        const data = await postJson("/api/exclude", { index });
        mergeState(data, "select");
      } catch (err) {
        setStatus(err.message);
      }
    }
    async function loadSession(sessionId) {
      try {
        const data = await postJson("/api/session", { sessionId });
        state.selectedSessionId = data.sessionId;
        state.selectedResults = data.results || [];
        state.selectedResultDisplayColumns = data.displayColumns || [];
        state.selectedCalculationSummary = data.calculationSummary || "";
        state.results = [];
        state.latestSessionId = null;
        activeTab = "results";
        render();
        setStatus(data.message);
        $("tableWrap").scrollTop = 0;
      } catch (err) {
        setStatus(err.message);
      }
    }
    $("uploadForm").addEventListener("submit", async event => {
      event.preventDefault();
      setStatus("読み込み中...");
      const res = await fetch("/api/upload", { method: "POST", body: new FormData(event.currentTarget) });
      const data = await res.json();
      if (!data.ok) { setStatus(data.error); return; }
      mergeState(data, "select");
    });
    $("raffleBtn").addEventListener("click", async () => {
      try {
        const data = await postJson("/api/raffle", {
          drawCount: $("drawCount").value,
          mode: $("mode").value,
          allowRepeat: $("allowRepeat").checked,
          notes: $("notes").value
        });
        mergeState(data, "results");
        $("tableWrap").scrollTop = 0;
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("mode").addEventListener("change", async () => {
      try {
        const data = await postJson("/api/mode", { mode: $("mode").value });
        mergeState(data, activeTab);
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("specialSave").addEventListener("click", async () => {
      if (!specialColumn) return;
      try {
        const data = await postJson("/api/special", {
          column: specialColumn,
          value: $("specialValue").value,
          multiplier: $("specialMultiplier").value,
          action: "set"
        });
        closeSpecialModal();
        mergeState(data, "select");
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("specialClear").addEventListener("click", async () => {
      if (!specialColumn) return;
      try {
        const data = await postJson("/api/special", {
          column: specialColumn,
          action: "clear"
        });
        closeSpecialModal();
        mergeState(data, "select");
      } catch (err) {
        setStatus(err.message);
      }
    });
    $("specialCancel").addEventListener("click", closeSpecialModal);
    $("specialModal").addEventListener("click", event => {
      if (event.target === $("specialModal")) closeSpecialModal();
    });
    document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
    document.querySelectorAll(".exitAction").forEach(button => {
      button.addEventListener("click", async () => {
        document.querySelectorAll(".exitAction").forEach(btn => btn.disabled = true);
        setStatus("サーバーを終了しています...");
        try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
        document.body.innerHTML = '<div class="empty">ローカルサーバーを終了しました。このタブは閉じて大丈夫です。</div>';
      });
    });
    async function loadInitialState() {
      try {
        const data = await postJson("/api/state", {});
        mergeState(data);
      } catch (err) {
        setStatus(err.message);
        render();
      }
    }
    loadInitialState();
  </script>
</body>
</html>
"""


class LocalWebHandler(BaseHTTPRequestHandler):
    server_version = "WeightedSelectionLocal/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = MODERN_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/upload":
                self.handle_upload()
            elif parsed.path == "/api/roles":
                self.handle_roles()
            elif parsed.path == "/api/raffle":
                self.handle_raffle()
            elif parsed.path == "/api/state":
                self.send_json(public_state("保存済みの記録を読み込みました。"))
            elif parsed.path == "/api/session":
                self.handle_session()
            elif parsed.path == "/api/mode":
                self.handle_mode()
            elif parsed.path == "/api/special":
                self.handle_special()
            elif parsed.path == "/api/exclude":
                self.handle_exclude()
            elif parsed.path == "/api/shutdown":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def handle_upload(self):
        form = FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type"),
            })
        item = form["file"] if "file" in form else None
        if item is None or not getattr(item, "filename", ""):
            raise ValueError("ファイルを選択してください。")

        suffix = os.path.splitext(item.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(item.file.read())
            tmp_path = tmp.name
        try:
            df = load_csv_or_excel(tmp_path)
        finally:
            os.unlink(tmp_path)

        STATE["records"] = build_records(df)
        STATE["source_columns"] = list(df.columns)
        STATE["id_column"] = None
        STATE["display_columns"] = []
        STATE["special_rules"] = []
        STATE["excluded_indices"] = set()
        STATE["last_winner_indices"] = set()
        STATE["latest_session_id"] = None
        STATE["csv_file"] = os.path.basename(item.filename)
        STATE["mode"] = "linear"
        apply_column_roles()
        recalculate_probabilities()
        self.send_json(public_state(
            f"{len(STATE['records'])}件を読み込みました。左クリックで抽選ID列を指定し、その後に表示列を指定してください。"))

    def handle_roles(self):
        payload = self.read_json()
        source = set(STATE["source_columns"])
        id_column = payload.get("idColumn")
        display_columns = payload.get("displayColumns", [])
        if id_column and id_column not in source:
            raise ValueError("指定されたID列が見つかりません。")
        STATE["id_column"] = id_column
        STATE["display_columns"] = [
            col for col in display_columns if col in source and col != id_column
        ]
        STATE["special_rules"] = [
            rule for rule in STATE.get("special_rules", [])
            if rule.get("column") in source
            and rule.get("column") != STATE["id_column"]
            and rule.get("column") not in STATE["display_columns"]
        ]
        apply_column_roles()
        recalculate_probabilities()
        msg = f"抽選ID: {STATE['id_column'] or '未指定'} / 展示列: {', '.join(STATE['display_columns']) or 'なし'}"
        self.send_json(public_state(msg))

    def handle_raffle(self):
        payload = self.read_json()
        self.send_json(run_raffle(payload))

    def handle_session(self):
        payload = self.read_json()
        session_id = safe_int(payload.get("sessionId"), None)
        if not session_id:
            raise ValueError("セッションを選択してください。")
        conn = sqlite3.connect(db_path())
        conn.row_factory = sqlite3.Row
        session = conn.execute("""
            SELECT id, session_name, csv_file, mode, draw_count, created_at, notes
            FROM raffle_sessions
            WHERE id=?
        """, (session_id,)).fetchone()
        if not session:
            conn.close()
            raise ValueError("指定されたセッションが見つかりません。")
        payload = db_session_results(conn, session_id)
        conn.close()
        payload.update({
            "ok": True,
            "session": dict(session),
            "calculationSummary": (session["notes"] or "").split("[計算]", 1)[1].strip()
            if session["notes"] and "[計算]" in session["notes"] else "",
            "message": f"Session #{session_id} の結果を表示しています。",
        })
        self.send_json(payload)

    def handle_mode(self):
        payload = self.read_json()
        mode = payload.get("mode", "linear")
        if mode not in ("equal", "linear", "double"):
            raise ValueError("確率モードが不正です。")
        STATE["mode"] = mode
        recalculate_probabilities()
        self.send_json(public_state(f"確率モードを「{mode_label(mode)}」に変更しました。"))

    def handle_special(self):
        payload = self.read_json()
        column = payload.get("column")
        value = payload.get("value")
        multiplier = float(payload.get("multiplier", 2.0) or 2.0)
        action = payload.get("action", "set")
        source = set(STATE["source_columns"])
        if column not in source:
            raise ValueError("指定された列が見つかりません。")
        if column == STATE["id_column"] or column in STATE["display_columns"]:
            raise ValueError("抽選ID列・表示列には特別条件を設定できません。")
        STATE["special_rules"] = [
            rule for rule in STATE.get("special_rules", [])
            if rule.get("column") != column
        ]
        if action != "clear":
            if not value:
                raise ValueError("特別条件の値を選択してください。")
            if multiplier <= 0:
                raise ValueError("倍率は0より大きい数値にしてください。")
            STATE["special_rules"].append({
                "column": column,
                "value": value,
                "multiplier": multiplier,
            })
        recalculate_probabilities()
        msg = "特別条件を解除しました。" if action == "clear" else f"特別条件: {column} = {value} の応募者を{multiplier:g}倍にしました。"
        self.send_json(public_state(msg))

    def handle_exclude(self):
        payload = self.read_json()
        idx = safe_int(payload.get("index"), None)
        if idx is None or idx < 0 or idx >= len(STATE["records"]):
            raise ValueError("対象行が見つかりません。")
        excluded = STATE.setdefault("excluded_indices", set())
        if idx in excluded:
            excluded.remove(idx)
            msg = "除外を解除しました。"
        else:
            excluded.add(idx)
            STATE["last_winner_indices"].discard(idx)
            msg = "この応募者を抽選から除外しました。"
        recalculate_probabilities()
        self.send_json(public_state(msg))


def make_server():
    for port in range(START_PORT, START_PORT + 20):
        try:
            return ThreadingHTTPServer((HOST, port), LocalWebHandler)
        except OSError:
            continue
    raise RuntimeError("利用可能なローカルポートが見つかりません。")


def main():
    init_db()
    server = make_server()
    url = f"http://{HOST}:{server.server_port}/"
    print(f"Local web app: {url}", flush=True)
    threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        print("Local web app stopped.", flush=True)


if __name__ == "__main__":
    main()
