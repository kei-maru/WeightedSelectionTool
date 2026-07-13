import datetime
import json
import os
import random
import sqlite3
import tempfile
from email.parser import BytesParser
from email.policy import default

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
    "history_import": None,
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
    first["participant_ids"] = [row["id"] for row in dict_rows]
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
                event_stats = participant_event_totals(
                    conn,
                    participant.get("participant_ids", [participant["id"]]),
                    STATE.get("event_id"))
                record["join_count"] = event_stats["join_count"]
                record["win_count"] = event_stats["win_count"]
                record["last_win_join_count"] = participant.get(
                    "last_win_join_count", record.get("last_win_join_count", 0))
                record["streak_count"] = event_stats["streak_count"]
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
        "linear": "ゆるやか加重",
        "double": "二乗加重",
    }.get(mode, mode)


def calculation_summary(mode=None, special_rules=None):
    mode = mode or STATE.get("mode", "linear")
    special_rules = special_rules if special_rules is not None else STATE.get("special_rules", [])
    if mode == "equal":
        parts = ["基本重み: 全員 1"]
    elif mode == "linear":
        parts = ["基本重み: n + 1"]
    else:
        parts = ["基本重み: (n + 1)^2"]
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


def event_db_id(event_id):
    return 0 if event_id in ("", None, "__default__") else safe_int(event_id, 0)


def participant_event_totals(conn, participant_ids, event_id):
    ids = [safe_int(value, None) for value in participant_ids if safe_int(value, None)]
    if not ids:
        return {"join_count": 0, "win_count": 0, "streak_count": 0}
    placeholders = ",".join("?" for _ in ids)
    baseline = conn.execute(f"""
        SELECT COALESCE(SUM(join_count), 0),
               COALESCE(SUM(win_count), 0),
               COALESCE(SUM(streak_count), 0)
        FROM event_participant_history
        WHERE event_id=? AND participant_id IN ({placeholders})
    """, [event_db_id(event_id), *ids]).fetchone()
    return {
        "join_count": safe_int(baseline[0], 0),
        "win_count": safe_int(baseline[1], 0),
        "streak_count": safe_int(baseline[2], 0),
    }


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

    if user_event_id in ("", None, "__all__"):
        baseline_rows = [dict(row) for row in conn.execute("""
            SELECT p.id, p.display_name, p.vrc_id, p.vrc_url, p.x_id, p.x_url,
                   SUM(h.join_count) AS join_count,
                   SUM(h.win_count) AS win_count,
                   SUM(h.streak_count) AS streak_count
            FROM event_participant_history h
            JOIN participants p ON p.id = h.participant_id
            GROUP BY p.id, p.display_name, p.vrc_id, p.vrc_url, p.x_id, p.x_url
        """).fetchall()]
    else:
        baseline_rows = [dict(row) for row in conn.execute("""
            SELECT p.id, p.display_name, p.vrc_id, p.vrc_url, p.x_id, p.x_url,
                   h.join_count, h.win_count, h.streak_count
            FROM event_participant_history h
            JOIN participants p ON p.id = h.participant_id
            WHERE h.event_id=?
        """, (event_db_id(user_event_id),)).fetchall()]
    users_by_id = {}
    for row in baseline_rows:
        participant_id = row.get("id")
        users_by_id[participant_id] = {
            "drawId": row.get("display_name") or row.get("vrc_id") or row.get("x_id") or f"User #{participant_id}",
            "displayFields": {},
            "join_count": safe_int(row.get("join_count", 0)),
            "win_count": safe_int(row.get("win_count", 0)),
            "last_win_join_count": 0,
            "streak_count": safe_int(row.get("streak_count", 0)),
            "current_probability": "",
            "matched": "同期済み",
            "status": "履歴",
            "winner": False,
        }
    batches = [dict(row) for row in conn.execute("""
        SELECT b.id, b.event_id, CASE WHEN b.event_id=0 THEN 'default' ELSE COALESCE(e.name, 'default') END AS event_name,
               b.filename, b.sync_mode, b.row_count, b.created_at, b.undone_at
        FROM history_sync_batches b
        LEFT JOIN events e ON e.id = b.event_id
        ORDER BY b.id DESC
        LIMIT 20
    """).fetchall()]
    conn.close()
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
        "historySyncBatches": batches,
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


def public_history_import():
    data = STATE.get("history_import")
    if not data:
        return None
    return {
        "filename": data["filename"],
        "columns": data["columns"],
        "rows": data["rows"][:100],
        "total": len(data["rows"]),
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
        "historyImport": public_history_import(),
        "historySyncBatches": snapshot["historySyncBatches"],
        "allowShutdown": os.environ.get("ALLOW_SHUTDOWN", "1") == "1",
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
    target_event_id = event_db_id(event_id)
    for record_idx in active_indices:
        record = records[record_idx]
        participant_id = save_participant_from_record(conn, record)
        participant_ids[record_idx] = participant_id
        record["join_count"] = record.get("join_count", 0) + 1
        record["streak_count"] = record.get("streak_count", 0) + 1
        conn.execute(
            "UPDATE participants SET join_count=join_count+1,streak_count=streak_count+1 WHERE id=?",
            (participant_id,))
        conn.execute("""
            INSERT INTO event_participant_history
            (event_id, participant_id, join_count, win_count, streak_count, updated_at)
            VALUES (?, ?, 1, 0, 1, ?)
            ON CONFLICT(event_id, participant_id) DO UPDATE SET
                join_count=event_participant_history.join_count+1,
                streak_count=event_participant_history.streak_count+1,
                updated_at=excluded.updated_at
        """, (target_event_id, participant_id, now))
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
        conn.execute("""
            UPDATE event_participant_history
            SET win_count=win_count+1, streak_count=0, updated_at=?
            WHERE event_id=? AND participant_id=?
        """, (now, target_event_id, participant_id))

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


def read_uploaded_dataframe(headers, rfile):
    content_type = headers.get("Content-Type", "")
    length = int(headers.get("Content-Length", "0"))
    body = rfile.read(length)
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    item = next(
        (part for part in message.iter_parts()
         if part.get_param("name", header="content-disposition") == "file"),
        None)
    filename = item.get_filename() if item is not None else ""
    if not filename:
        raise ValueError("ファイルを選択してください。")

    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(item.get_payload(decode=True) or b"")
        tmp_path = tmp.name
    try:
        df = load_csv_or_excel(tmp_path)
    finally:
        os.unlink(tmp_path)
    return os.path.basename(filename), df


def handle_upload(headers, rfile):
    filename, df = read_uploaded_dataframe(headers, rfile)

    STATE["records"] = build_records(df)
    STATE["source_columns"] = list(df.columns)
    STATE["id_column"] = None
    STATE["display_columns"] = []
    STATE["special_rules"] = []
    STATE["excluded_indices"] = set()
    STATE["last_winner_indices"] = set()
    STATE["latest_session_id"] = None
    STATE["csv_file"] = filename
    STATE["mode"] = "linear"
    apply_column_roles()
    recalculate_probabilities()
    return public_state(
        f"{len(STATE['records'])}件を読み込みました。左クリックで抽選ID列を指定し、その後に表示列を指定してください。")


def handle_history_upload(headers, rfile):
    filename, df = read_uploaded_dataframe(headers, rfile)
    rows = []
    for _, row in df.iterrows():
        values = {
            column: "" if pd.isna(row.get(column)) else str(row.get(column)).strip()
            for column in df.columns
        }
        if any(values.values()):
            rows.append(values)
    STATE["history_import"] = {
        "filename": filename,
        "columns": list(df.columns),
        "rows": rows,
    }
    return public_state(f"履歴ファイルを{len(rows)}件読み込みました。3つの列を指定してください。")


def find_or_create_history_participant(conn, draw_id):
    row = conn.execute("""
        SELECT id FROM participants
        WHERE lower(trim(display_name))=lower(trim(?))
        ORDER BY id LIMIT 1
    """, (draw_id,)).fetchone()
    if row:
        return row[0]
    now = datetime.datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO participants
        (display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count,
         last_win_join_count, streak_count, created_at)
        VALUES (?, '', '', '', '', 0, 0, 0, 0, ?)
    """, (draw_id, now))
    return cur.lastrowid


def handle_history_apply(payload):
    imported = STATE.get("history_import")
    if not imported:
        raise ValueError("先に履歴ファイルを読み込んでください。")
    id_column = payload.get("idColumn")
    join_column = payload.get("joinColumn")
    win_column = payload.get("winColumn")
    columns = set(imported["columns"])
    if not id_column or not join_column or not win_column:
        raise ValueError("ID列・参加回数列・当選回数列を指定してください。")
    if len({id_column, join_column, win_column}) != 3:
        raise ValueError("3つの列は別々に指定してください。")
    if not {id_column, join_column, win_column}.issubset(columns):
        raise ValueError("指定された列が見つかりません。")
    sync_mode = payload.get("syncMode", "add")
    if sync_mode not in ("overwrite", "add"):
        raise ValueError("同期方法が不正です。")
    event_id = payload.get("eventId")
    if event_id == "__all__":
        raise ValueError("同期先Eventを選択してください。")
    target_event_id = event_db_id(event_id)

    aggregated = {}
    duplicate_count = 0
    for row in imported["rows"]:
        draw_id = str(row.get(id_column, "")).strip()
        if not draw_id:
            continue
        join_count = safe_int(row.get(join_column), 0)
        win_count = safe_int(row.get(win_column), 0)
        if join_count < 0 or win_count < 0:
            raise ValueError(f"{draw_id}: 回数は0以上にしてください。")
        if win_count > join_count:
            raise ValueError(f"{draw_id}: 当選回数は参加回数以下にしてください。")
        current = aggregated.get(draw_id)
        if current:
            duplicate_count += 1
            current["join_count"] = max(current["join_count"], join_count)
            current["win_count"] = max(current["win_count"], win_count)
        else:
            aggregated[draw_id] = {"join_count": join_count, "win_count": win_count}
    if not aggregated:
        raise ValueError("同期できるユーザーがありません。")

    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("""
            INSERT INTO history_sync_batches
            (event_id, filename, sync_mode, row_count, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (target_event_id, imported["filename"], sync_mode, len(aggregated), now))
        batch_id = cur.lastrowid
        conn.execute("""
            INSERT INTO history_sync_snapshots
            (batch_id, participant_id, join_count, win_count, streak_count, updated_at)
            SELECT ?, participant_id, join_count, win_count, streak_count, updated_at
            FROM event_participant_history
            WHERE event_id=?
        """, (batch_id, target_event_id))
        conn.execute(
            "UPDATE history_sync_batches SET snapshot_complete=1 WHERE id=?",
            (batch_id,))
        imported_participants = {
            find_or_create_history_participant(conn, draw_id): counts
            for draw_id, counts in aggregated.items()
        }

        if sync_mode == "overwrite":
            existing = {
                row["participant_id"]: row
                for row in conn.execute("""
                    SELECT participant_id, join_count, win_count, streak_count
                    FROM event_participant_history WHERE event_id=?
                """, (target_event_id,)).fetchall()
            }
            for participant_id in set(existing) | set(imported_participants):
                before = existing.get(participant_id)
                counts = imported_participants.get(participant_id)
                before_exists = before is not None
                before_join = safe_int(before["join_count"], 0) if before else 0
                before_win = safe_int(before["win_count"], 0) if before else 0
                before_streak = safe_int(before["streak_count"], 0) if before else 0
                after_join = counts["join_count"] if counts else 0
                after_win = counts["win_count"] if counts else 0
                after_streak = max(after_join - after_win, 0) if counts else 0
                conn.execute("""
                    INSERT INTO history_sync_changes
                    (batch_id, participant_id, before_exists, before_join_count,
                     before_win_count, before_streak, after_join_count,
                     after_win_count, after_streak)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    batch_id, participant_id, int(before_exists), before_join,
                    before_win, before_streak, after_join, after_win, after_streak))
            conn.execute(
                "DELETE FROM event_participant_history WHERE event_id=?",
                (target_event_id,))
            for participant_id, counts in imported_participants.items():
                after_join = counts["join_count"]
                after_win = counts["win_count"]
                after_streak = max(after_join - after_win, 0)
                conn.execute("""
                    INSERT INTO event_participant_history
                    (event_id, participant_id, join_count, win_count, streak_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (target_event_id, participant_id, after_join, after_win, after_streak, now))
        else:
            for participant_id, counts in imported_participants.items():
                before = conn.execute("""
                    SELECT join_count, win_count, streak_count
                    FROM event_participant_history
                    WHERE event_id=? AND participant_id=?
                """, (target_event_id, participant_id)).fetchone()
                before_exists = before is not None
                before_join = safe_int(before["join_count"], 0) if before else 0
                before_win = safe_int(before["win_count"], 0) if before else 0
                before_streak = safe_int(before["streak_count"], 0) if before else 0
                after_join = before_join + counts["join_count"]
                after_win = before_win + counts["win_count"]
                after_streak = max(after_join - after_win, 0)
                conn.execute("""
                    INSERT INTO event_participant_history
                    (event_id, participant_id, join_count, win_count, streak_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id, participant_id) DO UPDATE SET
                        join_count=excluded.join_count,
                        win_count=excluded.win_count,
                        streak_count=excluded.streak_count,
                        updated_at=excluded.updated_at
                """, (target_event_id, participant_id, after_join, after_win, after_streak, now))
                conn.execute("""
                    INSERT INTO history_sync_changes
                    (batch_id, participant_id, before_exists, before_join_count,
                     before_win_count, before_streak, after_join_count,
                     after_win_count, after_streak)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    batch_id, participant_id, int(before_exists), before_join,
                    before_win, before_streak, after_join, after_win, after_streak))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    STATE["history_import"] = None
    STATE["user_event_id"] = "__default__" if target_event_id == 0 else target_event_id
    apply_column_roles()
    recalculate_probabilities()
    duplicate_note = f" / 重複ID {duplicate_count}件は最大値でまとめました。" if duplicate_count else ""
    mode_label_text = "追加" if sync_mode == "add" else "上書き"
    return public_state(f"{len(aggregated)}名の履歴を{mode_label_text}しました。Batch #{batch_id}{duplicate_note}")


def handle_history_rollback(payload):
    batch_id = safe_int(payload.get("batchId"), None)
    if not batch_id:
        raise ValueError("戻す同期Batchを選択してください。")
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        batch = conn.execute("""
            SELECT id, event_id, undone_at, snapshot_complete
            FROM history_sync_batches WHERE id=?
        """, (batch_id,)).fetchone()
        if not batch or batch["undone_at"]:
            raise ValueError("この同期Batchは戻せません。")
        latest = conn.execute("""
            SELECT id FROM history_sync_batches
            WHERE event_id=? AND undone_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (batch["event_id"],)).fetchone()
        if not latest or latest["id"] != batch_id:
            raise ValueError("このEventでは最新の同期Batchだけを戻せます。")
        if batch["snapshot_complete"]:
            conn.execute(
                "DELETE FROM event_participant_history WHERE event_id=?",
                (batch["event_id"],))
            conn.execute("""
                INSERT INTO event_participant_history
                (event_id, participant_id, join_count, win_count, streak_count, updated_at)
                SELECT ?, participant_id, join_count, win_count, streak_count, updated_at
                FROM history_sync_snapshots
                WHERE batch_id=?
            """, (batch["event_id"], batch_id))
        else:
            changes = conn.execute("""
                SELECT * FROM history_sync_changes WHERE batch_id=? ORDER BY id DESC
            """, (batch_id,)).fetchall()
            for change in changes:
                if change["before_exists"]:
                    conn.execute("""
                        INSERT INTO event_participant_history
                        (event_id, participant_id, join_count, win_count, streak_count, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(event_id, participant_id) DO UPDATE SET
                            join_count=excluded.join_count,
                            win_count=excluded.win_count,
                            streak_count=excluded.streak_count,
                            updated_at=excluded.updated_at
                    """, (
                        batch["event_id"], change["participant_id"],
                        change["before_join_count"], change["before_win_count"],
                        change["before_streak"], datetime.datetime.now().isoformat()))
                else:
                    conn.execute("""
                        DELETE FROM event_participant_history
                        WHERE event_id=? AND participant_id=?
                    """, (batch["event_id"], change["participant_id"]))
        conn.execute(
            "UPDATE history_sync_batches SET undone_at=? WHERE id=?",
            (datetime.datetime.now().isoformat(), batch_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    apply_column_roles()
    recalculate_probabilities()
    return public_state(f"同期Batch #{batch_id} を元に戻しました。")


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
    if path == "/api/history/upload":
        return handle_history_upload(headers, rfile)

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
        "/api/history/apply": lambda: handle_history_apply(payload),
        "/api/history/rollback": lambda: handle_history_rollback(payload),
        "/api/mode": lambda: handle_mode(payload),
        "/api/special": lambda: handle_special(payload),
        "/api/exclude": lambda: handle_exclude(payload),
    }
    if path not in routes:
        raise KeyError(path)
    return routes[path]()
