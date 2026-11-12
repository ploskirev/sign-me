"""macOS-inspired surfaces and controls, following the OS appearance live."""
from pathlib import Path
from string import Template

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QStyleFactory


LIGHT = dict(window='#f5f5f7', card='#ffffff', field='#fafafc', text='#1d1d1f',
    muted='#6e6e73', border='#dcdce1', accent='#0071e3', hover='#0066cc',
    selection='#dceaff', secondary='#ffffff', secondary_hover='#ededf2',
    disabled='#ededf0', disabled_text='#85858d', error='#b42332', error_bg='#fff0f0',
    success='#247348', success_bg='#e7f5ec', track='#e2e2e7', scroll='#b8b8bf', tint='rgba(245,245,247,45)')
DARK = dict(window='#1c1c1e', card='#2c2c2e', field='#242426', text='#f5f5f7',
    muted='#aeaeb6', border='#48484d', accent='#0a70dc', hover='#1680ef',
    selection='#274566', secondary='#363638', secondary_hover='#434347',
    disabled='#303032', disabled_text='#8c8c94', error='#ffb0b5', error_bg='#442b30',
    success='#8cdbac', success_bg='#263d31', track='#414146', scroll='#626269', tint='rgba(28,28,30,45)')


STYLESHEET = Template('''
QWidget { color:$text; font-size:13px; font-weight:400; }
QMainWindow { background:$window; }
QMainWindow[glass="true"] { background:transparent; }
QWidget#windowSurface[glass="true"] { background:$tint; }
QScrollArea,QStackedWidget,QWidget#formViewport,QWidget#formContents { background:transparent; }
QLabel { background:transparent; }
QLabel#heading { font-size:25px; font-weight:600; }
QLabel#subtitle { color:$muted; font-size:12px; }
QLabel#fieldLabel { color:$muted; font-size:12px; }
QLabel#stage { font-size:23px; font-weight:600; }
QLabel#message { font-size:14px; color:$muted; }
QLabel#badge { color:$muted; font-size:11px; font-weight:600; padding:5px 9px;
    background:$track; border-radius:6px; }
QLabel#badge[tone="success"] { color:$success; background:$success_bg; }
QLabel#badge[tone="error"] { color:$error; background:$error_bg; }
QLabel#appointmentTime { font-size:21px; font-weight:600; }
QGroupBox { background:$card; border:1px solid $border; border-radius:12px;
    margin-top:23px; padding:14px 16px 12px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:3px; top:1px; color:$text; }
QLineEdit,QDateEdit,QTimeEdit,QSpinBox,QComboBox { background:$field; color:$text;
    border:1px solid $border; border-radius:7px; padding:6px 10px; min-height:22px;
    selection-background-color:$selection; selection-color:$text; }
QLineEdit:focus,QDateEdit:focus,QTimeEdit:focus,QSpinBox:focus,QComboBox:focus { border:1px solid $accent; }
QLineEdit:disabled,QDateEdit:disabled,QTimeEdit:disabled,QSpinBox:disabled,QComboBox:disabled {
    background:$disabled; color:$disabled_text; }
QSpinBox,QTimeEdit { padding-right:28px; }
QSpinBox::up-button,QTimeEdit::up-button {
    subcontrol-origin:border; subcontrol-position:top right; width:24px; height:17px;
    margin:1px 1px 0 0; border:0; border-top-right-radius:6px; background:transparent; }
QSpinBox::down-button,QTimeEdit::down-button {
    subcontrol-origin:border; subcontrol-position:bottom right; width:24px; height:17px;
    margin:0 1px 1px 0; border:0; border-bottom-right-radius:6px; background:transparent; }
QSpinBox::up-button:hover,QTimeEdit::up-button:hover,QSpinBox::down-button:hover,QTimeEdit::down-button:hover {
    background:$secondary_hover; }
QSpinBox::up-arrow,QTimeEdit::up-arrow { image:url("$up"); width:12px; height:12px; }
QSpinBox::down-arrow,QTimeEdit::down-arrow { image:url("$down"); width:12px; height:12px; }
QComboBox,QDateEdit { padding-right:30px; }
QComboBox::drop-down,QDateEdit::drop-down {
    subcontrol-origin:border; subcontrol-position:top right; width:28px;
    margin:1px; border:0; border-top-right-radius:6px; border-bottom-right-radius:6px; }
QComboBox::down-arrow,QDateEdit::down-arrow { image:url("$down"); width:12px; height:12px; }
QComboBox QAbstractItemView { background:$card; color:$text; border:1px solid $border;
    selection-background-color:$selection; selection-color:$text; padding:4px; }
QPushButton { background:$secondary; color:$text; border:1px solid $border;
    border-radius:8px; padding:9px 15px; font-weight:500; }
QPushButton:hover { background:$secondary_hover; }
QPushButton:pressed { background:$track; }
QPushButton:focus { border-color:$accent; }
QPushButton#primary { background:$accent; color:white; border:1px solid $accent; font-weight:600; }
QPushButton#primary:hover { background:$hover; border-color:$hover; }
QPushButton:disabled,QPushButton#primary:disabled { color:$disabled_text; background:$disabled; border-color:$border; }
QPushButton#quiet { border-color:transparent; background:transparent; color:$muted; }
QPushButton#quiet:hover { background:$secondary_hover; color:$text; }
QLabel#error { color:$error; background:$error_bg; border-radius:7px; padding:10px; }
QFrame#statusCard { background:$card; border:1px solid $border; border-radius:12px; }
QProgressBar { max-height:5px; min-height:5px; background:$track; border:0; border-radius:2px; }
QProgressBar::chunk { background:$accent; border-radius:2px; }
QPlainTextEdit { background:$field; border:1px solid $border; border-radius:8px; color:$muted; font-size:12px; padding:10px; }
QCheckBox { spacing:8px; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid $border; border-radius:4px; background:$field; }
QCheckBox::indicator:checked { background:$accent; border-color:$accent; image:url("$check"); }
QCheckBox::indicator:hover,QCheckBox::indicator:focus { border-color:$accent; }
QScrollBar:vertical { background:transparent; width:9px; margin:0; }
QScrollBar::handle:vertical { background:$scroll; border:2px solid $window; border-radius:4px; min-height:28px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }
QCalendarWidget QWidget { background:$card; color:$text; }
QCalendarWidget QAbstractItemView { background:$card; color:$text; selection-background-color:$accent; selection-color:white; }
QCalendarWidget QToolButton { color:$text; background:transparent; border:0; padding:5px; }
QToolTip { color:$text; background:$card; border:1px solid $border; padding:5px; }
''')


class SystemTheme(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.dark = False
        self.fallback_dark = app.palette().color(QPalette.Window).lightness() < 128
        styles = QStyleFactory.keys()
        app.setStyle(next((s for s in styles if s.casefold() == 'macos'), 'Fusion'))
        app.setFont(QFontDatabase.systemFont(QFontDatabase.GeneralFont))
        app.styleHints().colorSchemeChanged.connect(self.system_changed)
        self.apply_scheme(app.styleHints().colorScheme())

    def system_changed(self, scheme):
        # Apply after Qt has updated its native palette for the new appearance.
        QTimer.singleShot(0, lambda: self.apply_scheme(scheme))

    def apply_scheme(self, scheme):
        self.dark = scheme == Qt.ColorScheme.Dark or (scheme == Qt.ColorScheme.Unknown and self.fallback_dark)
        colors = DARK if self.dark else LIGHT
        palette = QPalette()
        for role,key in ((QPalette.Window,'window'),(QPalette.WindowText,'text'),(QPalette.Base,'field'),
                         (QPalette.AlternateBase,'card'),(QPalette.Text,'text'),(QPalette.Button,'secondary'),
                         (QPalette.ButtonText,'text'),(QPalette.Highlight,'accent'),(QPalette.PlaceholderText,'muted'),
                         (QPalette.ToolTipBase,'card'),(QPalette.ToolTipText,'text'),(QPalette.Link,'accent')):
            palette.setColor(role,QColor(colors[key]))
        palette.setColor(QPalette.HighlightedText,QColor('white'))
        for role in (QPalette.Text,QPalette.ButtonText,QPalette.WindowText):
            palette.setColor(QPalette.Disabled,role,QColor(colors['disabled_text']))
        self.app.setPalette(palette)
        assets = Path(__file__).parent / 'gui_assets'
        theme = 'dark' if self.dark else 'light'
        self.app.setStyleSheet(STYLESHEET.substitute(colors,
            up=(assets/f'up-{theme}.svg').as_posix(), down=(assets/f'down-{theme}.svg').as_posix(),
            check=(assets/'check.svg').as_posix()))
        for window in self.app.topLevelWidgets():
            if window.property('glass') and hasattr(window,'backdrop'):
                window.backdrop.set_dark(window,self.dark)
