import datetime
import io
import json
import os
import random
import re
import sqlite3
import tempfile
from collections.abc import MutableMapping

import pandas as pd

from core import (
    FREE_BUILD,
    calc_weights,
    db_path,
    fuzzy_find_participant,
    load_csv_or_excel,
)
from .request_context import current_account_id, is_guest_mode


DEFAULT_STATE = {
    "records": [],
    "source_columns": [],
    "id_column": None,
    "display_columns": [],
    "special_rules": [],
    "excluded_indices": set(),
    "last_winner_indices": set(),
    "latest_session_id": None,
    "latest_session_number": None,
    "csv_file": "",
    "event_id": None,
    "user_event_id": "__default__",
    "history_import": None,
}


class AccountState(MutableMapping):
    """Expose the current request's in-memory import state as a mapping."""

    def __init__(self, defaults):
        self.defaults = defaults
        self.states = {}

    def _state(self):
        account_id = current_account_id()
        if account_id not in self.states:
            self.states[account_id] = {
                key: set(value) if isinstance(value, set)
                else list(value) if isinstance(value, list)
                else dict(value) if isinstance(value, dict)
                else value
                for key, value in self.defaults.items()
            }
            if is_guest_mode():
                self.states[account_id]["mode"] = "equal"
        return self.states[account_id]

    def __getitem__(self, key):
        return self._state()[key]

    def __setitem__(self, key, value):
        self._state()[key] = value

    def __delitem__(self, key):
        del self._state()[key]

    def __iter__(self):
        return iter(self._state())

    def __len__(self):
        return len(self._state())


STATE = AccountState(DEFAULT_STATE)


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
        WHERE owner_id=? AND lower(trim(display_name))=lower(trim(?))
        ORDER BY id
    """, (current_account_id(), key)).fetchall()
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
            conn, record["vrc_id"], record["vrc_url"], record["x_id"], record["x_url"],
            owner_id=current_account_id())
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
        "(vrc_id,vrc_url,x_id,x_url,display_name,join_count,win_count,last_win_join_count,streak_count,created_at,owner_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (*values, now, current_account_id()))
    record["participant_id"] = cur.lastrowid
    record["matched"] = False
    return cur.lastrowid


def event_db_id(event_id):
    return 0 if event_id in ("", None, "__default__") else safe_int(event_id, 0)


def next_session_number(conn, owner_id):
    used = {
        safe_int(row[0])
        for row in conn.execute("""
            SELECT session_number FROM raffle_sessions
            WHERE owner_id=? AND session_number IS NOT NULL
        """, (owner_id,)).fetchall()
    }
    number = 1
    while number in used:
        number += 1
    return number


def get_setting(conn, key, fallback=""):
    key = f"{current_account_id()}:{key}" if current_account_id() != "local" else key
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else fallback


def set_setting(conn, key, value):
    key = f"{current_account_id()}:{key}" if current_account_id() != "local" else key
    conn.execute("""
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))


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
    if is_guest_mode():
        return {
            "sessions": [], "events": [], "latestSessionId": None,
            "latestSessionNumber": None,
            "latestResults": [], "resultDisplayColumns": [], "savedUsers": [],
            "savedUserDisplayColumns": [], "userEventId": None,
            "historySyncBatches": [], "defaultEventName": "default",
            "defaultEventEnabled": False,
        }
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    owner_id = current_account_id()
    default_event_name = get_setting(conn, "default_event_name", "default")
    default_event_enabled = get_setting(conn, "default_event_enabled", "1") == "1"

    events = [dict(row) for row in conn.execute("""
        SELECT id, name, description, created_at
        FROM events
        WHERE owner_id=?
        ORDER BY id DESC
    """, (owner_id,)).fetchall()]
    if not default_event_enabled and events:
        if STATE.get("event_id") in (None, "", "__default__"):
            STATE["event_id"] = events[0]["id"]
        if STATE.get("user_event_id") in (None, "", "__default__"):
            STATE["user_event_id"] = events[0]["id"]
    user_event_id = STATE.get("user_event_id")

    sessions = [dict(row) for row in conn.execute("""
        SELECT s.id, COALESCE(s.session_number, s.id) AS number, s.event_id,
               CASE WHEN s.event_id IS NULL THEN ? ELSE COALESCE(e.name, ?) END AS event_name,
               s.session_name, s.csv_file, s.mode, s.draw_count, s.created_at, s.notes
        FROM raffle_sessions s
        LEFT JOIN events e ON e.id = s.event_id
        WHERE s.owner_id=?
        ORDER BY s.id DESC
        LIMIT 50
    """, (default_event_name, default_event_name, owner_id)).fetchall()]

    latest_session_id = sessions[0]["id"] if sessions else None
    latest_session_number = sessions[0]["number"] if sessions else None
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
            WHERE p.owner_id=?
            GROUP BY p.id, p.display_name, p.vrc_id, p.vrc_url, p.x_id, p.x_url
        """, (owner_id,)).fetchall()]
    else:
        baseline_rows = [dict(row) for row in conn.execute("""
            SELECT p.id, p.display_name, p.vrc_id, p.vrc_url, p.x_id, p.x_url,
                   h.join_count, h.win_count, h.streak_count
            FROM event_participant_history h
            JOIN participants p ON p.id = h.participant_id
            WHERE h.event_id=? AND p.owner_id=?
        """, (event_db_id(user_event_id), owner_id)).fetchall()]
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
        SELECT b.id, b.event_id,
               CASE WHEN b.event_id=0 THEN ? ELSE COALESCE(e.name, ?) END AS event_name,
               b.filename, b.sync_mode, b.row_count, b.created_at, b.undone_at
        FROM history_sync_batches b
        LEFT JOIN events e ON e.id = b.event_id
        WHERE b.owner_id=?
        ORDER BY b.id DESC
        LIMIT 20
    """, (default_event_name, default_event_name, owner_id)).fetchall()]
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
        "latestSessionNumber": latest_session_number,
        "latestResults": latest_payload["results"],
        "resultDisplayColumns": latest_payload["displayColumns"],
        "savedUsers": users,
        "savedUserDisplayColumns": [],
        "userEventId": user_event_id,
        "historySyncBatches": batches,
        "defaultEventName": default_event_name,
        "defaultEventEnabled": default_event_enabled,
    }


def build_event_export(event_id):
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    default_name = get_setting(conn, "default_event_name", "default")
    owner_id = current_account_id()
    if event_id in ("", None, "__all__"):
        event_name = "すべて"
        user_sql = """
            SELECT p.display_name, p.vrc_id, p.x_id,
                   SUM(h.join_count) AS join_count,
                   SUM(h.win_count) AS win_count,
                   SUM(h.streak_count) AS streak_count
            FROM event_participant_history h
            JOIN participants p ON p.id=h.participant_id
            WHERE p.owner_id=?
            GROUP BY p.id, p.display_name, p.vrc_id, p.x_id
            ORDER BY p.display_name
        """
        user_params = [owner_id]
        session_where = "WHERE s.owner_id=?"
        session_params = [owner_id]
    else:
        db_event_id = event_db_id(event_id)
        if db_event_id == 0:
            event_name = default_name
            session_where = "WHERE s.owner_id=? AND s.event_id IS NULL"
            session_params = [owner_id]
        else:
            row = conn.execute(
                "SELECT name FROM events WHERE id=? AND owner_id=?",
                (db_event_id, owner_id),
            ).fetchone()
            if not row:
                conn.close()
                raise ValueError("出力するEventが見つかりません。")
            event_name = row["name"]
            session_where = "WHERE s.owner_id=? AND s.event_id=?"
            session_params = [owner_id, db_event_id]
        user_sql = """
            SELECT p.display_name, p.vrc_id, p.x_id,
                   h.join_count, h.win_count, h.streak_count
            FROM event_participant_history h
            JOIN participants p ON p.id=h.participant_id
            WHERE h.event_id=? AND p.owner_id=?
            ORDER BY p.display_name
        """
        user_params = [db_event_id, owner_id]

    users = [dict(row) for row in conn.execute(user_sql, user_params).fetchall()]
    mode = "equal" if FREE_BUILD else STATE.get("mode", "linear")
    weights = [1.0] * len(users) if mode == "equal" else calc_weights(users, mode, max(len(users), 1))
    total_weight = sum(weights) or 1.0
    sheet1 = []
    for row, weight in zip(users, weights):
        sheet1.append({
            "抽選ID": row.get("display_name") or row.get("vrc_id") or row.get("x_id") or "unknown",
            "参加回数": safe_int(row.get("join_count"), 0),
            "当選回数": safe_int(row.get("win_count"), 0),
            "重み": weight,
            "現在確率": weight / total_weight,
        })

    result_rows = conn.execute(f"""
        SELECT COALESCE(s.session_number, s.id) AS session_number,
               s.created_at, s.mode, s.draw_count, s.csv_file,
               CASE WHEN s.event_id IS NULL THEN ? ELSE COALESCE(e.name, ?) END AS event_name,
               COALESCE(r.display_name, r.vrc_id, r.x_id, 'unknown') AS draw_id,
               r.extra_display_json
        FROM raffle_sessions s
        JOIN raffle_results r ON r.session_id=s.id
        LEFT JOIN events e ON e.id=s.event_id
        {session_where}
        ORDER BY s.id DESC, r.id
    """, [default_name, default_name, *session_params]).fetchall()
    sheet2 = []
    for row in result_rows:
        sheet2.append({
            "Session": row["session_number"],
            "日時": str(row["created_at"] or "").replace("T", " ")[:16],
            "Event": row["event_name"],
            "抽選ID": row["draw_id"],
            "確率モード": row["mode"],
            "抽選人数": row["draw_count"],
            "CSV": row["csv_file"],
            "表示内容": row["extra_display_json"] or "",
        })
    conn.close()

    sheet1_columns = ["抽選ID", "参加回数", "当選回数", "重み", "現在確率"]
    sheet2_columns = ["Session", "日時", "Event", "抽選ID", "確率モード", "抽選人数", "CSV", "表示内容"]
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(sheet1, columns=sheet1_columns).to_excel(writer, sheet_name="Sheet1", index=False)
        pd.DataFrame(sheet2, columns=sheet2_columns).to_excel(writer, sheet_name="Sheet2", index=False)
        sheet = writer.book["Sheet1"]
        for cell in sheet["E"][1:]:
            cell.number_format = "0.00%"
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
    safe_name = re.sub(r"[^0-9A-Za-zぁ-んァ-ヶ一-龠_-]+", "_", event_name).strip("_") or "event"
    return output.getvalue(), f"{safe_name}_抽選履歴.xlsx"


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
        "latestSessionNumber": STATE.get("latest_session_number"),
        "mode": STATE.get("mode", "linear"),
        "modeLabel": mode_label(STATE.get("mode", "linear")),
        "calculationSummary": calculation_summary(),
        "savedResults": snapshot["latestResults"],
        "savedUsers": snapshot["savedUsers"],
        "sessions": snapshot["sessions"],
        "events": snapshot["events"],
        "defaultEventName": snapshot["defaultEventName"],
        "defaultEventEnabled": snapshot["defaultEventEnabled"],
        "eventId": STATE.get("event_id"),
        "userEventId": snapshot["userEventId"],
        "savedLatestSessionId": snapshot["latestSessionId"],
        "savedLatestSessionNumber": snapshot["latestSessionNumber"],
        "resultDisplayColumns": snapshot["resultDisplayColumns"],
        "savedUserDisplayColumns": snapshot["savedUserDisplayColumns"],
        "historyImport": public_history_import(),
        "historySyncBatches": snapshot["historySyncBatches"],
        "allowShutdown": os.environ.get("ALLOW_SHUTDOWN", "1") == "1",
        "guestMode": is_guest_mode(),
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
    mode = "equal" if FREE_BUILD or is_guest_mode() else payload.get("mode", "linear")
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

    if is_guest_mode():
        STATE["last_winner_indices"] = set(winners_idx)
        STATE["latest_session_id"] = None
        STATE["latest_session_number"] = None
        recalculate_probabilities()
        return public_state(f"ゲスト抽選完了: {total}人中 {len(winners_idx)}名当選（保存なし）")

    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(db_path())
    owner_id = current_account_id()
    if event_id and not conn.execute(
        "SELECT 1 FROM events WHERE id=? AND owner_id=?", (event_id, owner_id)
    ).fetchone():
        conn.close()
        raise ValueError("指定されたEventが見つかりません。")
    session_number = next_session_number(conn, owner_id)
    cur = conn.execute(
        "INSERT INTO raffle_sessions "
        "(event_id,session_name,csv_file,mode,draw_count,created_at,notes,owner_id,session_number)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, session_name, STATE["csv_file"], mode, draw_count, now, saved_notes,
         owner_id, session_number))
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
    STATE["latest_session_number"] = session_number
    recalculate_probabilities()
    return public_state(
        f"抽選完了: Session #{session_number} | {total}人中 {len(winners_idx)}名当選"
    )


def read_uploaded_dataframe(filename, content):
    filename = os.path.basename(str(filename or ""))
    if not filename or not content:
        raise ValueError("ファイルを選択してください。")
    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        df = load_csv_or_excel(tmp_path)
    finally:
        os.unlink(tmp_path)
    return os.path.basename(filename), df


def handle_upload(filename, content):
    filename, df = read_uploaded_dataframe(filename, content)

    STATE["records"] = build_records(df)
    STATE["source_columns"] = list(df.columns)
    STATE["id_column"] = None
    STATE["display_columns"] = []
    STATE["special_rules"] = []
    STATE["excluded_indices"] = set()
    STATE["last_winner_indices"] = set()
    STATE["latest_session_id"] = None
    STATE["latest_session_number"] = None
    STATE["csv_file"] = filename
    STATE["mode"] = "equal" if is_guest_mode() else "linear"
    apply_column_roles()
    recalculate_probabilities()
    return public_state(
        f"{len(STATE['records'])}件を読み込みました。左クリックで抽選ID列を指定し、その後に表示列を指定してください。")


def handle_history_upload(filename, content):
    filename, df = read_uploaded_dataframe(filename, content)
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
        WHERE owner_id=? AND lower(trim(display_name))=lower(trim(?))
        ORDER BY id LIMIT 1
    """, (current_account_id(), draw_id)).fetchone()
    if row:
        return row[0]
    now = datetime.datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO participants
        (display_name, vrc_id, vrc_url, x_id, x_url, join_count, win_count,
         last_win_join_count, streak_count, created_at, owner_id)
        VALUES (?, '', '', '', '', 0, 0, 0, 0, ?, ?)
    """, (draw_id, now, current_account_id()))
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
    if target_event_id:
        check = sqlite3.connect(db_path())
        owned_event = check.execute(
            "SELECT 1 FROM events WHERE id=? AND owner_id=?",
            (target_event_id, current_account_id()),
        ).fetchone()
        check.close()
        if not owned_event:
            raise ValueError("同期先Eventが見つかりません。")

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
            (event_id, filename, sync_mode, row_count, created_at, owner_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (target_event_id, imported["filename"], sync_mode, len(aggregated), now,
              current_account_id()))
        batch_id = cur.lastrowid
        conn.execute("""
            INSERT INTO history_sync_snapshots
            (batch_id, participant_id, join_count, win_count, streak_count, updated_at)
            SELECT ?, participant_id, join_count, win_count, streak_count, updated_at
            FROM event_participant_history
            WHERE event_id=? AND participant_id IN (
                SELECT id FROM participants WHERE owner_id=?
            )
        """, (batch_id, target_event_id, current_account_id()))
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
                    FROM event_participant_history
                    WHERE event_id=? AND participant_id IN (
                        SELECT id FROM participants WHERE owner_id=?
                    )
                """, (target_event_id, current_account_id())).fetchall()
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
            conn.execute("""
                DELETE FROM event_participant_history
                WHERE event_id=? AND participant_id IN (
                    SELECT id FROM participants WHERE owner_id=?
                )
            """, (target_event_id, current_account_id()))
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
            FROM history_sync_batches WHERE id=? AND owner_id=?
        """, (batch_id, current_account_id())).fetchone()
        if not batch or batch["undone_at"]:
            raise ValueError("この同期Batchは戻せません。")
        latest = conn.execute("""
            SELECT id FROM history_sync_batches
            WHERE event_id=? AND owner_id=? AND undone_at IS NULL
            ORDER BY id DESC LIMIT 1
        """, (batch["event_id"], current_account_id())).fetchone()
        if not latest or latest["id"] != batch_id:
            raise ValueError("このEventでは最新の同期Batchだけを戻せます。")
        if batch["snapshot_complete"]:
            conn.execute("""
                DELETE FROM event_participant_history
                WHERE event_id=? AND participant_id IN (
                    SELECT id FROM participants WHERE owner_id=?
                )
            """, (batch["event_id"], current_account_id()))
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
        SELECT id, COALESCE(session_number, id) AS number,
               session_name, csv_file, mode, draw_count, created_at, notes
        FROM raffle_sessions
        WHERE id=? AND owner_id=?
    """, (session_id, current_account_id())).fetchone()
    if not session:
        conn.close()
        raise ValueError("指定されたセッションが見つかりません。")
    payload = db_session_results(conn, session_id)
    conn.close()
    payload.update({
        "ok": True,
        "session": dict(session),
        "sessionNumber": session["number"],
        "calculationSummary": (session["notes"] or "").split("[計算]", 1)[1].strip()
        if session["notes"] and "[計算]" in session["notes"] else "",
        "message": f"Session #{session['number']} の結果を表示しています。",
    })
    return payload


def handle_session_delete(payload):
    session_id = safe_int(payload.get("sessionId"), None)
    if not session_id:
        raise ValueError("セッションを選択してください。")
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    owner_id = current_account_id()
    try:
        session = conn.execute(
            "SELECT id, event_id, COALESCE(session_number, id) AS number "
            "FROM raffle_sessions WHERE id=? AND owner_id=?",
            (session_id, owner_id),
        ).fetchone()
        if not session:
            raise ValueError("指定されたセッションが見つかりません。")
        event_id = session["event_id"]
        session_number = session["number"]
        target_event_id = event_db_id(event_id)
        joins = {
            row["participant_id"]: row["count"]
            for row in conn.execute("""
                SELECT matched_participant_id AS participant_id, COUNT(*) AS count
                FROM submission_records
                WHERE session_id=? AND matched_participant_id IS NOT NULL
                GROUP BY matched_participant_id
            """, (session_id,)).fetchall()
        }
        wins = {
            row["participant_id"]: row["count"]
            for row in conn.execute("""
                SELECT participant_id, COUNT(*) AS count
                FROM raffle_results
                WHERE session_id=? AND participant_id IS NOT NULL AND is_winner=1
                GROUP BY participant_id
            """, (session_id,)).fetchall()
        }

        conn.execute("DELETE FROM raffle_results WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM submission_records WHERE session_id=?", (session_id,))
        conn.execute(
            "DELETE FROM raffle_sessions WHERE id=? AND owner_id=?", (session_id, owner_id)
        )

        for participant_id in set(joins) | set(wins):
            history = conn.execute("""
                SELECT join_count, win_count
                FROM event_participant_history
                WHERE event_id=? AND participant_id=?
            """, (target_event_id, participant_id)).fetchone()
            if not history:
                continue
            join_count = max(safe_int(history["join_count"]) - joins.get(participant_id, 0), 0)
            win_count = max(safe_int(history["win_count"]) - wins.get(participant_id, 0), 0)
            last_win = conn.execute("""
                SELECT MAX(s.id) AS session_id
                FROM raffle_results r
                JOIN raffle_sessions s ON s.id=r.session_id
                WHERE r.participant_id=? AND r.is_winner=1 AND s.owner_id=?
                  AND ((s.event_id IS NULL AND ? IS NULL) OR s.event_id=?)
            """, (participant_id, owner_id, event_id, event_id)).fetchone()["session_id"]
            if last_win:
                streak_count = conn.execute("""
                    SELECT COUNT(*)
                    FROM submission_records sr
                    JOIN raffle_sessions s ON s.id=sr.session_id
                    WHERE sr.matched_participant_id=? AND s.owner_id=? AND s.id>?
                      AND ((s.event_id IS NULL AND ? IS NULL) OR s.event_id=?)
                """, (participant_id, owner_id, last_win, event_id, event_id)).fetchone()[0]
            else:
                streak_count = max(join_count - win_count, 0)
            if join_count == 0 and win_count == 0:
                conn.execute("""
                    DELETE FROM event_participant_history
                    WHERE event_id=? AND participant_id=?
                """, (target_event_id, participant_id))
            else:
                conn.execute("""
                    UPDATE event_participant_history
                    SET join_count=?, win_count=?, streak_count=?, updated_at=?
                    WHERE event_id=? AND participant_id=?
                """, (
                    join_count, win_count, streak_count,
                    datetime.datetime.now().isoformat(), target_event_id, participant_id,
                ))
            totals = conn.execute("""
                SELECT COALESCE(SUM(h.join_count), 0), COALESCE(SUM(h.win_count), 0),
                       COALESCE(SUM(h.streak_count), 0)
                FROM event_participant_history h
                WHERE h.participant_id=?
            """, (participant_id,)).fetchone()
            conn.execute("""
                UPDATE participants
                SET join_count=?, win_count=?, streak_count=?
                WHERE id=? AND owner_id=?
            """, (totals[0], totals[1], totals[2], participant_id, owner_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if STATE.get("latest_session_id") == session_id:
        STATE["latest_session_id"] = None
        STATE["latest_session_number"] = None
        STATE["last_winner_indices"] = set()
    apply_column_roles()
    recalculate_probabilities()
    return public_state(f"Session #{session_number} を削除しました。")


def handle_event_select(payload):
    event_id = safe_int(payload.get("eventId"), None)
    if event_id:
        conn = sqlite3.connect(db_path())
        owned = conn.execute(
            "SELECT 1 FROM events WHERE id=? AND owner_id=?",
            (event_id, current_account_id()),
        ).fetchone()
        conn.close()
        if not owned:
            raise ValueError("指定されたEventが見つかりません。")
    STATE["event_id"] = event_id
    return public_state("Eventを選択しました。")


def handle_user_event(payload):
    event_id = payload.get("eventId")
    parsed = event_id if event_id in ("__all__", "__default__") else safe_int(event_id, None)
    if isinstance(parsed, int):
        conn = sqlite3.connect(db_path())
        owned = conn.execute(
            "SELECT 1 FROM events WHERE id=? AND owner_id=?",
            (parsed, current_account_id()),
        ).fetchone()
        conn.close()
        if not owned:
            raise ValueError("指定されたEventが見つかりません。")
    STATE["user_event_id"] = parsed
    return public_state("ユーザー一覧のEventを変更しました。")


def handle_event_save(payload):
    raw_event_id = payload.get("eventId")
    event_id = safe_int(raw_event_id, None)
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not name:
        raise ValueError("Event名を入力してください。")
    conn = sqlite3.connect(db_path())
    if raw_event_id == "__default__":
        set_setting(conn, "default_event_name", name)
        set_setting(conn, "default_event_enabled", "1")
        conn.commit()
        conn.close()
        return public_state(f"Event「{name}」へ名前を変更しました。")
    if event_id:
        cur = conn.execute(
            "UPDATE events SET name=?, description=? WHERE id=? AND owner_id=?",
            (name, description, event_id, current_account_id()))
        if cur.rowcount == 0:
            conn.close()
            raise ValueError("指定されたEventが見つかりません。")
    else:
        row = conn.execute(
            "SELECT id FROM events WHERE owner_id=? AND lower(trim(name))=lower(trim(?))",
            (current_account_id(), name)
        ).fetchone()
        if row:
            event_id = row[0]
            conn.execute("UPDATE events SET description=? WHERE id=?", (description, event_id))
        else:
            cur = conn.execute(
                "INSERT INTO events (name, description, created_at, owner_id) VALUES (?,?,?,?)",
                (name, description, datetime.datetime.now().isoformat(), current_account_id()))
            event_id = cur.lastrowid
    conn.commit()
    conn.close()
    STATE["event_id"] = event_id
    return public_state(f"Event「{name}」を保存しました。")


def handle_event_delete(payload):
    raw_event_id = payload.get("eventId")
    event_id = safe_int(raw_event_id, None)
    if raw_event_id == "__default__":
        target_event_id = safe_int(payload.get("targetEventId"), None)
        conn = sqlite3.connect(db_path())
        conn.row_factory = sqlite3.Row
        targets = conn.execute(
            "SELECT id, name FROM events WHERE owner_id=? ORDER BY id DESC",
            (current_account_id(),),
        ).fetchall()
        target = next(
            (row for row in targets if row["id"] == target_event_id),
            targets[0] if targets else None)
        if not target:
            conn.close()
            raise ValueError("他のEventがないため、defaultは削除できません。")
        now = datetime.datetime.now().isoformat()
        conn.execute("""
            INSERT INTO event_participant_history
            (event_id, participant_id, join_count, win_count, streak_count, updated_at)
            SELECT ?, participant_id, join_count, win_count, streak_count, ?
            FROM event_participant_history
            WHERE event_id=0 AND participant_id IN (
                SELECT id FROM participants WHERE owner_id=?
            )
            ON CONFLICT(event_id, participant_id) DO UPDATE SET
                join_count=event_participant_history.join_count+excluded.join_count,
                win_count=event_participant_history.win_count+excluded.win_count,
                streak_count=event_participant_history.streak_count+excluded.streak_count,
                updated_at=excluded.updated_at
        """, (target["id"], now, current_account_id()))
        conn.execute("""
            DELETE FROM event_participant_history
            WHERE event_id=0 AND participant_id IN (
                SELECT id FROM participants WHERE owner_id=?
            )
        """, (current_account_id(),))
        conn.execute(
            "UPDATE raffle_sessions SET event_id=? WHERE event_id IS NULL AND owner_id=?",
            (target["id"], current_account_id()))
        conn.execute("""
            UPDATE history_sync_batches
            SET undone_at=COALESCE(undone_at, ?)
            WHERE event_id=0 AND owner_id=?
        """, (now, current_account_id()))
        set_setting(conn, "default_event_enabled", "0")
        conn.commit()
        conn.close()
        STATE["event_id"] = target["id"]
        if STATE.get("user_event_id") in (None, "", "__default__"):
            STATE["user_event_id"] = target["id"]
        return public_state(
            f"defaultを削除し、保存データをEvent「{target['name']}」へ移動しました。")
    if not event_id:
        raise ValueError("Eventを選択してください。")
    conn = sqlite3.connect(db_path())
    if not conn.execute(
        "SELECT 1 FROM events WHERE id=? AND owner_id=?",
        (event_id, current_account_id()),
    ).fetchone():
        conn.close()
        raise ValueError("指定されたEventが見つかりません。")
    default_enabled = get_setting(conn, "default_event_enabled", "1") == "1"
    if default_enabled:
        fallback_event_id = 0
        conn.execute(
            "UPDATE raffle_sessions SET event_id=NULL WHERE event_id=? AND owner_id=?",
            (event_id, current_account_id()))
    else:
        fallback = conn.execute(
            "SELECT id FROM events WHERE id<>? AND owner_id=? ORDER BY id DESC LIMIT 1",
            (event_id, current_account_id())).fetchone()
        if not fallback:
            conn.close()
            raise ValueError("移動先がないため、このEventは削除できません。")
        conn.execute(
            "UPDATE raffle_sessions SET event_id=? WHERE event_id=? AND owner_id=?",
            (fallback[0], event_id, current_account_id()))
        fallback_event_id = fallback[0]
    now = datetime.datetime.now().isoformat()
    conn.execute("""
        INSERT INTO event_participant_history
        (event_id, participant_id, join_count, win_count, streak_count, updated_at)
        SELECT ?, participant_id, join_count, win_count, streak_count, ?
        FROM event_participant_history WHERE event_id=?
        ON CONFLICT(event_id, participant_id) DO UPDATE SET
            join_count=event_participant_history.join_count+excluded.join_count,
            win_count=event_participant_history.win_count+excluded.win_count,
            streak_count=event_participant_history.streak_count+excluded.streak_count,
            updated_at=excluded.updated_at
    """, (fallback_event_id, now, event_id))
    conn.execute("DELETE FROM event_participant_history WHERE event_id=?", (event_id,))
    conn.execute("""
        UPDATE history_sync_batches
        SET undone_at=COALESCE(undone_at, ?)
        WHERE event_id=? AND owner_id=?
    """, (now, event_id, current_account_id()))
    cur = conn.execute(
        "DELETE FROM events WHERE id=? AND owner_id=?", (event_id, current_account_id())
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise ValueError("指定されたEventが見つかりません。")
    if STATE.get("event_id") == event_id:
        STATE["event_id"] = None
    if STATE.get("user_event_id") == event_id:
        STATE["user_event_id"] = "__all__"
    return public_state("Eventを削除し、関連Sessionを利用可能なEventへ移動しました。")


def handle_mode(payload):
    mode = "equal" if is_guest_mode() else payload.get("mode", "linear")
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
