import datetime
import json
import os
import random
import sqlite3
import tempfile
from cgi import FieldStorage

import pandas as pd

from core import (
    FREE_BUILD,
    calc_weights,
    db_path,
    fuzzy_find_participant,
    load_csv_or_excel,
)


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
    "event_id": None,
    "user_event_id": "__default__",
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
        SELECT id, display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count, last_win_join_count, streak_count
        FROM participants
        WHERE lower(trim(display_name))=lower(trim(?))
        ORDER BY id
    """, (key,)).fetchall()
    if not rows:
        return None
    col_names = [d[0] for d in conn.execute(
        "SELECT id, display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count, last_win_join_count, streak_count FROM participants LIMIT 0"
    ).description]
    dict_rows = [dict(zip(col_names, row)) for row in rows]
    first = dict_rows[0]
    first["join_count"] = sum(safe_int(row.get("join_count", 0)) for row in dict_rows)
    first["win_count"] = sum(safe_int(row.get("win_count", 0)) for row in dict_rows)
    first["last_win_join_count"] = max(safe_int(row.get("last_win_join_count", 0)) for row in dict_rows)
    first["streak_count"] = max(safe_int(row.get("streak_count", 0)) for row in dict_rows)
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
            grouped[key]["last_win_join_count"] = safe_int(row.get("last_win_join_count", 0))
            grouped[key]["streak_count"] = safe_int(row.get("streak_count", 0))
            order.append(key)
            continue
        target = grouped[key]
        target["join_count"] += safe_int(row.get("join_count", 0))
        target["win_count"] += safe_int(row.get("win_count", 0))
        target["last_win_join_count"] = max(
            safe_int(target.get("last_win_join_count", 0)),
            safe_int(row.get("last_win_join_count", 0)))
        target["streak_count"] = max(
            safe_int(target.get("streak_count", 0)),
            safe_int(row.get("streak_count", 0)))
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
                record["last_win_join_count"] = participant.get(
                    "last_win_join_count", record.get("last_win_join_count", 0))
                record["streak_count"] = participant.get(
                    "streak_count", record.get("streak_count", 0))
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
        parts = ["基本重み: (n + 1)^2"]
    else:
        parts = ["基本重み: 2^n"]
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
            "last_win_join_count": safe_int(row.get("last_win_join_count", 0)),
            "streak_count": safe_int(row.get("streak_count", 0)),
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
            record["last_win_join_count"] = participant.get("last_win_join_count", 0)
            record["streak_count"] = participant.get("streak_count", 0)

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
        record.get("last_win_join_count", 0),
        record.get("streak_count", 0),
    )
    if participant_id:
        conn.execute(
            "UPDATE participants SET vrc_id=?,vrc_url=?,x_id=?,x_url=?,"
            "display_name=?,join_count=?,win_count=?,last_win_join_count=?,streak_count=? WHERE id=?",
            (*values, participant_id))
        return participant_id

    existing = participant_stats_by_name(conn, record_display_name(record))
    if existing:
        participant_id = existing["id"]
        conn.execute(
            "UPDATE participants SET vrc_id=?,vrc_url=?,x_id=?,x_url=?,"
            "display_name=?,join_count=?,win_count=?,last_win_join_count=?,streak_count=? WHERE id=?",
            (*values, participant_id))
        record["participant_id"] = participant_id
        record["matched"] = True
        return participant_id

    cur = conn.execute(
        "INSERT INTO participants "
        "(vrc_id,vrc_url,x_id,x_url,display_name,join_count,win_count,last_win_join_count,streak_count,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (*values, now))
    record["participant_id"] = cur.lastrowid
    record["matched"] = False
    return cur.lastrowid


def event_where_clause(column="s.event_id", event_id=None):
    if event_id in ("", None, "__all__"):
        return "", []
    if event_id == "__default__":
        return f" WHERE {column} IS NULL", []
    return f" WHERE {column}=?", [safe_int(event_id, None)]


def db_snapshot():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    user_event_id = STATE.get("user_event_id")

    events = [dict(row) for row in conn.execute("""
        SELECT id, name, description, created_at
        FROM events
        ORDER BY id DESC
    """).fetchall()]

    sessions = [dict(row) for row in conn.execute("""
        SELECT s.id, s.event_id, COALESCE(e.name, 'default') AS event_name,
               s.session_name, s.csv_file, s.mode, s.draw_count, s.created_at, s.notes
        FROM raffle_sessions s
        LEFT JOIN events e ON e.id = s.event_id
        ORDER BY s.id DESC
        LIMIT 50
    """).fetchall()]

    latest_session_id = sessions[0]["id"] if sessions else None
    latest_payload = db_session_results(conn, latest_session_id) if latest_session_id else {
        "results": [],
        "displayColumns": [],
    }

    where, params = event_where_clause("s.event_id", user_event_id)
    history_rows = [dict(row) for row in conn.execute(f"""
        SELECT p.id, p.display_name, p.vrc_id, p.vrc_url, p.x_id, p.x_url,
               s.id AS session_id,
               COALESCE(w.win_count, 0) AS session_win_count
        FROM submission_records sr
        JOIN raffle_sessions s ON s.id = sr.session_id
        JOIN participants p ON p.id = sr.matched_participant_id
        LEFT JOIN (
            SELECT session_id, participant_id, COUNT(id) AS win_count
            FROM raffle_results
            GROUP BY session_id, participant_id
        ) w ON w.session_id = s.id AND w.participant_id = p.id
        {where}
        ORDER BY s.id, sr.id
    """, params).fetchall()]
    conn.close()

    users_by_id = {}
    for row in history_rows:
        participant_id = row.get("id")
        if participant_id not in users_by_id:
            users_by_id[participant_id] = {
                "drawId": row.get("display_name") or row.get("vrc_id") or row.get("x_id") or f"User #{row.get('id')}",
                "displayFields": {},
                "join_count": 0,
                "win_count": 0,
                "last_win_join_count": 0,
                "streak_count": 0,
                "current_probability": "",
                "matched": "保存済み",
                "status": "記録",
                "winner": False,
            }
        user = users_by_id[participant_id]
        user["join_count"] += 1
        user["streak_count"] += 1
        session_win_count = safe_int(row.get("session_win_count", 0))
        if session_win_count:
            user["win_count"] += session_win_count
            user["last_win_join_count"] = user["join_count"]
            user["streak_count"] = 0
            user["winner"] = True
    users = list(users_by_id.values())
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
        "events": events,
        "latestSessionId": latest_session_id,
        "latestResults": latest_payload["results"],
        "resultDisplayColumns": latest_payload["displayColumns"],
        "savedUsers": users,
        "savedUserDisplayColumns": [],
        "userEventId": user_event_id,
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
            "last_win_join_count": record.get("last_win_join_count", 0),
            "streak_count": record.get("streak_count", 0),
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
        "events": snapshot["events"],
        "eventId": STATE.get("event_id"),
        "userEventId": snapshot["userEventId"],
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
    event_id = safe_int(payload.get("eventId"), None)
    STATE["event_id"] = event_id
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
        (event_id, session_name, STATE["csv_file"], mode, draw_count, now, saved_notes))
    session_id = cur.lastrowid

    participant_ids = {}
    for record_idx in active_indices:
        record = records[record_idx]
        participant_id = save_participant_from_record(conn, record)
        participant_ids[record_idx] = participant_id
        record["join_count"] = record.get("join_count", 0) + 1
        record["streak_count"] = record.get("streak_count", 0) + 1
        conn.execute(
            "UPDATE participants SET join_count=join_count+1,streak_count=streak_count+1 WHERE id=?",
            (participant_id,))
        conn.execute(
            "INSERT INTO submission_records "
            "(session_id,raw_data,matched_participant_id,created_at)"
            " VALUES (?,?,?,?)",
            (session_id, record.get("raw_data", json.dumps(record, ensure_ascii=False)),
             participant_id, now))

    for winner_idx in winners_idx:
        record = records[winner_idx]
        participant_id = participant_ids[winner_idx]
        reset_join_count = record.get("join_count", 0)
        conn.execute(
            "INSERT INTO raffle_results "
            "(session_id,participant_id,display_name,vrc_id,vrc_url,x_id,x_url,is_winner,extra_display_json)"
            " VALUES (?,?,?,?,?,?,?,1,?)",
            (session_id, participant_id, record_display_name(record),
             record.get("vrc_id", ""), record.get("vrc_url", ""),
             record.get("x_id", ""), record.get("x_url", ""),
             json.dumps(record.get("display_fields", {}), ensure_ascii=False)))
        record["win_count"] = record.get("win_count", 0) + 1
        record["last_win_join_count"] = reset_join_count
        record["streak_count"] = 0
        conn.execute(
            "UPDATE participants SET win_count=win_count+1,last_win_join_count=?,streak_count=0 WHERE id=?",
            (reset_join_count, participant_id))

    conn.commit()
    conn.close()
    STATE["last_winner_indices"] = set(winners_idx)
    STATE["latest_session_id"] = session_id
    recalculate_probabilities()
    return public_state(f"抽選完了: Session #{session_id} | {total}人中 {len(winners_idx)}名当選")


def read_json(headers, rfile):
    length = int(headers.get("Content-Length", "0"))
    raw = rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


def handle_upload(headers, rfile):
    form = FieldStorage(
        fp=rfile,
        headers=headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": headers.get("Content-Type"),
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
    return public_state(
        f"{len(STATE['records'])}件を読み込みました。左クリックで抽選ID列を指定し、その後に表示列を指定してください。")


def handle_roles(payload):
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
    msg = f"抽選ID: {STATE['id_column'] or '未指定'} / 表示列: {', '.join(STATE['display_columns']) or 'なし'}"
    return public_state(msg)


def handle_session(payload):
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
    return payload


def handle_session_delete(payload):
    session_id = safe_int(payload.get("sessionId"), None)
    if not session_id:
        raise ValueError("セッションを選択してください。")
    conn = sqlite3.connect(db_path())
    conn.execute("DELETE FROM raffle_results WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM submission_records WHERE session_id=?", (session_id,))
    cur = conn.execute("DELETE FROM raffle_sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("指定されたセッションが見つかりません。")
    if STATE.get("latest_session_id") == session_id:
        STATE["latest_session_id"] = None
        STATE["last_winner_indices"] = set()
    return public_state(f"Session #{session_id} を削除しました。")


def handle_event_select(payload):
    event_id = safe_int(payload.get("eventId"), None)
    STATE["event_id"] = event_id
    return public_state("Eventを選択しました。" if event_id else "Eventをdefaultにしました。")


def handle_user_event(payload):
    event_id = payload.get("eventId")
    STATE["user_event_id"] = event_id if event_id in ("__all__", "__default__") else safe_int(event_id, None)
    return public_state("ユーザー一覧のEventを変更しました。")


def handle_event_save(payload):
    event_id = safe_int(payload.get("eventId"), None)
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not name:
        raise ValueError("Event名を入力してください。")
    conn = sqlite3.connect(db_path())
    if event_id:
        cur = conn.execute(
            "UPDATE events SET name=?, description=? WHERE id=?",
            (name, description, event_id))
        if cur.rowcount == 0:
            conn.close()
            raise ValueError("指定されたEventが見つかりません。")
    else:
        row = conn.execute(
            "SELECT id FROM events WHERE lower(trim(name))=lower(trim(?))",
            (name,)
        ).fetchone()
        if row:
            event_id = row[0]
            conn.execute("UPDATE events SET description=? WHERE id=?", (description, event_id))
        else:
            cur = conn.execute(
                "INSERT INTO events (name, description, created_at) VALUES (?,?,?)",
                (name, description, datetime.datetime.now().isoformat()))
            event_id = cur.lastrowid
    conn.commit()
    conn.close()
    STATE["event_id"] = event_id
    return public_state(f"Event「{name}」を保存しました。")


def handle_event_delete(payload):
    event_id = safe_int(payload.get("eventId"), None)
    if not event_id:
        raise ValueError("Eventを選択してください。")
    conn = sqlite3.connect(db_path())
    conn.execute("UPDATE raffle_sessions SET event_id=NULL WHERE event_id=?", (event_id,))
    cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("指定されたEventが見つかりません。")
    if STATE.get("event_id") == event_id:
        STATE["event_id"] = None
    if STATE.get("user_event_id") == event_id:
        STATE["user_event_id"] = "__all__"
    return public_state("Eventを削除しました。関連Sessionはdefaultに戻しました。")


def handle_mode(payload):
    mode = payload.get("mode", "linear")
    if mode not in ("equal", "linear", "double"):
        raise ValueError("確率モードが不正です。")
    STATE["mode"] = mode
    recalculate_probabilities()
    return public_state(f"確率モードを「{mode_label(mode)}」に変更しました。")


def handle_special(payload):
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
    msg = "特別条件を解除しました。" if action == "clear" else f"特別条件 = 【{value}】 倍率 ×{multiplier:g}"
    return public_state(msg)


def handle_exclude(payload):
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
    return public_state(msg)


def handle_api(path, headers, rfile):
    if path == "/api/upload":
        return handle_upload(headers, rfile)

    payload = read_json(headers, rfile)
    routes = {
        "/api/roles": lambda: handle_roles(payload),
        "/api/raffle": lambda: run_raffle(payload),
        "/api/state": lambda: public_state("保存済みの記録を読み込みました。"),
        "/api/session": lambda: handle_session(payload),
        "/api/session/delete": lambda: handle_session_delete(payload),
        "/api/event": lambda: handle_event_save(payload),
        "/api/event/select": lambda: handle_event_select(payload),
        "/api/event/save": lambda: handle_event_save(payload),
        "/api/event/delete": lambda: handle_event_delete(payload),
        "/api/user-event": lambda: handle_user_event(payload),
        "/api/mode": lambda: handle_mode(payload),
        "/api/special": lambda: handle_special(payload),
        "/api/exclude": lambda: handle_exclude(payload),
    }
    if path not in routes:
        raise KeyError(path)
    return routes[path]()
