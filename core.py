import os
import sqlite3
import sys

import pandas as pd


FREE_BUILD = False
def db_path() -> str:
    configured = os.environ.get("DB_PATH")
    if configured:
        return os.path.abspath(configured)
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "vrc_raffle.db")


def init_db():
    conn = sqlite3.connect(db_path())
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS auth_users (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            x_user_id         TEXT NOT NULL UNIQUE,
            username          TEXT NOT NULL,
            display_name      TEXT,
            profile_image_url TEXT,
            first_login_at    TEXT NOT NULL,
            last_login_at     TEXT NOT NULL
        )
    """)
    c.execute("""
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES ('default_event_name', 'default')
    """)
    c.execute("""
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES ('default_event_enabled', '1')
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            description  TEXT,
            created_at   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            vrc_id       TEXT,
            vrc_url      TEXT,
            x_id         TEXT,
            x_url        TEXT,
            display_name TEXT,
            win_count    INTEGER DEFAULT 0,
            join_count   INTEGER DEFAULT 0,
            created_at   TEXT
        )
    """)
    participant_cols = [r[1] for r in c.execute("PRAGMA table_info(participants)").fetchall()]
    if "last_win_join_count" not in participant_cols:
        c.execute("ALTER TABLE participants ADD COLUMN last_win_join_count INTEGER DEFAULT 0")
    if "streak_count" not in participant_cols:
        c.execute("ALTER TABLE participants ADD COLUMN streak_count INTEGER DEFAULT 0")
        c.execute("""
            UPDATE participants
            SET streak_count = max(join_count - COALESCE(last_win_join_count, 0), 0)
        """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS raffle_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     INTEGER,
            session_name TEXT,
            csv_file     TEXT,
            mode         TEXT,
            draw_count   INTEGER,
            created_at   TEXT,
            notes        TEXT,
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS raffle_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       INTEGER,
            participant_id   INTEGER,
            display_name     TEXT,
            vrc_id           TEXT,
            vrc_url          TEXT,
            x_id             TEXT,
            x_url            TEXT,
            is_winner        INTEGER DEFAULT 1,
            FOREIGN KEY (session_id) REFERENCES raffle_sessions(id)
        )
    """)
    result_cols = [r[1] for r in c.execute("PRAGMA table_info(raffle_results)").fetchall()]
    if "extra_display_json" not in result_cols:
        c.execute("ALTER TABLE raffle_results ADD COLUMN extra_display_json TEXT")
    c.execute("""
        CREATE TABLE IF NOT EXISTS submission_records (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id             INTEGER,
            raw_data               TEXT,
            matched_participant_id INTEGER,
            created_at             TEXT,
            FOREIGN KEY (session_id) REFERENCES raffle_sessions(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS event_participant_history (
            event_id       INTEGER NOT NULL DEFAULT 0,
            participant_id INTEGER NOT NULL,
            join_count     INTEGER NOT NULL DEFAULT 0,
            win_count      INTEGER NOT NULL DEFAULT 0,
            streak_count   INTEGER NOT NULL DEFAULT 0,
            updated_at     TEXT,
            PRIMARY KEY (event_id, participant_id),
            FOREIGN KEY (participant_id) REFERENCES participants(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history_sync_batches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   INTEGER NOT NULL DEFAULT 0,
            filename   TEXT,
            sync_mode  TEXT,
            row_count  INTEGER DEFAULT 0,
            created_at TEXT,
            undone_at  TEXT
        )
    """)
    batch_cols = [r[1] for r in c.execute("PRAGMA table_info(history_sync_batches)").fetchall()]
    if "snapshot_complete" not in batch_cols:
        c.execute(
            "ALTER TABLE history_sync_batches "
            "ADD COLUMN snapshot_complete INTEGER NOT NULL DEFAULT 0")
    c.execute("""
        CREATE TABLE IF NOT EXISTS history_sync_snapshots (
            batch_id       INTEGER NOT NULL,
            participant_id INTEGER NOT NULL,
            join_count     INTEGER NOT NULL DEFAULT 0,
            win_count      INTEGER NOT NULL DEFAULT 0,
            streak_count   INTEGER NOT NULL DEFAULT 0,
            updated_at     TEXT,
            PRIMARY KEY (batch_id, participant_id),
            FOREIGN KEY (batch_id) REFERENCES history_sync_batches(id),
            FOREIGN KEY (participant_id) REFERENCES participants(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history_sync_changes (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id          INTEGER NOT NULL,
            participant_id    INTEGER NOT NULL,
            before_exists     INTEGER NOT NULL DEFAULT 0,
            before_join_count INTEGER NOT NULL DEFAULT 0,
            before_win_count  INTEGER NOT NULL DEFAULT 0,
            before_streak     INTEGER NOT NULL DEFAULT 0,
            after_join_count  INTEGER NOT NULL DEFAULT 0,
            after_win_count   INTEGER NOT NULL DEFAULT 0,
            after_streak      INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (batch_id) REFERENCES history_sync_batches(id),
            FOREIGN KEY (participant_id) REFERENCES participants(id)
        )
    """)
    for table in ("events", "participants", "raffle_sessions", "history_sync_batches"):
        columns = [row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if "owner_id" not in columns:
            c.execute(
                f"ALTER TABLE {table} ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'"
            )
    session_columns = [
        row[1] for row in c.execute("PRAGMA table_info(raffle_sessions)").fetchall()
    ]
    if "session_number" not in session_columns:
        c.execute("ALTER TABLE raffle_sessions ADD COLUMN session_number INTEGER")
    owners = [
        row[0] for row in c.execute(
            "SELECT DISTINCT owner_id FROM raffle_sessions"
        ).fetchall()
    ]
    for owner_id in owners:
        used = {
            row[0] for row in c.execute("""
                SELECT session_number FROM raffle_sessions
                WHERE owner_id=? AND session_number IS NOT NULL
            """, (owner_id,)).fetchall()
        }
        next_number = 1
        rows = c.execute("""
            SELECT id FROM raffle_sessions
            WHERE owner_id=? AND session_number IS NULL
            ORDER BY id
        """, (owner_id,)).fetchall()
        for row in rows:
            while next_number in used:
                next_number += 1
            c.execute(
                "UPDATE raffle_sessions SET session_number=? WHERE id=?",
                (next_number, row[0]),
            )
            used.add(next_number)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_owner ON events(owner_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_participants_owner ON participants(owner_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON raffle_sessions(owner_id)")
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_owner_number
        ON raffle_sessions(owner_id, session_number)
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_sync_batches_owner ON history_sync_batches(owner_id)")
    conn.commit()
    conn.close()


def fuzzy_find_participant(
    conn, vrc_id="", vrc_url="", x_id="", x_url="", owner_id="local"
):
    c = conn.cursor()
    rows = c.execute(
        "SELECT * FROM participants WHERE owner_id=?", (owner_id,)
    ).fetchall()
    col_names = [d[0] for d in c.description]

    def normalize(value):
        return str(value or "").strip().rstrip("/").lower()

    for row in rows:
        r = dict(zip(col_names, row))
        values = [
            (vrc_id, r.get("vrc_id", "") or ""),
            (vrc_url, r.get("vrc_url", "") or ""),
            (x_id, r.get("x_id", "") or ""),
            (x_url, r.get("x_url", "") or ""),
        ]
        if any(normalize(a) and normalize(a) == normalize(b) for a, b in values):
            return r, 100
    return None, 0


def calc_weights(participants_info: list, mode: str, total: int) -> list:
    weights = []
    for p in participants_info:
        join_count = p.get("join_count", 0)
        win_count = p.get("win_count", 0)
        streak_count = p.get("streak_count")
        if streak_count is None:
            last_win_join_count = p.get("last_win_join_count")
            if last_win_join_count is None:
                last_win_join_count = max(join_count - win_count, 0)
            streak_count = max(join_count - last_win_join_count, 0)
        n = max(streak_count, 0)
        if mode == "linear":
            weight = float(n + 1)
        else:
            weight = float((n + 1) ** 2)
        weights.append(weight)
    return weights


COLUMN_ALIASES = {
    "vrc_id": ["vrc id", "vrchat id", "vrchat_id", "vrcid", "vrc名前", "vrc name"],
    "vrc_url": ["vrc url", "vrchat url", "vrchat_url", "vrcurl", "vrchatリンク"],
    "x_id": ["x id", "twitter id", "x name", "twitter name", "x_id", "twitterid"],
    "x_url": ["x url", "twitter url", "xリンク", "twitterリンク", "x_url"],
    "display_name": ["name", "名前", "display name", "display_name", "nickname", "ニックネーム"],
    "join_count": ["join count", "join_count", "joins", "参加回数", "参加次数", "応募回数"],
    "win_count": ["win count", "win_count", "wins", "当選回数", "当选次数", "中奖次数"],
    "current_probability": [
        "current probability", "current_probability", "probability", "prob", "now probability",
        "現在確率", "现在概率", "当前概率", "抽選確率", "抽签概率"
    ],
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for col in df.columns:
        col_lower = col.strip().lower()
        for internal, aliases in COLUMN_ALIASES.items():
            if col_lower in aliases or col_lower == internal:
                rename_map[col] = internal
                break
    return df.rename(columns=rename_map)


def load_csv_or_excel(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xls", ".xlsx"):
        df = pd.read_excel(path)
    else:
        for enc in ("utf-8-sig", "utf-8", "gbk", "shift_jis", "cp932"):
            try:
                df = pd.read_csv(path, encoding=enc)
                break
            except Exception:
                continue
        else:
            raise ValueError("ファイルのエンコードを認識できません。UTF-8で保存し直してください。")
    return normalize_columns(df)
