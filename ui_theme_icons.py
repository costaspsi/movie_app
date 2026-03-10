# ui_theme_icons.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, QByteArray, QSize
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QPalette
from PySide6.QtSvg import QSvgRenderer


@dataclass(frozen=True)
class Theme:
    # neutrals
    bg: str = "#f5f4ee"
    panel: str = "#f5f4ee"
    raised: str = "#ffffff"
    divider: str = "#c9c7bf"

    # text
    text: str = "#111111"
    text2: str = "#4a4a4a"

    # accents
    teal: str = "#2aa198"
    teal_hi: str = "#4cd6cf"
    gold: str = "#b58900"

    # inputs
    input_bg: str = "#ffffff"

    selection_bg: str = "rgba(42, 161, 152, 0.18)"
    selection_text: str = "#111111"


DEFAULT_THEME = Theme()


def _luma(hex_color: str) -> float:
    try:
        c = QColor(hex_color)
        r, g, b = c.red(), c.green(), c.blue()
        # perceived luminance
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    except Exception:
        return 255.0


def _is_light_bg(hex_color: str) -> bool:
    return _luma(hex_color) >= 160.0


def theme_from_ui(ui: Optional[Dict[str, Any]]) -> Theme:
    """
    Builds a Theme from ui.ini-like dict:
      ui["theme"]["accent"], ui["theme"]["accent2"], ui["theme"]["bg"], ui["theme"]["fg"]
    """
    t = (ui or {}).get("theme") if isinstance(ui, dict) else None
    if not isinstance(t, dict):
        return DEFAULT_THEME

    accent = str(t.get("accent", DEFAULT_THEME.teal))
    accent2 = str(t.get("accent2", DEFAULT_THEME.gold))
    bg = str(t.get("bg", DEFAULT_THEME.bg))
    fg = str(t.get("fg", DEFAULT_THEME.text))

    light = _is_light_bg(bg)

    if light:
        return Theme(
            bg=bg,
            panel=bg,
            raised="#ffffff",
            divider="#c9c7bf",
            text=fg if fg else "#111111",
            text2="#4a4a4a",
            teal=accent,
            teal_hi=accent,
            gold=accent2,
            input_bg="#ffffff",
            selection_bg="rgba(42, 161, 152, 0.18)",
            selection_text=fg if fg else "#111111",
        )
    else:
        # dark-ish bg provided: choose safe dark defaults but keep accents
        return Theme(
            bg=bg,
            panel="#2B2D2F",
            raised="#303336",
            divider="#3C3F42",
            text=fg if fg else "#E6E6E6",
            text2="#B7B7B7",
            teal=accent,
            teal_hi=accent,
            gold=accent2,
            input_bg="#1D1F21",
            selection_bg="rgba(42, 161, 152, 0.20)",
            selection_text=fg if fg else "#E6E6E6",
        )


def apply_app_theme(app, theme: Theme, ui_overrides: Optional[Dict[str, Any]] = None, extra_qss: str = "") -> None:
    ui = ui_overrides or {}

    # --------- PALETTE ----------
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(theme.bg))
    pal.setColor(QPalette.Base, QColor(theme.raised))
    pal.setColor(QPalette.AlternateBase, QColor(theme.panel))
    pal.setColor(QPalette.Button, QColor(theme.panel))
    pal.setColor(QPalette.ToolTipBase, QColor(theme.raised))
    pal.setColor(QPalette.ToolTipText, QColor(theme.text))

    pal.setColor(QPalette.Text, QColor(theme.text))
    pal.setColor(QPalette.WindowText, QColor(theme.text))
    pal.setColor(QPalette.ButtonText, QColor(theme.text))
    pal.setColor(QPalette.PlaceholderText, QColor(theme.text2))

    pal.setColor(QPalette.Highlight, QColor(theme.teal))
    pal.setColor(QPalette.HighlightedText, QColor(theme.selection_text))

    app.setPalette(pal)

    # --------- QSS (make widgets readable in LIGHT UI too) ----------
    radius_btn = int(ui.get("radius_button", 10))
    radius_in = int(ui.get("radius_input", 12))
    border_w = int(ui.get("border_w", 1))
    pad_v = int(ui.get("pad_v", 6))
    pad_h = int(ui.get("pad_h", 10))

    qss = f"""
    QMainWindow {{
        background: {theme.bg};
    }}

    QLabel {{
        color: {theme.text};
    }}

    QPushButton {{
        background: {theme.panel};
        color: {theme.text};
        border: {border_w}px solid {theme.divider};
        border-radius: {radius_btn}px;
        padding: {pad_v}px {pad_h}px;
    }}
    QPushButton:hover {{
        border: {border_w}px solid {theme.teal};
        background: rgba(0,0,0,0.03);
    }}
    QPushButton:disabled {{
        color: rgba(0,0,0,0.35);
        border-color: rgba(0,0,0,0.15);
        background: rgba(0,0,0,0.03);
    }}

    QLineEdit {{
        background: {theme.input_bg};
        color: {theme.text};
        border: {border_w}px solid {theme.divider};
        border-radius: {radius_in}px;
        padding: {pad_v}px {pad_h}px;
    }}
    QLineEdit:focus {{
        border: {border_w}px solid {theme.teal};
    }}

    QComboBox {{
        background: {theme.input_bg};
        color: {theme.text};
        border: {border_w}px solid {theme.divider};
        border-radius: {radius_in}px;
        padding: {pad_v}px {pad_h}px;
    }}
    QComboBox:disabled {{
        color: rgba(0,0,0,0.35);
        border-color: rgba(0,0,0,0.15);
        background: rgba(0,0,0,0.03);
    }}

    QListWidget, QListView {{
        background: {theme.raised};
        color: {theme.text};
        border: {border_w}px solid {theme.divider};
    }}
    QListWidget::item:selected, QListView::item:selected {{
        background: {theme.selection_bg};
        color: {theme.selection_text};
    }}

    QSplitter::handle {{
        background: rgba(0,0,0,0.06);
    }}
    """

    if extra_qss:
        qss += "\n" + extra_qss + "\n"

    app.setStyleSheet(qss)


def _render_svg_to_pixmap(svg_text: str, size: QSize) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    pm = QPixmap(size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return pm


def _tint_pixmap(pm: QPixmap, color: QColor) -> QPixmap:
    tinted = QPixmap(pm.size())
    tinted.fill(Qt.transparent)
    p = QPainter(tinted)
    p.drawPixmap(0, 0, pm)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(tinted.rect(), color)
    p.end()
    return tinted


def icon_from_svg_file(path: str, px: int = 24, color_hex: Optional[str] = None) -> QIcon:
    try:
        with open(path, "r", encoding="utf-8") as f:
            svg = f.read()
    except Exception:
        return QIcon()

    base_pm = _render_svg_to_pixmap(svg, QSize(px, px))
    color = QColor(color_hex or DEFAULT_THEME.teal)
    return QIcon(_tint_pixmap(base_pm, color))


# ---------------------------------------------------------------------
# Entry point expected by main.py
# ---------------------------------------------------------------------
def apply_ui_theme_and_icons(main_window=None, ui: Optional[Dict[str, Any]] = None) -> Theme:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return DEFAULT_THEME

    theme = theme_from_ui(ui if isinstance(ui, dict) else None)

    # ui may also contain a dedicated ui-overrides bucket — keep it optional
    ui_overrides: Dict[str, Any] = {}
    if isinstance(ui, dict) and isinstance(ui.get("ui"), dict):
        ui_overrides = ui["ui"]

    apply_app_theme(app, theme, ui_overrides=ui_overrides)
    return theme
