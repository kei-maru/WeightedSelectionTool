# vrc_raffle.py
# VRC イベント抽選ツール — 完全版（Free / Pro 共通ソース）
# 依存: pip install pandas openpyxl thefuzz python-Levenshtein

import sqlite3
import os
import sys
import random
import json
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional

import pandas as pd
from thefuzz import fuzz

# ─────────────────────────────────────────────────────────────────────────────
#  ビルド設定  ★ PyInstaller でビルドする際にここを変える ★
#  FREE_BUILD = True  → フリー版 exe
#  FREE_BUILD = False → Pro 版 exe
# ─────────────────────────────────────────────────────────────────────────────
try:
    from _build_config import FREE_BUILD  # ビルド時に注入
except ImportError:
    FREE_BUILD = False  # 開発時デフォルト = Pro

APP_VERSION = "1.0.0"
APP_TITLE_FREE = "VRC 抽選ツール  [フリー版]"
APP_TITLE_PRO  = "VRC 抽選ツール  [Pro版]"
APP_TITLE = APP_TITLE_FREE if FREE_BUILD else APP_TITLE_PRO


# ─────────────────────────────────────────────────────────────────────────────
#  パス
# ─────────────────────────────────────────────────────────────────────────────
def db_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "vrc_raffle.db")


# ─────────────────────────────────────────────────────────────────────────────
#  DB 初期化
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(db_path())
    c = conn.cursor()

    # イベントマスタ
    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            description  TEXT,
            created_at   TEXT
        )
    """)

    # 参加者
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

    # 抽選セッション（イベントに紐づく）
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

    # 抽選結果
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

    # 提出レコード
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
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
#  ファジーマッチング
# ─────────────────────────────────────────────────────────────────────────────
FUZZY_THRESHOLD = 80

def fuzzy_find_participant(conn, vrc_id="", vrc_url="", x_id="", x_url=""):
    c = conn.cursor()
    rows = c.execute("SELECT * FROM participants").fetchall()
    col_names = [d[0] for d in c.description]

    best_score, best_row = 0, None

    def sf(a, b):
        if not a or not b:
            return 0
        return fuzz.token_sort_ratio(a.strip().lower(), b.strip().lower())

    for row in rows:
        r = dict(zip(col_names, row))
        candidates = [
            (vrc_id,  r.get("vrc_id",  "") or ""),
            (vrc_url, r.get("vrc_url", "") or ""),
            (x_id,    r.get("x_id",    "") or ""),
            (x_url,   r.get("x_url",   "") or ""),
        ]
        valid = [sf(a, b) for a, b in candidates if a]
        if not valid:
            continue
        s = max(valid)
        if s > best_score:
            best_score, best_row = s, r

    return (best_row, best_score) if best_score >= FUZZY_THRESHOLD else (None, best_score)


# ─────────────────────────────────────────────────────────────────────────────
#  重み計算
# ─────────────────────────────────────────────────────────────────────────────
def calc_weights(participants_info: list, mode: str, total: int) -> list:
    weights = []
    for p in participants_info:
        jc = p.get("join_count", 0)
        wc = p.get("win_count", 0)
        if mode == "linear":
            w = (1.0 + (jc / max(total, 1))) / (1.0 + wc)
        else:
            w = 2.0 ** max(jc - wc, 0)
        weights.append(w)
    return weights


# ─────────────────────────────────────────────────────────────────────────────
#  CSV / Excel 読み込み
# ─────────────────────────────────────────────────────────────────────────────
COLUMN_ALIASES = {
    "vrc_id":       ["vrc id", "vrchat id", "vrchat_id", "vrcid", "vrc名前", "vrc name"],
    "vrc_url":      ["vrc url", "vrchat url", "vrchat_url", "vrcurl", "vrchatリンク"],
    "x_id":         ["x id", "twitter id", "x name", "twitter name", "x_id", "twitterid"],
    "x_url":        ["x url", "twitter url", "xリンク", "twitterリンク", "x_url"],
    "display_name": ["name", "名前", "display name", "display_name", "nickname", "ニックネーム"],
    "join_count":   ["join count", "join_count", "joins", "参加回数", "参加次数", "応募回数"],
    "win_count":    ["win count", "win_count", "wins", "当選回数", "当选次数", "中奖次数"],
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


# ─────────────────────────────────────────────────────────────────────────────
#  CSV 列選択テーブル
# ─────────────────────────────────────────────────────────────────────────────
class CsvColumnTable(ttk.Frame):
    def __init__(self, parent, on_left_click=None, on_right_click=None,
                 on_double_click=None):
        super().__init__(parent)
        self.on_left_click = on_left_click
        self.on_right_click = on_right_click
        self.on_double_click = on_double_click
        self.columns = []
        self.rows = []
        self.source_columns = set()
        self.id_column = None
        self.display_columns = []
        self.winner_indices = set()
        self.selected_row = None
        self.col_widths = []
        self.header_h = 34
        self.row_h = 28

        self.canvas = tk.Canvas(
            self, bg="#ffffff", highlightthickness=1,
            highlightbackground="#bfdbfe")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.canvas.bind("<Button-1>", self._handle_left_click)
        self.canvas.bind("<Button-3>", self._handle_right_click)
        self.canvas.bind("<Button-2>", self._handle_right_click)
        self.canvas.bind("<Double-1>", self._handle_double_click)

    def set_data(self, columns, rows, source_columns=None, id_column=None,
                 display_columns=None, winner_indices=None):
        self.columns = list(columns)
        self.rows = list(rows)
        self.source_columns = set(source_columns or self.columns)
        self.id_column = id_column
        self.display_columns = list(display_columns or [])
        self.winner_indices = set(winner_indices or set())
        if self.selected_row is not None and self.selected_row >= len(self.rows):
            self.selected_row = None
        self._draw()

    def selection(self):
        if self.selected_row is None:
            return ()
        return (str(self.selected_row),)

    def _column_width(self, col, values):
        texts = [str(col)] + [str(v) for v in values[:80]]
        width = max(110, min(240, max(len(t) for t in texts) * 9 + 28))
        return width

    def _draw(self):
        self.canvas.delete("all")
        self.col_widths = []
        for idx, col in enumerate(self.columns):
            values = [row[idx] if idx < len(row) else "" for row in self.rows]
            self.col_widths.append(self._column_width(col, values))

        total_w = sum(self.col_widths)
        total_h = self.header_h + len(self.rows) * self.row_h
        self.canvas.configure(scrollregion=(0, 0, total_w, total_h))

        x = 0
        for idx, col in enumerate(self.columns):
            w = self.col_widths[idx]
            fill = "#eaf2ff"
            title = col
            if col == self.id_column:
                fill = "#2563eb"
                title = f"{col}  [抽選ID]"
            elif col in self.display_columns:
                fill = "#22c55e"
                title = f"{col}  [展示列]"
            self.canvas.create_rectangle(
                x, 0, x + w, self.header_h, fill=fill, outline="#bfdbfe")
            self.canvas.create_text(
                x + 8, self.header_h / 2, anchor="w", text=title,
                fill="#ffffff" if fill in ("#2563eb", "#22c55e") else "#172554",
                font=("Yu Gothic UI", 9, "bold"))
            x += w

        for row_idx, row in enumerate(self.rows):
            y1 = self.header_h + row_idx * self.row_h
            y2 = y1 + self.row_h
            x = 0
            for col_idx, col in enumerate(self.columns):
                w = self.col_widths[col_idx]
                fill = "#ffffff"
                if row_idx == self.selected_row:
                    fill = "#eff6ff"
                if row_idx in self.winner_indices:
                    fill = "#dbeafe"
                if col == self.id_column:
                    fill = "#dbeafe"
                elif col in self.display_columns:
                    fill = "#dcfce7"
                value = row[col_idx] if col_idx < len(row) else ""
                self.canvas.create_rectangle(
                    x, y1, x + w, y2, fill=fill, outline="#e2e8f0")
                self.canvas.create_text(
                    x + 8, y1 + self.row_h / 2, anchor="w", text=str(value),
                    fill="#172554", font=("Yu Gothic UI", 9),
                    width=max(w - 16, 20))
                x += w

    def _hit_test(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        col_idx = None
        cursor = 0
        for idx, width in enumerate(self.col_widths):
            if cursor <= x < cursor + width:
                col_idx = idx
                break
            cursor += width
        row_idx = None
        if y >= self.header_h:
            row_idx = int((y - self.header_h) // self.row_h)
            if row_idx < 0 or row_idx >= len(self.rows):
                row_idx = None
        col = self.columns[col_idx] if col_idx is not None else None
        return row_idx, col

    def _select_row(self, row_idx):
        if row_idx is not None:
            self.selected_row = row_idx
            self._draw()

    def _handle_left_click(self, event):
        row_idx, col = self._hit_test(event)
        self._select_row(row_idx)
        if col and col in self.source_columns and self.on_left_click:
            self.on_left_click(col)

    def _handle_right_click(self, event):
        row_idx, col = self._hit_test(event)
        self._select_row(row_idx)
        if col and col in self.source_columns and self.on_right_click:
            self.on_right_click(col)

    def _handle_double_click(self, event):
        row_idx, _ = self._hit_test(event)
        self._select_row(row_idx)
        if self.on_double_click:
            self.on_double_click()


# ─────────────────────────────────────────────────────────────────────────────
#  Pro 機能ガード デコレータ
# ─────────────────────────────────────────────────────────────────────────────
def pro_only(func):
    def wrapper(*args, **kwargs):
        if FREE_BUILD:
            messagebox.showinfo(
                "Pro版限定機能",
                "この機能はPro版でのみご利用いただけます。\n\nPro版にアップグレードしてください。"
            )
            return
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
#  メインアプリ
# ─────────────────────────────────────────────────────────────────────────────
class VRCRaffleApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1150x750")
        self.minsize(960, 620)
        init_db()
        # 状態
        self._sort_col = "id"
        self._sort_rev = True
        self._players_sort_rev = True
        self.imported_records = []
        self.import_source_columns = []
        self.import_id_column = None
        self.import_display_columns = []
        self.last_winner_indices = set()
        self._apply_style()
        self._build_ui()


    # ══════════════════════════════════════════════════════════════════════════
    #   スタイル
    # ══════════════════════════════════════════════════════════════════════════
    def _apply_style(self):
        # 共通: シンプルな白 × ブルー基調
        BG    = "#f6f9fc"
        PANEL = "#ffffff"
        ACC   = "#2563eb"
        HIGH  = "#dbeafe"
        FG    = "#172554"
        MUTED = "#64748b"
        BORDER = "#bfdbfe"
        FIELD = "#ffffff"
        SELECT_BG = "#2563eb"
        SELECT_FG = "#ffffff"
        BADGE_BG = "#dbeafe"
        BADGE_FG = "#1d4ed8"

        self.BG = BG; self.PANEL = PANEL; self.ACC = ACC
        self.HIGH = HIGH; self.FG = FG
        self.MUTED = MUTED; self.BORDER = BORDER; self.FIELD = FIELD
        self.SELECT_BG = SELECT_BG; self.SELECT_FG = SELECT_FG
        self.configure(bg=BG)

        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".",              background=BG,    foreground=FG,    font=("Yu Gothic UI", 10))
        s.configure("TNotebook",      background=BG,    borderwidth=0)
        s.configure("TNotebook.Tab",  background="#eaf2ff", foreground=FG, padding=[14, 6])
        s.map("TNotebook.Tab",        background=[("selected", PANEL), ("active", HIGH)],
                                      foreground=[("selected", ACC), ("active", ACC)])
        s.configure("TFrame",         background=BG)
        s.configure("Panel.TFrame",   background=PANEL)
        s.configure("TLabel",         background=BG,    foreground=FG)
        s.configure("Panel.TLabel",   background=PANEL, foreground=FG)
        s.configure("Muted.TLabel",   background=BG,    foreground=MUTED)
        s.configure("TButton",        background="#eaf2ff", foreground=ACC, padding=[8, 4],
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        s.map("TButton",              background=[("active", HIGH)],
                                      foreground=[("active", ACC)])
        s.configure("Accent.TButton", background=ACC,  foreground=SELECT_FG, padding=[10, 5],
                    font=("Yu Gothic UI", 10, "bold"))
        s.map("Accent.TButton",       background=[("active", "#1d4ed8")])
        s.configure("TEntry",         fieldbackground=FIELD,
                    foreground=FG, insertcolor=FG, bordercolor=BORDER)
        s.configure("TCombobox",      fieldbackground=FIELD,
                    foreground=FG, selectbackground=SELECT_BG, selectforeground=SELECT_FG,
                    bordercolor=BORDER, arrowcolor=ACC)
        s.configure("Treeview",       background=FIELD,
                    foreground=FG,
                    fieldbackground=FIELD,
                    rowheight=26,
                    bordercolor=BORDER)
        s.configure("Treeview.Heading", background="#eaf2ff", foreground=FG,
                    font=("Yu Gothic UI", 9, "bold"))
        s.map("Treeview",             background=[("selected", SELECT_BG)],
                                      foreground=[("selected", SELECT_FG)])
        s.configure("TSpinbox",       fieldbackground=FIELD,
                    foreground=FG, bordercolor=BORDER, arrowcolor=ACC)
        s.configure("TRadiobutton",   background=PANEL, foreground=FG)
        s.configure("TCheckbutton",   background=PANEL, foreground=FG)
        s.map("TRadiobutton",         background=[("active", PANEL)])
        s.map("TCheckbutton",         background=[("active", PANEL)])
        s.configure("TSeparator",     background=BORDER)
        s.configure("TScrollbar",     background="#cfe3ff", troughcolor=BG, bordercolor=BG,
                    arrowcolor=ACC)

        # バッジ用
        self.BADGE_BG = BADGE_BG
        self.BADGE_FG = BADGE_FG


    # ══════════════════════════════════════════════════════════════════════════
    #   UI 組み立て
    # ══════════════════════════════════════════════════════════════════════════
    def _build_ui(self):
        # ヘッダー
        hdr = tk.Frame(self, bg=self.PANEL, pady=8, highlightthickness=1,
                       highlightbackground=self.BORDER)
        hdr.pack(fill="x")
        tk.Label(hdr, text="VRC イベント抽選ツール",
                 font=("Yu Gothic UI", 15, "bold"),
                 bg=self.PANEL, fg=self.FG).pack(side="left", padx=14)
        badge_text = "FREE" if FREE_BUILD else "PRO"
        tk.Label(hdr, text=f" {badge_text} ", font=("Yu Gothic UI", 10, "bold"),
                 bg=self.BADGE_BG, fg=self.BADGE_FG,
                 relief="flat", padx=6, pady=2).pack(side="left", padx=4)
        tk.Label(hdr, text=f"v{APP_VERSION}",
                 font=("Yu Gothic UI", 9), bg=self.PANEL, fg=self.MUTED).pack(side="right", padx=14)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(6, 8))

        self.tab_raffle   = ttk.Frame(nb)
        self.tab_events   = ttk.Frame(nb)
        self.tab_sessions = ttk.Frame(nb)
        self.tab_players  = ttk.Frame(nb)
        self.tab_help     = ttk.Frame(nb)

        nb.add(self.tab_raffle,   text="  抽選  ")

        if FREE_BUILD:
            nb.add(self.tab_events,   text="  イベント  LOCK  ")
            nb.add(self.tab_sessions, text="  記録  LOCK  ")
            nb.add(self.tab_players,  text="  参加者  LOCK  ")
        else:
            nb.add(self.tab_events,   text="  イベント  ")
            nb.add(self.tab_sessions, text="  記録  ")
            nb.add(self.tab_players,  text="  参加者  ")

        nb.add(self.tab_help,     text="  ヘルプ  ")

        self._build_raffle_tab()
        self._build_events_tab()
        self._build_sessions_tab()
        self._build_players_tab()
        self._build_help_tab()

        # フリー版: Pro タブをクリックしたときに警告
        if FREE_BUILD:
            def on_tab_change(event):
                idx = nb.index(nb.select())
                if idx in (1, 2, 3):
                    messagebox.showinfo(
                        "Pro版限定",
                        "この機能はPro版でのみご利用いただけます。"
                    )
                    nb.select(0)
            nb.bind("<<NotebookTabChanged>>", on_tab_change)


    # ══════════════════════════════════════════════════════════════════════════
    #   TAB 1 — 抽選
    # ══════════════════════════════════════════════════════════════════════════
    def _build_raffle_tab(self):
        f = self.tab_raffle
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        # ── 左パネル ──────────────────────────────────────────────────────────
        left = ttk.Frame(f, style="Panel.TFrame", padding=16)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        ttk.Label(left, text="抽選設定",
                  font=("Yu Gothic UI", 11, "bold"),
                  style="Panel.TLabel").pack(anchor="w", pady=(0, 12))

        # イベント選択（Pro のみ意味を持つ）
        ttk.Label(left, text="イベント:", style="Panel.TLabel").pack(anchor="w")
        self.raffle_event_var = tk.StringVar()
        self.raffle_event_combo = ttk.Combobox(
            left, textvariable=self.raffle_event_var, state="readonly", width=28)
        self.raffle_event_combo.pack(fill="x", pady=(2, 2))

        if FREE_BUILD:
            lk = ttk.Label(left,
                           text="イベント管理はPro版限定です",
                           style="Panel.TLabel",
                           foreground=self.HIGH,
                           font=("Yu Gothic UI", 8))
            lk.pack(anchor="w", pady=(0, 8))
        else:
            ttk.Button(left, text="＋ 新規イベント作成",
                       command=self._quick_create_event).pack(fill="x", pady=(2, 10))

        # CSV ファイル
        ttk.Label(left, text="Google Form 出力ファイル (CSV/Excel):",
                  style="Panel.TLabel").pack(anchor="w")
        ff = ttk.Frame(left, style="Panel.TFrame")
        ff.pack(fill="x", pady=(2, 8))
        self.csv_path_var = tk.StringVar()
        ttk.Entry(ff, textvariable=self.csv_path_var, width=24).pack(
            side="left", fill="x", expand=True)
        ttk.Button(ff, text="参照", command=self._browse_csv).pack(side="left", padx=(4, 0))

        # セッション名
        ttk.Label(left, text="セッション名:", style="Panel.TLabel").pack(anchor="w")
        self.session_name_var = tk.StringVar(
            value=f"抽選_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}")
        ttk.Entry(left, textvariable=self.session_name_var).pack(fill="x", pady=(2, 8))

        # 抽選人数
        ttk.Label(left, text="抽選人数:", style="Panel.TLabel").pack(anchor="w")
        self.draw_count_var = tk.IntVar(value=1)
        ttk.Spinbox(left, from_=1, to=9999,
                    textvariable=self.draw_count_var, width=8).pack(anchor="w", pady=(2, 8))

        # 確率モード
        mode_frame = ttk.Frame(left, style="Panel.TFrame", padding=8)
        mode_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(mode_frame, text="加重確率モード:",
                  style="Panel.TLabel",
                  font=("Yu Gothic UI", 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.mode_var = tk.StringVar(value="linear")
        ttk.Radiobutton(mode_frame,
                        text="線形加重  [(1 + 参加回数 / 総人数) / (1 + 当選回数)]",
                        variable=self.mode_var, value="linear",
                        style="TRadiobutton",
                        command=self._on_raffle_mode_change).pack(anchor="w")
        ttk.Radiobutton(mode_frame,
                        text="指数加重  [2 ^ max(参加回数 - 当選回数, 0)]",
                        variable=self.mode_var, value="double",
                        style="TRadiobutton",
                        command=self._on_raffle_mode_change).pack(anchor="w", pady=(4, 0))

        if FREE_BUILD:
            ttk.Label(mode_frame,
                      text="加重確率はPro版限定です（フリー版は均等抽選）",
                      style="Panel.TLabel",
                      foreground=self.HIGH,
                      font=("Yu Gothic UI", 8)).pack(anchor="w", pady=(4, 0))

        # 重複当選
        self.allow_repeat_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="重複当選を許可（同一人物が複数回当選可）",
                        variable=self.allow_repeat_var).pack(anchor="w", pady=(0, 8))

        # 備考
        ttk.Label(left, text="備考:", style="Panel.TLabel").pack(anchor="w")
        self.notes_text = tk.Text(
            left, height=3,
            bg=self.FIELD,
            fg=self.FG, insertbackground=self.FG,
            font=("Yu Gothic UI", 9))
        self.notes_text.pack(fill="x", pady=(2, 10))

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(left, text="CSVプレビュー",
                   command=self._preview_csv).pack(fill="x", pady=2)
        ttk.Button(left, text="CSVを読込・自動認識",
                   command=self._load_csv_records).pack(fill="x", pady=2)
        ttk.Button(left, text="抽選開始",
                   style="Accent.TButton",
                   command=self._run_raffle).pack(fill="x", pady=(6, 2))

        # ── 右パネル ──────────────────────────────────────────────────────────
        right = ttk.Frame(f, padding=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        top_right = ttk.Frame(right)
        top_right.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(top_right, text="抽選記録",
                  font=("Yu Gothic UI", 11, "bold")).pack(side="left")
        ttk.Button(top_right, text="記録編集",
                   command=self._edit_import_record).pack(side="right", padx=4)
        ttk.Button(top_right, text="記録削除",
                   command=self._delete_import_record).pack(side="right", padx=4)

        self.result_table = CsvColumnTable(
            right,
            on_left_click=self._on_import_column_left_click,
            on_right_click=self._on_import_column_right_click,
            on_double_click=self._edit_import_record)
        self.result_table.grid(row=1, column=0, columnspan=2, sticky="nsew")

        self.result_status = ttk.Label(
            right, text="CSVを選択して「CSVを読込・自動認識」を押してください。",
            font=("Yu Gothic UI", 10, "bold"),
            foreground=self.ACC)
        self.result_status.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # 初期イベントリスト読み込み
        self._refresh_event_combo()


    def _refresh_event_combo(self):
        conn = sqlite3.connect(db_path())
        rows = conn.execute(
            "SELECT id, name FROM events ORDER BY id DESC").fetchall()
        conn.close()
        self._event_map = {f"[{r[0]}] {r[1]}": r[0] for r in rows}
        vals = list(self._event_map.keys())
        self.raffle_event_combo["values"] = vals
        if vals:
            self.raffle_event_combo.current(0)

    def _quick_create_event(self):
        win = tk.Toplevel(self)
        win.title("新規イベント作成")
        win.configure(bg=self.BG)
        win.geometry("360x180")
        win.grab_set()

        ttk.Label(win, text="イベント名:").grid(row=0, column=0, sticky="e", padx=10, pady=8)
        name_v = tk.StringVar()
        ttk.Entry(win, textvariable=name_v, width=28).grid(row=0, column=1, sticky="ew", padx=8)

        ttk.Label(win, text="説明:").grid(row=1, column=0, sticky="ne", padx=10, pady=8)
        desc_t = tk.Text(win, height=3,
                         bg=self.FIELD, fg=self.FG,
                         insertbackground=self.FG, font=("Yu Gothic UI", 9))
        desc_t.grid(row=1, column=1, sticky="ew", padx=8)
        win.columnconfigure(1, weight=1)

        def save():
            n = name_v.get().strip()
            if not n:
                messagebox.showerror("エラー", "イベント名を入力してください", parent=win)
                return
            conn = sqlite3.connect(db_path())
            conn.execute(
                "INSERT INTO events (name,description,created_at) VALUES (?,?,?)",
                (n, desc_t.get("1.0", "end").strip(),
                 datetime.datetime.now().isoformat()))
            conn.commit(); conn.close()
            self._refresh_event_combo()
            # 作成したばかりのイベントを選択
            key = next((k for k in self._event_map if n in k), None)
            if key:
                self.raffle_event_var.set(key)
            win.destroy()
            messagebox.showinfo("成功", f"イベント「{n}」を作成しました")

        ttk.Button(win, text="保存", style="Accent.TButton",
                   command=save).grid(row=2, column=0, columnspan=2, pady=10)

    def _browse_csv(self):
        path = filedialog.askopenfilename(
            title="Google Form 出力ファイルを選択",
            filetypes=[("CSV / Excel", "*.csv *.xls *.xlsx"), ("すべて", "*.*")])
        if path:
            self.csv_path_var.set(path)

    def _preview_csv(self):
        path = self.csv_path_var.get()
        if not path or not os.path.exists(path):
            messagebox.showerror("エラー", "有効なファイルを選択してください")
            return
        try:
            df = load_csv_or_excel(path)
        except Exception as e:
            messagebox.showerror("読み込み失敗", str(e))
            return

        win = tk.Toplevel(self)
        win.title(f"プレビュー: {os.path.basename(path)}")
        win.configure(bg=self.BG)
        win.geometry("960x480")

        cols = list(df.columns)
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=140, anchor="w")
        for _, row in df.head(200).iterrows():
            tree.insert("", "end",
                        values=[str(v) if pd.notna(v) else "" for v in row])

        vsb = ttk.Scrollbar(win, orient="vertical",   command=tree.yview)
        hsb = ttk.Scrollbar(win, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        win.rowconfigure(0, weight=1); win.columnconfigure(0, weight=1)

        found = [c for c in COLUMN_ALIASES if c in cols]
        ttk.Label(win,
                  text=f"認識済みフィールド: {', '.join(found) or '(標準フィールドなし — 列名を確認してください)'}",
                  foreground=self.ACC, background=self.BG).grid(
            row=2, column=0, sticky="w", padx=8, pady=4)

    def _safe_int(self, value, default=0):
        if pd.isna(value):
            return default
        text = str(value).strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default

    def _record_display_name(self, record):
        return (
            record.get("draw_id")
            or record.get("display_name")
            or record.get("vrc_id")
            or record.get("x_id")
            or "unknown"
        )

    def _raw_cell_text(self, row, column):
        value = row.get(column, "")
        if pd.isna(value):
            return ""
        return str(value).strip()

    def _apply_import_column_roles(self):
        if not self.imported_records:
            return
        for record in self.imported_records:
            raw = record.get("raw_values", {})
            draw_id = raw.get(self.import_id_column, "") if self.import_id_column else ""
            record["draw_id"] = draw_id
            record["display_fields"] = {
                col: raw.get(col, "") for col in self.import_display_columns
            }
            if draw_id:
                record["display_name"] = draw_id
            else:
                record["display_name"] = (
                    record.get("base_display_name")
                    or record.get("vrc_id")
                    or record.get("x_id")
                    or "unknown"
                )
            record["raw_data"] = json.dumps({
                "raw_values": raw,
                "draw_id_column": self.import_id_column,
                "display_columns": self.import_display_columns,
            }, ensure_ascii=False)

    def _on_import_column_left_click(self, column):
        if not self.imported_records:
            return
        if self.import_id_column is None or column == self.import_id_column:
            self.import_id_column = column
            if column in self.import_display_columns:
                self.import_display_columns.remove(column)
            hint = f"抽選ID列: {column}。次に左クリックした他の列は展示列になります。"
        else:
            if column not in self.import_display_columns:
                self.import_display_columns.append(column)
            hint = f"展示列: {', '.join(self.import_display_columns)}。結果表に表示されます。"
        self._apply_import_column_roles()
        self._refresh_import_tree()
        self.result_status.config(text=hint)

    def _on_import_column_right_click(self, column):
        if not self.imported_records:
            return
        changed = False
        if column == self.import_id_column:
            self.import_id_column = None
            changed = True
        if column in self.import_display_columns:
            self.import_display_columns.remove(column)
            changed = True
        if not changed:
            return
        self._apply_import_column_roles()
        self._refresh_import_tree()
        id_hint = self.import_id_column or "未指定"
        display_hint = ", ".join(self.import_display_columns) or "なし"
        self.result_status.config(
            text=f"列指定を取消しました。抽選ID: {id_hint} / 展示列: {display_hint}")

    def _load_csv_records(self):
        path = self.csv_path_var.get()
        if not path or not os.path.exists(path):
            messagebox.showerror("エラー", "有効なCSV/Excelファイルを選択してください")
            return
        try:
            df = load_csv_or_excel(path)
        except Exception as e:
            messagebox.showerror("読み込み失敗", str(e))
            return

        conn = sqlite3.connect(db_path())
        records = []
        self.import_source_columns = list(df.columns)
        self.import_id_column = None
        self.import_display_columns = []
        has_join_count = "join_count" in df.columns
        has_win_count = "win_count" in df.columns
        for _, row in df.iterrows():
            raw_values = {col: self._raw_cell_text(row, col) for col in df.columns}
            record = {
                "display_name": str(row.get("display_name", "") or "").strip(),
                "base_display_name": str(row.get("display_name", "") or "").strip(),
                "vrc_id": str(row.get("vrc_id", "") or "").strip(),
                "vrc_url": str(row.get("vrc_url", "") or "").strip(),
                "x_id": str(row.get("x_id", "") or "").strip(),
                "x_url": str(row.get("x_url", "") or "").strip(),
                "join_count": self._safe_int(row.get("join_count", 0)),
                "win_count": self._safe_int(row.get("win_count", 0)),
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

            p, _ = fuzzy_find_participant(
                conn, record["vrc_id"], record["vrc_url"], record["x_id"], record["x_url"])
            if p:
                record["participant_id"] = p["id"]
                record["matched"] = True
                if not record["display_name"]:
                    record["display_name"] = p.get("display_name", "")
                if not record["base_display_name"]:
                    record["base_display_name"] = record["display_name"]
                if not has_join_count:
                    record["join_count"] = p.get("join_count", 0)
                if not has_win_count:
                    record["win_count"] = p.get("win_count", 0)

            record["display_name"] = self._record_display_name(record)
            if not record["base_display_name"]:
                record["base_display_name"] = record["display_name"]
            records.append(record)
        conn.close()

        self.imported_records = records
        self.last_winner_indices = set()
        self._apply_import_column_roles()
        self._recalculate_record_probabilities()
        self._refresh_import_tree()

        found = [c for c in COLUMN_ALIASES if c in df.columns]
        self.result_status.config(
            text=f"{len(records)}件を読み込みました。左クリックで抽選ID列を指定、右クリックで取消。")

    def _on_raffle_mode_change(self):
        if self.imported_records:
            self._recalculate_record_probabilities()
            self._refresh_import_tree()

    def _recalculate_record_probabilities(self):
        if not self.imported_records:
            return []
        mode = "equal" if FREE_BUILD else self.mode_var.get()
        total = len(self.imported_records)
        if mode == "equal":
            weights = [1.0] * total
        else:
            weights = calc_weights(self.imported_records, mode, total)

        total_weight = sum(weights) or 1.0
        for record, weight in zip(self.imported_records, weights):
            record["weight"] = weight
            record["current_probability"] = f"{(weight / total_weight) * 100:.2f}%"
        return weights

    def _refresh_import_tree(self):
        columns = ["状態"]
        source_columns = list(self.import_source_columns)
        columns.extend(source_columns)
        tail_columns = ["参加回数", "当選回数", "現在確率", "照合"]
        columns.extend(tail_columns)

        rows = []
        for idx, record in enumerate(self.imported_records):
            is_winner = idx in self.last_winner_indices
            raw = record.get("raw_values", {})
            row = ["当選" if is_winner else "待機"]
            row.extend(raw.get(col, "") for col in source_columns)
            row.extend([
                record.get("join_count", 0),
                record.get("win_count", 0),
                record.get("current_probability", ""),
                "既存" if record.get("matched") else "新規",
            ])
            rows.append(row)

        self.result_table.set_data(
            columns,
            rows,
            source_columns=source_columns,
            id_column=self.import_id_column,
            display_columns=self.import_display_columns,
            winner_indices=self.last_winner_indices)

    def _get_selected_import_index(self):
        sel = self.result_table.selection()
        if not sel:
            messagebox.showinfo("ヒント", "編集する記録を選択してください")
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _edit_import_record(self):
        idx = self._get_selected_import_index()
        if idx is None or idx >= len(self.imported_records):
            return
        record = self.imported_records[idx]

        win = tk.Toplevel(self)
        win.title(f"抽選記録編集 #{idx + 1}")
        win.configure(bg=self.BG)
        win.geometry("430x360")
        win.grab_set()

        fields = [
            ("display_name", "名前"),
            ("vrc_id", "VRC ID"),
            ("vrc_url", "VRC URL"),
            ("x_id", "X ID"),
            ("x_url", "X URL"),
            ("join_count", "参加回数"),
            ("win_count", "当選回数"),
        ]
        vars_ = {}
        for row_idx, (field, label) in enumerate(fields):
            ttk.Label(win, text=label + ":").grid(row=row_idx, column=0, sticky="e", padx=10, pady=5)
            v = tk.StringVar(value=str(record.get(field, "")))
            ttk.Entry(win, textvariable=v, width=34).grid(row=row_idx, column=1, sticky="ew", padx=8)
            vars_[field] = v
        win.columnconfigure(1, weight=1)

        def save():
            try:
                join_count = int(vars_["join_count"].get() or 0)
                win_count = int(vars_["win_count"].get() or 0)
            except ValueError:
                messagebox.showerror("形式エラー", "参加回数・当選回数は整数で入力してください", parent=win)
                return

            for field, _ in fields:
                if field in ("join_count", "win_count"):
                    continue
                record[field] = vars_[field].get().strip()
            record["base_display_name"] = record.get("display_name", "")
            record["join_count"] = join_count
            record["win_count"] = win_count
            self._apply_import_column_roles()
            record["raw_data"] = json.dumps(
                {
                    "raw_values": record.get("raw_values", {}),
                    "draw_id_column": self.import_id_column,
                    "display_columns": self.import_display_columns,
                    "edited_fields": {field: record.get(field, "") for field, _ in fields},
                },
                ensure_ascii=False)

            self._recalculate_record_probabilities()
            self._refresh_import_tree()
            win.destroy()

        ttk.Button(win, text="保存", style="Accent.TButton",
                   command=save).grid(row=len(fields), column=0, columnspan=2, pady=14)

    def _delete_import_record(self):
        idx = self._get_selected_import_index()
        if idx is None or idx >= len(self.imported_records):
            return
        if not messagebox.askyesno("確認", "選択した抽選記録を削除しますか？"):
            return
        self.imported_records.pop(idx)
        self.last_winner_indices = set()
        self._recalculate_record_probabilities()
        self._refresh_import_tree()
        self.result_status.config(text=f"{len(self.imported_records)}件の記録があります。")

    def _save_participant_from_record(self, conn, record):
        now = datetime.datetime.now().isoformat()
        participant_id = record.get("participant_id")
        values = (
            record.get("vrc_id", ""),
            record.get("vrc_url", ""),
            record.get("x_id", ""),
            record.get("x_url", ""),
            self._record_display_name(record),
            record.get("join_count", 0),
            record.get("win_count", 0),
        )
        if participant_id:
            conn.execute(
                "UPDATE participants SET vrc_id=?,vrc_url=?,x_id=?,x_url=?,"
                "display_name=?,join_count=?,win_count=? WHERE id=?",
                (*values, participant_id))
            return participant_id

        cur = conn.execute(
            "INSERT INTO participants "
            "(vrc_id,vrc_url,x_id,x_url,display_name,join_count,win_count,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (*values, now))
        record["participant_id"] = cur.lastrowid
        record["matched"] = False
        return cur.lastrowid

    def _run_raffle(self):
        if not self.imported_records:
            self._load_csv_records()
        if not self.imported_records:
            return

        draw_count   = self.draw_count_var.get()
        # フリー版は常に均等抽選
        mode         = "equal" if FREE_BUILD else self.mode_var.get()
        allow_rep    = self.allow_repeat_var.get()
        session_name = self.session_name_var.get().strip() or \
            f"抽選_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}"
        notes = self.notes_text.get("1.0", "end").strip()

        # イベント ID
        event_id = None
        if not FREE_BUILD:
            ev_key = self.raffle_event_var.get()
            event_id = self._event_map.get(ev_key)

        total = len(self.imported_records)
        if draw_count > total and not allow_rep:
            draw_count = total
            messagebox.showwarning(
                "警告",
                f"参加者数({total})が抽選人数を下回るため、全員抽選({total}人)に変更しました")

        # ── 重み計算 ──
        if mode == "equal":
            weights = [1.0] * total
        else:
            weights = calc_weights(self.imported_records, mode, total)
        total_weight = sum(weights) or 1.0
        for record, weight in zip(self.imported_records, weights):
            record["weight"] = weight
            record["current_probability"] = f"{(weight / total_weight) * 100:.2f}%"

        # ── 加重抽選 ──
        pool_idx  = list(range(total))
        pool_w    = list(weights)
        winners_idx = []

        for _ in range(draw_count):
            if not pool_idx:
                break
            total_w = sum(pool_w[i] for i in pool_idx)
            r = random.uniform(0, total_w)
            cumul = 0.0
            chosen = pool_idx[0]
            for i in pool_idx:
                cumul += pool_w[i]
                if cumul >= r:
                    chosen = i
                    break
            winners_idx.append(chosen)
            if not allow_rep:
                pool_idx.remove(chosen)

        # ── セッション保存 ──
        now_str = datetime.datetime.now().isoformat()
        conn = sqlite3.connect(db_path())
        cur = conn.execute(
            "INSERT INTO raffle_sessions "
            "(event_id,session_name,csv_file,mode,draw_count,created_at,notes)"
            " VALUES (?,?,?,?,?,?,?)",
            (event_id, session_name, os.path.basename(self.csv_path_var.get()),
             mode, draw_count, now_str, notes))
        session_id = cur.lastrowid

        # 提出レコード
        participant_ids = []
        for record in self.imported_records:
            participant_id = self._save_participant_from_record(conn, record)
            participant_ids.append(participant_id)
            record["join_count"] = record.get("join_count", 0) + 1
            conn.execute(
                "UPDATE participants SET join_count=join_count+1 WHERE id=?",
                (participant_id,))
            conn.execute(
                "INSERT INTO submission_records "
                "(session_id,raw_data,matched_participant_id,created_at)"
                " VALUES (?,?,?,?)",
                (session_id, record.get("raw_data", json.dumps(record, ensure_ascii=False)),
                 participant_id, now_str))

        # 当選レコード
        for wi in winners_idx:
            record = self.imported_records[wi]
            participant_id = participant_ids[wi]
            conn.execute(
                "INSERT INTO raffle_results "
                "(session_id,participant_id,display_name,vrc_id,vrc_url,x_id,x_url,is_winner,extra_display_json)"
                " VALUES (?,?,?,?,?,?,?,1,?)",
                (session_id, participant_id, self._record_display_name(record),
                 record.get("vrc_id", ""), record.get("vrc_url", ""),
                 record.get("x_id", ""), record.get("x_url", ""),
                 json.dumps(record.get("display_fields", {}), ensure_ascii=False)))
            record["win_count"] = record.get("win_count", 0) + 1
            conn.execute(
                "UPDATE participants SET win_count=win_count+1 WHERE id=?",
                (participant_id,))

        conn.commit()
        conn.close()

        # ── 結果表示 ──
        self.last_winner_indices = set(winners_idx)
        self._recalculate_record_probabilities()
        self._refresh_import_tree()

        mode_label = {
            "equal":  "均等抽選（フリー版）",
            "linear": "線形加重",
            "double": "指数加重"
        }.get(mode, mode)

        self.result_status.config(
            text=(f"抽選完了: Session #{session_id} | {total}人中 {len(winners_idx)}名当選  "
                  f"|  モード: {mode_label}"))

        if not FREE_BUILD:
            self._load_sessions()
            self._load_players()


    # ══════════════════════════════════════════════════════════════════════════
    #   TAB 2 — イベント管理（Pro専用）
    # ══════════════════════════════════════════════════════════════════════════
    def _build_events_tab(self):
        f = self.tab_events
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)

        if FREE_BUILD:
            self._build_pro_lock_screen(f, "イベント管理")
            return

        top = ttk.Frame(f, padding=(8, 6, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="イベント一覧",
                  font=("Yu Gothic UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="更新",    command=self._load_events).pack(side="right", padx=4)
        ttk.Button(top, text="削除",    command=self._delete_event).pack(side="right", padx=4)
        ttk.Button(top, text="編集",    command=self._edit_event).pack(side="right", padx=4)
        ttk.Button(top, text="新規作成", command=self._new_event).pack(side="right", padx=4)

        # イベント一覧 Treeview
        cols = ("id", "name", "description", "session_count", "created_at")
        self.events_tree = ttk.Treeview(f, columns=cols, show="headings")
        heads = {"id": "ID", "name": "イベント名", "description": "説明",
                 "session_count": "セッション数", "created_at": "作成日時"}
        widths = [45, 220, 280, 90, 160]
        for c, w in zip(cols, widths):
            self.events_tree.heading(c, text=heads[c])
            self.events_tree.column(c, width=w, anchor="center")
        self.events_tree.bind("<Double-1>", lambda _: self._view_event_sessions())

        vsb = ttk.Scrollbar(f, orient="vertical",   command=self.events_tree.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self.events_tree.xview)
        self.events_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.events_tree.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=6)
        vsb.grid(row=1, column=1, sticky="ns", pady=6)
        hsb.grid(row=2, column=0, sticky="ew", padx=8)

        btn_f = ttk.Frame(f, padding=(8, 2))
        btn_f.grid(row=3, column=0, sticky="ew")
        ttk.Button(btn_f, text="このイベントのセッション一覧を表示",
                   command=self._view_event_sessions).pack(side="left", padx=4)

        self._load_events()

    def _build_pro_lock_screen(self, parent, feature_name):
        lf = ttk.Frame(parent)
        lf.pack(expand=True, fill="both")
        tk.Label(lf,
                 text=f"{feature_name}\n\nこの機能はPro版でのみご利用いただけます。",
                 font=("Yu Gothic UI", 14),
                 bg=self.BG, fg=self.ACC,
                 justify="center").pack(expand=True)

    def _load_events(self):
        if FREE_BUILD: return
        self.events_tree.delete(*self.events_tree.get_children())
        conn = sqlite3.connect(db_path())
        rows = conn.execute("""
            SELECT e.id, e.name, e.description,
                   COUNT(s.id) as session_count, e.created_at
            FROM events e
            LEFT JOIN raffle_sessions s ON s.event_id = e.id
            GROUP BY e.id
            ORDER BY e.id DESC
        """).fetchall()
        conn.close()
        for row in rows:
            self.events_tree.insert("", "end", values=row)

    def _get_selected_event_id(self):
        sel = self.events_tree.selection()
        if not sel:
            messagebox.showinfo("ヒント", "イベントを選択してください")
            return None
        return self.events_tree.item(sel[0])["values"][0]

    def _new_event(self):
        self._event_form(None)

    def _edit_event(self):
        eid = self._get_selected_event_id()
        if eid is None: return
        conn = sqlite3.connect(db_path())
        row = conn.execute(
            "SELECT id,name,description FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        if row:
            self._event_form({"id": row[0], "name": row[1], "description": row[2]})

    def _event_form(self, data: Optional[dict]):
        is_new = data is None
        win = tk.Toplevel(self)
        win.title("イベント作成" if is_new else f"イベント編集 #{data['id']}")
        win.configure(bg=self.BG)
        win.geometry("400x220")
        win.grab_set()

        ttk.Label(win, text="イベント名:").grid(row=0, column=0, sticky="e", padx=10, pady=8)
        name_v = tk.StringVar(value="" if is_new else data["name"])
        ttk.Entry(win, textvariable=name_v, width=32).grid(row=0, column=1, sticky="ew", padx=8)

        ttk.Label(win, text="説明:").grid(row=1, column=0, sticky="ne", padx=10, pady=8)
        desc_t = tk.Text(win, height=4,
                         bg=self.FIELD, fg=self.FG,
                         insertbackground=self.FG, font=("Yu Gothic UI", 9))
        desc_t.grid(row=1, column=1, sticky="ew", padx=8)
        if not is_new and data.get("description"):
            desc_t.insert("1.0", data["description"])
        win.columnconfigure(1, weight=1)

        def save():
            n = name_v.get().strip()
            if not n:
                messagebox.showerror("エラー", "イベント名を入力してください", parent=win)
                return
            conn = sqlite3.connect(db_path())
            if is_new:
                conn.execute(
                    "INSERT INTO events (name,description,created_at) VALUES (?,?,?)",
                    (n, desc_t.get("1.0", "end").strip(),
                     datetime.datetime.now().isoformat()))
            else:
                conn.execute(
                    "UPDATE events SET name=?, description=? WHERE id=?",
                    (n, desc_t.get("1.0", "end").strip(), data["id"]))
            conn.commit(); conn.close()
            self._load_events()
            self._refresh_event_combo()
            win.destroy()
            messagebox.showinfo("成功", "イベントを保存しました")

        ttk.Button(win, text="保存", style="Accent.TButton",
                   command=save).grid(row=2, column=0, columnspan=2, pady=10)

    def _delete_event(self):
        eid = self._get_selected_event_id()
        if eid is None: return
        if not messagebox.askyesno(
                "確認",
                f"イベント #{eid} を削除しますか？\n関連するセッション・結果も全て削除されます。"):
            return
        conn = sqlite3.connect(db_path())
        # 関連セッション取得
        sids = [r[0] for r in conn.execute(
            "SELECT id FROM raffle_sessions WHERE event_id=?", (eid,)).fetchall()]
        for sid in sids:
            conn.execute("DELETE FROM raffle_results WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM submission_records WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM raffle_sessions WHERE event_id=?", (eid,))
        conn.execute("DELETE FROM events WHERE id=?", (eid,))
        conn.commit(); conn.close()
        self._load_events()
        self._refresh_event_combo()
        messagebox.showinfo("削除完了", f"イベント #{eid} を削除しました")

    def _view_event_sessions(self):
        eid = self._get_selected_event_id()
        if eid is None: return

        conn = sqlite3.connect(db_path())
        ev = conn.execute(
            "SELECT name FROM events WHERE id=?", (eid,)).fetchone()
        rows = conn.execute(
            "SELECT id,session_name,mode,draw_count,created_at,notes "
            "FROM raffle_sessions WHERE event_id=? ORDER BY id DESC",
            (eid,)).fetchall()
        conn.close()

        win = tk.Toplevel(self)
        win.title(f"イベント「{ev[0] if ev else eid}」のセッション")
        win.configure(bg=self.BG)
        win.geometry("840x440")

        cols = ("id", "session_name", "mode", "draw_count", "created_at", "notes")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        heads = {"id":"ID","session_name":"セッション名","mode":"モード",
                 "draw_count":"抽選数","created_at":"日時","notes":"備考"}
        widths = [45, 200, 90, 70, 160, 220]
        for c, w in zip(cols, widths):
            tree.heading(c, text=heads[c])
            tree.column(c, width=w, anchor="center")
        for row in rows:
            tree.insert("", "end", values=row)

        vsb = ttk.Scrollbar(win, orient="vertical",   command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        win.rowconfigure(0, weight=1); win.columnconfigure(0, weight=1)

        def view_results():
            sel = tree.selection()
            if not sel: messagebox.showinfo("ヒント","セッションを選択してください",parent=win); return
            sid = tree.item(sel[0])["values"][0]
            self._open_results_window(sid)

        bf = ttk.Frame(win); bf.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Button(bf, text="結果を表示", command=view_results).pack(side="left", padx=8)


    # ══════════════════════════════════════════════════════════════════════════
    #   TAB 3 — セッション記録（Pro専用）
    # ══════════════════════════════════════════════════════════════════════════
    def _build_sessions_tab(self):
        f = self.tab_sessions
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)

        if FREE_BUILD:
            self._build_pro_lock_screen(f, "セッション記録")
            return

        top = ttk.Frame(f, padding=(8, 6, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="抽選セッション一覧",
                  font=("Yu Gothic UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="更新",       command=self._load_sessions).pack(side="right", padx=4)
        ttk.Button(top, text="削除",       command=self._delete_session).pack(side="right", padx=4)
        ttk.Button(top, text="編集",       command=self._edit_session).pack(side="right", padx=4)
        ttk.Button(top, text="結果を表示", command=self._view_session_results).pack(side="right", padx=4)

        # イベントフィルター
        filter_f = ttk.Frame(f, padding=(8, 2))
        filter_f.grid(row=2, column=0, sticky="ew")
        ttk.Label(filter_f, text="イベントで絞り込み:").pack(side="left")
        self.session_filter_var = tk.StringVar(value="すべて")
        self.session_filter_combo = ttk.Combobox(
            filter_f, textvariable=self.session_filter_var,
            state="readonly", width=28)
        self.session_filter_combo.pack(side="left", padx=6)
        self.session_filter_combo.bind(
            "<<ComboboxSelected>>", lambda _: self._load_sessions())

        cols = ("id", "event_name", "session_name", "mode", "draw_count", "created_at", "notes")
        self.sessions_tree = ttk.Treeview(f, columns=cols, show="headings")
        heads = {"id":"ID","event_name":"イベント","session_name":"セッション名",
                 "mode":"モード","draw_count":"抽選数","created_at":"日時","notes":"備考"}
        widths = [40, 140, 170, 80, 60, 155, 190]
        for c, w in zip(cols, widths):
            self.sessions_tree.heading(c, text=heads[c])
            self.sessions_tree.column(c, width=w, anchor="center")

        vsb = ttk.Scrollbar(f, orient="vertical",   command=self.sessions_tree.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self.sessions_tree.xview)
        self.sessions_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.sessions_tree.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=6)
        vsb.grid(row=1, column=1, sticky="ns", pady=6)
        hsb.grid(row=3, column=0, sticky="ew", padx=8)

        self._load_sessions()
        self._refresh_session_filter()

    def _refresh_session_filter(self):
        if FREE_BUILD: return
        conn = sqlite3.connect(db_path())
        rows = conn.execute(
            "SELECT id, name FROM events ORDER BY id DESC").fetchall()
        conn.close()
        self._session_filter_map = {"すべて": None}
        self._session_filter_map.update({f"[{r[0]}] {r[1]}": r[0] for r in rows})
        self.session_filter_combo["values"] = list(self._session_filter_map.keys())
        self.session_filter_combo.current(0)

    def _load_sessions(self):
        if FREE_BUILD: return
        self.sessions_tree.delete(*self.sessions_tree.get_children())
        fv = self.session_filter_var.get() if hasattr(self, "session_filter_var") else "すべて"
        eid = self._session_filter_map.get(fv) if hasattr(self, "_session_filter_map") else None

        conn = sqlite3.connect(db_path())
        if eid:
            rows = conn.execute("""
                SELECT s.id, COALESCE(e.name,'—'), s.session_name,
                       s.mode, s.draw_count, s.created_at, s.notes
                FROM raffle_sessions s
                LEFT JOIN events e ON e.id = s.event_id
                WHERE s.event_id=?
                ORDER BY s.id DESC
            """, (eid,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT s.id, COALESCE(e.name,'—'), s.session_name,
                       s.mode, s.draw_count, s.created_at, s.notes
                FROM raffle_sessions s
                LEFT JOIN events e ON e.id = s.event_id
                ORDER BY s.id DESC
            """).fetchall()
        conn.close()
        for row in rows:
            self.sessions_tree.insert("", "end", values=row)

    def _get_selected_session_id(self):
        sel = self.sessions_tree.selection()
        if not sel:
            messagebox.showinfo("ヒント", "セッションを選択してください")
            return None
        return self.sessions_tree.item(sel[0])["values"][0]

    def _view_session_results(self):
        sid = self._get_selected_session_id()
        if sid is None: return
        self._open_results_window(sid)

    def _open_results_window(self, sid):
        conn = sqlite3.connect(db_path())
        rows = conn.execute(
            "SELECT id,display_name,vrc_id,vrc_url,x_id,x_url,is_winner,extra_display_json "
            "FROM raffle_results WHERE session_id=?", (sid,)).fetchall()
        conn.close()
        extra_keys = []
        parsed_rows = []
        for row in rows:
            extra = {}
            if len(row) > 7 and row[7]:
                try:
                    extra = json.loads(row[7])
                except (TypeError, json.JSONDecodeError):
                    extra = {}
            for key in extra:
                if key not in extra_keys:
                    extra_keys.append(key)
            parsed_rows.append((row[:7], extra))

        win = tk.Toplevel(self)
        win.title(f"セッション #{sid} — 抽選結果")
        win.configure(bg=self.BG)
        win.geometry("900x420")

        cols = ("id","display_name","vrc_id","vrc_url","x_id","x_url", *extra_keys, "is_winner")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        heads = {"id":"ID","display_name":"名前","vrc_id":"VRC ID","vrc_url":"VRC URL",
                 "x_id":"X ID","x_url":"X URL","is_winner":"当選"}
        widths = {
            "id": 45, "display_name": 130, "vrc_id": 130, "vrc_url": 150,
            "x_id": 110, "x_url": 150, "is_winner": 50
        }
        for c in cols:
            tree.heading(c, text=heads.get(c, c))
            w = widths.get(c, 140)
            tree.column(c, width=w, anchor="center")
        for base, extra in parsed_rows:
            values = list(base[:6]) + [extra.get(key, "") for key in extra_keys] + [base[6]]
            tag = "win" if base[-1] == 1 else ""
            tree.insert("", "end", values=values, tags=(tag,))
        tree.tag_configure("win",
                           background="#eff6ff",
                           foreground=self.FG)

        vsb = ttk.Scrollbar(win, orient="vertical",   command=tree.yview)
        hsb = ttk.Scrollbar(win, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        win.rowconfigure(0, weight=1); win.columnconfigure(0, weight=1)

        bf = ttk.Frame(win); bf.grid(row=2, column=0, sticky="ew", pady=4)
        ttk.Button(bf, text="編集",
                   command=lambda: self._edit_result_entry(tree, sid)).pack(side="left", padx=8)
        ttk.Button(bf, text="削除",
                   command=lambda: self._delete_result_entry(tree)).pack(side="left")

    def _edit_result_entry(self, tree, session_id):
        sel = tree.selection()
        if not sel: messagebox.showinfo("ヒント", "行を選択してください"); return
        vals = tree.item(sel[0])["values"]
        result_id = vals[0]

        win = tk.Toplevel(self)
        win.title(f"結果編集 #{result_id}")
        win.configure(bg=self.BG)
        win.geometry("420x300")
        win.grab_set()

        fields = ["display_name","vrc_id","vrc_url","x_id","x_url"]
        labels = ["名前","VRC ID","VRC URL","X ID","X URL"]
        vars_ = {}
        for i, (field, label) in enumerate(zip(fields, labels)):
            ttk.Label(win, text=label+":").grid(row=i, column=0, sticky="e", padx=8, pady=4)
            v = tk.StringVar(value=str(vals[i+1]))
            ttk.Entry(win, textvariable=v, width=32).grid(row=i, column=1, sticky="ew", padx=8)
            vars_[field] = v
        win.columnconfigure(1, weight=1)

        def save():
            conn = sqlite3.connect(db_path())
            conn.execute(
                "UPDATE raffle_results SET display_name=?,vrc_id=?,vrc_url=?,x_id=?,x_url=? WHERE id=?",
                (vars_["display_name"].get(), vars_["vrc_id"].get(),
                 vars_["vrc_url"].get(), vars_["x_id"].get(),
                 vars_["x_url"].get(), result_id))
            conn.commit(); conn.close()
            updated = list(vals)
            updated[1:6] = [
                vars_["display_name"].get(), vars_["vrc_id"].get(),
                vars_["vrc_url"].get(), vars_["x_id"].get(),
                vars_["x_url"].get()]
            tree.item(sel[0], values=updated)
            win.destroy()
            messagebox.showinfo("成功", "結果を更新しました")

        ttk.Button(win, text="保存", style="Accent.TButton",
                   command=save).grid(row=len(fields), column=0, columnspan=2, pady=12)

    def _delete_result_entry(self, tree):
        sel = tree.selection()
        if not sel: messagebox.showinfo("ヒント","行を選択してください"); return
        result_id = tree.item(sel[0])["values"][0]
        if not messagebox.askyesno("確認","この結果を削除しますか？"): return
        conn = sqlite3.connect(db_path())
        conn.execute("DELETE FROM raffle_results WHERE id=?", (result_id,))
        conn.commit(); conn.close()
        tree.delete(sel[0])

    def _edit_session(self):
        sid = self._get_selected_session_id()
        if sid is None: return
        conn = sqlite3.connect(db_path())
        row = conn.execute(
            "SELECT session_name, notes FROM raffle_sessions WHERE id=?", (sid,)).fetchone()
        conn.close()
        if not row: return

        win = tk.Toplevel(self)
        win.title(f"セッション編集 #{sid}")
        win.configure(bg=self.BG)
        win.geometry("380x210")
        win.grab_set()

        ttk.Label(win, text="セッション名:").grid(row=0, column=0, sticky="e", padx=8, pady=8)
        name_v = tk.StringVar(value=row[0])
        ttk.Entry(win, textvariable=name_v, width=28).grid(row=0, column=1, sticky="ew", padx=8)

        ttk.Label(win, text="備考:").grid(row=1, column=0, sticky="ne", padx=8, pady=8)
        notes_t = tk.Text(win, height=4,
                          bg=self.FIELD, fg=self.FG,
                          insertbackground=self.FG, font=("Yu Gothic UI", 9))
        notes_t.grid(row=1, column=1, sticky="ew", padx=8)
        notes_t.insert("1.0", row[1] or "")
        win.columnconfigure(1, weight=1)

        def save():
            conn2 = sqlite3.connect(db_path())
            conn2.execute(
                "UPDATE raffle_sessions SET session_name=?, notes=? WHERE id=?",
                (name_v.get(), notes_t.get("1.0","end").strip(), sid))
            conn2.commit(); conn2.close()
            self._load_sessions()
            win.destroy()
            messagebox.showinfo("成功", "セッションを更新しました")

        ttk.Button(win, text="保存", style="Accent.TButton",
                   command=save).grid(row=2, column=0, columnspan=2, pady=10)

    def _delete_session(self):
        sid = self._get_selected_session_id()
        if sid is None: return
        if not messagebox.askyesno(
                "確認",
                f"セッション #{sid} とその全結果を削除しますか？\nこの操作は取り消せません。"):
            return
        conn = sqlite3.connect(db_path())
        conn.execute("DELETE FROM raffle_results WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM submission_records WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM raffle_sessions WHERE id=?", (sid,))
        conn.commit(); conn.close()
        self._load_sessions()
        messagebox.showinfo("削除完了", f"セッション #{sid} を削除しました")


    # ══════════════════════════════════════════════════════════════════════════
    #   TAB 4 — 参加者管理（Pro専用）
    # ══════════════════════════════════════════════════════════════════════════
    def _build_players_tab(self):
        f = self.tab_players
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)

        if FREE_BUILD:
            self._build_pro_lock_screen(f, "参加者データベース")
            return

        top = ttk.Frame(f, padding=(8, 6, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="参加者データベース",
                  font=("Yu Gothic UI", 11, "bold")).pack(side="left")
        ttk.Button(top, text="更新",     command=self._load_players).pack(side="right", padx=4)
        ttk.Button(top, text="削除",     command=self._delete_player).pack(side="right", padx=4)
        ttk.Button(top, text="編集",     command=self._edit_player).pack(side="right", padx=4)
        ttk.Button(top, text="手動追加", command=self._add_player).pack(side="right", padx=4)

        # 検索バー
        sf = ttk.Frame(f, padding=(8, 2))
        sf.grid(row=2, column=0, sticky="ew")
        ttk.Label(sf, text="検索:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._load_players())
        ttk.Entry(sf, textvariable=self.search_var, width=32).pack(side="left", padx=4)
        ttk.Label(sf, text="（名前 / VRC ID / X ID で検索）",
                  font=("Yu Gothic UI", 8),
                  foreground=self.MUTED).pack(side="left")

        cols = ("id","display_name","vrc_id","vrc_url","x_id","x_url",
                "join_count","win_count","created_at")
        self.players_tree = ttk.Treeview(f, columns=cols, show="headings")
        heads = {"id":"ID","display_name":"名前","vrc_id":"VRC ID","vrc_url":"VRC URL",
                 "x_id":"X ID","x_url":"X URL","join_count":"参加回数",
                 "win_count":"当選回数","created_at":"初回記録"}
        widths = [35,120,130,145,100,135,70,70,150]
        for c, w in zip(cols, widths):
            self.players_tree.heading(
                c, text=heads[c],
                command=lambda _c=c: self._sort_players_by(_c))
            self.players_tree.column(c, width=w, anchor="center")

        vsb = ttk.Scrollbar(f, orient="vertical",   command=self.players_tree.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self.players_tree.xview)
        self.players_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.players_tree.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=6)
        vsb.grid(row=1, column=1, sticky="ns", pady=6)
        hsb.grid(row=3, column=0, sticky="ew", padx=8)

        self._load_players()

    def _load_players(self):
        if FREE_BUILD: return
        kw = self.search_var.get().strip().lower() \
            if hasattr(self, "search_var") else ""
        self.players_tree.delete(*self.players_tree.get_children())
        conn = sqlite3.connect(db_path())
        rows = conn.execute(
            "SELECT id,display_name,vrc_id,vrc_url,x_id,x_url,"
            "join_count,win_count,created_at FROM participants").fetchall()
        conn.close()
        for row in rows:
            if kw and not any(kw in str(v).lower() for v in row):
                continue
            self.players_tree.insert("", "end", values=row)

    def _sort_players_by(self, col):
        if FREE_BUILD: return
        data = [(self.players_tree.set(child, col), child)
                for child in self.players_tree.get_children("")]
        try:
            data.sort(key=lambda x: float(x[0]), reverse=self._players_sort_rev)
        except ValueError:
            data.sort(key=lambda x: str(x[0]).lower(), reverse=self._players_sort_rev)
        for i, (_, child) in enumerate(data):
            self.players_tree.move(child, "", i)
        self._players_sort_rev = not self._players_sort_rev

    def _get_selected_player_id(self):
        sel = self.players_tree.selection()
        if not sel:
            messagebox.showinfo("ヒント", "参加者を選択してください")
            return None
        return self.players_tree.item(sel[0])["values"][0]

    def _add_player(self):
        self._player_form(None)

    def _edit_player(self):
        pid = self._get_selected_player_id()
        if pid is None: return
        conn = sqlite3.connect(db_path())
        row = conn.execute(
            "SELECT id,display_name,vrc_id,vrc_url,x_id,x_url,join_count,win_count "
            "FROM participants WHERE id=?", (pid,)).fetchone()
        conn.close()
        if row:
            keys = ["id","display_name","vrc_id","vrc_url","x_id","x_url",
                    "join_count","win_count"]
            self._player_form(dict(zip(keys, row)))

    def _player_form(self, data: Optional[dict]):
        is_new = data is None
        win = tk.Toplevel(self)
        win.title("参加者追加" if is_new else f"参加者編集 #{data['id']}")
        win.configure(bg=self.BG)
        win.geometry("400x360")
        win.grab_set()

        fields = ["display_name","vrc_id","vrc_url","x_id","x_url","join_count","win_count"]
        labels = ["名前/ニックネーム","VRC ID","VRC URL","X ID","X URL","参加回数","当選回数"]
        vars_  = {}
        for i, (field, label) in enumerate(zip(fields, labels)):
            ttk.Label(win, text=label+":").grid(row=i, column=0, sticky="e", padx=10, pady=4)
            v = tk.StringVar(value=str(data[field]) if data and field in data else "")
            ttk.Entry(win, textvariable=v, width=32).grid(row=i, column=1, sticky="ew", padx=8)
            vars_[field] = v
        win.columnconfigure(1, weight=1)

        def save():
            conn = sqlite3.connect(db_path())
            try:
                jc = int(vars_["join_count"].get() or 0)
                wc = int(vars_["win_count"].get()  or 0)
            except ValueError:
                messagebox.showerror("形式エラー","参加回数・当選回数は整数で入力してください",
                                     parent=win); return
            if is_new:
                conn.execute(
                    "INSERT INTO participants "
                    "(display_name,vrc_id,vrc_url,x_id,x_url,join_count,win_count,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (vars_["display_name"].get(), vars_["vrc_id"].get(),
                     vars_["vrc_url"].get(), vars_["x_id"].get(),
                     vars_["x_url"].get(), jc, wc,
                     datetime.datetime.now().isoformat()))
            else:
                conn.execute(
                    "UPDATE participants SET display_name=?,vrc_id=?,vrc_url=?,"
                    "x_id=?,x_url=?,join_count=?,win_count=? WHERE id=?",
                    (vars_["display_name"].get(), vars_["vrc_id"].get(),
                     vars_["vrc_url"].get(), vars_["x_id"].get(),
                     vars_["x_url"].get(), jc, wc, data["id"]))
            conn.commit(); conn.close()
            self._load_players()
            win.destroy()
            messagebox.showinfo("成功", "参加者を保存しました")

        ttk.Button(win, text="保存", style="Accent.TButton",
                   command=save).grid(row=len(fields), column=0, columnspan=2, pady=12)

    def _delete_player(self):
        pid = self._get_selected_player_id()
        if pid is None: return
        if not messagebox.askyesno(
                "確認",
                f"参加者 #{pid} を削除しますか？\n関連する抽選結果は保持されます。"):
            return
        conn = sqlite3.connect(db_path())
        conn.execute("DELETE FROM participants WHERE id=?", (pid,))
        conn.commit(); conn.close()
        self._load_players()


    # ══════════════════════════════════════════════════════════════════════════
    #   TAB 5 — ヘルプ
    # ══════════════════════════════════════════════════════════════════════════
    def _build_help_tab(self):
        f = self.tab_help
        f.rowconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)

        text = tk.Text(f, bg=self.FIELD,
                       fg=self.FG, font=("Yu Gothic UI", 10),
                       wrap="word", padx=18, pady=14, relief="flat")
        text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        vsb = ttk.Scrollbar(f, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns", pady=8)

        edition = "フリー版" if FREE_BUILD else "Pro版"
        free_limits = """
【フリー版の制限】
  ・ 抽選機能: 均等確率の1回抽選のみ利用可能
  ・ 加重確率モード: Pro版限定
  ・ イベント管理:   Pro版限定
  ・ セッション記録: Pro版限定
  ・ 参加者データベース: Pro版限定

""" if FREE_BUILD else """
【Pro版の全機能】
  ・ 線形/指数 加重確率抽選
  ・ イベントごとの複数セッション管理
  ・ 全セッション記録の閲覧・編集・削除
  ・ 参加者データベースの完全管理
  ・ ファジーマッチングによる自動履歴照合

"""

        help_content = f"""
══════════════════════════════════════════════════════
  VRC イベント抽選ツール  ({edition})  使い方ガイド
══════════════════════════════════════════════════════
{free_limits}
【基本的な流れ】
  1. Google Form でエントリーを収集し、CSV または Excel で出力
  2.「抽選」タブでファイルを選択し、抽選人数・モードを設定
  3.「抽選開始」ボタンをクリック → 結果が右側に表示されます
  4. 結果は自動的にデータベースに保存されます（Pro版）

【Google Form の列名について】
  以下の列名（大文字・小文字不問）が自動認識されます:
    ・ VRC ID  /  VRC URL
    ・ X ID    /  X URL
    ・ Name    /  ニックネーム / display_name

  「CSVプレビュー」ボタンで認識状況を事前確認できます。

【ファジーマッチング（Pro版）】
  VRC ID / VRC URL / X ID / X URL の4フィールドを使って
  過去のデータベースと照合します（類似度 80% 以上で一致判定）。
  ・ 一致した場合: 参加回数 +1 → 加重確率に反映
  ・ 一致しない場合: 新規参加者として自動登録

【加重確率モード（Pro版）】

  線形加重:
    weight = 1 + 参加回数 / 総参加人数
    → 参加が多いほど緩やかに確率アップ（穏健モード）

  指数加重:
    weight = 2 ^ 参加回数
    → 参加回数が増えるほど確率が急激にアップ（熱心な参加者優遇）

【イベント管理（Pro版）】
  ・ 複数のイベントを作成し、各イベントに複数のセッションを紐づけ可能
  ・「イベント」タブで作成・編集・削除
  ・「記録」タブでイベントによる絞り込みが可能

【.exe のビルド方法】
  pip install pyinstaller thefuzz python-Levenshtein pandas openpyxl

  # フリー版
  echo FREE_BUILD=True > _build_config.py
  pyinstaller --onefile --windowed --name "VRC抽選ツール_Free" vrc_raffle.py

  # Pro版
  echo FREE_BUILD=False > _build_config.py
  pyinstaller --onefile --windowed --name "VRC抽選ツール_Pro" vrc_raffle.py

  ※ データベース vrc_raffle.db は exe と同じフォルダに自動生成されます
══════════════════════════════════════════════════════
"""
        text.insert("1.0", help_content)
        text.configure(state="disabled")


# ─────────────────────────────────────────────────────────────────────────────
#  エントリーポイント
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = VRCRaffleApp()
    app.mainloop()
