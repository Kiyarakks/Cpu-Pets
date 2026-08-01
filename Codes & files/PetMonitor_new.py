"""
CPU Pets
--------
A Windows system tray application built entirely on PyQt5.

Core feature ("the pet"):
  - An animated animal icon lives in the system tray and reacts to CPU
    usage: the busier the CPU, the faster it animates.
  - The icon automatically matches the current Windows light/dark theme.
  - Right-clicking the icon opens a custom dark, modern context menu
    (not the native Windows menu).
  - Hovering over the icon shows a live tooltip with CPU usage, RAM
    usage, and system uptime.
  - Optional alert when CPU usage hits 100%.
  - Optional "Run on Startup" (adds/removes a registry entry).
  - Animal choice (cat / parrot / horse) and all preferences are saved
    to disk and restored on next launch.

Secondary feature (opened from the tray menu or a left click):
  - "Screen Time" window: tracks how long each application has been the
    active (foreground) window, skipping idle time. Usage is stored in
    a small local SQLite database, one row per (day, app), so history
    builds up indefinitely (months, years) instead of being overwritten
    every day. A date picker lets you browse any past day, not just
    today.

Run:      pythonw cpu_pets.pyw   (or: python cpu_pets.pyw)
Requires: psutil, Pillow, PyQt5, pywin32
"""

import ctypes
import io
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import psutil
from PIL import Image, ImageDraw

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QComboBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMenu, QProgressBar, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

try:
    import winreg
except ImportError:
    winreg = None

try:
    import win32gui
    import win32process
    WINDOWS = True
except ImportError:
    WINDOWS = False


# ============================================================================
# Shared configuration
# ============================================================================

APP_NAME = "CPU Pets"
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ---- Pet / tray icon settings ----
TRAY_ICON_SIZE = 32
MIN_DELAY_S = 0.05
MAX_DELAY_S = 0.50
SMOOTHING_ALPHA = 0.3
THEME_POLL_S = 2.0
TOOLTIP_REFRESH_MS = 2000

ANIMALS = ("cat", "parrot", "horse")
DEFAULT_ANIMAL = "cat"

# ---- CPU alert settings ----
CPU_ALERT_THRESHOLD = 100.0        # CPU percent that triggers the alert
CPU_ALERT_RESET_THRESHOLD = 90.0   # must drop below this before the alert can fire again
ALERT_TITLE = "CPU Pets"
ALERT_MESSAGE = "Your PC is using 100% of the CPU"

# ---- Screen time settings ----
SCREEN_TIME_POLL_S = 2
SCREEN_TIME_IDLE_THRESHOLD_S = 60


def get_app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA", str(Path.home())))
    else:
        base = Path.home()
    folder = base / APP_NAME.replace(" ", "_")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


APP_DATA_DIR = get_app_data_dir()
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
SCREEN_TIME_DB = APP_DATA_DIR / "screen_time.db"
ALERT_LOG_FILE = APP_DATA_DIR / "alert_error.log"


# ============================================================================
# Windows helpers shared by both features
# ============================================================================

def hide_and_detach_console():
    try:
        get_console_window = ctypes.windll.kernel32.GetConsoleWindow
        show_window = ctypes.windll.user32.ShowWindow
        free_console = ctypes.windll.kernel32.FreeConsole
        SW_HIDE = 0
        hwnd = get_console_window()
        if hwnd:
            show_window(hwnd, SW_HIDE)
            free_console()
    except Exception:
        pass


def get_windows_app_theme() -> str:
    """Return 'light' or 'dark' based on the current Windows app theme."""
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(val) == 1 else "dark"
    except Exception:
        return "light"


def is_run_on_startup() -> bool:
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_run_on_startup(enable: bool = True):
    if not winreg:
        return
    exe_path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        print("Error setting startup:", e)


def format_duration(seconds) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def pil_image_to_qicon(img: Image.Image) -> QIcon:
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return QIcon(pixmap)


# ============================================================================
# Screen time storage (SQLite - keeps history for months/years)
# ============================================================================

def _open_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SCREEN_TIME_DB))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage (
            day TEXT NOT NULL,
            process_name TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, process_name)
        )
        """
    )
    conn.commit()
    return conn


def add_usage_seconds(conn: sqlite3.Connection, day: str, process_name: str, seconds: int):
    conn.execute(
        """
        INSERT INTO usage (day, process_name, seconds)
        VALUES (?, ?, ?)
        ON CONFLICT(day, process_name) DO UPDATE SET seconds = seconds + excluded.seconds
        """,
        (day, process_name, seconds),
    )
    conn.commit()


def get_usage_for_day(day: str) -> dict:
    conn = _open_db_connection()
    try:
        rows = conn.execute(
            "SELECT process_name, seconds FROM usage WHERE day = ? ORDER BY seconds DESC",
            (day,),
        ).fetchall()
        return {name: seconds for name, seconds in rows}
    finally:
        conn.close()


def get_available_days() -> list:
    conn = _open_db_connection()
    try:
        rows = conn.execute("SELECT DISTINCT day FROM usage ORDER BY day DESC").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


# ============================================================================
# Windows-specific screen time helpers
# ============================================================================

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input, system-wide."""
    if not WINDOWS:
        return 0.0
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        millis_idle = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return millis_idle / 1000.0
    return 0.0


def get_foreground_process_name() -> str:
    if not WINDOWS:
        return "unknown.exe"
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return "unknown.exe"


class ScreenTimeTracker(QThread):
    """Polls the foreground process at a fixed interval, skips idle time,
    and persists the totals into the SQLite database so history survives
    across days, restarts, and indefinitely into the future."""

    updated = pyqtSignal(dict, str)  # (apps for `day`, day)

    def __init__(self):
        super().__init__()
        self._running = False

    def run(self):
        self._running = True
        conn = _open_db_connection()
        try:
            while self._running:
                today = date.today().isoformat()
                if get_idle_seconds() < SCREEN_TIME_IDLE_THRESHOLD_S:
                    process_name = get_foreground_process_name()
                    add_usage_seconds(conn, today, process_name, SCREEN_TIME_POLL_S)

                self.updated.emit(get_usage_for_day(today), today)
                time.sleep(SCREEN_TIME_POLL_S)
        finally:
            conn.close()

    def stop(self):
        self._running = False
        self.wait(2000)


# ============================================================================
# Dark, modern theme (applies to the main window, the tray context menu,
# and the date picker combo box)
# ============================================================================

DARK_QSS = """
QWidget {
    background-color: #121212;
    color: #e0e0e0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 10pt;
}
QLabel#Title {
    font-size: 12pt;
    font-weight: bold;
    color: #4fd1c5;
}
QLabel#Muted {
    color: #888888;
    font-size: 9pt;
}
QListWidget {
    background-color: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 4px;
}
QProgressBar {
    background-color: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 3px;
    text-align: center;
    color: #e0e0e0;
    height: 14px;
}
QProgressBar::chunk {
    background-color: #4fd1c5;
    border-radius: 3px;
}
QComboBox {
    background-color: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 4px;
    padding: 4px 8px;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #1a1a1a;
    color: #e0e0e0;
    selection-background-color: #2a2a2a;
    selection-color: #4fd1c5;
    border: 1px solid #2a2a2a;
    outline: none;
}
QMenu {
    background-color: #1a1a1a;
    color: #e0e0e0;
    border: 1px solid #2a2a2a;
    padding: 4px;
}
QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #2a2a2a;
    color: #4fd1c5;
}
QMenu::item:disabled {
    color: #666666;
}
QMenu::separator {
    height: 1px;
    background: #2a2a2a;
    margin: 4px 8px;
}
QMenu::indicator {
    width: 14px;
    height: 14px;
}
"""


# ============================================================================
# Screen time window (Qt, dark theme, with a date picker for history)
# ============================================================================

class ScreenTimeWindow(QWidget):
    """Opened on demand from the pet's tray menu or a left click on the
    tray icon. Owns its own tracking thread so it keeps recording usage
    even while hidden. A date picker lets you look back at any past day
    stored in the local database."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Screen Time - CPU Pets")
        self.setFixedSize(320, 420)
        self.setWindowFlags(Qt.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Screen Time")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        self.date_combo = QComboBox()
        self.date_combo.currentIndexChanged.connect(self._on_date_changed)
        layout.addWidget(self.date_combo)

        self.total_label = QLabel("Total: 0m")
        self.total_label.setObjectName("Muted")
        layout.addWidget(self.total_label)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, stretch=1)

        hint = QLabel("Closing this window keeps tracking in the background. "
                       "History is kept indefinitely - pick any past day above.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tracker = ScreenTimeTracker()
        self.tracker.updated.connect(self._on_tracker_update)
        self.tracker.start()

    # ---------- Date picker ----------
    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_date_list()

    def _refresh_date_list(self):
        today = date.today().isoformat()
        days = get_available_days()
        if today not in days:
            days.insert(0, today)

        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        for day in days:
            label = "Today" if day == today else self._format_day_label(day)
            self.date_combo.addItem(label, day)
        self.date_combo.setCurrentIndex(0)
        self.date_combo.blockSignals(False)
        self._load_selected_day()

    @staticmethod
    def _format_day_label(iso_day: str) -> str:
        try:
            return date.fromisoformat(iso_day).strftime("%b %d, %Y")
        except ValueError:
            return iso_day

    def _on_date_changed(self, _index):
        self._load_selected_day()

    def _load_selected_day(self):
        idx = self.date_combo.currentIndex()
        if idx < 0:
            return
        selected_day = self.date_combo.itemData(idx)
        self._render(get_usage_for_day(selected_day), selected_day)

    def _on_tracker_update(self, apps: dict, day: str):
        # Only live-update the list if the user is currently looking at
        # the day the tracker just wrote to (normally "Today").
        idx = self.date_combo.currentIndex()
        if idx >= 0 and self.date_combo.itemData(idx) == day:
            self._render(apps, day)

    # ---------- Rendering ----------
    def _render(self, apps: dict, day: str):
        self.list_widget.clear()
        total_seconds = sum(apps.values())
        label = "Today" if day == date.today().isoformat() else self._format_day_label(day)
        self.total_label.setText(f"Total ({label}): {format_duration(total_seconds)}")

        max_seconds = max(apps.values(), default=1) or 1
        for process_name, seconds in sorted(apps.items(), key=lambda kv: kv[1], reverse=True):
            item = QListWidgetItem()
            self.list_widget.addItem(item)

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 2, 6, 2)

            name_label = QLabel(process_name)
            name_label.setFixedWidth(120)

            bar = QProgressBar()
            bar.setRange(0, max_seconds)
            bar.setValue(seconds)
            bar.setFormat(format_duration(seconds))

            row_layout.addWidget(name_label)
            row_layout.addWidget(bar, stretch=1)
            item.setSizeHint(row.sizeHint())
            self.list_widget.setItemWidget(item, row)

    def closeEvent(self, event):
        """Hide instead of quitting; the pet's tray menu fully quits."""
        event.ignore()
        self.hide()

    def shutdown(self):
        self.tracker.stop()


# ============================================================================
# CPU Pets: the core tray pet (QSystemTrayIcon based, custom dark menu)
# ============================================================================

class CpuPetTray(QSystemTrayIcon):
    def __init__(self, screen_time_window: ScreenTimeWindow, app: QApplication):
        super().__init__()
        self.screen_time_window = screen_time_window
        self.app = app

        self.base = Path(__file__).resolve().parent
        self.frames = {animal: {"light": [], "dark": []} for animal in ANIMALS}
        self._load_all_frames_or_fail()

        self._paused = False
        self._cpu_alert_notified = False
        self.cpu_alert_enabled = True
        self.current_animal = DEFAULT_ANIMAL
        self.load_settings()

        self.current_theme = get_windows_app_theme()
        self._idx = 0
        self._cpu_smooth = psutil.cpu_percent(interval=None)

        self.setIcon(self._get_colored_icon(0))
        self._build_menu()
        self.activated.connect(self._on_activated)

        self._anim_timer = QTimer(self)
        self._anim_timer.setSingleShot(True)
        self._anim_timer.timeout.connect(self._animate_step)

        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._update_theme)
        self._theme_timer.start(int(THEME_POLL_S * 1000))

        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.timeout.connect(self._update_tooltip)
        self._tooltip_timer.start(TOOLTIP_REFRESH_MS)
        self._update_tooltip()

        self.setVisible(True)
        self._animate_step()

    # ---------- Icon rendering ----------
    def _get_colored_frame_image(self, index) -> Image.Image:
        """Get the frame colored appropriately for the current theme."""
        frames = self.frames[self.current_animal][self.current_theme]
        if not frames or index >= len(frames):
            img = Image.new('RGBA', (TRAY_ICON_SIZE, TRAY_ICON_SIZE), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            fill = (255, 255, 255, 255) if self.current_theme == "dark" else (0, 0, 0, 255)
            draw.ellipse([4, 4, 28, 28], fill=fill)
            return img

        img = frames[index].copy()
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        _, _, _, alpha = img.split()

        result = Image.new('RGBA', img.size, (0, 0, 0, 0))
        color = (255, 255, 255, 255) if self.current_theme == "dark" else (0, 0, 0, 255)
        overlay = Image.new('RGBA', img.size, color)
        return Image.composite(overlay, result, alpha)

    def _get_colored_icon(self, index) -> QIcon:
        return pil_image_to_qicon(self._get_colored_frame_image(index))

    # ---------- Settings ----------
    def save_settings(self):
        try:
            data = {
                "animal": self.current_animal,
                "run_on_startup": is_run_on_startup(),
                "cpu_alert_enabled": self.cpu_alert_enabled,
            }
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            print("Failed to save settings:", e)

    def load_settings(self):
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.current_animal = data.get("animal", DEFAULT_ANIMAL)
                self.cpu_alert_enabled = data.get("cpu_alert_enabled", True)
                if data.get("run_on_startup", False):
                    set_run_on_startup(True)
            except Exception as e:
                print("Failed to load settings:", e)

    # ---------- Menu (custom dark style, not the native Windows menu) ----------
    def _build_menu(self):
        menu = QMenu()

        self.pause_action = QAction("Pause", menu, checkable=True)
        self.pause_action.toggled.connect(self._on_pause_toggled)
        menu.addAction(self.pause_action)

        animal_menu = menu.addMenu("Animal")
        animal_group = QActionGroup(menu)
        animal_group.setExclusive(True)
        self.animal_actions = {}
        for animal in ANIMALS:
            action = QAction(animal.capitalize(), animal_menu, checkable=True)
            action.setChecked(animal == self.current_animal)
            action.triggered.connect(lambda _checked, a=animal: self.set_animal(a))
            animal_group.addAction(action)
            animal_menu.addAction(action)
            self.animal_actions[animal] = action

        alerts_menu = menu.addMenu("Alerts")
        self.cpu_alert_action = QAction("CPU 100% Usage Alert", alerts_menu, checkable=True)
        self.cpu_alert_action.setChecked(self.cpu_alert_enabled)
        self.cpu_alert_action.toggled.connect(self._on_cpu_alert_toggled)
        alerts_menu.addAction(self.cpu_alert_action)

        menu.addSeparator()

        screen_time_action = QAction("Screen Time", menu)
        screen_time_action.triggered.connect(self._show_screen_time)
        menu.addAction(screen_time_action)

        menu.addSeparator()

        self.startup_action = QAction("Run on Startup", menu, checkable=True)
        self.startup_action.setChecked(is_run_on_startup())
        self.startup_action.toggled.connect(self._on_startup_toggled)
        menu.addAction(self.startup_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    # ---------- Loading frames ----------
    def _load_frames_for(self, animal, theme):
        folder = self.base / animal / theme
        if folder.exists():
            paths = sorted(folder.glob("*.ico"))
        else:
            paths = sorted(self.base.glob(f"{animal}_{theme}_*.ico"))

        frames = []
        if not paths:
            print(f"Warning: No frames found for {animal}/{theme}, creating default")
            img = Image.new('RGBA', (TRAY_ICON_SIZE, TRAY_ICON_SIZE), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([4, 4, 28, 28], fill=(128, 128, 128, 255))
            frames.append(img)
        else:
            for p in paths:
                try:
                    img = Image.open(p).convert("RGBA").resize((TRAY_ICON_SIZE, TRAY_ICON_SIZE), Image.LANCZOS)
                    frames.append(img)
                except Exception as e:
                    print(f"Skipping {p.name}: {e}")
        return frames

    def _load_all_frames_or_fail(self):
        for animal in ANIMALS:
            for theme in ("light", "dark"):
                self.frames[animal][theme] = self._load_frames_for(animal, theme)

    # ---------- Menu action handlers ----------
    def _on_pause_toggled(self, checked):
        self._paused = checked

    def _on_cpu_alert_toggled(self, checked):
        self.cpu_alert_enabled = checked
        if not checked:
            self._cpu_alert_notified = False
        self.save_settings()

    def _on_startup_toggled(self, checked):
        set_run_on_startup(checked)
        self.save_settings()

    def set_animal(self, animal):
        if animal not in ANIMALS:
            return
        self.current_animal = animal
        self._idx = 0
        try:
            self.setIcon(self._get_colored_icon(0))
        except Exception:
            pass
        self.save_settings()

    def _show_screen_time(self, _checked=False):
        w = self.screen_time_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _on_activated(self, reason):
        # Left click: quick show/hide toggle for the Screen Time window.
        # Right click already opens the custom context menu automatically.
        if reason == QSystemTrayIcon.Trigger:
            w = self.screen_time_window
            if w.isVisible():
                w.hide()
            else:
                self._show_screen_time()

    def _quit(self, _checked=False):
        self.save_settings()
        self.setVisible(False)
        self.app.quit()

    # ---------- Tooltip: CPU, RAM, and system uptime on hover ----------
    def _update_tooltip(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        uptime = format_duration(time.time() - psutil.boot_time())
        self.setToolTip(f"{APP_NAME}\nCPU: {cpu:.0f}%   RAM: {ram:.0f}%\nUptime: {uptime}")

    # ---------- Theme ----------
    def _update_theme(self):
        new_theme = get_windows_app_theme()
        if new_theme != self.current_theme:
            self.current_theme = new_theme
            self._idx = 0
            try:
                self.setIcon(self._get_colored_icon(0))
            except Exception:
                pass

    # ---------- CPU alert ----------
    def _check_cpu_alert(self, instant_cpu):
        """If CPU has reached the alert threshold and we haven't notified
        yet in this spike, notify."""
        if not self.cpu_alert_enabled:
            return
        if instant_cpu >= CPU_ALERT_THRESHOLD:
            if not self._cpu_alert_notified:
                try:
                    self.showMessage(ALERT_TITLE, ALERT_MESSAGE, QSystemTrayIcon.Information, 5000)
                except Exception as e:
                    self._log_alert_error(e)
                self._cpu_alert_notified = True
        elif instant_cpu < CPU_ALERT_RESET_THRESHOLD:
            self._cpu_alert_notified = False

    @staticmethod
    def _log_alert_error(err):
        """Since the console is hidden, log any notify() error to a file so
        it can be checked later."""
        try:
            with open(ALERT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Notification failed: {err}\n")
        except Exception:
            pass

    # ---------- Animation ----------
    def _cpu_delay(self):
        instant = psutil.cpu_percent(interval=None)
        self._cpu_smooth = (SMOOTHING_ALPHA * instant) + ((1 - SMOOTHING_ALPHA) * self._cpu_smooth)
        factor = max(0.0, min(1.0, self._cpu_smooth / 100.0))
        # The alert is checked against the instant CPU value, not the
        # smoothed one, since smoothing means the value almost never
        # reaches exactly 100.
        self._check_cpu_alert(instant)
        return MIN_DELAY_S + (MAX_DELAY_S - MIN_DELAY_S) * factor

    def _animate_step(self):
        if not self._paused:
            try:
                self.setIcon(self._get_colored_icon(self._idx))
            except Exception as e:
                print(f"Animation error: {e}")
            frame_count = len(self.frames[self.current_animal][self.current_theme])
            self._idx = (self._idx + 1) % max(frame_count, 1)

        delay_ms = int(self._cpu_delay() * 1000)
        self._anim_timer.start(delay_ms)




def main():
    hide_and_detach_console()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_QSS)

    screen_time_window = ScreenTimeWindow()
    tray = CpuPetTray(screen_time_window, app)  # noqa: F841 (kept alive by reference)

    app.aboutToQuit.connect(screen_time_window.shutdown)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
