# main.py (v1.0.0-alpha.41.fix111)
# UI_INI_PURGE_PASS_FIX111
from __future__ import annotations

import os
import re
import sys
import socket
# Avoid rare hangs on the last TMDB call by enforcing a global network socket timeout.
socket.setdefaulttimeout(20)
import difflib
import subprocess
import time
import traceback
import sqlite3
import json
import csv
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import math
import unicodedata
from PySide6 import QtCore
from PySide6.QtCore import (
    Qt,
    QSize,
    QAbstractListModel,
    QModelIndex,
    Signal,
    QSortFilterProxyModel,
    QTimer,
    QRect,    QPoint,    QUrl,
)
from PySide6.QtGui import QPixmap, QPainter, QFont, QFontMetrics, QColor, QPen, QBrush, QIcon, QPalette, QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QSplitter,
    QListView,
    QAbstractItemView,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QStatusBar,
    QMessageBox,
    QCheckBox,
    QStyledItemDelegate,
    QStyle,
    QComboBox,
    QDialog,
    QListWidget,
    QListWidgetItem,
    QDialogButtonBox,
    QFrame,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMenu,
    QStackedWidget,    QTextEdit,
)

import configparser

from database import ensure_db, db_session
from filename_parser import is_video_file, parse_movie_name
from tmdb_client import TMDBClient, TMDBMovie


APP_NAME = "Movie Collection App"

# Roman numeral suffix used by smart filename parsing (e.g., 'Rocky II')
_ROMAN_END_RE = re.compile(r"^(?P<base>.*?)(?P<roman>\s+(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|xx))\s*$", re.IGNORECASE)

APP_VERSION = "v1.0.0-alpha.41.fix111"

APP_DIR = os.path.abspath(os.path.dirname(__file__))
CACHE_DIR = os.path.join(APP_DIR, "cache")
UI_INI_PATH = os.path.join(APP_DIR, "ui.ini")

SCAN_DEBUG_LOG_PATH = os.path.join(APP_DIR, "scan_debug.csv")

def append_scan_debug(action: str,
                      file_path: str,
                      want_title: str = "",
                      want_year: str = "",
                      smart_title: str = "",
                      eff_year: str = "",
                      tmdb_id: str = "",
                      picked_title: str = "",
                      picked_year: str = "",
                      note: str = "") -> None:
    """Append one scan-debug row to scan_debug.csv (best-effort; never raises)."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = {
            "ts": ts,
            "action": str(action or ""),
            "file_path": str(file_path or ""),
            "want_title": str(want_title or ""),
            "want_year": str(want_year or ""),
            "smart_title": str(smart_title or ""),
            "eff_year": str(eff_year or ""),
            "tmdb_id": str(tmdb_id or ""),
            "picked_title": str(picked_title or ""),
            "picked_year": str(picked_year or ""),
            "note": str(note or ""),
        }
        new_file = (not os.path.exists(SCAN_DEBUG_LOG_PATH)) or (os.path.getsize(SCAN_DEBUG_LOG_PATH) == 0)
        with open(SCAN_DEBUG_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new_file:
                w.writeheader()
            w.writerow(row)
    except Exception:
        # Debug logging must never break scanning.
        return



# ---------------- Data models ----------------
@dataclass(frozen=True)
class MovieRow:
    tmdb_id: int
    title: str
    year: int | None
    overview: str
    runtime: int | None
    poster_path: str | None
    backdrop_path: str | None
    lang_original: str | None
    studio: str | None
    tmdb_rating: float | None
    tmdb_votes: int | None
    user_rating: int
    watched: int
    date_added: str | None
    primary_genre: str | None
    genres_str: str
    edition: str | None
    color_mode: str | None
    file_path: str | None

# ---------------- DB compatibility (migrations for old movies.db) ----------------
def ensure_db_compat(db_path: str) -> None:
    """
    Ensures older movies.db schemas are upgraded to match what this main.py expects.
    Safe to run on every startup. Only adds missing columns; it does NOT drop data.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # movies table must exist (ensure_db should create it)
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='movies';"
        ).fetchone()
        if not row:
            return

        cols = {r["name"] for r in cur.execute("PRAGMA table_info(movies);").fetchall()}

        # Columns required by DB.list_movies() and insert/update paths
        required = {
            "overview": "TEXT DEFAULT ''",
            "runtime": "INTEGER",
            "poster_path": "TEXT",
            "backdrop_path": "TEXT",
            "lang_original": "TEXT",
            "studio": "TEXT",
            "tmdb_rating": "REAL",
            "tmdb_votes": "INTEGER",
            "user_rating": "INTEGER DEFAULT 0",
            "watched": "INTEGER DEFAULT 0",
            "date_added": "INTEGER DEFAULT 0",
            "primary_genre_id": "INTEGER",
            "edition": "TEXT DEFAULT 'Standard'",
            "color_mode": "TEXT DEFAULT 'unknown'",
            # File metadata (used by scan / rescan logic in some versions)
            "file_path": "TEXT",
            "file_mtime": "INTEGER",
            "file_size": "INTEGER",
        }

        for name, decl in required.items():
            if name not in cols:
                cur.execute(f"ALTER TABLE movies ADD COLUMN {name} {decl};")

        conn.commit()
    finally:
        conn.close()


# ---------------- ini helpers ----------------
def _ini_defaults() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "window": {"start_maximized": "true"},
            "library": {"recursive_scan": "true"},
            "tmdb": {"api_key": "", "language": "en-US"},
        }
    )
    return cfg


def ensure_scan_failures_table(db_path: str) -> None:
    """Creates the scan_failures table used to log 'No TMDB results' cases for later diagnosis."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_failures (
                file_path TEXT PRIMARY KEY,
                file_name TEXT,
                want_title TEXT,
                want_year INTEGER,
                smart_title TEXT,
                effective_year INTEGER,
                variants_json TEXT,
                reason TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                hit_count INTEGER DEFAULT 1
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _get(cfg: configparser.ConfigParser, sec: str, key: str, default: str) -> str:
    try:
        return cfg.get(sec, key)
    except Exception:
        return default


def _get_bool(cfg: configparser.ConfigParser, sec: str, key: str, default: bool) -> bool:
    try:
        return cfg.getboolean(sec, key)
    except Exception:
        return default


def load_ui_ini() -> Dict[str, Any]:
    """
    STRICT UI INI loader (organized layout).

    RULE: All UI-related values MUST come from ui.ini (no hard-coded styling values).
    This loader expects exactly 5 top-level sections:
      [app], [toolbar], [pane_genres], [pane_movies], [pane_details]

    It builds the internal ui-dict structure the rest of the app expects:
      ui["window"], ui["panes"], ui["library"], ui["tmdb"], ui["fonts"], ui["colors"],
      ui["radii"], ui["movies_grid"], ui["qss"], plus optional ui["details"].
    """
    import configparser

    if not os.path.exists(UI_INI_PATH):
        raise RuntimeError(f"Missing ui.ini: {UI_INI_PATH}")

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(UI_INI_PATH, encoding="utf-8")

    def req(section: str, key: str) -> str:
        if not cfg.has_section(section):
            raise RuntimeError(f"ui.ini missing section [{section}] in {UI_INI_PATH}")
        if not cfg.has_option(section, key):
            raise RuntimeError(f"ui.ini missing key '{key}' in section [{section}] ({section}.{key}) in {UI_INI_PATH}")
        return cfg.get(section, key)

    def opt(section: str, key: str, default: str) -> str:
        if not cfg.has_section(section):
            return default
        if not cfg.has_option(section, key):
            return default
        return cfg.get(section, key)

    def req_int(section: str, key: str) -> int:
        v = req(section, key).strip()
        try:
            return int(v)
        except Exception:
            raise RuntimeError(f"ui.ini invalid int for {section}.{key}='{v}' in {UI_INI_PATH}")

    def req_bool(section: str, key: str) -> bool:
        v = req(section, key).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise RuntimeError(f"ui.ini invalid bool for {section}.{key}='{v}' in {UI_INI_PATH}")

    def opt_int(section: str, key: str, default: int) -> int:
        v = opt(section, key, str(default)).strip()
        try:
            return int(v)
        except Exception:
            raise RuntimeError(f"ui.ini invalid int for {section}.{key}='{v}' in {UI_INI_PATH}")

    def opt_bool(section: str, key: str, default: bool) -> bool:
        v = opt(section, key, "true" if default else "false").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise RuntimeError(f"ui.ini invalid bool for {section}.{key}='{v}' in {UI_INI_PATH}")

    # --- Required core keys (kept strict) ---
    ui: Dict[str, Any] = {}

    ui["window"] = {
        "start_maximized": req_bool("app", "start_maximized"),
    }

    ui["panes"] = {
        "genres_width": req_int("pane_genres", "width"),
        "details_width": req_int("pane_details", "width"),
    }

    ui["toolbar"] = {
        "height": opt_int("toolbar", "height", 60),
        "spacing": opt_int("toolbar", "spacing", 8),
        "pad_v": opt_int("toolbar", "pad_v", 8),
        "pad_h": opt_int("toolbar", "pad_h", 8),
        "icon_size": opt_int("toolbar", "icon_size", 30),
        "button_border": opt_int("toolbar", "button_border", 2),
        "button_radius": opt_int("toolbar", "button_radius", ui["radii"]["button"] if "radii" in ui else 8),
        "button_padding": opt_int("toolbar", "button_padding", 6),
        "button_font_size": opt_int("toolbar", "button_font_size", ui["fonts"]["base_size"]),
        "button_font_weight": opt_int("toolbar", "button_font_weight", 400),
        "button_text_color": opt("toolbar", "button_text_color", ui["colors"]["text"]),
        "search_height": opt_int("toolbar", "search_height", 36),
        "search_min_width": opt_int("toolbar", "search_min_width", 320),
        "search_max_width": opt_int("toolbar", "search_max_width", 640),
        "search_icon_size": opt_int("toolbar", "search_icon_size", 30),
        "search_border": opt_int("toolbar", "search_border", 2),
        "search_radius": opt_int("toolbar", "search_radius", ui["radii"]["input"] if "radii" in ui else 8),
        "search_pad_v": opt_int("toolbar", "search_pad_v", 5),
        "search_pad_h": opt_int("toolbar", "search_pad_h", 10),
        "search_font_size": opt_int("toolbar", "search_font_size", ui["fonts"]["base_size"]),
        "search_font_weight": opt_int("toolbar", "search_font_weight", 400),
        "search_text_color": opt("toolbar", "search_text_color", ui["colors"]["text"]),
        "search_placeholder_color": opt("toolbar", "search_placeholder_color", ui["colors"]["text2"]),
        "label_font_size": opt_int("toolbar", "label_font_size", ui["fonts"]["base_size"]),
        "label_font_weight": opt_int("toolbar", "label_font_weight", 400),
        "label_text_color": opt("toolbar", "label_text_color", ui["colors"]["text"]),
        "combo_font_size": opt_int("toolbar", "combo_font_size", ui["fonts"]["base_size"]),
        "combo_font_weight": opt_int("toolbar", "combo_font_weight", 400),
        "combo_text_color": opt("toolbar", "combo_text_color", ui["colors"]["text"]),
        "total_movies_bold": opt_bool("toolbar", "total_movies_bold", True),
        "total_movies_size": opt_int("toolbar", "total_movies_size", ui["fonts"]["base_size"]),
        "total_movies_weight": opt_int("toolbar", "total_movies_weight", 700),
        "total_movies_color": opt("toolbar", "total_movies_color", ui["colors"]["text"]),
    }

    ui["genres"] = {
        "item_h": opt_int("pane_genres", "item_h", 30),
    }

    ui["library"] = {
        "recursive_scan": req_bool("app", "recursive_scan"),
    }

    ui["fonts"] = {
        "base_family": req("app", "font_base_family"),
        "base_size": req_int("app", "font_base_size"),
    }

    ui["tmdb"] = {
        "api_key": req("app", "tmdb_api_key"),
        "language": req("app", "tmdb_language"),
    }

    ui["colors"] = {
        "bg": req("app", "color_bg"),
        "panel": req("app", "color_panel"),
        "raised": req("app", "color_raised"),
        "divider": req("app", "color_divider"),
        "text": req("app", "color_text"),
        "text2": req("app", "color_text2"),
        "teal": req("app", "color_teal"),
        "teal_hi": req("app", "color_teal_hi"),
        "input_bg": req("app", "color_input_bg"),
        "selection_bg": req("app", "color_selection_bg"),
        "selection_text": req("app", "color_selection_text"),
    }

    ui["radii"] = {
        "panel": req_int("app", "radii_panel"),
        "button": req_int("app", "radii_button"),
        "input": req_int("app", "radii_input"),
    }

    ui["movies_grid"] = {
        "poster_w": req_int("pane_movies", "poster_w"),
        "poster_h": req_int("pane_movies", "poster_h"),
        "cell_w": req_int("pane_movies", "cell_w"),
        "cell_h": req_int("pane_movies", "cell_h"),
        "spacing": req_int("pane_movies", "spacing"),
        "item_padding": req_int("pane_movies", "item_padding"),
        "cell_gap": req_int("pane_movies", "cell_gap"),
        "title_lines": req_int("pane_movies", "title_lines"),
        "title_font_size": req_int("pane_movies", "title_font_size"),
        "title_font_weight": opt_int("pane_movies", "title_font_weight", 600),
        "title_color": opt("pane_movies", "title_color", ui["colors"]["text"]),
        "year_font_size": opt_int("pane_movies", "year_font_size", req_int("pane_movies", "title_font_size")),
        "year_font_weight": opt_int("pane_movies", "year_font_weight", 400),
        "year_color": opt("pane_movies", "year_color", ui["colors"]["text"]),
        "title_line_gap": req_int("pane_movies", "title_line_gap"),
        "title_bottom_pad": req_int("pane_movies", "title_bottom_pad"),
    }

    ui["qss"] = {
        "extra": req("app", "qss_extra"),
    }

    # Optional details configuration (used by DetailsPane.apply_ui)
    ui["details"] = {
        "accent": opt("pane_details", "accent", ui["colors"]["teal"]),
        "title_size": opt_int("pane_details", "title_size", 24),
        "title_weight": opt_int("pane_details", "title_weight", 800),
        "title_color": opt("pane_details", "title_color", opt("pane_details", "accent", ui["colors"]["teal"])),
        "header_size": opt_int("pane_details", "header_size", 16),
        "header_weight": opt_int("pane_details", "header_weight", 800),
        "header_color": opt("pane_details", "header_color", opt("pane_details", "accent", ui["colors"]["teal"])),
        "body_size": opt_int("pane_details", "body_size", ui["fonts"]["base_size"] + 5),
        "studio_size": opt_int("pane_details", "studio_size", opt_int("pane_details", "body_size", ui["fonts"]["base_size"] + 5) + 2),
        "studio_weight": opt_int("pane_details", "studio_weight", 600),
        "poster_w": opt_int("pane_details", "poster_w", 220),
        "poster_h": opt_int("pane_details", "poster_h", 330),
        "layout_spacing": opt_int("pane_details", "layout_spacing", 8),
        "meta_spacing": opt_int("pane_details", "meta_spacing", 2),
        "info_top_margin": opt_int("pane_details", "info_top_margin", 0),
        "placeholder_text": opt("pane_details", "placeholder_text", "Select a movie"),
        "placeholder_size": opt_int("pane_details", "placeholder_size", 18),
        "placeholder_weight": opt_int("pane_details", "placeholder_weight", 800),
        "placeholder_color": opt("pane_details", "placeholder_color", opt("pane_details", "accent", ui["colors"]["teal"])),
        "meta_show_labels": opt_bool("pane_details", "meta_show_labels", False),
    }

    # Optional pane headers (Genres / Movies List / Details)
    ui["pane_headers"] = {
        "size": opt_int("app", "pane_header_size", ui["fonts"]["base_size"] + 2),
        "weight": opt_int("app", "pane_header_weight", 800),
        "color": opt("app", "pane_header_color", ui["colors"]["text"]),
    }

    return ui
def _sanitize_no_rounding(qss: str) -> str:
    # Force square corners everywhere (DEV request: no rounding for now)
    if not qss:
        return ""
    qss = re.sub(r"border-radius\s*:\s*\d+px\s*;", "border-radius: 0px;", qss, flags=re.IGNORECASE)
    qss = re.sub(r"border-radius\s*:\s*\d+\s*;", "border-radius: 0;", qss, flags=re.IGNORECASE)
    return qss




def _css_font(size: int, weight: int, color: str | None = None) -> str:
    parts = [f"font-size:{int(size)}px", f"font-weight:{int(weight)}"]
    if color:
        parts.append(f"color:{color}")
    return "; ".join(parts) + ";"

def apply_ui_theme(app: QApplication, ui: Dict[str, Any]) -> None:
    c = ui.get("colors", {}) or {}

    bg = c.get("bg", "#202224")
    panel = c.get("panel", "#2B2D2F")
    raised = c.get("raised", "#303336")
    divider = c.get("divider", "#3C3F42")
    text = c.get("text", "#E6E6E6")
    text2 = c.get("text2", "#B7B7B7")
    teal = c.get("teal", "#2AA7A1")
    teal_hi = c.get("teal_hi", "#4CD6CF")
    input_bg = c.get("input_bg", "#1D1F21")
    sel_bg = c.get("selection_bg", "#2B3A3D")
    sel_text = c.get("selection_text", "#E6E6E6")

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(bg))
    pal.setColor(QPalette.Base, QColor(raised))
    pal.setColor(QPalette.AlternateBase, QColor(panel))
    pal.setColor(QPalette.Button, QColor(panel))
    pal.setColor(QPalette.ToolTipBase, QColor(panel))
    pal.setColor(QPalette.ToolTipText, QColor(text))

    pal.setColor(QPalette.Text, QColor(text))
    pal.setColor(QPalette.WindowText, QColor(text))
    pal.setColor(QPalette.ButtonText, QColor(text))
    pal.setColor(QPalette.PlaceholderText, QColor(text2))

    pal.setColor(QPalette.Highlight, QColor(sel_bg))
    pal.setColor(QPalette.Link, QColor(teal))
    pal.setColor(QPalette.LinkVisited, QColor(teal_hi))

    app.setPalette(pal)

    # Minimal global QSS (square corners, readable list mode)
    qss = f"""
    QMainWindow {{ background: {bg}; }}
    QWidget {{ color: {text}; }}

    QPushButton, QComboBox, QLineEdit {{
        border: 1px solid {divider};
        border-radius: 0px;
        padding: 4px 8px;
        background: {panel};
        color: {text};
    }}
    QLineEdit {{
        background: {input_bg};
    }}
    QComboBox {{
        background: {panel};
    }}
    QPushButton:hover, QComboBox:hover, QLineEdit:hover {{
        border-color: {teal};
    }}
    QPushButton:pressed {{
        background: {sel_bg};
        border-color: {teal_hi};
    }}

    QListView, QListWidget {{
        background: {raised};
        border: 1px solid {divider};
    }}
    QListView::item:selected, QListWidget::item:selected {{
        background: {sel_bg};
        color: {sel_text};
    }}
    QSplitter::handle {{
        background: {divider};
    }}
    """

    # Scrollbars (thin + rounded, theme-aligned)
    sb = ui.get("scrollbars", {}) or {}
    sb_w = int(sb.get("w", 10))
    sb_r = int(sb.get("radius", 6))

    qss += f"""
    QScrollBar:vertical {{
        background: transparent;
        width: {sb_w}px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {divider};
        min-height: 24px;
        border-radius: {sb_r}px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {teal};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
        subcontrol-origin: margin;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}

    QScrollBar:horizontal {{
        background: transparent;
        height: {sb_w}px;
        margin: 0px;
    }}
    QScrollBar::handle:horizontal {{
        background: {divider};
        min-width: 24px;
        border-radius: {sb_r}px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {teal};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
        subcontrol-origin: margin;
    }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
        background: transparent;
    }}
    """

    extra = (ui.get("qss", {}) or {}).get("extra", "") or ""
    extra = _sanitize_no_rounding(extra)
    qss += "\n" + extra + "\n"

    app.setStyleSheet(qss)


class ScanFailuresDialog(QDialog):
    """Shows the contents of scan_failures so the user can inspect TMDB-not-found cases."""

    resolve_requested = Signal(dict)

    COLS = [
        ("Last seen", "last_seen"),
        ("Hits", "hit_count"),
        ("Wanted title", "want_title"),
        ("Wanted year", "want_year"),
        ("Smart title", "smart_title"),
        ("Eff. year", "effective_year"),
        ("Path", "file_path"),
    ]

    def __init__(self, db: "DB", tmdb: TMDBClient, ui: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        try:
            self.setStyleSheet(QApplication.instance().styleSheet())
        except Exception:
            pass
        self.db = db
        self.tmdb = tmdb
        self.ui = ui
        self._all_rows: List[Dict[str, Any]] = []

        self.setWindowTitle("TMDB Not Found (Scan Failures)")
        self.setMinimumSize(980, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter (title/path)…")
        self.filter_edit.setClearButtonEnabled(True)
        top.addWidget(QLabel("Filter:"))
        top.addWidget(self.filter_edit, 1)

        self.btn_refresh = QPushButton("Refresh")
        top.addWidget(self.btn_refresh)

        self.btn_copy_path = QPushButton("Copy Path")
        top.addWidget(self.btn_copy_path)

        self.btn_copy_tsv = QPushButton("Copy TSV")
        top.addWidget(self.btn_copy_tsv)

        self.btn_resolve = QPushButton("Resolve…")
        top.addWidget(self.btn_resolve)

        root.addLayout(top)

        # Debug/info line (helps verify we are reading the expected movies.db)
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.info_label)


        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels([h for (h, _) in self.COLS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self._apply_table_theme()

        # Navigation helpers (Stage A2.1)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.cellDoubleClicked.connect(self._on_table_double_clicked)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Stretch)

        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.btn_refresh.clicked.connect(self.reload)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.btn_copy_path.clicked.connect(self._copy_selected_path)
        self.btn_copy_tsv.clicked.connect(self._copy_tsv)
        self.btn_resolve.clicked.connect(self._on_resolve)


        self.reload()

    def _apply_table_theme(self) -> None:
        """Apply theme colors from ui.ini to the TMDB failures table (readability fix)."""
        c = (self.ui.get("colors", {}) or {})
        panel = c.get("panel", "#2B2D2F")
        raised = c.get("raised", "#303336")
        divider = c.get("divider", "#3C3F42")
        textc = c.get("text", "#E6E6E6")
        sel_bg = c.get("selection_bg", "#2B3A3D")
        sel_text = c.get("selection_text", "#E6E6E6")

        self.table.setStyleSheet(f"""
            QTableWidget {{
                background: {raised};
                alternate-background-color: {panel};
                color: {textc};
                gridline-color: {divider};
                selection-background-color: {sel_bg};
                selection-color: {sel_text};
                border: 1px solid {divider};
            }}
            QTableWidget::item {{
                padding: 2px 6px;
            }}
            QHeaderView::section {{
                background: {panel};
                color: {textc};
                padding: 4px 6px;
                border: 1px solid {divider};
            }}
            QTableCornerButton::section {{
                background: {panel};
                border: 1px solid {divider};
            }}
        """)

    def reload(self) -> None:

        self._all_rows = self.db.list_scan_failures(limit=5000)
        try:
            dbp = str(self.db.db_path)
            exists = os.path.exists(dbp)
            size = os.path.getsize(dbp) if exists else 0
        except Exception:
            dbp = str(getattr(self.db, 'db_path', ''))
            exists = False
            size = 0
        self.info_label.setText(f"DB: {dbp} | scan_failures rows: {len(self._all_rows)} | exists: {exists} | size: {size} bytes")
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = (self.filter_edit.text() or "").strip().lower()
        if needle:
            rows = [
                r
                for r in self._all_rows
                if needle in str(r.get("file_path") or "").lower()
                or needle in str(r.get("want_title") or "").lower()
                or needle in str(r.get("smart_title") or "").lower()
            ]
        else:
            rows = list(self._all_rows)
        self._populate(rows)

    def _populate(self, rows: List[Dict[str, Any]]) -> None:
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, (_, key) in enumerate(self.COLS):
                val = r.get(key)
                if key in ("last_seen",):
                    try:
                        val = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(val))) if val else ""
                    except Exception:
                        val = str(val or "")
                item = QTableWidgetItem(str(val if val is not None else ""))
                if j in (1, 3, 5):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, j, item)

        self.table.setSortingEnabled(False)

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return None
        idx = int(sel[0].row())
        # Rebuild current visible rows mapping by reading table Path column
        fp_item = self.table.item(idx, 6)
        fp = fp_item.text() if fp_item else ""
        for r in self._all_rows:
            if str(r.get("file_path") or "") == fp:
                return r
        return None


    def _on_resolve(self) -> None:
        row = self._selected_row()
        if not row:
            return
        self.resolve_requested.emit(row)

    def _copy_selected_path(self) -> None:
        r = self._selected_row()
        if not r:
            QMessageBox.information(self, APP_NAME, "Select a row first.")
            return
        QApplication.clipboard().setText(str(r.get("file_path") or ""))

    def _copy_tsv(self) -> None:
        # Copy current visible table content as TSV
        lines: List[str] = []
        headers = [h for (h, _) in self.COLS]
        lines.append("\t".join(headers))
        for i in range(self.table.rowCount()):
            vals = []
            for j in range(self.table.columnCount()):
                it = self.table.item(i, j)
                vals.append((it.text() if it else "").replace("\t", " ").replace("\n", " "))
            lines.append("\t".join(vals))
        QApplication.clipboard().setText("\n".join(lines))


    def _selected_path(self) -> str:
        row = self._selected_row()
        if not row:
            return ""
        return (row.get("file_path") or "").strip()

    def _open_folder_select_file(self, path: str) -> None:
        path = (path or "").strip()
        if not path:
            return
        try:
            p = os.path.normpath(path)
            if os.path.isdir(p):
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", p])
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(p))
                return

            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", p])
            else:
                folder = os.path.dirname(p) or p
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception:
            # We keep this silent: user can still copy the path.
            return

    def _open_file(self, path: str) -> None:
        path = (path or "").strip()
        if not path:
            return
        try:
            p = os.path.normpath(path)
            if sys.platform.startswith("win"):
                os.startfile(p)  # noqa: S606
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
        except Exception:
            return

    def _apply_menu_theme(self, menu: QMenu) -> None:
        """Apply ui.ini theme colors to context menus (avoid default light menu)."""
        c = (self.ui.get("colors", {}) or {})
        raised = c.get("raised", "#303336")
        divider = c.get("divider", "#3C3F42")
        textc = c.get("text", "#E6E6E6")
        sel_bg = c.get("selection_bg", "#2B3A3D")
        sel_text = c.get("selection_text", "#E6E6E6")
        menu.setStyleSheet(f"""
            QMenu {{
                background: {raised};
                color: {textc};
                border: 1px solid {divider};
            }}
            QMenu::item {{
                padding: 6px 12px;
                background: transparent;
            }}
            QMenu::item:selected {{
                background: {sel_bg};
                color: {sel_text};
            }}
            QMenu::separator {{
                height: 1px;
                background: {divider};
                margin: 4px 8px;
            }}
        """)

    def _on_table_context_menu(self, pos: QPoint) -> None:
        # Make sure the row under the cursor becomes current, so actions work reliably.
        try:
            item = self.table.itemAt(pos)
            if item is not None:
                self.table.setCurrentItem(item)
        except Exception:
            pass

        path = self._selected_path()

        menu = QMenu(self)
        self._apply_menu_theme(menu)

        act_open_folder = QAction("Open folder", self)
        act_open_file = QAction("Open file", self)
        act_copy_path = QAction("Copy full path", self)

        act_open_folder.setEnabled(bool(path))
        act_open_file.setEnabled(bool(path))
        act_copy_path.setEnabled(bool(path))

        act_open_folder.triggered.connect(lambda: self._open_folder_select_file(path))
        act_open_file.triggered.connect(lambda: self._open_file(path))
        act_copy_path.triggered.connect(lambda: QApplication.clipboard().setText(path))

        menu.addAction(act_open_folder)
        menu.addAction(act_open_file)
        menu.addSeparator()
        menu.addAction(act_copy_path)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_table_double_clicked(self, row: int, col: int) -> None:
        path = self._selected_path()
        self._open_folder_select_file(path)

class IssuesDialog(QDialog):
    """Aggregated 'Issues' view (Phase: Cleanup & Control Layer, Stage A1).

    Stage A1 scope:
    - Not Found (scan_failures)
    - Multi-part flagged (movies whose file_path looks like CD1/CD2/Disc1/Part1 etc.)
    - Possible duplicates (same normalized title + year appears more than once)
    """

    def __init__(self, db: "DB", tmdb: TMDBClient, ui: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        try:
            self.setStyleSheet(QApplication.instance().styleSheet())
        except Exception:
            pass

        self.db = db
        self.tmdb = tmdb
        self.ui = ui

        self.setWindowTitle("Issues")
        self.setMinimumSize(1050, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)

        self.btn_refresh = QPushButton("Refresh")
        top.addWidget(self.btn_refresh)

        self.btn_copy_tsv = QPushButton("Copy TSV")
        top.addWidget(self.btn_copy_tsv)

        top.addStretch(1)

        self.info_label = QLabel("")
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.info_label)

        root.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.categories = QListWidget()
        self.categories.setFixedWidth(220)
        self.categories.addItem("Not Found")
        self.categories.addItem("Multi-part")
        self.categories.addItem("Possible duplicates")
        body.addWidget(self.categories)

        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self._apply_table_theme()
        body.addWidget(self.table, 1)

        root.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.btn_refresh.clicked.connect(self.reload)
        self.btn_copy_tsv.clicked.connect(self._copy_tsv)
        self.categories.currentRowChanged.connect(self._on_category_changed)
        self.table.cellDoubleClicked.connect(self._on_table_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        self.categories.setCurrentRow(0)
        self.reload()


    def _apply_table_theme(self) -> None:
        """Apply theme colors from ui.ini to the issues table (readability fix)."""
        c = (self.ui.get("colors", {}) or {})
        panel = c.get("panel", "#2B2D2F")
        raised = c.get("raised", "#303336")
        divider = c.get("divider", "#3C3F42")
        text = c.get("text", "#E6E6E6")
        sel_bg = c.get("selection_bg", "#2B3A3D")
        sel_text = c.get("selection_text", "#E6E6E6")

        # Per-widget QSS to ensure headers/items have proper contrast regardless of OS theme.
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background: {raised};
                alternate-background-color: {panel};
                color: {text};
                gridline-color: {divider};
                selection-background-color: {sel_bg};
                selection-color: {sel_text};
                border: 1px solid {divider};
            }}
            QTableWidget::item {{
                padding: 2px 6px;
            }}
            QHeaderView::section {{
                background: {panel};
                color: {text};
                padding: 4px 6px;
                border: 1px solid {divider};
            }}
            QTableCornerButton::section {{
                background: {panel};
                border: 1px solid {divider};
            }}
        """)

    # ---------- navigation helpers (Stage A2.1) ----------
    def _row_path(self, row: int) -> str:
        """Return best-effort file path for a given table row (if available)."""
        if row < 0:
            return ""
        col_count = self.table.columnCount()
        if col_count <= 0:
            return ""
        # Find the 'Path' column by header (fallback to last column).
        path_col = col_count - 1
        try:
            for c in range(col_count):
                h = self.table.horizontalHeaderItem(c)
                if h and (h.text() or "").strip().lower() == "path":
                    path_col = c
                    break
        except Exception:
            pass

        item = self.table.item(row, path_col)
        raw = (item.text() if item else "").strip()
        if not raw:
            return ""
        # For duplicates we join paths with " | " — take the first usable one.
        for part in raw.replace("\n", " ").split(" | "):
            p = part.strip()
            if p:
                return p
        return ""

    def _current_row_path(self) -> str:
        """Return best-effort file path for the currently selected row (if available)."""
        return self._row_path(self.table.currentRow())


    def _open_folder_select_file(self, path: str) -> None:
        """Open file's folder (and select the file) in the OS file manager."""
        path = (path or "").strip()
        if not path:
            return
        try:
            p = os.path.normpath(path)
            # If path is a directory, open it directly.
            if os.path.isdir(p):
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", p])
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(p))
                return

            # Otherwise assume file path.
            folder = os.path.dirname(p) or p
            if sys.platform.startswith("win"):
                # explorer expects: /select,<path>
                subprocess.Popen(["explorer", "/select,", p])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception:
            # Non-fatal; keep UI responsive.
            pass

    def _open_file(self, path: str) -> None:
        """Open the file with the OS default application."""
        path = (path or "").strip()
        if not path:
            return
        try:
            p = os.path.normpath(path)
            if sys.platform.startswith("win"):
                os.startfile(p)  # type: ignore[attr-defined]
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))
        except Exception:
            pass

    def _copy_path_to_clipboard(self, path: str) -> None:
        try:
            cb = QApplication.clipboard()
            cb.setText(path or "")
        except Exception:
            pass

    def _on_table_double_clicked(self, row: int, _col: int) -> None:
        path = self._current_row_path()
        self._open_folder_select_file(path)

    def _apply_menu_theme(self, menu: QMenu) -> None:
        """Apply ui.ini theme colors to context menus (so it isn't the default light menu)."""
        c = (self.ui.get("colors", {}) or {})
        panel = c.get("panel", "#2B2D2F")
        raised = c.get("raised", "#303336")
        divider = c.get("divider", "#3C3F42")
        text = c.get("text", "#E6E6E6")
        sel_bg = c.get("selection_bg", "#2B3A3D")
        sel_text = c.get("selection_text", "#E6E6E6")
        menu.setStyleSheet(f"""
            QMenu {{
                background: {raised};
                color: {text};
                border: 1px solid {divider};
            }}
            QMenu::item {{
                padding: 6px 18px;
                background: transparent;
            }}
            QMenu::item:selected {{
                background: {sel_bg};
                color: {sel_text};
            }}
            QMenu::separator {{
                height: 1px;
                background: {divider};
                margin: 4px 8px;
            }}
        """)

    def _on_table_context_menu(self, pos) -> None:
        # Ensure right-click also targets/selects the row under the cursor.
        row = -1
        try:
            it = self.table.itemAt(pos)
            row = it.row() if it else -1
        except Exception:
            row = -1
        if row >= 0:
            self.table.setCurrentCell(row, 0)

        path = self._row_path(row if row >= 0 else self.table.currentRow())

        menu = QMenu(self)
        self._apply_menu_theme(menu)

        act_open_folder = menu.addAction("Open folder")
        act_open_file = menu.addAction("Open file")
        menu.addSeparator()
        act_copy = menu.addAction("Copy full path")

        has_path = bool(path)
        act_open_folder.setEnabled(has_path)
        act_open_file.setEnabled(has_path)
        act_copy.setEnabled(has_path)

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_open_folder:
            self._open_folder_select_file(path)
        elif chosen is act_open_file:
            self._open_file(path)
        elif chosen is act_copy:
            self._copy_path_to_clipboard(path)


    def reload(self) -> None:
        # Refresh info line (helps verify DB used)
        try:
            dbp = str(self.db.db_path)
            exists = os.path.exists(dbp)
            size = os.path.getsize(dbp) if exists else 0
        except Exception:
            dbp = str(getattr(self.db, "db_path", ""))
            exists = False
            size = 0

        # Compute counts (lightweight; list_movies is already fast for typical libraries)
        try:
            nf = int(self.db.count_scan_failures())
        except Exception:
            nf = 0

        try:
            movies = self.db.list_movies(sort_key="title")
        except Exception:
            movies = []

        mp = sum(1 for m in movies if getattr(m, "file_path", None) and _looks_like_multipart_release(m.file_path))
        dup = 0
        try:
            buckets: Dict[Tuple[str, Optional[int]], int] = {}
            for m in movies:
                k = (norm_title(m.title or ""), m.year)
                if not k[0]:
                    continue
                buckets[k] = buckets.get(k, 0) + 1
            dup = sum(1 for _k, c in buckets.items() if c > 1)
        except Exception:
            dup = 0

        self.info_label.setText(f"DB: {dbp} | exists: {exists} | size: {size} bytes | Not Found: {nf} | Multi-part: {mp} | Duplicates: {dup}")

        # Re-render current category
        self._render_category(self._current_category_name())

    def _current_category_name(self) -> str:
        it = self.categories.currentItem()
        return str(it.text()) if it else "Not Found"

    def _on_category_changed(self, *_):
        self._render_category(self._current_category_name())

    def _render_category(self, cat: str) -> None:
        cat = (cat or "").strip()

        if cat == "Not Found":
            cols = [
                ("Last seen", "last_seen"),
                ("Hits", "hit_count"),
                ("Wanted title", "want_title"),
                ("Wanted year", "want_year"),
                ("Smart title", "smart_title"),
                ("Eff. year", "effective_year"),
                ("Path", "file_path"),
            ]
            rows = self.db.list_scan_failures(limit=5000)

            self._set_columns([c[0] for c in cols])
            self.table.setRowCount(len(rows))

            for i, r in enumerate(rows):
                for j, (_h, key) in enumerate(cols):
                    val = r.get(key)
                    if key in ("last_seen",):
                        try:
                            val = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(val))) if val else ""
                        except Exception:
                            val = str(val or "")
                    item = QTableWidgetItem(str(val if val is not None else ""))
                    if j in (1, 3, 5):
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(i, j, item)

            self._autosize_not_found()

        elif cat == "Multi-part":
            cols = ["Title", "Year", "TMDB ID", "Path"]
            movies = self.db.list_movies(sort_key="title")
            rows = [m for m in movies if getattr(m, "file_path", None) and _looks_like_multipart_release(m.file_path)]

            self._set_columns(cols)
            self.table.setRowCount(len(rows))

            for i, m in enumerate(rows):
                self.table.setItem(i, 0, QTableWidgetItem(str(m.title or "")))
                self.table.setItem(i, 1, QTableWidgetItem(str(m.year or "")))
                self.table.setItem(i, 2, QTableWidgetItem(str(m.tmdb_id or "")))
                self.table.setItem(i, 3, QTableWidgetItem(str(m.file_path or "")))

            self._autosize_generic()

        elif cat == "Possible duplicates":
            cols = ["Title", "Year", "Count", "TMDB IDs", "Paths"]
            movies = self.db.list_movies(sort_key="title")

            buckets: Dict[Tuple[str, Optional[int]], List[MovieRow]] = {}
            for m in movies:
                k = (norm_title(m.title or ""), m.year)
                if not k[0]:
                    continue
                buckets.setdefault(k, []).append(m)

            dup_rows: List[List[str]] = []
            for (_k, items) in buckets.items():
                if len(items) <= 1:
                    continue
                items_sorted = sorted(items, key=lambda x: (x.year or 0, (x.title or "").lower(), int(x.tmdb_id)))
                title_disp = items_sorted[0].title or ""
                year_disp = str(items_sorted[0].year or "")
                ids = ", ".join(str(it.tmdb_id) for it in items_sorted)
                paths = " | ".join(str(getattr(it, "file_path", "") or "") for it in items_sorted)
                dup_rows.append([title_disp, year_disp, str(len(items_sorted)), ids, paths])

            self._set_columns(cols)
            self.table.setRowCount(len(dup_rows))

            for i, r in enumerate(dup_rows):
                for j, val in enumerate(r):
                    item = QTableWidgetItem(str(val))
                    if j == 2:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(i, j, item)

            self._autosize_generic()

        else:
            self._set_columns(["Info"])
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("No data for this category yet."))
            self._autosize_generic()

    def _set_columns(self, headers: List[str]) -> None:
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(list(headers))

    def _autosize_not_found(self) -> None:
        hh = self.table.horizontalHeader()
        # Similar to ScanFailuresDialog layout
        try:
            hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(2, QHeaderView.Stretch)
            hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(4, QHeaderView.Stretch)
            hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(6, QHeaderView.Stretch)
        except Exception:
            pass

    def _autosize_generic(self) -> None:
        hh = self.table.horizontalHeader()
        try:
            for i in range(self.table.columnCount()):
                hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            if self.table.columnCount() >= 1:
                hh.setSectionResizeMode(0, QHeaderView.Stretch)
            if self.table.columnCount() >= 4:
                hh.setSectionResizeMode(self.table.columnCount() - 1, QHeaderView.Stretch)
        except Exception:
            pass

    def _copy_tsv(self) -> None:
        lines: List[str] = []
        headers = []
        for j in range(self.table.columnCount()):
            headers.append(self.table.horizontalHeaderItem(j).text() if self.table.horizontalHeaderItem(j) else f"Col{j+1}")
        lines.append("\t".join(headers))
        for i in range(self.table.rowCount()):
            vals = []
            for j in range(self.table.columnCount()):
                it = self.table.item(i, j)
                vals.append((it.text() if it else "").replace("\t", " ").replace("\n", " "))
            lines.append("\t".join(vals))
        QApplication.clipboard().setText("\n".join(lines))

# ---------------- helpers ----------------
def iso_to_flag(iso: str) -> str:
    if not iso or len(iso) != 2:
        return ""
    iso = iso.upper()
    return chr(ord(iso[0]) + 127397) + chr(ord(iso[1]) + 127397)


def norm_title(s: str) -> str:
    s = (s or "").lower().strip()
    s = ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch))
    for ch in [":", "-", "_", ".", ",", "’", "'"]:
        s = s.replace(ch, " ")
    s = " ".join(s.split())
    for art in ("the ", "a ", "an "):
        if s.startswith(art):
            s = s[len(art):]
            break
    return s



def smart_parse_title_year_from_path(path: str, max_parent_levels: int = 4):
    """Extract (title, year) from a movie file path.

    Key rules (to avoid the regressions you reported):
    - Years are ONLY trusted when explicitly present as (YYYY) / [YYYY] / {YYYY} or as a standalone token.
    - Pure numeric titles like "1917" or "21" are treated as TITLES, not years.
    - We prefer the most-right explicit year token (usually closest to the filename end).
    """
    import os
    import re

    p = (path or "").replace("/", os.sep)
    parts = [x for x in p.split(os.sep) if x]
    if not parts:
        return "", None, 0
    # Candidate strings: filename (without ext) + a few parent folders (closest first)
    filename = os.path.splitext(parts[-1])[0]
    parents = []
    for i in range(1, min(max_parent_levels + 1, len(parts))):
        parents.append(parts[-1 - i])

    candidates = [filename] + parents

    # Normalize separators early
    def normalize_seps(s: str) -> str:
        s = s.replace("_", " ").replace(".", " ").replace("-", " ")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    # Explicit year patterns: (1999) [1999] {1999}
    year_pat = re.compile(r"[\(\[\{]\s*((?:19\d{2})|(?:20\d{2}))\s*[\)\]\}]")

    # Also accept standalone year token surrounded by spaces (but NOT as part of another number/word)
    year_token_pat = re.compile(r"(?:^|\s)((?:19\d{2})|(?:20\d{2}))(?:\s|$)")

    found_bracketed = []  # list of (year_int, where_index, pos_in_string, src_string)
    found_tokens = []     # list of (year_int, where_index, pos_in_string, src_string)
    for ci, s in enumerate(candidates):
        s2 = normalize_seps(s)
        for m in year_pat.finditer(s2):
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                found_bracketed.append((y, ci, m.start(), s2))
        # standalone tokens (lower priority than bracketed years)
        for m in year_token_pat.finditer(s2):
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                found_tokens.append((y, ci, m.start(), s2))

    chosen_year = None
    if found_bracketed:
        # Prefer bracketed years even if a title contains a year-like token (e.g. "Legend of 1900 (1998)").
        # Within bracketed: closest to filename (ci small), then right-most occurrence (pos high).
        found_bracketed.sort(key=lambda t: (t[1], -t[2]))
        chosen_year = found_bracketed[0][0]
    elif found_tokens:
        # No bracketed year anywhere; fall back to standalone year tokens.
        found_tokens.sort(key=lambda t: (t[1], -t[2]))
        chosen_year = found_tokens[0][0]

    # Build a working title seed: start from filename, then parents if filename is too short
    seed = normalize_seps(filename)
    if len(seed) < 3:
        for p2 in parents:
            t = normalize_seps(p2)
            if len(t) >= 3:
                seed = t
                break

    # Remove explicit year tokens ONLY if they match chosen_year
    if chosen_year:
        seed = re.sub(rf"[\(\[\{{]]\s*{chosen_year}\s*[\)\]\}}]", " ", seed)
        seed = re.sub(rf"(?:^|\s){chosen_year}(?:\s|$)", " ", seed)

    # Drop common junk tokens (keep numbers unless clearly quality/junk)
    junk_tokens = {
        "1080p", "720p", "2160p", "4k", "x264", "x265", "h264", "h265",
        "hevc", "aac", "ac3", "dts", "ddp", "brrip", "bdrip", "bluray", "blu", "ray",
        "webrip", "web", "hdrip", "dvdrip", "dvdscr", "cam", "remastered", "extended",
        "yify", "rarbg", "etrg", "proper", "repack",
    }

    # Also drop CD1/CD2 tokens etc
    seed = re.sub(r"\b(cd|disc)\s*\d+\b", " ", seed, flags=re.I)
    seed = re.sub(r"\bpart\s*\d+\b", " ", seed, flags=re.I)

    toks = []
    for tok in seed.split():
        t = tok.strip()
        if not t:
            continue
        tl = t.lower()
        # remove bracket leftovers
        tl = tl.strip("[](){}")

        if tl in junk_tokens:
            continue
        # remove pure codec-ish gibberish like "xvid" / "hdtv"
        if tl in {"xvid", "hdtv", "webdl", "web-dl", "bdr", "dvdr"}:
            continue
        # keep numeric titles: "1917", "21", "8"
        toks.append(t)

    title = " ".join(toks)
    title = re.sub(r"\s+", " ", title).strip()

    # If title ends up empty, fall back to folder name
    if not title:
        for p2 in parents:
            t = normalize_seps(p2)
            if chosen_year:
                t = re.sub(rf"[\(\[\{{]]\s*{chosen_year}\s*[\)\]\}}]", " ", t)
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                title = t
                break

    # Final safety cleanup: don't allow title == str(year) (common with swapped parse)
    if chosen_year and title.isdigit() and int(title) == int(chosen_year):
        # in this rare case, prefer filename without year removal
        title = normalize_seps(filename)
    year_hits = (len(found_bracketed) + len(found_tokens))
    return title, chosen_year, year_hits
def strip_trailing_roman_numeral(title: str) -> str:
    t = (title or "").strip()
    m = _ROMAN_END_RE.match(t)
    if not m:
        return t
    return (m.group("base") or "").strip()


def clean_tmdb_query_title(title: str) -> str:
    """Prepare a query title for TMDB search (no year suffix, no trailing junk tokens)."""
    t = (title or "").strip()
    # Remove trailing year like "Title (1999)"
    t = re.sub(r"\s*\(\s*\d{4}\s*\)\s*$", "", t).strip()
    # Remove trailing [...] tokens (resolution, tags) if any survived upstream cleaning.
    t = re.sub(r"\s*\[[^\]]+\]\s*$", "", t).strip()
    # Common collection naming: "Title 1" for the first movie. Drop the trailing " 1"
    # only when it's the *only* digit present in the title.
    if re.search(r"\s+1$", t) and not re.search(r"\d", t[:-1]):
        t = t[:-2].strip()
    return t



def _clean_folder_title(txt: str) -> str:
    s = (txt or "").strip()
    if not s:
        return ""
    # drop leading track numbers like "01 " or "01."
    s = re.sub(r"^\s*(?:0\d{1,2}|\d{2,3})[\.\)\-_\s]+", "", s)
    s = re.sub(r"^\s*\d[\.\)\-_]+\s*", "", s)
    # remove bracketed groups without years (common tags)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\{[^}]*\}", " ", s)
    # normalize separators
    s = s.replace(".", " ").replace("_", " ").replace("-", " ")
    s = " ".join(s.split())
    return s.strip()

def extract_title_year_from_folders(fp: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Try to derive (title, year, movie_root_folder) from parent folders.

    - Prefers the nearest folder that contains a year marker like "(2011)" or "2011".
    - If the file is inside CD1/CD2 style folders, we treat the *movie folder* as the parent of CD1/CD2.
    Returns: (title, year, movie_root_folder_path)
    """
    try:
        # Be robust to mixed path separators (we sometimes store paths with '/' even on Windows).
        fp_norm = os.path.normpath(fp)
        parts = re.split(r"[\\/]+", fp_norm)
    except Exception:
        return None, None, None
    if len(parts) < 2:
        return None, None, None

    # exclude filename
    folders = parts[:-1]

    # If inside CD1/CD2 (or Disc1/Disc2), shift the search base one level up
    cd_idx = None
    for i in range(len(folders) - 1, -1, -1):
        p = (folders[i] or "").strip()
        if re.fullmatch(r"(cd|disc)\s*\d+", p, re.IGNORECASE) or re.fullmatch(r"cd\d+", p, re.IGNORECASE):
            cd_idx = i
            break

    # movie_root = parent of CD folder, otherwise the immediate parent folder
    movie_root = None
    if cd_idx is not None and cd_idx - 1 >= 0:
        movie_root = os.sep.join(folders[:cd_idx])  # up to parent of CDx
    else:
        movie_root = None

    # Search backwards for best "Title (YYYY)" / "Title YYYY" folder
    best_title = None
    best_year = None

    for i in range(len(folders) - 1, -1, -1):
        name = (folders[i] or "").strip()
        if not name:
            continue

        # ignore technical folders
        if re.fullmatch(r"(subs?|subtitles?|sample|extras?)", name, re.IGNORECASE):
            continue
        if re.fullmatch(r"(cd|disc)\s*\d+", name, re.IGNORECASE) or re.fullmatch(r"cd\d+", name, re.IGNORECASE):
            continue

        m = re.search(r"\((19\d{2}|20\d{2})\)", name)
        y = None
        if m:
            y = int(m.group(1))
            t = re.sub(r"\((19\d{2}|20\d{2})\)", " ", name).strip()
            t = _clean_folder_title(t)
        else:
            # standalone year token
            m2 = re.search(r"\b(19\d{2}|20\d{2})\b", name)
            if m2:
                y = int(m2.group(1))
                t = re.sub(r"\b(19\d{2}|20\d{2})\b", " ", name).strip()
                t = _clean_folder_title(t)
            else:
                continue

        if t and (1900 <= y <= 2029):
            best_title, best_year = t, y
            break

    return best_title, best_year, movie_root

def is_junk_wanted_title(title: str) -> bool:
    """Return True when the extracted 'wanted title' is clearly unusable noise.

    This is used only as a fallback trigger. It must NOT reject legitimate short titles
    (e.g. '21', 'Pi', '1917', '2012', 'FIST') or titles without spaces.
    """
    t = (title or "").strip()
    if not t:
        return True

    # obvious release-group / filename residue patterns
    if re.fullmatch(r"[A-Za-z0-9]+-[A-Za-z0-9]+", t):  # e.g. YTS-MX, WEBRip-x265
        return True
    if re.fullmatch(r"(?:www\.)?[A-Za-z0-9_-]+\.(?:com|net|org|ws|mx)", t, flags=re.I):
        return True

    # extreme noise: very long token without vowels and no spaces (rare but helps)
    if " " not in t and len(t) >= 25 and not re.search(r"[AEIOUYaeiouy]", t):
        return True

    return False

def _extract_double_year_pattern_from_basename(fp: str) -> Tuple[Optional[int], Optional[int]]:
    """Detect filenames that start with two 4-digit year-like numbers, e.g. '1917.2019.1080p...'
    Returns (first_number, second_number) if detected, else (None, None).
    This helps numeric-title movies like 1917 (2019) and 2012 (2009).
    """
    try:
        base = os.path.basename(fp)
    except Exception:
        return None, None
    # strip extension
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", base)
    # normalize separators to spaces
    norm = re.sub(r"[\._\-\[\]\(\)\{\}]+", " ", base)
    # Look for two 4-digit numbers near the start
    m = re.match(r"\s*(\d{4})\s+(\d{4})\b", norm)
    if not m:
        return None, None
    a = int(m.group(1))
    b = int(m.group(2))
    if 1900 <= a <= 2029 and 1900 <= b <= 2029:
        return a, b
    return None, None



def derive_scan_query(fp: str, parsed_title: str, parsed_year: Optional[int]) -> Tuple[str, Optional[int], Optional[str]]:
    """Decide the effective (title, year) to query TMDB with, plus movie_root for dedupe."""
    folder_title, folder_year, movie_root = extract_title_year_from_folders(fp)

    title = (parsed_title or "").strip()
    year = parsed_year

    # If the filename contains a bare 4-digit token that looks like a year (e.g. "Legend of 1900")
    # it may be part of the *story/title* and not the release year.
    # When a proper folder year exists as "(YYYY)" and conflicts, prefer the folder year
    # unless the basename explicitly brackets the year.
    base_noext = os.path.splitext(os.path.basename(fp))[0]
    basename_has_bracketed_year = False
    if year is not None:
        basename_has_bracketed_year = re.search(r"[\(\[]\s*%d\s*[\)\]]" % int(year), base_noext) is not None

    def _numeric_title_ok(t: str, y: Optional[int]) -> bool:
        s = (t or "").strip()
        if not s or not s.isdigit():
            return False
        if y is None:
            # allow numeric-only titles like "300"
            return True
        if s == str(y):
            # pure year-as-title is usually junk
            return False
        return True


    # Numeric-title movies can look like "1917.2019.1080p..." where a naive parser swaps title/year.
    # If the basename starts with two year-like numbers, treat the first as title and the second as year.
    a_num, b_num = _extract_double_year_pattern_from_basename(fp)
    if a_num is not None and b_num is not None:
        title = str(a_num)
        year = b_num


    if folder_year is not None and (year is None or year < 1900 or year > 2029):
        year = folder_year

    # Conflict resolution: folder "(YYYY)" beats a bare basename token.
    if folder_year is not None and year is not None and year != folder_year and not basename_has_bracketed_year:
        year = folder_year

    if folder_title and ((is_junk_wanted_title(title) and not _numeric_title_ok(title, folder_year or year)) or year is None or folder_year is not None):
        # folder title usually better than filename tokens (wal-oceans12, dmd-warrior-cd1, etc)
        title = folder_title

    # If still junk, fall back to smart filename parser
    if is_junk_wanted_title(title) and not _numeric_title_ok(title, year):
        smart_title, smart_year, _ = smart_parse_title_year_from_path(fp)
        if smart_title:
            title = smart_title
        if smart_year is not None and (year is None or year == 1917):
            year = smart_year

    return title, year, movie_root


def score_tmdb_result(want_title: str, want_year: int | None, rd: dict) -> float:
    """Deterministic TMDB result scoring.
    Priorities:
      1) Exact normalized title match
      2) Strong year match / strong year mismatch penalty
      3) Token overlap
      4) Popularity / votes as a light tie-breaker
    """
    import math
    import unicodedata
    import re

    def strip_diacritics(s: str) -> str:
        # Turn “Brüno” -> “Bruno”
        s = unicodedata.normalize("NFKD", s)
        return "".join(ch for ch in s if not unicodedata.combining(ch))

    def norm(s: str) -> str:
        s = strip_diacritics((s or "").lower())
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    wt = norm(want_title)
    rt = norm(rd.get("title") or rd.get("name") or "")
    orig = strip_diacritics((rd.get("title") or rd.get("name") or "").lower())

    score = 0.0

    # --- Title matching ---
    if wt and rt and wt == rt:
        score += 6000.0  # exact title match wins almost always
    else:
        # token overlap
        wset = set(wt.split()) if wt else set()
        rset = set(rt.split()) if rt else set()
        if wset and rset:
            overlap = len(wset & rset) / max(1, len(wset))
            score += overlap * 1200.0

        # substring heuristics (useful for “The ...” variants)
        if wt and rt and (wt in rt or rt in wt):
            score += 450.0

        # prefix match bonus (handles “Iron Man” vs “Ironman” etc.)
        if wt and rt and (rt.startswith(wt) or wt.startswith(rt)):
            score += 250.0

    # --- Year boosting / penalty ---
    result_year = None
    rdate = (rd.get("release_date") or rd.get("first_air_date") or "").strip()
    if len(rdate) >= 4 and rdate[:4].isdigit():
        result_year = int(rdate[:4])

    if want_year and result_year:
        diff = abs(result_year - int(want_year))
        if diff == 0:
            score += 2200.0
        elif diff == 1:
            score += 650.0
        elif diff <= 2:
            score += 250.0
        else:
            # big penalty for mismatched years (prevents “Zone of the Dead” beating “2012”)
            score -= min(2600.0, 800.0 + diff * 80.0)
    elif want_year and result_year is None:
        score -= 120.0  # mild penalty if we wanted a year but TMDB has no date

    # --- Popularity / votes (weak tie-breakers) ---
    pop = float(rd.get("popularity") or 0.0)
    votes = float(rd.get("vote_count") or 0.0)
    score += math.log10(1.0 + pop) * 35.0
    score += math.log10(1.0 + votes) * 25.0

    # small bonus if original title contains the wanted title as plain text
    if want_title and strip_diacritics(want_title.lower()) in orig:
        score += 80.0

    return float(score)


class DB:
    def __init__(self, db_path: str):
        self.db_path = db_path


    def _sf_connect(self):
        """Dedicated sqlite connection for scan_failures.
        We intentionally bypass database.db_session because some setups may not commit/refresh
        immediately (leading to an empty Not Found window).
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute('PRAGMA busy_timeout=5000;')
        try:
            conn.execute('PRAGMA journal_mode=WAL;')
        except Exception:
            pass
        conn.row_factory = sqlite3.Row
        return conn

    def list_movies(self, sort_key: str = "title") -> List[MovieRow]:
        sort_sql = {
            "title": "m.title COLLATE NOCASE ASC",
            "user_rating": "m.user_rating DESC, m.title COLLATE NOCASE ASC",
            "year_desc": "m.year DESC, m.title COLLATE NOCASE ASC",
            "year_asc": "m.year ASC, m.title COLLATE NOCASE ASC",
            "recent": "m.date_added DESC, m.title COLLATE NOCASE ASC",
        }.get(sort_key, "m.title COLLATE NOCASE ASC")

        sql = f"""
        SELECT
            m.tmdb_id, m.title, m.year, COALESCE(m.overview, '') AS overview,
            m.runtime, m.poster_path, m.backdrop_path,
            m.lang_original, m.studio, m.tmdb_rating, m.tmdb_votes,
            m.user_rating, m.watched, m.date_added,
            pg.name AS primary_genre,
            COALESCE((
                SELECT group_concat(g.name, ', ')
                FROM movie_genres mg
                JOIN genres g ON g.id = mg.genre_id
                WHERE mg.movie_id = m.id
                ORDER BY mg.pos ASC
            ), '') AS genres_str,
            m.edition,
            m.color_mode,
            m.file_path
        FROM movies m
        LEFT JOIN genres pg ON pg.id = m.primary_genre_id
        ORDER BY {sort_sql};
        """

        with db_session(self.db_path) as conn:
            rows = conn.execute(sql).fetchall()

        out: List[MovieRow] = []
        for r in rows:
            out.append(
                MovieRow(
                    tmdb_id=int(r["tmdb_id"]),
                    title=str(r["title"]),
                    year=(int(r["year"]) if r["year"] is not None else None),
                    overview=str(r["overview"] or ""),
                    runtime=(int(r["runtime"]) if r["runtime"] is not None else None),
                    poster_path=(str(r["poster_path"]) if r["poster_path"] else None),
                    backdrop_path=(str(r["backdrop_path"]) if r["backdrop_path"] else None),
                    lang_original=(str(r["lang_original"]) if r["lang_original"] else None),
                    studio=(str(r["studio"]) if r["studio"] else None),
                    tmdb_rating=(float(r["tmdb_rating"]) if r["tmdb_rating"] is not None else None),
                    tmdb_votes=(int(r["tmdb_votes"]) if r["tmdb_votes"] is not None else None),
                    user_rating=(int(r["user_rating"]) if r["user_rating"] is not None else 0),
                    watched=int(r["watched"] or 0),
                    date_added=int(r["date_added"]),
                    primary_genre=(str(r["primary_genre"]) if r["primary_genre"] else None),
                    genres_str=str(r["genres_str"] or ""),
                    edition=str(r["edition"] or "Standard"),
                    color_mode=str(r["color_mode"] or "unknown"),
                    file_path=(str(r["file_path"]) if "file_path" in r.keys() and r["file_path"] else None),
                )
            )
        return out

    def file_exists(self, file_path: str) -> bool:
        with db_session(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM movies WHERE file_path = ? LIMIT 1;", (file_path,)).fetchone()
        return bool(row)

    def tmdb_exists(self, tmdb_id: int) -> bool:
        with db_session(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM movies WHERE tmdb_id = ? LIMIT 1;", (int(tmdb_id),)).fetchone()
        return bool(row)


    def get_file_path_by_tmdb_id(self, tmdb_id: int) -> Optional[str]:
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "SELECT file_path FROM movies WHERE tmdb_id=? LIMIT 1;",
                (int(tmdb_id),),
            ).fetchone()
        if row and row["file_path"]:
            return str(row["file_path"])
        return None

    def find_tmdb_id_by_title_year(self, title: str, year: int) -> Optional[int]:
        """Best-effort lookup for an existing movie by (title, year).
        Returns tmdb_id only when the match is unambiguous (exactly one row).
        Matching is case-insensitive on title.
        """
        tt = (title or "").strip()
        if not tt:
            return None
        try:
            yy = int(year)
        except Exception:
            return None

        with db_session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT tmdb_id FROM movies WHERE year=? AND lower(title)=lower(?) LIMIT 2;",
                (yy, tt),
            ).fetchall()

        if len(rows) == 1 and rows[0] and rows[0]["tmdb_id"] is not None:
            try:
                return int(rows[0]["tmdb_id"])
            except Exception:
                return None
        return None

    def update_movie_file_path_by_tmdb_id(self, tmdb_id: int, new_file_path: str) -> None:
        with db_session(self.db_path) as conn:
            conn.execute(
                "UPDATE movies SET file_path=? WHERE tmdb_id=?;",
                (str(new_file_path), int(tmdb_id)),
            )


    def upsert_scan_failure(
        self,
        file_path: str,
        want_title: str,
        want_year: Optional[int],
        smart_title: str,
        effective_year: Optional[int],
        attempted_variants: List[Tuple[str, Optional[int]]],
        reason: str = "no_tmdb_results",
    ) -> None:
        """Append/update a scan failure record so we can diagnose bad parsing/search queries later."""
        fp = str(file_path)
        fn = os.path.basename(fp)
        now = int(time.time())
        variants_json = json.dumps(
            [{"title": t, "year": y} for (t, y) in attempted_variants],
            ensure_ascii=False,
        )
        # IMPORTANT: use a dedicated sqlite3 connection + explicit commit.
        # We intentionally bypass database.db_session here because some environments ended up
        # not persisting these rows (Not Found window stayed empty).
                # IMPORTANT: use a dedicated sqlite3 connection + explicit commit (with retries).
        # Also apply busy_timeout/WAL in _sf_connect to reduce "database is locked" failures.
        last_err: Optional[Exception] = None
        for _attempt in range(3):
            conn = self._sf_connect()
            try:
                conn.execute(
                    """
                    INSERT INTO scan_failures (
                        file_path, file_name, want_title, want_year, smart_title, effective_year,
                        variants_json, reason, first_seen, last_seen, hit_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(file_path) DO UPDATE SET
                        file_name=excluded.file_name,
                        want_title=excluded.want_title,
                        want_year=excluded.want_year,
                        smart_title=excluded.smart_title,
                        effective_year=excluded.effective_year,
                        variants_json=excluded.variants_json,
                        reason=excluded.reason,
                        last_seen=excluded.last_seen,
                        hit_count=hit_count + 1;
                    """,
                    (
                        fp,
                        fn,
                        str(want_title or ""),
                        int(want_year) if want_year is not None else None,
                        str(smart_title or ""),
                        int(effective_year) if effective_year is not None else None,
                        variants_json,
                        str(reason or "no_tmdb_results"),
                        now,
                        now,
                    ),
                )
                conn.commit()
                return
            except Exception as e:
                last_err = e
                # small backoff for transient locks
                try:
                    time.sleep(0.05)
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        # If we got here, all attempts failed.
        if last_err is not None:
            raise last_err

    def count_scan_failures(self) -> int:
        conn = self._sf_connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM scan_failures;").fetchone()
            return int(row["c"] or 0) if row else 0
        finally:
            conn.close()

    def list_scan_failures(self, limit: int = 1000) -> List[Dict[str, Any]]:
        lim = max(1, int(limit))
        conn = self._sf_connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    file_path, file_name,
                    want_title, want_year,
                    smart_title, effective_year,
                    reason, first_seen, last_seen, hit_count,
                    variants_json
                FROM scan_failures
                ORDER BY last_seen DESC
                LIMIT ?;
                """,
                (lim,),
            ).fetchall()
        finally:
            conn.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({k: r[k] for k in r.keys()})
        return out


    def delete_scan_failure(self, file_path: str) -> None:
        fp = str(file_path or "")
        if not fp:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM scan_failures WHERE file_path=?;", (fp,))
            conn.commit()
        finally:
            conn.close()

    def update_user_fields(self, tmdb_id: int, user_rating: int, watched: int, edition: str, color_mode: str) -> None:
        user_rating = int(user_rating)
        if user_rating < 0 or user_rating > 5:
            raise ValueError("user_rating must be 0..5")
        watched = 1 if int(watched) else 0
        edition = (edition or "Standard").strip() or "Standard"
        if color_mode not in ("unknown", "color", "bw"):
            color_mode = "unknown"

        with db_session(self.db_path) as conn:
            conn.execute(
                """
                UPDATE movies
                SET user_rating = ?, watched = ?, edition = ?, color_mode = ?
                WHERE tmdb_id = ?;
                """,
                (user_rating, watched, edition, color_mode, int(tmdb_id)),
            )

    def insert_movie_from_tmdb(
        self,
        tm: TMDBMovie,
        file_path: str,
        file_mtime: Optional[int],
        file_size: Optional[int],
        poster_local: Optional[str],
        backdrop_local: Optional[str],
        people_profiles: Dict[int, Optional[str]],
    ) -> None:
        now_utc = int(time.time())

        with db_session(self.db_path) as conn:
            genre_id_map: Dict[int, int] = {}
            for pos, (tmdb_gid, gname) in enumerate(tm.genres):
                conn.execute(
                    """
                    INSERT INTO genres (tmdb_genre_id, name)
                    VALUES (?, ?)
                    ON CONFLICT(tmdb_genre_id) DO UPDATE SET name=excluded.name;
                    """,
                    (int(tmdb_gid), str(gname)),
                )
                gid_row = conn.execute("SELECT id FROM genres WHERE tmdb_genre_id = ?;", (int(tmdb_gid),)).fetchone()
                if gid_row:
                    genre_id_map[int(tmdb_gid)] = int(gid_row["id"])

            primary_genre_id: Optional[int] = None
            if tm.genres:
                primary_genre_id = genre_id_map.get(int(tm.genres[0][0]))

            conn.execute(
                """
                INSERT INTO movies (
                    tmdb_id, title, year, runtime, overview,
                    poster_path, backdrop_path,
                    lang_original, color_mode, edition, user_rating, watched,
                    date_added,
                    studio, tmdb_rating, tmdb_votes,
                    primary_genre_id,
                    file_path, file_mtime, file_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    int(tm.tmdb_id),
                    tm.title,
                    tm.year,
                    tm.runtime,
                    tm.overview,
                    poster_local,
                    backdrop_local,
                    getattr(tm, "original_language", None),
                    "unknown",
                    "Standard",
                    0,
                    0,
                    now_utc,
                    tm.studio,
                    tm.tmdb_rating,
                    tm.tmdb_votes,
                    primary_genre_id,
                    file_path,
                    file_mtime,
                    file_size,
                ),
            )

            movie_row = conn.execute("SELECT id FROM movies WHERE tmdb_id = ?;", (int(tm.tmdb_id),)).fetchone()
            if not movie_row:
                return
            movie_id = int(movie_row["id"])

            for pos, (tmdb_gid, _gname) in enumerate(tm.genres):
                gid = genre_id_map.get(int(tmdb_gid))
                if gid:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO movie_genres (movie_id, genre_id, pos)
                        VALUES (?, ?, ?);
                        """,
                        (movie_id, gid, int(pos)),
                    )

            # --- Cast (Top 12) ---
            for cast_order, (pid, name, role, profile_path) in enumerate(tm.cast[:12]):
                conn.execute(
                    """
                    INSERT INTO people (tmdb_person_id, name, profile_path)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tmdb_person_id) DO UPDATE SET
                        name=excluded.name,
                        profile_path=COALESCE(excluded.profile_path, people.profile_path);
                    """,
                    (int(pid), str(name), people_profiles.get(int(pid)) or None),
                )
                pr = conn.execute("SELECT id FROM people WHERE tmdb_person_id = ?;", (int(pid),)).fetchone()
                if pr:
                    person_id = int(pr["id"])
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO movie_cast (movie_id, person_id, role, cast_order)
                        VALUES (?, ?, ?, ?);
                        """,
                        (movie_id, person_id, str(role), int(cast_order)),
                    )

            # --- Crew (only the roles allowed by schema) ---
            for pid, name, job, profile_path in tm.crew:
                job_norm = (job or "").strip().lower()
                if job_norm not in ("director", "producer", "writer", "musician"):
                    continue

                conn.execute(
                    """
                    INSERT INTO people (tmdb_person_id, name, profile_path)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tmdb_person_id) DO UPDATE SET
                        name=excluded.name,
                        profile_path=COALESCE(excluded.profile_path, people.profile_path);
                    """,
                    (int(pid), str(name), people_profiles.get(int(pid)) or None),
                )
                pr = conn.execute("SELECT id FROM people WHERE tmdb_person_id = ?;", (int(pid),)).fetchone()
                if pr:
                    person_id = int(pr["id"])
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO movie_crew (movie_id, person_id, job, dept)
                        VALUES (?, ?, ?, NULL);
                        """,
                        (movie_id, person_id, job_norm),
                    )


# ---------------- Qt models ----------------
class MoviesModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self._rows: List[MovieRow] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.DisplayRole:
            if row.year:
                return f"{row.title}\n({row.year})"
            return row.title
        if role == Qt.DecorationRole:
            if row.poster_path:
                # DEV FIX: DB stores relative path like posters/xxx.jpg; files live under APP_DIR/cache/
                poster_file = row.poster_path
                if not os.path.isabs(poster_file):
                    poster_file = os.path.join(APP_DIR, "cache", poster_file)
                if os.path.exists(poster_file):
                    return QIcon(poster_file)
            return None
        if role == Qt.UserRole:
            return row
        return None

    def set_rows(self, rows: List[MovieRow]):
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class GenresModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self._items: List[Tuple[str, int]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        name, count = self._items[index.row()]
        if role == Qt.DisplayRole:
            return f"{name} ({count})" if name != "All" else f"All ({count})"
        if role == Qt.UserRole:
            return name
        return None

    def set_items(self, items: List[Tuple[str, int]]):
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()


# ---------------- Shelf delegate ----------------
class ShelfDelegate(QStyledItemDelegate):
    def __init__(self, cache_dir: str, parent=None):
        super().__init__(parent)
        self.cache_dir = cache_dir
        # STRICT UI: sizes come from ui.ini via set_sizes()
        self._poster_w = 0
        self._poster_h = 0
        self._cell_w = 0
        self._cell_h = 0
        self._gap = 0
        self._pad = 0
        self._cell_gap = 0

        # Fonts / colors (INI-driven via set_fonts)
        self._font_title = QFont()
        self._font_year = QFont()
        self._title_color = None
        self._year_color = None

    def set_sizes(self, poster_w: int, poster_h: int, cell_w: int, cell_h: int, gap: int, pad: int, cell_gap: int):
        self._poster_w = int(poster_w)
        self._poster_h = int(poster_h)
        self._cell_w = int(cell_w)
        self._cell_h = int(cell_h)
        self._gap = int(gap)
        self._pad = int(pad)
        self._cell_gap = int(cell_gap)


    def set_fonts(self, base_family: str, title_size: int, title_weight: int, title_color: str, small_size: int, small_weight: int, small_color: str) -> None:
        self._font_title = QFont(str(base_family), int(title_size))
        self._font_title.setWeight(QFont.Weight(int(title_weight)) if int(title_weight) in [100,200,300,400,500,600,700,800,900] else QFont.Weight.Normal)
        self._font_year = QFont(str(base_family), int(small_size))
        self._font_year.setWeight(QFont.Weight(int(small_weight)) if int(small_weight) in [100,200,300,400,500,600,700,800,900] else QFont.Weight.Normal)
        self._title_color = QColor(str(title_color))
        self._year_color = QColor(str(small_color))

    def _wrap_lines(self, text: str, fm: QFontMetrics, width: int, max_lines: int) -> List[str]:
        words = (text or "").split()
        if not words:
            return [""]
        lines: List[str] = []
        cur = words[0]
        for w in words[1:]:
            test = f"{cur} {w}"
            if fm.horizontalAdvance(test) <= width:
                cur = test
            else:
                lines.append(cur)
                cur = w
                if len(lines) >= max_lines:
                    break
        if len(lines) < max_lines:
            lines.append(cur)

        if len(lines) > max_lines:
            lines = lines[:max_lines]

        if len(lines) == max_lines:
            lines[-1] = fm.elidedText(lines[-1], Qt.ElideRight, width)

        return lines

    def paint(self, painter: QPainter, option, index: QModelIndex):
        row: MovieRow = index.data(Qt.UserRole)
        if not row:
            return
        
        cell_rect = option.rect
        gap = getattr(self, "_cell_gap", 0)
        if gap:
            cell_rect = cell_rect.adjusted(gap, gap, -gap, -gap)

        rect = cell_rect
        painter.save()
        g = int(getattr(self, "_cell_gap", 0) or 0)
        cell_rect = option.rect.adjusted(g, g, -g, -g)
        painter.setRenderHint(QPainter.Antialiasing, False)

        # Colors from current palette (works for dark/light themes)
        base = option.palette.color(QPalette.Base)
        text_col = option.palette.color(QPalette.Text)
        divider = option.palette.color(QPalette.Mid)
        hi = option.palette.color(QPalette.Highlight)

        painter.fillRect(rect, base)

        # Selection / outline (NO rounding)
        if option.state & QStyle.State_Selected:
            # Teal outline (matches Movie Collector accent)
            painter.setPen(QPen(option.palette.color(QPalette.Link), 2))
            inset = 1  # keep 2px pen from clipping
        else:
            painter.setPen(QPen(divider, 1))
            inset = 0  # draw border flush to cell edges
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(inset, inset, -1 - inset, -1 - inset))

        pad = getattr(self, "_pad", self._gap)

        # Poster rect centered horizontally
        poster_x = rect.x() + (rect.width() - self._poster_w) // 2
        poster_y = rect.y() + pad
        poster_rect = QRect(poster_x, poster_y, self._poster_w, self._poster_h)

        pm = None
        if row.poster_path and os.path.exists(row.poster_path):
            p = QPixmap(row.poster_path)
            if not p.isNull():
                pm = p.scaled(
                    poster_rect.size(),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )

        if pm is not None:
            # crop center
            sx = max(0, (pm.width() - poster_rect.width()) // 2)
            sy = max(0, (pm.height() - poster_rect.height()) // 2)
            painter.drawPixmap(poster_rect, pm, QRect(sx, sy, poster_rect.width(), poster_rect.height()))
        else:
            painter.setPen(QPen(divider))
            painter.drawRect(poster_rect.adjusted(0, 0, -1, -1))
            painter.setPen(text_col)
            painter.drawText(poster_rect, Qt.AlignCenter, "No\nPoster")

        # Title (2 lines) + year line
        title_pen = self._title_color if isinstance(self._title_color, QColor) else text_col
        year_pen = self._year_color if isinstance(self._year_color, QColor) else text_col
        painter.setPen(title_pen)
        painter.setFont(self._font_title)
        fm = QFontMetrics(self._font_title)

        text_top = poster_rect.bottom() + 2
        available_w = rect.width() - 12
        x = rect.x() + 6
        y = text_top
        line_h = fm.height()

        for ln in self._wrap_lines(row.title or "", fm, available_w, 2):
            painter.drawText(QRect(x, y, available_w, line_h), Qt.AlignHCenter | Qt.AlignVCenter, ln)
            y += line_h

        if row.year:
            y += 2
            painter.setPen(year_pen)
            painter.setPen(QPen(self._year_color))
            painter.setFont(self._font_year)
            fm_y = QFontMetrics(self._font_year)
            line_h_y = fm_y.height()
            painter.drawText(
                QRect(x, y, available_w, line_h_y),
                Qt.AlignHCenter | Qt.AlignVCenter,
                f"({row.year})",
            )

        painter.restore()

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(self._cell_w, self._cell_h)


# ---------------- Fallback dialog ----------------
class TMDBPickDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        tmdb: TMDBClient,
        query_title: str,
        query_year: Optional[int],
        results: List[Dict[str, Any]],
        preselect_tmdb_id: Optional[int],
        want_title: str,
        want_year: Optional[int],
        src_title: Optional[str] = None,
        src_year: Optional[int] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Select TMDB Match")
        self.setModal(True)
        self.setMinimumSize(820, 560)

        lay = QVBoxLayout(self)
        lines = []

        # 1) The cleaned "wanted" title/year (what the user is actually trying to match)
        primary = (want_title or "").strip()
        if want_year:
            primary += f" ({want_year})"
        lines.append("Pick a match for: " + (primary or "—"))

        # 2) The exact query we sent to TMDB
        q_title = (query_title or want_title or "").strip()
        q_year = query_year if query_year else want_year
        q_line = "TMDB query: " + (q_title or "—")
        if q_year:
            q_line += f" ({q_year})"
        lines.append(q_line)

        lbl = QLabel("\n".join(lines))
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self.list = QListWidget()
        self.list.setIconSize(QSize(80, 120))
        lay.addWidget(self.list, 1)

        ranked = sorted(
            (results or []),
            key=lambda r: score_tmdb_result(want_title, want_year, r),
            reverse=True,
        )[:30]

        best_idx = -1
        if ranked:
            best_idx = 0
            if preselect_tmdb_id is not None:
                for _i, _r in enumerate(ranked):
                    if _r.get('id') == preselect_tmdb_id:
                        best_idx = _i
                        break

        shown = 0
        for idx, r in enumerate(ranked):
            rid = r.get("id")
            title = r.get("title") or r.get("original_title") or ""
            rd = r.get("release_date") or ""
            y = rd[:4] if len(rd) >= 4 else (str(want_year) if want_year else "—")
            poster_path = r.get("poster_path")

            vote_avg = r.get("vote_average")
            vote_cnt = r.get("vote_count")
            try:
                vote_avg_s = f"{float(vote_avg):.1f}" if vote_avg is not None else "—"
            except Exception:
                vote_avg_s = "—"
            try:
                vote_cnt_s = f"{int(vote_cnt)}" if vote_cnt is not None else "—"
            except Exception:
                vote_cnt_s = "—"

            prefix = "★ " if idx == best_idx else "  "
            text = f"{prefix}{title} ({y})   TMDB {vote_avg_s}  ({vote_cnt_s} votes)"
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(0, self.list.iconSize().height() + 10))

            tmdb_id: Optional[int] = None
            if isinstance(rid, int):
                tmdb_id = rid
            elif isinstance(rid, str) and rid.isdigit():
                tmdb_id = int(rid)
            if tmdb_id is not None:
                item.setData(Qt.UserRole, tmdb_id)

            thumb_local = tmdb.poster_thumb_file(poster_path, size="w185")
            if thumb_local and os.path.exists(thumb_local):
                pm = QPixmap(thumb_local)
                if not pm.isNull():
                    pm = pm.scaled(self.list.iconSize(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    item.setIcon(QIcon(pm))

            self.list.addItem(item)
            shown += 1

        if shown == 0:
            self.list.addItem(QListWidgetItem("No results from TMDB for this filename."))
            self.list.setEnabled(False)

        if preselect_tmdb_id is not None and shown > 0:
            for i in range(self.list.count()):
                it = self.list.item(i)
                if it.data(Qt.UserRole) == int(preselect_tmdb_id):
                    self.list.setCurrentRow(i)
                    break
        elif shown > 0:
            self.list.setCurrentRow(0)

        buttons = QDialogButtonBox()
        self.btn_select = buttons.addButton("Select", QDialogButtonBox.AcceptRole)
        self.btn_skip = buttons.addButton("Skip", QDialogButtonBox.RejectRole)

        if shown == 0:
            self.btn_select.setEnabled(False)

        self.btn_select.clicked.connect(self.accept)
        self.btn_skip.clicked.connect(self.reject)
        lay.addWidget(buttons)

    def selected_tmdb_id(self) -> Optional[int]:
        if self.result() != QDialog.Accepted:
            return None
        it = self.list.currentItem()
        if not it:
            return None
        rid = it.data(Qt.UserRole)
        return int(rid) if rid is not None else None


# ---------------- UI panes ----------------
class GenresPane(QWidget):
    genre_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_lay = QVBoxLayout(self)
        self.root_lay.setContentsMargins(8, 8, 8, 8)
        self.root_lay.setSpacing(8)

        self.lbl_header = QLabel("Genres")
        self.root_lay.addWidget(self.lbl_header)

        self.model = GenresModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.clicked.connect(self._on_clicked)
        self.root_lay.addWidget(self.view, 1)

        self.model.set_items([("All", 0)])
        self.view.setCurrentIndex(self.model.index(0, 0))

    def apply_ui(self, ui: Dict[str, Any]) -> None:
        ph = ui.get("pane_headers", {}) or {}
        genres = ui.get("genres", {}) or {}
        size = int(ph.get("size", 12))
        weight = int(ph.get("weight", 800))
        color = str(ph.get("color", ui.get("colors", {}).get("text", "#E6E6E6")))
        self.lbl_header.setStyleSheet(f"font-size:{size}px; font-weight:{weight}; color:{color};")
        item_h = int(genres.get("item_h", 30))
        self.view.setStyleSheet(f"QListView::item {{ min-height: {item_h}px; }}")


    def apply_ui(self, ui: Dict[str, Any]) -> None:
        ph = ui.get("pane_headers", {}) or {}
        size = int(ph.get("size", 12) or 12)
        weight = int(ph.get("weight", 800) or 800)
        color = str(ph.get("color", ui.get("colors", {}).get("text", "#E6E6E6")))
        self.lbl_header.setStyleSheet(f"font-size:{size}px; font-weight:{weight}; color:{color};")
        try:
            item_h = int((ui.get("genres", {}) or {}).get("item_h", 30) or 30)
            self.view.setSpacing(0)
            self.view.setStyleSheet(f"QListView::item{{height:{item_h}px;}}")
        except Exception:
            pass

    def _on_clicked(self, idx: QModelIndex):
        genre = idx.data(Qt.UserRole) or idx.data(Qt.DisplayRole)
        if isinstance(genre, str):
            self.genre_changed.emit(genre)

    def rebuild_from_movies(self, rows):
        counts = {"All": len(rows)}
        for m in rows:
            parts = [x.strip() for x in (getattr(m, "genres_str", "") or "").split(",") if x.strip()]
            for g in parts:
                if g == "Science Fiction":
                    g = "Sci-Fi"
                counts[g] = counts.get(g, 0) + 1

        items = [("All", counts["All"])] + sorted(
            [(k, v) for k, v in counts.items() if k != "All"],
            key=lambda x: x[0].lower(),
        )
        self.model.set_items(items)
        try:
            self.view.setCurrentIndex(self.model.index(0, 0))
        except Exception:
            pass


class MoviesPane(QWidget):
    movie_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_lay = QVBoxLayout(self)
        self.root_lay.setContentsMargins(10, 10, 10, 10)
        self.root_lay.setSpacing(8)

        top = QHBoxLayout()
        self.lbl_header = QLabel("Movies List")
        top.addWidget(self.lbl_header)
        top.addStretch(1)
        self.root_lay.addLayout(top)

        self.model = MoviesModel()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)

        self.view = QListView()
        self.view.setModel(self.proxy)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.clicked.connect(self._on_clicked)

        self.delegate = ShelfDelegate(cache_dir=CACHE_DIR, parent=self.view)
        self.list_delegate = ListDelegate(parent=self.view)
        self.view.setItemDelegate(self.delegate)
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setUniformItemSizes(True)

        self.root_lay.addWidget(self.view, 1)

        self._genre = "All"
        self._all_rows: List[MovieRow] = []
        self._search = ""
        self._view_mode = "shelf"

    def apply_ui(self, ui: Dict[str, Any]) -> None:
        ph = ui.get("pane_headers", {}) or {}
        size = int(ph.get("size", 12))
        weight = int(ph.get("weight", 800))
        color = str(ph.get("color", ui.get("colors", {}).get("text", "#E6E6E6")))
        self.lbl_header.setStyleSheet(f"font-size:{size}px; font-weight:{weight}; color:{color};")

        mg = ui["movies_grid"]
        self.delegate.set_sizes(
            poster_w=int(mg["poster_w"]),
            poster_h=int(mg["poster_h"]),
            cell_w=int(mg["cell_w"]),
            cell_h=int(mg["cell_h"]),
            gap=int(mg["spacing"]),
            pad=int(mg["item_padding"]),
            cell_gap=int(mg["cell_gap"]),
        )

        ff = ui.get("fonts", {}) or {}
        self.delegate.set_fonts(
            base_family=str(ff.get("base_family", "Segoe UI")),
            title_size=int(mg.get("title_font_size", ff.get("base_size", 12))),
            title_weight=int(mg.get("title_font_weight", 600)),
            title_color=str(mg.get("title_color", ui.get("colors", {}).get("text", "#E6E6E6"))),
            small_size=int(mg.get("year_font_size", mg.get("title_font_size", ff.get("base_size", 12)))),
            small_weight=int(mg.get("year_font_weight", 400)),
            small_color=str(mg.get("year_color", ui.get("colors", {}).get("text", "#E6E6E6"))),
        )

        if (self._view_mode or "shelf") == "shelf":
            self.view.setIconSize(QSize(self.delegate._poster_w, self.delegate._poster_h))
            self.view.setGridSize(QSize(self.delegate._cell_w, self.delegate._cell_h))
            self.view.setSpacing(self.delegate._gap)
            self.view.updateGeometries()
            self.view.viewport().update()

    def set_view_mode(self, mode: str):
        self._view_mode = mode or "shelf"

        if self._view_mode == "shelf":
            self.view.setItemDelegate(self.delegate)
            self.view.setViewMode(QListView.IconMode)
            self.view.setWrapping(True)
            self.view.setResizeMode(QListView.Adjust)
            self.view.setUniformItemSizes(True)
            self.view.setIconSize(QSize(self.delegate._poster_w, self.delegate._poster_h))
            self.view.setGridSize(QSize(self.delegate._cell_w, self.delegate._cell_h))
            self.view.setSpacing(self.delegate._gap)
        else:
            self.view.setItemDelegate(self.list_delegate)
            self.view.setViewMode(QListView.ListMode)
            self.view.setWrapping(False)
            self.view.setResizeMode(QListView.Adjust)
            self.view.setGridSize(QSize())
            self.view.setUniformItemSizes(True)
            self.view.setSpacing(1)
            self.view.setIconSize(QSize(self.list_delegate.thumb_w, self.list_delegate.thumb_h))

        self.view.viewport().update()

    def set_genre(self, genre: str):
        self._genre = genre or "All"
        self._apply_filters()

    def set_search(self, s: str):
        self._search = (s or "").strip()
        self.proxy.setFilterFixedString(self._search)

    def set_movies(self, rows: List[MovieRow]):
        self._all_rows = list(rows)
        self.model.set_rows(list(rows))
        self._apply_filters()

    def _apply_filters(self):
        all_rows = self._all_rows if self._all_rows is not None else self.model._rows
        if self._genre and self._genre != "All":
            filt = [r for r in all_rows if self._genre.lower() in (r.genres_str or "").lower()]
        else:
            filt = list(all_rows)
        self.model.set_rows(filt)
        self.proxy.invalidate()

    def _on_clicked(self, idx: QModelIndex):
        src = self.proxy.mapToSource(idx)
        row = self.model.data(src, Qt.UserRole)
        if isinstance(row, MovieRow):
            self.movie_selected.emit(row)


class StarsWidget(QWidget):
    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self.setFixedHeight(22)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._stars: List[QPushButton] = []
        for i in range(1, 6):
            b = QPushButton("☆")
            b.setFixedSize(22, 22)
            b.clicked.connect(lambda _=False, v=i: self.set_value(v))
            self._stars.append(b)
            lay.addWidget(b)

        self.set_value(0)

    def value(self) -> int:
        return self._value

    def set_value(self, v: int):
        v = max(0, min(5, int(v)))
        self._value = v
        for i, b in enumerate(self._stars, start=1):
            b.setText("★" if i <= v else "☆")
        self.changed.emit(v)


class DetailsPane(QWidget):
    def __init__(self, db: DB, parent=None):
        super().__init__(parent)
        self.db = db
        self._current: Optional[MovieRow] = None

        self.root_lay = QVBoxLayout(self)
        self.root_lay.setContentsMargins(10, 10, 10, 10)
        self.root_lay.setSpacing(8)

        self.lbl_header = QLabel("Details")
        self.root_lay.addWidget(self.lbl_header)

        btns = QHBoxLayout()
        self.btn_edit = QPushButton("Edit")
        self.btn_rescan = QPushButton("Rescan")
        self.btn_delete = QPushButton("Delete")
        btns.addWidget(self.btn_edit)
        btns.addWidget(self.btn_rescan)
        btns.addWidget(self.btn_delete)
        btns.addStretch(1)
        self.root_lay.addLayout(btns)

        self.lbl_title = QLabel("")
        self.lbl_title.setWordWrap(True)
        self.root_lay.addWidget(self.lbl_title)

        self.mid = QHBoxLayout()
        self.lbl_poster = QLabel()
        self.lbl_poster.setFixedSize(220, 330)
        self.lbl_poster.setFrameShape(QFrame.Box)
        self.mid.addWidget(self.lbl_poster)

        self.info_col = QVBoxLayout()
        self.lbl_studio = QLabel("")
        self.lbl_genres = QLabel("")
        self.lbl_meta = QLabel("")
        self.lbl_meta.setWordWrap(True)
        self.info_col.addWidget(self.lbl_studio)
        self.info_col.addWidget(self.lbl_genres)
        self.info_col.addWidget(self.lbl_meta)

        self.edrow = QHBoxLayout()
        self.lbl_edition = QLabel("Edition")
        self.edrow.addWidget(self.lbl_edition)
        self.edition = QComboBox()
        self.edition.addItems(["Standard", "Extended", "Director's Cut", "Other…"])
        self.edrow.addWidget(self.edition)
        self.edrow.addStretch(1)
        self.info_col.addLayout(self.edrow)

        self.lbl_tmdb = QLabel("")
        self.info_col.addWidget(self.lbl_tmdb)

        self.ur = QHBoxLayout()
        self.lbl_user_rating = QLabel("User rating:")
        self.ur.addWidget(self.lbl_user_rating)
        self.stars = StarsWidget()
        self.ur.addWidget(self.stars)
        self.chk_watched = QCheckBox("Watched")
        self.ur.addWidget(self.chk_watched)
        self.ur.addStretch(1)
        self.info_col.addLayout(self.ur)

        self.mid.addLayout(self.info_col, 1)
        self.mid.setAlignment(self.info_col, Qt.AlignTop)
        self.root_lay.addLayout(self.mid)

        self.lbl_plot = QLabel("")
        self.lbl_plot.setWordWrap(True)
        self.root_lay.addWidget(self.lbl_plot)

        bottom = QHBoxLayout()
        self.cast = QListWidget()
        self.crew = QListWidget()
        bottom.addWidget(self.cast, 1)
        bottom.addWidget(self.crew, 1)
        self.root_lay.addLayout(bottom, 1)

        self.stars.changed.connect(self._save_user)
        self.chk_watched.stateChanged.connect(self._save_user)
        self.edition.currentIndexChanged.connect(lambda *_: self._save_user())

    def apply_ui(self, ui: Dict[str, Any]) -> None:
        self._ui = ui
        c = ui.get("colors", {}) or {}
        ph = ui.get("pane_headers", {}) or {}
        d = ui.get("details", {}) or {}

        self.root_lay.setSpacing(int(d.get("layout_spacing", 8)))
        self.info_col.setSpacing(int(d.get("meta_spacing", 8)))
        top_margin = int(d.get("info_top_margin", 0))
        self.info_col.setContentsMargins(0, top_margin, 0, 0)

        # Critical: always restore visible texts from ui/defaults.
        self.lbl_header.setText(str(d.get("header_text", "Details")))
        self.btn_edit.setText(str(d.get("edit_button_text", "Edit")))
        self.btn_rescan.setText(str(d.get("rescan_button_text", "Rescan")))
        self.btn_delete.setText(str(d.get("delete_button_text", "Delete")))
        self.lbl_plot_header.setText(str(d.get("plot_label_text", "Plot")))
        self.lbl_cast_header.setText(str(d.get("cast_label_text", "Cast")))
        self.lbl_crew_header.setText(str(d.get("crew_label_text", "Crew")))
        self.lbl_edition.setText(str(d.get("edition_label_text", "Edition")))
        self.lbl_user_rating.setText(str(d.get("user_rating_label_text", "User rating")))
        self.chk_watched.setText(str(d.get("watched_text", "Watched")))

        header_size = int(ph.get("size", 12))
        header_weight = int(ph.get("weight", 800))
        header_color = str(ph.get("color", c.get("text", "#E6E6E6")))
        self.lbl_header.setStyleSheet(f"font-size:{header_size}px; font-weight:{header_weight}; color:{header_color};")

        accent = str(d.get("accent", c.get("teal", "#2AA7A1")))
        t_color = str(d.get("title_color", accent))
        t_size = int(d.get("title_size", 24))
        t_weight = int(d.get("title_weight", 800))
        self.lbl_title.setStyleSheet(f"color:{t_color}; font-size:{t_size}px; font-weight:{t_weight};")

        self.lbl_poster.setFixedSize(int(d.get("poster_w", 220)), int(d.get("poster_h", 330)))

        body_size = int(d.get("body_size", ui.get("fonts", {}).get("base_size", 10) + 5))
        body_color = str(d.get("body_color", c.get("text", "#E6E6E6")))
        studio_size = int(d.get("studio_size", body_size + 7))
        studio_weight = int(d.get("studio_weight", 700))
        studio_color = str(d.get("studio_color", body_color))
        self.lbl_studio.setStyleSheet(f"font-size:{studio_size}px; font-weight:{studio_weight}; color:{studio_color};")

        body_css = f"font-size:{body_size}px; color:{body_color};"
        self.lbl_genres.setStyleSheet(body_css)
        self.lbl_meta.setStyleSheet(body_css)
        self.lbl_edition.setStyleSheet(
            f"font-size:{int(d.get('edition_label_size', body_size))}px; "
            f"font-weight:{int(d.get('edition_label_weight', 400))}; "
            f"color:{str(d.get('edition_label_color', body_color))};"
        )
        self.edition.setStyleSheet(f"font-size:{body_size}px; color:{body_color};")

        self.lbl_tmdb.setStyleSheet(
            f"font-size:{int(d.get('tmdb_rating_size', body_size))}px; "
            f"font-weight:{int(d.get('tmdb_rating_weight', 400))}; "
            f"color:{str(d.get('tmdb_rating_color', body_color))}; "
            f"background:{str(d.get('tmdb_box_bg', c.get('panel', '#2B2D2F')))}; "
            f"border:1px solid {str(d.get('tmdb_box_border', c.get('divider', '#3C3F42')))}; "
            f"border-radius:{int(d.get('tmdb_box_radius', 4))}px; "
            f"padding:{int(d.get('tmdb_box_pad_v', 4))}px {int(d.get('tmdb_box_pad_h', 8))}px;"
        )

        self.lbl_user_rating.setStyleSheet(
            f"font-size:{int(d.get('user_rating_label_size', body_size))}px; "
            f"font-weight:{int(d.get('user_rating_label_weight', 400))}; "
            f"color:{str(d.get('user_rating_label_color', body_color))};"
        )

        self.chk_watched.setStyleSheet(
            f"font-size:{int(d.get('watched_font_size', body_size))}px; "
            f"font-weight:{int(d.get('watched_font_weight', 400))}; "
            f"color:{str(d.get('watched_text_color', body_color))};"
            f"QCheckBox::indicator{{width:{int(d.get('watched_indicator_size', 18))}px; height:{int(d.get('watched_indicator_size', 18))}px;}}"
        )

        self.stars.apply_ui(d)

        hdr_css_tpl = "font-size:{size}px; font-weight:{weight}; color:{color};"
        self.lbl_plot_header.setStyleSheet(hdr_css_tpl.format(
            size=int(d.get("plot_label_size", 22)),
            weight=int(d.get("plot_label_weight", 800)),
            color=str(d.get("plot_label_color", accent)),
        ))
        self.lbl_cast_header.setStyleSheet(hdr_css_tpl.format(
            size=int(d.get("cast_label_size", 22)),
            weight=int(d.get("cast_label_weight", 800)),
            color=str(d.get("cast_label_color", accent)),
        ))
        self.lbl_crew_header.setStyleSheet(hdr_css_tpl.format(
            size=int(d.get("crew_label_size", 22)),
            weight=int(d.get("crew_label_weight", 800)),
            color=str(d.get("crew_label_color", accent)),
        ))

        self.plot_box.setStyleSheet(
            f"background:{str(d.get('plot_box_bg', c.get('panel', '#2B2D2F')))}; "
            f"color:{str(d.get('plot_text_color', body_color))}; "
            f"border:1px solid {str(d.get('plot_box_border', c.get('divider', '#3C3F42')))}; "
            f"border-radius:{int(d.get('plot_box_radius', 4))}px; "
            f"font-size:{int(d.get('plot_font_size', body_size))}px; "
            f"font-weight:{int(d.get('plot_font_weight', 400))};"
            f"QScrollBar:vertical{{width:{int(d.get('plot_scrollbar_w', 10))}px;}}"
        )

        list_css = (
            f"background:{str(d.get('list_box_bg', d.get('plot_box_bg', c.get('panel', '#2B2D2F'))))}; "
            f"color:{str(d.get('list_text_color', body_color))}; "
            f"border:1px solid {str(d.get('list_box_border', d.get('plot_box_border', c.get('divider', '#3C3F42'))))}; "
            f"border-radius:{int(d.get('list_box_radius', 4))}px; "
            f"font-size:{int(d.get('list_font_size', body_size))}px; "
            f"font-weight:{int(d.get('list_font_weight', 400))};"
            f"QScrollBar:vertical{{width:{int(d.get('list_scrollbar_w', 10))}px;}}"
        )
        self.cast.setStyleSheet(list_css)
        self.crew.setStyleSheet(list_css)

        p_text = str(d.get("placeholder_text", "Select a movie"))
        p_color = str(d.get("placeholder_color", accent))
        p_size = int(d.get("placeholder_size", 18))
        p_weight = int(d.get("placeholder_weight", 800))
        self._placeholder_text = p_text
        self._placeholder_css = f"color:{p_color}; font-size:{p_size}px; font-weight:{p_weight};"
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        self.lbl_poster.setPixmap(QPixmap())
        self.lbl_poster.setAlignment(Qt.AlignCenter)
        self.lbl_poster.setText(getattr(self, "_placeholder_text", "Select a movie"))
        css = getattr(self, "_placeholder_css", "")
        if css:
            self.lbl_poster.setStyleSheet(css)

    def set_movie(self, row: Optional[MovieRow]):
        self._current = row
        d = (getattr(self, "_ui", {}) or {}).get("details", {}) or {}

        if row is None:
            self.lbl_title.setText("")
            self.lbl_studio.setText("")
            self.lbl_genres.setText("")
            self.lbl_meta.setText("")
            self.lbl_tmdb.setText("")
            self.plot_box.setPlainText("")
            self.cast.clear()
            self.crew.clear()
            self._show_placeholder()
            return

        self.lbl_title.setText(f"{row.title} ({row.year})" if row.year else row.title)

        pm = QPixmap()
        if row.poster_path and os.path.exists(row.poster_path):
            pm = QPixmap(row.poster_path)
        if not pm.isNull():
            pm = pm.scaled(self.lbl_poster.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self.lbl_poster.setStyleSheet("")
            self.lbl_poster.setText("")
            self.lbl_poster.setPixmap(pm)
        else:
            self.lbl_poster.setPixmap(QPixmap())
            self.lbl_poster.setText(str(d.get("no_poster_text", "No Poster")))

        show_labels = bool(d.get("meta_show_labels", False))
        studio_label = str(d.get("studio_label_text", "Studio:"))
        genres_label = str(d.get("genres_label_text", "Genres:"))
        language_label = str(d.get("language_label_text", "Language:"))
        runtime_label = str(d.get("runtime_label_text", "Runtime:"))
        edition_label = str(d.get("edition_label_text", "Edition"))
        tmdb_label = str(d.get("tmdb_label_text", "TMDB rating"))
        tmdb_votes_text = str(d.get("tmdb_votes_text", "votes"))

        self.lbl_studio.setText(f"{studio_label} {row.studio or '—'}" if show_labels else (row.studio or "—"))
        self.lbl_genres.setText(f"{genres_label} {row.genres_str or '—'}" if show_labels else (row.genres_str or "—"))

        lang = row.lang_original or "—"
        lang_flag = iso_to_flag((row.lang_original or "")[:2])
        runtime_txt = f"{row.runtime} {str(d.get('runtime_suffix_text', 'min'))}" if row.runtime else "—"
        if show_labels:
            self.lbl_meta.setText(f"{language_label} {lang} {lang_flag}   |   {runtime_label} {runtime_txt}")
            self.lbl_edition.setText(edition_label + ":")
        else:
            self.lbl_meta.setText(f"{lang} {lang_flag}   |   {runtime_txt}")
            self.lbl_edition.setText(edition_label)

        tmdb_rating = f"{row.tmdb_rating}" if row.tmdb_rating is not None else "—"
        tmdb_votes = f"{row.tmdb_votes or 0}"
        self.lbl_tmdb.setText(f"{tmdb_label}: {tmdb_rating} ({tmdb_votes} {tmdb_votes_text})")

        self.lbl_user_rating.setText(str(d.get("user_rating_label_text", "User rating")))
        self.chk_watched.setText(str(d.get("watched_text", "Watched")))
        self.stars.set_value(int(row.user_rating or 0))
        self.chk_watched.setChecked(bool(row.watched))

        ed = (row.edition or "Standard").strip()
        idx = self.edition.findText(ed)
        self.edition.setCurrentIndex(idx if idx >= 0 else 0)

        self.lbl_plot_header.setText(str(d.get("plot_label_text", "Plot")))
        self.lbl_cast_header.setText(str(d.get("cast_label_text", "Cast")))
        self.lbl_crew_header.setText(str(d.get("crew_label_text", "Crew")))

        self.plot_box.setPlainText(row.overview or "")
        self.cast.clear()
        self.crew.clear()
        cast_rows, crew_rows = self._load_people(row.tmdb_id)
        for r in cast_rows:
            name = str(r["name"] or "")
            role = str(r["role"] or "")
            self.cast.addItem(f"{name} — {role}" if role else name)
        for r in crew_rows:
            name = str(r["name"] or "")
            job = str(r["job"] or "")
            self.crew.addItem(f"{job.title()}: {name}" if job else name)

    def _save_user(self, *_):
        if not self._current:
            return
        user_rating = int(self.stars.value())
        watched = 1 if self.chk_watched.isChecked() else 0
        edition = str(self.edition.currentText() or "Standard")
        cm = self._current.color_mode or "unknown"
        try:
            self.db.update_user_fields(self._current.tmdb_id, user_rating, watched, edition, cm)
        except Exception:
            pass


# ---------------- List delegate (List mode) ----------------
class ListDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.thumb_w = 45
        self.thumb_h = 68
        self.pad = 6

    def sizeHint(self, option, index: QModelIndex):
        fm = option.fontMetrics
        h = max(self.thumb_h + 2 * self.pad, fm.height() + 2 * self.pad)
        return QSize(option.rect.width(), h)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        row: MovieRow = index.data(Qt.UserRole)
        if not row:
            return

        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)

        base = option.palette.color(QPalette.Base)
        text_col = option.palette.color(QPalette.Text)
        divider = option.palette.color(QPalette.Mid)
        hi = option.palette.color(QPalette.Highlight)
        hi_text = option.palette.color(QPalette.HighlightedText)

        painter.fillRect(rect, base)

        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, hi)
            text_col = hi_text

        # NOTE: separators must stay visible even when selected
        sep_pen = QPen(divider, 1)

        # thumbnail
        x = rect.x() + self.pad
        y = rect.y() + self.pad
        thumb_rect = QRect(x, y, self.thumb_w, self.thumb_h)

        if row.poster_path and os.path.exists(row.poster_path):
            pm = QPixmap(row.poster_path)
            if not pm.isNull():
                pm = pm.scaled(self.thumb_w, self.thumb_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                painter.drawPixmap(thumb_rect, pm, pm.rect())
        painter.setPen(QPen(divider, 1))
        painter.drawRect(thumb_rect)

        # title text
        tx = thumb_rect.right() + self.pad + 8
        ty = rect.y()
        tw = rect.width() - (tx - rect.x()) - self.pad
        th = rect.height()

        title = row.title or ""
        if row.year:
            title = f"{title} ({row.year})"

        painter.setPen(QPen(text_col))
        fm = option.fontMetrics
        el = fm.elidedText(title, Qt.ElideRight, max(10, tw))
        painter.drawText(QRect(tx, ty, tw, th), Qt.AlignVCenter | Qt.AlignLeft, el)

        # separators (top + bottom) - always visible
        painter.setPen(sep_pen)
        painter.drawLine(rect.topLeft(), rect.topRight())
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        painter.restore()

# ---------------- Scan helpers ----------------
_TV_PATTERNS = [
    re.compile(r"\bS\d{1,2}E\d{1,2}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}x\d{1,2}\b", re.IGNORECASE),
    re.compile(r"\bseason\s*\d+\b", re.IGNORECASE),
    re.compile(r"\bepisode\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b(19|20)\d{2}\s*-\s*S\d{1,2}E\d{1,2}\b", re.IGNORECASE),
    re.compile(r"\bS\d{1,2}\b", re.IGNORECASE),
    re.compile(r"\bE\d{1,2}\b", re.IGNORECASE),
    re.compile(r"\b(?:ep|e)\.?\s*\d{1,3}\b", re.IGNORECASE),
]


def looks_like_tv_episode(path: str) -> bool:
    """TV-episode detector (best-effort).

    We skip TV-like files during MOVIE scanning to avoid polluting results
    (e.g. "S03E10", "3x10", "Season 3 Episode 10", etc.).
    """
    try:
        full = str(path)
    except Exception:
        full = path

    name = os.path.basename(full)

    # Strong, unambiguous markers
    if re.search(r"\bS\d{1,2}E\d{1,2}\b", name, re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,2}x\d{1,2}\b", name, re.IGNORECASE):
        return True

    # Common "Season / Episode" wording (covers files like: "Prison Break Season 3 Episode 13 - ...")
    if re.search(r"\bseason\s*\d{1,3}\b", name, re.IGNORECASE) and re.search(r"\bepisode\s*\d{1,3}\b", name, re.IGNORECASE):
        return True

    # Slightly looser patterns (still pretty safe)
    if re.search(r"\b(ep|episode)\.?\s*\d{1,3}\b", name, re.IGNORECASE) and re.search(r"\bseason\s*\d{1,3}\b", name, re.IGNORECASE):
        return True

    # Check parent folders too (some releases keep episode tags in folder names)
    if re.search(r"\bseason\s*\d{1,3}\b", full, re.IGNORECASE) and re.search(r"\bepisode\s*\d{1,3}\b", full, re.IGNORECASE):
        return True

    return False






def _looks_like_multipart_release(path: str) -> bool:
    """
    Heuristic: detect *release* multipart markers (CD1/CD2, Disc1/Disc2, etc.)
    IMPORTANT: Do NOT treat a movie title containing "Part 1/2/II/III" as multipart.
    We only flag "part/pt" when it is a *standalone* suffix/segment marker (e.g. "...-part1.mkv" or "\\Part1\\").
    """
    try:
        s = str(path)
    except Exception:
        return False

    s_l = s.lower().replace("/", "\\")
    segments = [seg for seg in s_l.split("\\") if seg]

    # Strong signals: dedicated folder segments (common in releases)
    strong_segments = {"cd1", "cd2", "disc1", "disc2", "disk1", "disk2"}
    if any(seg in strong_segments for seg in segments):
        return True

    # "part/pt" are only considered if they appear as an *exact* segment like "part1"/"pt2"
    # (not "Harry Potter ... Part 1 (2010)" which contains spaces/parentheses).
    weak_segments = {"part1", "part2", "pt1", "pt2"}
    if any(seg in weak_segments for seg in segments):
        return True

    # Filename suffix markers (before extension). This avoids false positives where "Part 1"
    # is part of the actual title and followed by other tokens (1080p, WEBRip, etc.).
    base = segments[-1] if segments else s_l
    base_no_ext = re.sub(r"\.[a-z0-9]{1,5}$", "", base)

    # Suffix-only checks
    if re.search(r"(?:^|[\W_])(cd|disc|disk)\s*([12])$", base_no_ext):
        return True
    if re.search(r"(?:^|[\W_])(part|pt)\s*([12])$", base_no_ext):
        return True

    return False

    # Broad regex checks (filename + folders) — boundary-aware to avoid false positives.
    patterns = (
        r"\bcd\s*[12]\b",
        r"\bdisc\s*[12]\b",
        r"\bdisk\s*[12]\b",
        r"\bpart\s*[12]\b",
        r"\bpt\s*[12]\b",
        r"(?:^|[\W_])(cd|disc|disk|part|pt)[\W_]*([12])(?:$|[\W_])",
    )
    for pat in patterns:
        if re.search(pat, s):
            return True

    # Common explicit folder markers (Windows paths).
    for token in ("\\cd1\\", "\\cd2\\", "\\disc1\\", "\\disc2\\", "\\disk1\\", "\\disk2\\", "\\part1\\", "\\part2\\"):
        if token in s:
            return True

    return False

def apply_dark_messagebox_style(msgbox: QMessageBox, ui: dict | None = None, accent: str | None = None):
    """Apply the app's dark theme to QMessageBox (including Detailed Text)."""
    colors = (ui or {}).get("colors", {}) if isinstance(ui, dict) else {}
    bg = colors.get("bg", "#1f2326")
    panel = colors.get("panel", "#262b2f")
    panel_raised = colors.get("panel_raised", panel)
    text = colors.get("text", "#d7d7d7")
    muted = colors.get("muted", "#9aa2a9")
    divider = colors.get("divider", "#3a3f44")
    teal = colors.get("teal", accent or "#2aa198")
    accent = accent or teal

    msgbox.setStyleSheet(f"""
        QMessageBox {{
            background-color: {bg};
            color: {text};
        }}
        QMessageBox QLabel {{
            color: {text};
        }}
        QMessageBox QLabel#qt_msgbox_label {{
            color: {text};
        }}
        QMessageBox QLabel#qt_msgboxex_icon_label {{
            background: transparent;
        }}
        QMessageBox QPushButton {{
            background-color: {panel_raised};
            color: {text};
            border: 1px solid {divider};
            padding: 5px 12px;
            border-radius: 6px;
        }}
        QMessageBox QPushButton:hover {{
            border: 1px solid {accent};
        }}
        QMessageBox QPushButton:pressed {{
            background-color: {panel};
        }}
        QMessageBox QTextEdit,
        QMessageBox QPlainTextEdit {{
            background-color: {panel};
            color: {text};
            border: 1px solid {divider};
            selection-background-color: {accent};
            selection-color: {bg};
        }}
        QMessageBox QAbstractScrollArea {{
            background-color: {panel};
            border: 1px solid {divider};
        }}
        QMessageBox QAbstractScrollArea QWidget {{
            background-color: {panel};
            color: {text};
        }}
        QMessageBox QScrollBar:vertical {{
            background: {panel};
            width: 10px;
            margin: 0px;
            border: 1px solid {divider};
        }}
        QMessageBox QScrollBar::handle:vertical {{
            background: {divider};
            min-height: 20px;
        }}
        QMessageBox QScrollBar::add-line:vertical,
        QMessageBox QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QMessageBox QScrollBar:horizontal {{
            background: {panel};
            height: 10px;
            margin: 0px;
            border: 1px solid {divider};
        }}
        QMessageBox QScrollBar::handle:horizontal {{
            background: {divider};
            min-width: 20px;
        }}
        QMessageBox QScrollBar::add-line:horizontal,
        QMessageBox QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
    """)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.ui = load_ui_ini()

        # Apply dark theme from ui.ini (square corners for now)
        app = QApplication.instance()
        if app is not None:
            apply_ui_theme(app, self.ui)

        self.db_path = os.path.join(APP_DIR, "movies.db")
        self.db = DB(self.db_path)

        api_key = self.ui["tmdb"]["api_key"]
        self.tmdb = TMDBClient(api_key, cache_dir=CACHE_DIR)
        self.tmdb_language = self.ui["tmdb"]["language"]

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")

        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        tb = QWidget()
        tb_l = QHBoxLayout(tb)
        tb_cfg = self.ui.get("toolbar", {}) or {}
        colors = self.ui.get("colors", {}) or {}
        tb_l.setContentsMargins(int(tb_cfg.get("pad_h", 8)), int(tb_cfg.get("pad_v", 8)), int(tb_cfg.get("pad_h", 8)), int(tb_cfg.get("pad_v", 8)))
        tb_l.setSpacing(int(tb_cfg.get("spacing", 8)))
        tb.setFixedHeight(int(tb_cfg.get("height", 60)))

        self.btn_scan = QPushButton("Scan Folder…")
        tb_l.addWidget(self.btn_scan)
        # Subtle separator under toolbar (requested)
        tb.setStyleSheet((tb.styleSheet() + "\n" if tb.styleSheet() else "") + f"border-bottom: 1px solid {colors.get('divider', '#3C3F42')};")

        self.btn_not_found = QPushButton("Not Found…")
        tb_l.addWidget(self.btn_not_found)

        self.btn_issues = QPushButton("Issues…")
        tb_l.addWidget(self.btn_issues)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(int(tb_cfg.get("search_min_width", 320)))
        self.search.setMaximumWidth(int(tb_cfg.get("search_max_width", 640)))
        self.search.setFixedHeight(int(tb_cfg.get("search_height", 36)))
        tb_l.addWidget(self.search)

        self.sort = QComboBox()
        self.sort.addItem("Title (A–Z)", "title")
        self.sort.addItem("User Rating", "user_rating")
        self.sort.addItem("Year (Desc)", "year_desc")
        self.sort.addItem("Year (Asc)", "year_asc")
        self.sort.addItem("Recently Added", "recent")
        self.lbl_sort = QLabel("Sort:")
        tb_l.addWidget(self.lbl_sort)
        tb_l.addWidget(self.sort)

        self.view_mode = QComboBox()
        self.view_mode.addItem("Shelf", "shelf")
        self.view_mode.addItem("List", "list")
        self.lbl_view = QLabel("View:")
        tb_l.addWidget(self.lbl_view)
        tb_l.addWidget(self.view_mode)

        tb_l.addStretch(1)

        self.total_lbl = QLabel("Total Movies: 0")
        tb_l.addWidget(self.total_lbl)

        self._toolbar_widget = tb
        self._toolbar_layout = tb_l
        self._apply_toolbar_ui()

        root.addWidget(tb)

        self.main_splitter = QSplitter()
        self.main_splitter.setOrientation(Qt.Horizontal)

        self.genres_pane = GenresPane()
        self.movies_pane = MoviesPane()
        self.details_pane = DetailsPane(self.db)

        if hasattr(self.genres_pane, "apply_ui"):
            self.genres_pane.apply_ui(self.ui)
        if hasattr(self.movies_pane, "apply_ui"):
            self.movies_pane.apply_ui(self.ui)
        if hasattr(self.details_pane, "apply_ui"):
            self.details_pane.apply_ui(self.ui)
        self.main_splitter.addWidget(self.genres_pane)
        self.main_splitter.addWidget(self.movies_pane)
        self.main_splitter.addWidget(self.details_pane)

        # Pane sizes from ui.ini (no hard-coded layout values)
        self._did_apply_panes = False
        self._apply_pane_sizes_from_ini()

        root.addWidget(self.main_splitter, 1)

        self.status = QStatusBar()
        # Subtle separator above info bar (requested)
        self.status.setStyleSheet((self.status.styleSheet() + "\n" if self.status.styleSheet() else "") + f"border-top: 1px solid {self.ui.get('colors', {}).get('divider', '#3C3F42')};")
        self.setStatusBar(self.status)
        self.status.showMessage("Ready.")

        self.genres_pane.genre_changed.connect(self.movies_pane.set_genre)
        self.movies_pane.movie_selected.connect(self.details_pane.set_movie)
        self.search.textChanged.connect(self.movies_pane.set_search)

        self.btn_scan.clicked.connect(self._on_scan_folder)
        self.btn_not_found.clicked.connect(self._on_show_scan_failures)
        self.btn_issues.clicked.connect(self._on_show_issues)
        self.sort.currentIndexChanged.connect(lambda *_: self._load_movies())
        self.view_mode.currentIndexChanged.connect(self._on_view_changed)

        self._apply_toolbar_ui()
        self._load_movies()

        # Update Not Found button label with current count (based on scan_failures)
        self._refresh_not_found_badge()

        if self.ui["window"]["start_maximized"]:
            QTimer.singleShot(0, self.showMaximized)


    def _apply_toolbar_ui(self) -> None:
        tb_cfg = self.ui.get("toolbar", {}) or {}
        colors = self.ui.get("colors", {}) or {}
        ff = self.ui.get("fonts", {}) or {}
        base_family = str(ff.get("base_family", "Segoe UI"))

        self._toolbar_layout.setContentsMargins(int(tb_cfg.get("pad_h", 8)), int(tb_cfg.get("pad_v", 8)), int(tb_cfg.get("pad_h", 8)), int(tb_cfg.get("pad_v", 8)))
        self._toolbar_layout.setSpacing(int(tb_cfg.get("spacing", 8)))
        self._toolbar_widget.setFixedHeight(int(tb_cfg.get("height", 60)))
        self._toolbar_widget.setStyleSheet((self._toolbar_widget.styleSheet() + "\n" if self._toolbar_widget.styleSheet() else "") + f"border-bottom: 1px solid {colors.get('divider', '#3C3F42')};")

        btn_css = (
            f"font-family:{base_family};"
            + _css_font(int(tb_cfg.get("button_font_size", ff.get("base_size", 10))), int(tb_cfg.get("button_font_weight", 400)), str(tb_cfg.get("button_text_color", colors.get("text", "#E6E6E6"))))
            + f" border-width:{int(tb_cfg.get('button_border', 1))}px; border-radius:{int(tb_cfg.get('button_radius', 0))}px; padding:{int(tb_cfg.get('button_padding', 4))}px;"
        )
        for b in (self.btn_scan, self.btn_not_found, self.btn_issues):
            b.setStyleSheet(btn_css)

        self.search.setMinimumWidth(int(tb_cfg.get("search_min_width", 320)))
        self.search.setMaximumWidth(int(tb_cfg.get("search_max_width", 640)))
        self.search.setFixedHeight(int(tb_cfg.get("search_height", 36)))
        self.search.setStyleSheet(
            f"font-family:{base_family};"
            + _css_font(int(tb_cfg.get("search_font_size", ff.get("base_size", 10))), int(tb_cfg.get("search_font_weight", 400)), str(tb_cfg.get("search_text_color", colors.get("text", "#E6E6E6"))))
            + f" border-width:{int(tb_cfg.get('search_border', 1))}px; border-radius:{int(tb_cfg.get('search_radius', 0))}px; padding:{int(tb_cfg.get('search_pad_v', 4))}px {int(tb_cfg.get('search_pad_h', 8))}px;"
        )
        pal = self.search.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(str(tb_cfg.get("search_placeholder_color", colors.get("text2", "#B7B7B7")))))
        self.search.setPalette(pal)

        label_css = f"font-family:{base_family};" + _css_font(int(tb_cfg.get("label_font_size", ff.get("base_size", 10))), int(tb_cfg.get("label_font_weight", 400)), str(tb_cfg.get("label_text_color", colors.get("text", "#E6E6E6"))))
        self.lbl_sort.setStyleSheet(label_css)
        self.lbl_view.setStyleSheet(label_css)

        combo_css = f"font-family:{base_family};" + _css_font(int(tb_cfg.get("combo_font_size", ff.get("base_size", 10))), int(tb_cfg.get("combo_font_weight", 400)), str(tb_cfg.get("combo_text_color", colors.get("text", "#E6E6E6"))))
        self.sort.setStyleSheet(combo_css)
        self.view_mode.setStyleSheet(combo_css)

        total_weight = int(tb_cfg.get("total_movies_weight", 700 if bool(tb_cfg.get("total_movies_bold", True)) else 400))
        self.total_lbl.setStyleSheet(
            f"font-family:{base_family};"
            + _css_font(int(tb_cfg.get("total_movies_size", ff.get("base_size", 10))), total_weight, str(tb_cfg.get("total_movies_color", colors.get("text", "#E6E6E6"))))
        )

    def _apply_toolbar_ui(self) -> None:
        tb = self.ui.get("toolbar", {}) or {}
        colors = self.ui.get("colors", {}) or {}
        panel = colors.get("panel", "#2B2D2F")
        divider = colors.get("divider", "#3C3F42")
        input_bg = colors.get("input_bg", "#1D1F21")

        btn_css = (
            f"font-size:{int(tb.get('button_font_size', 10) or 10)}px; "
            f"font-weight:{int(tb.get('button_font_weight', 400) or 400)}; "
            f"color:{str(tb.get('button_text_color', colors.get('text', '#E6E6E6')))}; "
            f"padding:{int(tb.get('button_padding', 2) or 2)}px; "
            f"border:{int(tb.get('button_border', 1) or 1)}px solid {divider}; "
            f"border-radius:{int(tb.get('button_radius', 0) or 0)}px; background:{panel};"
        )
        for b in (self.btn_scan, self.btn_not_found, self.btn_issues):
            b.setStyleSheet(btn_css)

        search_css = (
            f"font-size:{int(tb.get('search_font_size', 10) or 10)}px; "
            f"font-weight:{int(tb.get('search_font_weight', 400) or 400)}; "
            f"color:{str(tb.get('search_text_color', colors.get('text', '#E6E6E6')))}; "
            f"padding:{int(tb.get('search_pad_v', 5) or 5)}px {int(tb.get('search_pad_h', 10) or 10)}px; "
            f"border:{int(tb.get('search_border', 1) or 1)}px solid {divider}; "
            f"border-radius:{int(tb.get('search_radius', 0) or 0)}px; background:{input_bg};"
        )
        self.search.setStyleSheet(search_css)

        label_css = (
            f"font-size:{int(tb.get('label_font_size', 10) or 10)}px; "
            f"font-weight:{int(tb.get('label_font_weight', 400) or 400)}; "
            f"color:{str(tb.get('label_text_color', colors.get('text', '#E6E6E6')))};"
        )
        for w in (self.lbl_sort, self.lbl_view):
            w.setStyleSheet(label_css)

        combo_css = (
            f"font-size:{int(tb.get('combo_font_size', 10) or 10)}px; "
            f"font-weight:{int(tb.get('combo_font_weight', 400) or 400)}; "
            f"color:{str(tb.get('combo_text_color', colors.get('text', '#E6E6E6')))};"
        )
        self.sort.setStyleSheet(combo_css)
        self.view_mode.setStyleSheet(combo_css)

        tm_weight = int(tb.get('total_movies_weight', 700) or 700)
        if not bool(tb.get('total_movies_bold', True)):
            tm_weight = 400
        self.total_lbl.setStyleSheet(
            f"font-size:{int(tb.get('total_movies_size', 10) or 10)}px; font-weight:{tm_weight}; color:{str(tb.get('total_movies_color', colors.get('text', '#E6E6E6')))};"
        )

    def _apply_pane_sizes_from_ini(self) -> None:
        panes = self.ui.get("panes", {}) or {}
        genres_w = int(panes.get("genres_width") or 0)
        details_w = int(panes.get("details_width") or 0)

        if genres_w > 0 and details_w > 0:
            # Make the middle pane exactly the remaining width (Qt needs an explicit value).
            total_w = int(self.main_splitter.size().width() or 0)
            mid_w = max(1, total_w - genres_w - details_w) if total_w > 0 else 1
            self.main_splitter.setSizes([genres_w, mid_w, details_w])

    def showEvent(self, event):
        super().showEvent(event)
        # Apply once after show so Qt doesn't ignore setSizes().
        if not getattr(self, "_did_apply_panes", False):
            self._apply_pane_sizes_from_ini()
            self._did_apply_panes = True

    def _on_view_changed(self, *_):
        mode = str(self.view_mode.currentData() or "shelf")
        self.movies_pane.set_view_mode(mode)

    def _load_movies(self):
        sort_key = str(self.sort.currentData() or "title")
        rows = self.db.list_movies(sort_key=sort_key)
        self.genres_pane.rebuild_from_movies(rows)
        self.movies_pane.set_movies(rows)
        self.total_lbl.setText(f"Total Movies: {len(rows)}")

    def _refresh_not_found_badge(self) -> None:
        try:
            n = int(self.db.count_scan_failures())
        except Exception:
            n = 0
        if n > 0:
            self.btn_not_found.setText(f"Not Found… ({n})")
        else:
            self.btn_not_found.setText("Not Found…")


    def _on_show_scan_failures(self) -> None:
        dlg = ScanFailuresDialog(self.db, self.tmdb, self.ui, self)
        dlg.resolve_requested.connect(self._resolve_scan_failure_from_dialog)
        dlg.exec()
        self._refresh_not_found_badge()

    def _on_show_issues(self) -> None:
        dlg = IssuesDialog(self.db, self.tmdb, self.ui, self)
        dlg.exec()
        # Keep Not Found badge in sync (Issues uses the same underlying scan_failures table)
        self._refresh_not_found_badge()


    
    def _show_scan_complete_dialog(self, summary_text: str) -> None:
        # Non-modal scan completion notification that persists (keeps a reference),
        # otherwise Qt may destroy it immediately after the function returns.
        try:
            if getattr(self, "_scan_complete_box", None) is not None:
                try:
                    self._scan_complete_box.close()
                except Exception:
                    traceback.print_exc()
                self._scan_complete_box = None

            m = QMessageBox(self)
            m.setIcon(QMessageBox.Information)
            m.setWindowTitle("Scan complete")
            m.setText(summary_text)
            m.setStandardButtons(QMessageBox.Ok)
            QTimer.singleShot(0, m.show)
            try:
                apply_dark_messagebox_style(m, self.ui)
            except Exception:
                traceback.print_exc()

            # Ensure it appears above the main window.
            try:
                m.setWindowModality(Qt.ApplicationModal)
                m.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            except Exception:
                traceback.print_exc()

            self._scan_complete_box = m
            # Defer show() to the event loop so it reliably appears.
        except Exception:
            # As a last resort, do not crash the app because of the notification.
            pass

    def _on_scan_folder(self) -> None:
        # Wrapper: implementation lives at module scope.
        _on_scan_folder_impl(self)

    def _resolve_scan_failure_from_dialog(self, row: dict) -> None:
        """Manual resolver for a single scan_failures row (invoked from the Not Found dialog)."""
        try:
            fp = row.get("file_path") or ""
        except Exception:
            fp = ""
        if not fp:
            return

        # Recompute effective query (folder-first), because the stored row may have junk (wal-oceans12, etc.).
        try:
            parsed_title = row.get("want_title") or ""
            parsed_year = row.get("want_year")
            try:
                parsed_year = int(parsed_year) if parsed_year not in (None, "", "—") else None
            except Exception:
                parsed_year = None
        except Exception:
            parsed_title, parsed_year = "", None

        eff_title, eff_year, _movie_root = derive_scan_query(fp, parsed_title, parsed_year)

        # TMDB search with a small fallback: strip trailing roman numerals (Rocky I -> Rocky)
        results: List[Dict[str, Any]] = []
        try:
            results = self.tmdb.search_movie(eff_title, eff_year, language=self.tmdb_language) or []
        except Exception:
            results = []

        if not results:
            alt = strip_trailing_roman_numeral(eff_title)
            if alt and alt.lower() != (eff_title or "").lower():
                try:
                    results = self.tmdb.search_movie(alt, eff_year, language=self.tmdb_language) or []
                    eff_title = alt
                except Exception:
                    results = []

        if not results:
            mb = QMessageBox(self)
            mb.setIcon(QMessageBox.Information)
            mb.setWindowTitle("No TMDB Results")
            y = f" ({eff_year})" if eff_year else ""
            mb.setText(f"No TMDB results for:\n{eff_title}{y}\n\nPath:\n{fp}")
            mb.setStandardButtons(QMessageBox.Ok)
            apply_dark_messagebox_style(mb, self.ui)
            mb.exec()
            return

        ranked = sorted(results, key=lambda r: score_tmdb_result(eff_title, eff_year, r), reverse=True)
        pre = ranked[0].get("id") if ranked else None
        try:
            pre = int(pre) if pre is not None else None
        except Exception:
            pre = None

        dlg = TMDBPickDialog(
            self,
            self.tmdb,
            eff_title,
            eff_year,
            ranked,
            preselect_tmdb_id=pre,
            want_title=eff_title,
            want_year=eff_year,
            src_title=parsed_title,
            src_year=parsed_year,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        pick_id = dlg.selected_tmdb_id()
        if pick_id is None:
            return

        # Respect uniqueness: if already in DB, update path when existing is missing/dead.
        if self.db.tmdb_exists(int(pick_id)):
            try:
                existing_path = self.db.get_file_path_by_tmdb_id(int(pick_id))
            except Exception:
                existing_path = None
            can_update = False
            if not existing_path:
                can_update = True
            else:
                try:
                    can_update = not os.path.exists(existing_path)
                except Exception:
                    can_update = True
            if can_update:
                try:
                    self.db.update_movie_file_path_by_tmdb_id(int(pick_id), fp)
                except Exception:
                    pass
            # In any case, remove failure row since it is now resolved/linked.
            try:
                self.db.delete_scan_failure(fp)
            except Exception:
                traceback.print_exc()
            return

        tm = self.tmdb.get_movie_details(int(pick_id), language=self.tmdb_language)


        # Year Integrity (fix46): if details came back without a year, bypass cache and refetch once.


        if not getattr(tm, "year", None):


            try:


                tm = self.tmdb.get_movie_details(int(pick_id), language=self.tmdb_language, force_refresh=True)


            except Exception:


                pass


        


        # last resort: use effective_year (parsed from path) if still missing


        if not getattr(tm, "year", None):


            try:


                tm.year = int(effective_year) if effective_year else None


            except Exception:


                pass


        


        poster_local = self.tmdb.poster_file(tm.poster_path)
        backdrop_local = self.tmdb.backdrop_file(tm.backdrop_path)

        people_profiles: Dict[int, Optional[str]] = {}
        for pid, _name, _role, profile_path in tm.cast:
            if pid not in people_profiles:
                people_profiles[pid] = self.tmdb.profile_file(profile_path)
        for pid, _name, _job, profile_path in tm.crew:
            if pid not in people_profiles:
                people_profiles[pid] = self.tmdb.profile_file(profile_path)

        try:
            st = os.stat(fp)
            mtime, fsize = int(st.st_mtime), int(st.st_size)
        except Exception:
            mtime, fsize = None, None

        try:
            self.db.insert_movie_from_tmdb(
                tm=tm,
                file_path=fp,
                file_mtime=mtime,
                file_size=fsize,
                poster_local=poster_local,
                backdrop_local=backdrop_local,
                people_profiles=people_profiles,
            )
        except Exception:
            # If insert fails for any reason, keep the failure row for inspection.
            return

        try:
            self.db.delete_scan_failure(fp)
        except Exception:
            pass

        # Refresh UI
        try:
            self.movies_pane.reload_from_db()
            self.genre_pane.refresh_counts()
        except Exception:
            pass

def _on_scan_folder_impl(self):
    if not self.ui["tmdb"]["api_key"]:
        QMessageBox.warning(self, "TMDB API key missing", "Please set [tmdb] api_key in ui.ini and restart.")
        return

    folder = QFileDialog.getExistingDirectory(self, "Select folder to scan", "")
    if not folder:
        return

    recursive = bool(self.ui["library"].get("recursive_scan", True))
    files: List[str] = []
    if recursive:
        markers = [m.lower() for m in (self.ui["library"].get("skip_folders") or [])]
        for base, dirs, fnames in os.walk(folder):
            # prune directories that contain any marker (e.g., 'SERIALS')
            if markers:
                parts = [p.lower() for p in os.path.normpath(base).split(os.sep) if p]
                if any(m in parts for m in markers):
                    dirs[:] = []
                    continue
                # also prevent descending into marked subfolders
                dirs[:] = [d for d in dirs if d.lower() not in markers]
            for fn in fnames:
                p = os.path.join(base, fn)
                if is_video_file(p):
                    files.append(p)
    else:
        for fn in os.listdir(folder):
            p = os.path.join(folder, fn)
            if os.path.isfile(p) and is_video_file(p):
                files.append(p)
    files.sort(key=lambda x: x.lower())

    if not files:
        QMessageBox.information(self, "No videos found", "No video files found in folder.")
        return

    added = 0
    skipped_existing = 0                 # same file_path already in DB
    skipped_multipart = 0                # multi-part rips (CD1/CD2) skipped by dedupe
    skipped_tv = 0                       # filename/path looks like TV episode
    skipped_tv_filtered = 0              # TMDB returned results but all filtered out as non-movie
    skipped_tmdb_duplicate = 0           # same TMDB movie already in DB (no path change)
    updated_existing_path = 0            # same TMDB movie exists; we updated path because old path missing
    no_results = 0                       # TMDB search returned no movie results
    no_results_logged = 0
    no_results_log_failed = 0
    skipped_user = 0                     # user pressed Skip / canceled the pick dialog
    errors = 0
    error_details: List[str] = []

    processed_movie_roots: set = set()  # dedupe CD1/CD2 etc by movie root folder

    self.status.showMessage(f"Scanning {len(files)} files…")
    QApplication.processEvents()

    for i, fp in enumerate(files, start=1):
        if looks_like_tv_episode(fp):
            skipped_tv += 1
            continue

        # Fast path: exact file already recorded.
        if self.db.file_exists(fp):
            skipped_existing += 1
            continue

        parsed = parse_movie_name(fp)
        want_title = parsed.title
        want_year = parsed.year

        # Smart fallback parsing (from file + parent folders) to recover missing years / messy names.
        smart_title, smart_year, year_hits = smart_parse_title_year_from_path(fp)

        if is_junk_wanted_title(want_title) and smart_title:
            want_title = smart_title

        if want_year is None and smart_year:
            want_year = smart_year

        eff_title, eff_year, movie_root = derive_scan_query(fp, want_title, want_year)

        # Dedupe multipart releases (CD1/CD2, Part1/Part2, etc.)
        # so they don't double-count or break matching.
        if movie_root and _looks_like_multipart_release(fp):
            if movie_root in processed_movie_roots:
                skipped_multipart += 1
                continue
            processed_movie_roots.add(movie_root)

        self.status.showMessage(
            f"[{i}/{len(files)}] TMDB: {eff_title}" + (f" ({eff_year})" if eff_year else "")
        )
        QApplication.processEvents()

        movie_t0 = time.monotonic()

        try:
            raw_results = []
            results: List[Dict[str, Any]] = []
            title_for_match = eff_title

            def _guard_movie_time():
                # Prevent pathological cases from stalling the entire scan.
                if time.monotonic() - movie_t0 > 25:
                    raise TimeoutError("Per-movie processing timeout")
            any_raw = False
            any_raw_filtered = False

            smart_title, smart_year, _smart_years = smart_parse_title_year_from_path(fp)
            # Choose an effective year for TMDB querying.
            effective_year = eff_year
            # If the title itself is a year (e.g. "1917") and we have a better year from smart parsing, prefer it.
            if smart_year is not None:
                try:
                    if effective_year is None:
                        effective_year = smart_year
                    else:
                        # keep folder-derived year unless it looks obviously wrong
                        if re.fullmatch(r"\d{4}", str(eff_title or "")) and int(str(eff_title)) == effective_year:
                            effective_year = smart_year
                except Exception:
                    pass

            def _scan_title_variants(title: str, year: Optional[int]) -> List[Tuple[str, Optional[int]]]:
                """Return ordered (query, year) variants for TMDB search."""
                seen: set[Tuple[str, Optional[int]]] = set()
                out: List[Tuple[str, Optional[int]]] = []
                def _push(q: str, y: Optional[int]) -> None:
                    q2 = ' '.join((q or '').split()).strip()
                    if not q2:
                        return
                    key = (q2.casefold(), y)
                    if key in seen:
                        return
                    seen.add(key)
                    out.append((q2, y))
            
                base = (title or '').strip()
                _push(base, year)
            
                # 1) Remove bracketed tags: [1080p], (REMASTERED), 5.1, etc.
                q = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", base)
                q = re.sub(r"[._]+", " ", q)
                q = re.sub(r"\s+", " ", q).strip()
                if q and q.casefold() != base.casefold():
                    _push(q, year)
            
                # 2) Drop common release-noise tokens that frequently poison TMDB queries.
                noise_re = re.compile(r"\b(?:1080p|720p|2160p|4k|hdr|sdr|web[- ]?rip|webrip|web[- ]?dl|bluray|brrip|hdrip|dvdrip|x264|x265|h\.?264|h\.?265|hevc|aac\d(?:\.\d)?|ddp?\d(?:\.\d)?|dts|eac3|nf|yts(?:\.[a-z0-9]+)?|rarbg|galaxyrg|etrg|evo|anoxmous|proper|repack|limited|internal|remastered|extended|theatrical|dc|uncut|subbed|dubbed|multi|dual|greek|eng|ita|spa|french|\d+mb|\d+gb)\b", re.IGNORECASE)
                q2 = noise_re.sub(" ", q)
                q2 = re.sub(r"\b(?:cd|disc)\s*\d+\b", " ", q2, flags=re.IGNORECASE)
                q2 = re.sub(r"\s+", " ", q2).strip()
                if q2 and q2.casefold() not in (base.casefold(), q.casefold()):
                    _push(q2, year)
            
                # 3) If title ends with a sequel number like 'Samurai Cop 1', try without it.
                m = re.match(r"^(.*?)(?:\s+(?:part|pt)\s*)?(\d+)$", q2, flags=re.IGNORECASE)
                if m:
                    prefix = m.group(1).strip()
                    num = m.group(2)
                    if num and len(num) <= 2 and prefix:
                        _push(prefix, year)
            
                return out
            
            attempted_variants: List[Tuple[str, Optional[int]]] = []

            for cand_title, cand_year in _scan_title_variants(eff_title, effective_year):
                # 1) Try with year (when available) first.
                attempted_variants.append((cand_title, cand_year))
                _guard_movie_time()
                raw_results = self.tmdb.search_movie(cand_title, year=cand_year, language=self.tmdb_language)
                if raw_results:
                    any_raw = True

                # Movies-only scan policy
                results = [
                    r for r in raw_results
                    if (str(r.get('media_type') or 'movie') == 'movie')
                    and (r.get('title') or r.get('original_title') or r.get('release_date'))
                    and not (r.get('name') and not r.get('title'))
                    and not r.get('first_air_date')
                ]

                if raw_results and not results:
                    # TMDB returned something, but everything got filtered out as TV/non-movie.
                    any_raw_filtered = True
                    results = []
                if results:
                    title_for_match = cand_title
                    break

                # 2) If no results and we used a year filter, retry WITHOUT year.
                if cand_year is not None:
                    attempted_variants.append((cand_title, None))
                    _guard_movie_time()
                    raw_results2 = self.tmdb.search_movie(cand_title, year=None, language=self.tmdb_language)
                    if raw_results2:
                        any_raw = True

                    results2 = [
                        r for r in raw_results2
                        if (str(r.get('media_type') or 'movie') == 'movie')
                        and (r.get('title') or r.get('original_title') or r.get('release_date'))
                        and not (r.get('name') and not r.get('title'))
                        and not r.get('first_air_date')
                    ]

                    if raw_results2 and not results2:
                        any_raw_filtered = True
                        results2 = []

                    if results2:
                        raw_results = raw_results2
                        results = results2
                        title_for_match = cand_title
                        break
            if any_raw and not results and any_raw_filtered:
                skipped_tv_filtered += 1
                continue

            if not results:
                # If TMDB yields no results, but the movie might already exist in our DB (e.g. filename has
                # sequel markers like 'I' / 'Part I' and our TMDB lookup fails), try a conservative local match.
                local_tmdb_id: Optional[int] = None
                if cand_year is not None:
                    for cand_title, cand_year in _scan_title_variants(want_title, want_year or effective_year):
                        local_tmdb_id = self.db.find_tmdb_id_by_title_year(cand_title, int(cand_year))
                        if local_tmdb_id is not None:
                            break

                if local_tmdb_id is not None:
                    # Treat as existing movie; update path only if the stored path is missing/dead.
                    try:
                        existing_path = self.db.get_file_path_by_tmdb_id(int(local_tmdb_id))
                    except Exception:
                        existing_path = None

                    can_update = False
                    if not existing_path:
                        can_update = True
                    else:
                        try:
                            can_update = not os.path.exists(existing_path)
                        except Exception:
                            can_update = True

                    if can_update:
                        try:
                            self.db.update_movie_file_path_by_tmdb_id(int(local_tmdb_id), fp)
                            updated_existing_path += 1
                        except Exception:
                            skipped_tmdb_duplicate += 1
                    else:
                        skipped_tmdb_duplicate += 1
                    continue
                try:
                    self.db.upsert_scan_failure(
                        file_path=fp,
                        want_title=want_title,
                        want_year=want_year,
                        smart_title=smart_title,
                        effective_year=effective_year,
                        attempted_variants=attempted_variants,
                        reason="no_tmdb_results",
                    )
                    no_results_logged += 1
                except Exception as e:
                    no_results_log_failed += 1
                    # don't abort scan, but do record the failure so we can see what's wrong
                    error_details.append(f"{fp} :: scan_failures insert failed: {e}")
                no_results += 1
                append_scan_debug("no_tmdb_results", fp, want_title=str(want_title), want_year=str(effective_year), smart_title=str(smart_title), eff_year=str(effective_year))
                continue
            ranked = sorted(results, key=lambda r: score_tmdb_result(title_for_match, effective_year, r), reverse=True)
            best = ranked[0] if ranked else None
            pick_id: Optional[int] = None

            # Helper: normalize TMDB id.
            def _norm_id(r: dict) -> Optional[int]:
                rid = r.get("id")
                if isinstance(rid, int):
                    return rid
                if isinstance(rid, str) and rid.isdigit():
                    return int(rid)
                return None

            # 1) If any candidate is ALREADY in DB (same TMDB movie), prefer it to avoid re-prompting.
            #    IMPORTANT: only allow this shortcut when the year is compatible.
            def _result_year(r: dict) -> Optional[int]:
                rdate = (r.get("release_date") or r.get("first_air_date") or "").strip()
                if len(rdate) >= 4 and rdate[:4].isdigit():
                    try:
                        return int(rdate[:4])
                    except Exception:
                        return None
                return None

            existing_candidates: List[Tuple[float, int]] = []
            for r in ranked[:8]:
                rid = _norm_id(r)
                if rid is None:
                    continue
                if not self.db.tmdb_exists(int(rid)):
                    continue

                # If we have an effective year, do NOT prefer an existing DB candidate with a big year mismatch.
                ry = _result_year(r)
                if effective_year is not None and ry is not None:
                    if abs(int(ry) - int(effective_year)) > 1:
                        continue

                sc = score_tmdb_result(title_for_match, effective_year, r)
                existing_candidates.append((sc, int(rid)))

            if existing_candidates:
                existing_candidates.sort(reverse=True)
                best_sc, best_rid = existing_candidates[0]

                # score_tmdb_result returns scores in the thousands (exact title ~6000, exact title+year ~8200).
                # Use a realistic threshold so we don't incorrectly hijack matches (e.g. Road House 1989 vs 2024).
                if best_sc >= 4800.0:
                    pick_id = best_rid

            # 2) Normal auto-pick (for new movies).
            if pick_id is None and best:
                best_score = score_tmdb_result(title_for_match, want_year, best)
                second_score = score_tmdb_result(title_for_match, want_year, ranked[1]) if len(ranked) > 1 else -999.0
                gap = best_score - second_score
                best_id = _norm_id(best)
                if want_year is not None and best_id is not None and (best_score >= 5.4 and gap >= 0.8):
                    pick_id = best_id

            # 3) Ask user when not sure.
            if pick_id is None:
                pre: Optional[int] = _norm_id(best) if best else None
                dlg = TMDBPickDialog(
                    self,
                    self.tmdb,
                    title_for_match,
                    effective_year,
                    results,
                    preselect_tmdb_id=pre,
                    want_title=eff_title,
                    want_year=(eff_year if eff_year else effective_year),
                    src_title=want_title,
                    src_year=want_year,
                )
                if dlg.exec() != QDialog.Accepted:
                    skipped_user += 1
                    append_scan_debug("user_skip", fp, want_title=str(want_title), want_year=str(effective_year), smart_title=str(smart_title), eff_year=str(effective_year))
                    continue
                pick_id = dlg.selected_tmdb_id()
                if pick_id is None:
                    skipped_user += 1
                    continue

            # tmdb_id is UNIQUE in schema -> can't store “2 copies” as separate rows.
            if self.db.tmdb_exists(int(pick_id)):
                # If the existing entry has a missing/dead path, update it to the newly found path.
                try:
                    existing_path = self.db.get_file_path_by_tmdb_id(int(pick_id))
                except Exception:
                    existing_path = None

                can_update = False
                if not existing_path:
                    can_update = True
                else:
                    try:
                        can_update = not os.path.exists(existing_path)
                    except Exception:
                        can_update = True

                if can_update:
                    try:
                        self.db.update_movie_file_path_by_tmdb_id(int(pick_id), fp)
                        updated_existing_path += 1
                    except Exception:
                        # If update fails, fall back to counting as duplicate.
                        skipped_tmdb_duplicate += 1
                else:
                    skipped_tmdb_duplicate += 1
                continue

            tm = self.tmdb.get_movie_details(int(pick_id), language=self.tmdb_language)


            # Year Integrity (fix46): if details came back without a year, bypass cache and refetch once.


            if not getattr(tm, "year", None):


                try:


                    tm = self.tmdb.get_movie_details(int(pick_id), language=self.tmdb_language, force_refresh=True)


                except Exception:


                    pass


            


            # last resort: use effective_year (parsed from path) if still missing


            if not getattr(tm, "year", None):


                try:


                    tm.year = int(effective_year) if effective_year else None


                except Exception:


                    pass


            


            poster_local = self.tmdb.poster_file(tm.poster_path)
            backdrop_local = self.tmdb.backdrop_file(tm.backdrop_path)

            people_profiles: Dict[int, Optional[str]] = {}
            for pid, _name, _role, profile_path in tm.cast:
                if pid not in people_profiles:
                    people_profiles[pid] = self.tmdb.profile_file(profile_path)
            for pid, _name, _job, profile_path in tm.crew:
                if pid not in people_profiles:
                    people_profiles[pid] = self.tmdb.profile_file(profile_path)

            try:
                st = os.stat(fp)
                mtime, fsize = int(st.st_mtime), int(st.st_size)
            except Exception:
                mtime, fsize = None, None

            self.db.insert_movie_from_tmdb(
                tm=tm,
                file_path=fp,
                file_mtime=mtime,
                file_size=fsize,
                poster_local=poster_local,
                backdrop_local=backdrop_local,
                people_profiles=people_profiles,
            )
            added += 1
            append_scan_debug("added", fp, want_title=str(want_title), want_year=str(effective_year), smart_title=str(smart_title), eff_year=str(effective_year), tmdb_id=str(pick_id), picked_title=str(getattr(tm, "title", "") if tm else ""), picked_year=str(getattr(tm, "year", "") if tm else ""))

        except Exception as e:
            errors += 1
            try:
                yr = want_year if want_year is not None else "—"
            except Exception:
                yr = "—"
            error_details.append(f"{fp} :: {want_title} ({yr}) :: {type(e).__name__}: {e}")
            append_scan_debug("exception", fp, want_title=str(want_title), want_year=str(yr), note=f"{type(e).__name__}: {e}")
            error_details.append(traceback.format_exc())
            continue


    # --- Scan completion marker (debug) ---
    try:
        append_scan_debug("scan_end", "", want_title="", want_year="", smart_title="", eff_year="", tmdb_id="", picked_title="", picked_year="", extra=f"added={added} skipped_existing={skipped_existing} skipped_user={skipped_user} no_results={no_results} errors={len(error_details)}")
    except Exception:
        pass

    try:
        self._load_movies()
        self._refresh_not_found_badge()
        self.status.showMessage("Ready.")
        try:
            append_scan_debug("post_loop_ui_ok", "", extra="ui_refresh_ok")
        except Exception:
            pass
    except Exception as e:
        error_details.append(f"POST-LOOP UI ERROR: {type(e).__name__}: {e}")
        error_details.append(traceback.format_exc())
        try:
            append_scan_debug("post_loop_ui_exception", "", note=f"{type(e).__name__}: {e}")
        except Exception:
            pass


    errors = len(error_details)

    # NOTE: Use real newlines (not literal "\n").
    summary = "\n".join([
        f"Added: {added}",
        f"Skipped (already in DB): {skipped_existing}",
        f"Skipped (multi-part dedupe CD1/CD2): {skipped_multipart}",
        f"Updated (existing movie path): {updated_existing_path}",
        f"Skipped (same TMDB movie): {skipped_tmdb_duplicate}",
        f"Skipped (TV episodes - filename/path): {skipped_tv}",
        f"Skipped (TV results filtered): {skipped_tv_filtered}",
        f"No TMDB results (movies): {no_results} (logged: {no_results_logged}, log failed: {no_results_log_failed})",
        f"Skipped (you pressed Skip): {skipped_user}",
        f"Errors: {errors}",
    ])

    try:
        append_scan_debug("before_scan_finished_msg", "", extra=f"errors={len(error_details)}")
    except Exception:
        pass

    # NOTE: Show a visible scan summary at the end. Info-bar messages are easy to miss.
    imported = added
    not_found = no_results
    try:
        if errors or error_details:
            self.set_info(f"Scan complete (with issues). Imported: {imported} | Not found: {not_found} | Skipped: {skipped_user} | Errors: {errors}")
        else:
            self.set_info(f"Scan complete. Imported: {imported} | Not found: {not_found} | Skipped: {skipped_user} | Errors: {errors}")
    except Exception:
        pass
    try:
        summary_text = (
            f"Imported: {imported}\n"
            f"Not found: {not_found}\n"
            f"Skipped: {skipped_user}\n"
            f"Errors: {errors}"
        )
        self._show_scan_complete_dialog(summary_text)
    except Exception:
        # Never fail the scan completion just because the UI summary failed.
        traceback.print_exc()



def main():
    db_path = os.path.join(APP_DIR, "movies.db")
    ensure_db(db_path)
    ensure_db_compat(db_path)
    ensure_scan_failures_table(db_path)

    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())



if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
