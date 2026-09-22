"""Design tokens and Qt stylesheet generation for dark and light themes."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

FONT_FAMILIES = ["Inter", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Cantarell", "Ubuntu", "Arial"]
MONO_FAMILIES = ["JetBrains Mono", "Cascadia Mono", "SF Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "monospace"]


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    sidebar: str
    surface: str
    surface_alt: str
    elevated: str
    hover: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_text: str
    accent_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    selection: str
    shadow: str
    scrim: str

    def color(self, token: str, alpha: float | None = None) -> QColor:
        color = QColor(getattr(self, token))
        if alpha is not None:
            color.setAlphaF(alpha)
        return color

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


DARK = Palette(
    name="dark",
    bg="#0A0C10",
    sidebar="#0D1015",
    surface="#12151B",
    surface_alt="#161A22",
    elevated="#1A1F28",
    hover="#1C212B",
    border="#222733",
    border_strong="#2E3544",
    text="#E9ECF2",
    text_muted="#9097A8",
    text_faint="#5E6576",
    accent="#7C83FF",
    accent_hover="#8F95FF",
    accent_text="#FFFFFF",
    accent_soft="#1E2142",
    success="#3DD68C",
    success_soft="#12291F",
    warning="#F5B942",
    warning_soft="#2D2412",
    danger="#F2555A",
    danger_soft="#2E1517",
    selection="#1F2340",
    shadow="#000000",
    scrim="#000000",
)

LIGHT = Palette(
    name="light",
    bg="#F4F5F8",
    sidebar="#FBFBFD",
    surface="#FFFFFF",
    surface_alt="#F7F8FA",
    elevated="#FFFFFF",
    hover="#F0F1F5",
    border="#E3E5EB",
    border_strong="#D2D6DE",
    text="#12151C",
    text_muted="#5A6172",
    text_faint="#9097A6",
    accent="#5A61EE",
    accent_hover="#4B52E0",
    accent_text="#FFFFFF",
    accent_soft="#ECEDFF",
    success="#12A86A",
    success_soft="#E3F6EE",
    warning="#C98A0B",
    warning_soft="#FBF1DC",
    danger="#DC3D43",
    danger_soft="#FCE8E9",
    selection="#E9EAFF",
    shadow="#1B2140",
    scrim="#0B0D12",
)


def _stylesheet(p: Palette) -> str:
    font = ", ".join(f'"{f}"' for f in FONT_FAMILIES)
    return f"""
* {{
    font-family: {font};
    outline: none;
}}
QWidget {{
    color: {p.text};
    font-size: 13px;
}}
QMainWindow, QWidget#AppRoot, QStackedWidget#Pages, QWidget#Page {{
    background: {p.bg};
}}
QScrollArea, QScrollArea > QWidget > QWidget#ScrollContent {{
    background: transparent;
    border: none;
}}
QToolTip {{
    background: {p.elevated};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 6px 9px;
}}

/* ------------------------------------------------------------ sidebar */
QFrame#Sidebar {{
    background: {p.sidebar};
    border: none;
    border-right: 1px solid {p.border};
}}
QLabel#BrandTitle {{
    font-size: 14px;
    font-weight: 600;
    color: {p.text};
}}
QLabel#BrandSubtitle {{
    font-size: 11px;
    color: {p.text_faint};
}}
QLabel#SidebarSection {{
    font-size: 10px;
    font-weight: 600;
    color: {p.text_faint};
    padding-left: 12px;
}}
QPushButton#NavButton {{
    text-align: left;
    padding: 9px 12px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    color: {p.text_muted};
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#NavButton:hover {{
    background: {p.hover};
    color: {p.text};
}}
QPushButton#NavButton:checked {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    color: {p.text};
    font-weight: 600;
}}
QFrame#ConnectionCard {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QLabel#ConnTitle {{ font-weight: 600; font-size: 12px; }}
QLabel#ConnDetail {{ color: {p.text_muted}; font-size: 11px; }}

/* ------------------------------------------------------------- header */
QLabel#H1 {{
    font-size: 22px;
    font-weight: 700;
    color: {p.text};
}}
QLabel#H2 {{
    font-size: 15px;
    font-weight: 600;
    color: {p.text};
}}
QLabel#Subtitle {{
    font-size: 13px;
    color: {p.text_muted};
}}
QLabel#Muted {{ color: {p.text_muted}; }}
QLabel#Faint {{ color: {p.text_faint}; font-size: 12px; }}
QLabel#Pill {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 2px 9px;
    color: {p.text_muted};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#CountBadge {{
    background: {p.accent_soft};
    color: {p.accent};
    border-radius: 9px;
    padding: 1px 8px;
    font-size: 11px;
    font-weight: 700;
}}

/* -------------------------------------------------------------- cards */
QFrame#Card {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 16px;
}}
QFrame#StatCard {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 16px;
}}
QFrame#StatCard:hover {{
    border: 1px solid {p.border_strong};
}}
QLabel#StatTitle {{
    font-size: 11px;
    font-weight: 600;
    color: {p.text_muted};
}}
QLabel#StatValue {{
    font-size: 30px;
    font-weight: 700;
    color: {p.text};
}}
QLabel#StatCaption {{
    font-size: 12px;
    color: {p.text_faint};
}}
QFrame#Divider {{
    background: {p.border};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

/* ------------------------------------------------------------ buttons */
QPushButton {{
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 600;
    font-size: 13px;
}}
QPushButton#PrimaryButton {{
    background: {p.accent};
    color: {p.accent_text};
    border: 1px solid {p.accent};
}}
QPushButton#PrimaryButton:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#PrimaryButton:pressed {{ background: {p.accent}; }}
QPushButton#PrimaryButton:disabled {{ background: {p.border_strong}; border-color: {p.border_strong}; color: {p.text_faint}; }}
QPushButton#SecondaryButton {{
    background: {p.surface_alt};
    color: {p.text};
    border: 1px solid {p.border};
}}
QPushButton#SecondaryButton:hover {{ background: {p.hover}; border-color: {p.border_strong}; }}
QPushButton#SecondaryButton:pressed {{ background: {p.surface}; }}
QPushButton#SecondaryButton:disabled {{ color: {p.text_faint}; }}
QPushButton#GhostButton {{
    background: transparent;
    color: {p.text_muted};
    border: 1px solid transparent;
    padding: 6px 10px;
}}
QPushButton#GhostButton:hover {{ background: {p.hover}; color: {p.text}; }}
QPushButton#DangerButton {{
    background: transparent;
    color: {p.danger};
    border: 1px solid {p.border};
}}
QPushButton#DangerButton:hover {{ background: {p.danger_soft}; border-color: {p.danger}; }}
QPushButton#DangerButton:disabled {{ color: {p.text_faint}; background: transparent; border-color: {p.border}; }}
QPushButton#IconButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 5px;
}}
QPushButton#IconButton:hover {{ background: {p.hover}; border-color: {p.border}; }}

/* ------------------------------------------------------------- inputs */
QLineEdit, QSpinBox, QDateTimeEdit, QComboBox {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 10px;
    padding: 7px 11px;
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
    min-height: 20px;
}}
QLineEdit:hover, QSpinBox:hover, QDateTimeEdit:hover, QComboBox:hover {{
    border-color: {p.border_strong};
}}
QLineEdit:focus, QSpinBox:focus, QDateTimeEdit:focus, QComboBox:focus {{
    border-color: {p.accent};
    background: {p.surface};
}}
QLineEdit:disabled {{ color: {p.text_faint}; }}
QLineEdit#SearchField {{
    padding-left: 6px;
    min-width: 200px;
}}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{
    border: none;
    width: 26px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
QComboBox::down-arrow {{
    image: url("{{CHEVRON}}");
    width: 14px;
    height: 14px;
}}
QComboBox QAbstractItemView {{
    background: {p.elevated};
    border: 1px solid {p.border_strong};
    border-radius: 10px;
    padding: 4px;
    selection-background-color: {p.selection};
    selection-color: {p.text};
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    min-height: 28px;
    padding: 2px 8px;
    border-radius: 6px;
}}
QSpinBox::up-button, QSpinBox::down-button, QDateTimeEdit::up-button, QDateTimeEdit::down-button {{
    width: 0px;
    border: none;
}}
QDateTimeEdit::drop-down {{
    border: none;
    width: 26px;
}}
QDateTimeEdit::down-arrow {{
    image: url("{{CHEVRON}}");
    width: 14px;
    height: 14px;
}}
QCalendarWidget QWidget {{ alternate-background-color: {p.surface_alt}; }}
QCalendarWidget QAbstractItemView:enabled {{
    background: {p.elevated};
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
QCalendarWidget QToolButton {{
    color: {p.text};
    background: transparent;
    border-radius: 6px;
    padding: 4px 8px;
    font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{ background: {p.hover}; }}
QCalendarWidget #qt_calendar_navigationbar {{ background: {p.elevated}; }}

/* -------------------------------------------------------------- table */
QTableView#MemberTable {{
    background: transparent;
    border: none;
    gridline-color: transparent;
    selection-background-color: transparent;
    alternate-background-color: transparent;
}}
QTableView#MemberTable::item {{ border: none; padding: 0; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent;
    color: {p.text_faint};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 10px 14px;
    font-size: 11px;
    font-weight: 600;
}}
QTableView#HistoryTable {{
    background: transparent;
    border: none;
    gridline-color: transparent;
    selection-background-color: {p.selection};
    selection-color: {p.text};
}}
QTableView#HistoryTable::item {{ padding: 6px 14px; border-bottom: 1px solid {p.border}; }}

QListView#ActivityFeed, QListView#ChannelList {{
    background: transparent;
    border: none;
}}

/* ----------------------------------------------------------- scrollbars */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border_strong};
    border-radius: 3px;
    min-height: 32px;
    margin: 0 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    border-radius: 3px;
    min-width: 32px;
    margin: 2px 0;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ------------------------------------------------------ drawer & toast */
QFrame#Drawer {{
    background: {p.surface};
    border-left: 1px solid {p.border_strong};
}}
QFrame#DrawerSection {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QLabel#FieldLabel {{
    color: {p.text_faint};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#FieldValue {{
    color: {p.text};
    font-size: 13px;
}}
QLabel#Mono {{
    font-family: {", ".join(f'"{f}"' for f in MONO_FAMILIES)};
    font-size: 12px;
    color: {p.text};
}}
QFrame#Toast {{
    background: {p.elevated};
    border: 1px solid {p.border_strong};
    border-radius: 12px;
}}
QLabel#ToastTitle {{ font-weight: 600; font-size: 13px; }}
QLabel#ToastBody {{ color: {p.text_muted}; font-size: 12px; }}

QFrame#SettingsRow {{
    background: transparent;
    border: none;
    border-top: 1px solid {p.border};
}}
QLabel#SettingLabel {{ font-weight: 600; }}
QLabel#SettingHint {{ color: {p.text_faint}; font-size: 12px; }}
QLabel#TestResult {{
    border-radius: 10px;
    padding: 10px 12px;
    font-size: 12px;
}}
QMenu {{
    background: {p.elevated};
    border: 1px solid {p.border_strong};
    border-radius: 10px;
    padding: 4px;
}}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.selection}; }}
QMessageBox {{ background: {p.surface}; }}
"""


class ThemeManager(QObject):
    """Singleton holding the active palette. Emits ``changed`` on switch."""

    changed = Signal(object)  # Palette
    _instance: "ThemeManager | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._mode = "dark"
        self._palette = DARK
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    @classmethod
    def instance(cls) -> "ThemeManager":
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    @property
    def palette(self) -> Palette:
        return self._palette

    @property
    def mode(self) -> str:
        return self._mode

    @staticmethod
    def system_is_dark() -> bool:
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorScheme"):
            scheme = hints.colorScheme()
            if scheme == Qt.ColorScheme.Light:
                return False
            if scheme == Qt.ColorScheme.Dark:
                return True
        return True  # dark-first default when the platform doesn't say

    def apply(self, mode: str) -> None:
        self._mode = mode
        if mode == "light":
            palette = LIGHT
        elif mode == "system":
            palette = DARK if self.system_is_dark() else LIGHT
        else:
            palette = DARK
        self._palette = palette
        app = QApplication.instance()
        if app is None:
            return
        app.setFont(self._base_font())
        app.setPalette(self._qpalette(palette))
        from ui.components.icons import themed_icon_file

        chevron = themed_icon_file("chevron-down", palette.text_muted).replace("\\", "/")
        app.setStyleSheet(_stylesheet(palette).replace("{CHEVRON}", chevron))
        self.changed.emit(palette)

    def _on_system_scheme_changed(self, *_args) -> None:
        if self._mode == "system":
            self.apply("system")

    @staticmethod
    def _base_font() -> QFont:
        font = QFont()
        font.setFamilies(FONT_FAMILIES)
        font.setPointSizeF(10)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return font

    @staticmethod
    def _qpalette(p: Palette) -> QPalette:
        pal = QPalette()
        roles = {
            QPalette.ColorRole.Window: p.bg,
            QPalette.ColorRole.WindowText: p.text,
            QPalette.ColorRole.Base: p.surface_alt,
            QPalette.ColorRole.AlternateBase: p.surface,
            QPalette.ColorRole.Text: p.text,
            QPalette.ColorRole.Button: p.surface_alt,
            QPalette.ColorRole.ButtonText: p.text,
            QPalette.ColorRole.Highlight: p.accent,
            QPalette.ColorRole.HighlightedText: p.accent_text,
            QPalette.ColorRole.ToolTipBase: p.elevated,
            QPalette.ColorRole.ToolTipText: p.text,
            QPalette.ColorRole.PlaceholderText: p.text_faint,
            QPalette.ColorRole.Link: p.accent,
        }
        for role, value in roles.items():
            pal.setColor(role, QColor(value))
        pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.text_faint))
        pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.text_faint))
        return pal


def palette() -> Palette:
    return ThemeManager.instance().palette


__all__ = ["DARK", "LIGHT", "Palette", "ThemeManager", "palette"]
