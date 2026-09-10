import ctypes
import io
import json
import os
import sqlite3
import statistics
import sys
import time
import uuid
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
import psutil
from PIL import Image, ImageDraw

from PyQt5.QtCore import Qt, QDateTime, QObject, QPointF, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QCheckBox, QComboBox, QDateTimeEdit,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
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

# ---- System monitor: history / sparkline / sensors ----
HISTORY_POLL_MS = 1000       # how often CPU/RAM/sensors are sampled
BASELINE_WINDOW = 300        # samples kept for the anomaly baseline (5 min)
SPARKLINE_POINTS = 60        # samples shown in the sparkline graphs (1 min)

# ---- Anomaly detection (CPU) ----
ANOMALY_MIN_SAMPLES = 30         # need this many samples before judging
ANOMALY_MIN_STD = 5.0            # floor for std-dev so a flat baseline
                                  # doesn't make every tiny move an anomaly
ANOMALY_Z_THRESHOLD = 3.0        # spike must be this many std-devs above mean
ANOMALY_RESET_Z = 1.0            # must drop back below this to re-arm
ANOMALY_MIN_ABSOLUTE_CPU = 50.0  # ignore spikes below this, even if "unusual"

# ---- Weekly report ----
WEEKLY_REPORT_CHECK_INTERVAL_MS = 60 * 60 * 1000  # check once an hour
WEEKLY_REPORT_DAYS = 7
WEEKLY_REPORT_WEEKDAY = 0  # Monday

# ---- Custom alerts ----
CUSTOM_ALERT_CHECK_INTERVAL_MS = 2000
CUSTOM_ALERT_METRICS = {
    "cpu_total": "Total CPU %",
    "ram_total": "Total RAM %",
    "disk_total": "Disk usage % (C:)",
    "battery_percent": "Battery %",
    "process_cpu": "A specific app's CPU %",
    "process_ram": "A specific app's RAM (MB)",
}
CUSTOM_ALERT_OPERATORS = {
    "above": "goes above",
    "below": "drops below",
}

# ---- Reminders ----
REMINDER_CHECK_INTERVAL_MS = 20 * 1000  # check every 20s
REMINDER_REPEAT_NONE = "none"
REMINDER_REPEAT_DAILY = "daily"
REMINDER_REPEAT_WEEKLY = "weekly"
REMINDER_REPEAT_MONTHLY = "monthly"
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ---- Not Responding detection ----
NOT_RESPONDING_CHECK_INTERVAL_MS = 4000


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
# Weekly report aggregation
# ============================================================================

def generate_report_for_offset_range(start_offset: int, end_offset: int) -> dict:
    """Aggregate per-process seconds for the calendar days that are
    `start_offset`..`end_offset` days back from today (both inclusive,
    0 = today), using whatever history already exists in the DB."""
    combined = {}
    today = date.today()
    for offset in range(start_offset, end_offset + 1):
        day_iso = (today - timedelta(days=offset)).isoformat()
        for process_name, seconds in get_usage_for_day(day_iso).items():
            combined[process_name] = combined.get(process_name, 0) + seconds
    return combined


def generate_weekly_report(days: int = WEEKLY_REPORT_DAYS) -> dict:
    """Aggregate per-process seconds across the last `days` calendar days
    (today included), using whatever history already exists in the DB."""
    return generate_report_for_offset_range(0, days - 1)


def generate_previous_period_report(days: int = WEEKLY_REPORT_DAYS) -> dict:
    """Same as generate_weekly_report, but for the `days` before that -
    i.e. the comparison period used for 'compared to last week'."""
    return generate_report_for_offset_range(days, (2 * days) - 1)


def categorize_totals(apps: dict) -> dict:
    """{process_name: seconds} -> {category: seconds}, using categorize_app."""
    totals = {}
    for process_name, seconds in apps.items():
        category = categorize_app(process_name)
        totals[category] = totals.get(category, 0) + seconds
    return totals


def percent_change(old_value: float, new_value: float):
    """Returns the percent change from old_value to new_value, or None if
    there's no meaningful baseline to compare against (old_value == 0)."""
    if not old_value:
        return None
    return ((new_value - old_value) / old_value) * 100.0


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            repeat_mode TEXT NOT NULL DEFAULT 'none',
            weekdays TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_health (
            day TEXT NOT NULL,
            process_name TEXT NOT NULL,
            hangs INTEGER NOT NULL DEFAULT 0,
            force_closes INTEGER NOT NULL DEFAULT 0,
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
# Application health storage (crash/hang history) - a "hang" is logged every
# time an app is newly detected as Not Responding; a "crash" is logged every
# time a hung app has to be force-closed (via the tray menu action), which
# is the closest reliable signal this app can get to a real crash without
# hooking into Windows Error Reporting.
# ============================================================================

def record_app_health_event(process_name: str, *, hang: bool = False, force_close: bool = False):
    if not process_name or not (hang or force_close):
        return
    day = date.today().isoformat()
    conn = _open_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO app_health (day, process_name, hangs, force_closes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day, process_name) DO UPDATE SET
                hangs = hangs + excluded.hangs,
                force_closes = force_closes + excluded.force_closes
            """,
            (day, process_name, 1 if hang else 0, 1 if force_close else 0),
        )
        conn.commit()
    finally:
        conn.close()


def get_app_health_totals(days: int = WEEKLY_REPORT_DAYS) -> dict:
    """Returns {process_name: {"hangs": n, "force_closes": n}} summed over
    the last `days` calendar days (today included)."""
    start_day = (date.today() - timedelta(days=days - 1)).isoformat()
    conn = _open_db_connection()
    try:
        rows = conn.execute(
            "SELECT process_name, SUM(hangs), SUM(force_closes) FROM app_health "
            "WHERE day >= ? GROUP BY process_name",
            (start_day,),
        ).fetchall()
        return {
            name: {"hangs": hangs or 0, "force_closes": force_closes or 0}
            for name, hangs, force_closes in rows
        }
    finally:
        conn.close()


# ============================================================================
# Reminders storage (SQLite) - one-time or repeating, like a phone alarm app
# ============================================================================

def get_all_reminders() -> list:
    """Returns a list of dicts, soonest first."""
    conn = _open_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, remind_at, repeat_mode, weekdays, enabled "
            "FROM reminders ORDER BY remind_at ASC"
        ).fetchall()
        return [
            {
                "id": r[0], "title": r[1], "remind_at": r[2],
                "repeat_mode": r[3], "weekdays": r[4], "enabled": bool(r[5]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def save_reminder(reminder: dict):
    """Insert or update (by id) a reminder."""
    conn = _open_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO reminders (id, title, remind_at, repeat_mode, weekdays, enabled)
            VALUES (:id, :title, :remind_at, :repeat_mode, :weekdays, :enabled)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, remind_at=excluded.remind_at,
                repeat_mode=excluded.repeat_mode, weekdays=excluded.weekdays,
                enabled=excluded.enabled
            """,
            {**reminder, "enabled": int(reminder["enabled"])},
        )
        conn.commit()
    finally:
        conn.close()


def delete_reminder(reminder_id: str):
    conn = _open_db_connection()
    try:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
    finally:
        conn.close()


def compute_next_occurrence(current: datetime, repeat_mode: str, weekdays: str) -> datetime:
    """Given the datetime that just fired, compute the next one for a
    repeating reminder. `weekdays` is a comma list of ints (0=Mon..6=Sun),
    used only for REMINDER_REPEAT_WEEKLY; empty means 'same weekday'."""
    if repeat_mode == REMINDER_REPEAT_DAILY:
        return current + timedelta(days=1)

    if repeat_mode == REMINDER_REPEAT_WEEKLY:
        chosen = [int(d) for d in weekdays.split(",") if d != ""] or [current.weekday()]
        for offset in range(1, 8):
            candidate = current + timedelta(days=offset)
            if candidate.weekday() in chosen:
                return candidate
        return current + timedelta(days=7)

    if repeat_mode == REMINDER_REPEAT_MONTHLY:
        month = current.month + 1
        year = current.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        day = current.day
        while True:
            try:
                return current.replace(year=year, month=month, day=day)
            except ValueError:
                day -= 1  # e.g. Jan 31 -> Feb 28

    return current  # REMINDER_REPEAT_NONE: caller disables it instead


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


# ============================================================================
# Friendly app names + categories. `process_name` (e.g. "chrome.exe") is
# always what gets stored in the database, so historical data stays
# consistent - these mappings only affect how a process is *displayed*
# (Productivity Analytics, Weekly Report, Application Health). This is a
# starter set; unrecognized processes fall back to a cleaned-up version of
# their executable name / the "Other" category.
# ============================================================================

KNOWN_APP_NAMES = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "opera.exe": "Opera",
    "brave.exe": "Brave",
    "code.exe": "VS Code",
    "devenv.exe": "Visual Studio",
    "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ IDEA",
    "sublime_text.exe": "Sublime Text",
    "notepad++.exe": "Notepad++",
    "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell",
    "windowsterminal.exe": "Windows Terminal",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "teams.exe": "Microsoft Teams",
    "outlook.exe": "Outlook",
    "whatsapp.exe": "WhatsApp",
    "telegram.exe": "Telegram",
    "zoom.exe": "Zoom",
    "skype.exe": "Skype",
    "spotify.exe": "Spotify",
    "steam.exe": "Steam",
    "vlc.exe": "VLC",
    "blender.exe": "Blender",
    "photoshop.exe": "Photoshop",
    "illustrator.exe": "Illustrator",
    "winword.exe": "Word",
    "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint",
    "explorer.exe": "File Explorer",
    "notepad.exe": "Notepad",
}

APP_CATEGORIES = {
    "code.exe": "Development",
    "devenv.exe": "Development",
    "pycharm64.exe": "Development",
    "idea64.exe": "Development",
    "sublime_text.exe": "Development",
    "notepad++.exe": "Development",
    "cmd.exe": "Development",
    "powershell.exe": "Development",
    "windowsterminal.exe": "Development",

    "discord.exe": "Communication",
    "slack.exe": "Communication",
    "teams.exe": "Communication",
    "outlook.exe": "Communication",
    "whatsapp.exe": "Communication",
    "telegram.exe": "Communication",
    "zoom.exe": "Communication",
    "skype.exe": "Communication",

    "spotify.exe": "Entertainment",
    "steam.exe": "Entertainment",
    "vlc.exe": "Entertainment",
}


def friendly_app_name(process_name: str) -> str:
    """Turns a raw process name like 'chrome.exe' into a display name like
    'Chrome'. Falls back to a cleaned-up version of the executable name for
    anything not in KNOWN_APP_NAMES."""
    if not process_name:
        return "Unknown"
    key = process_name.strip().lower()
    if key in KNOWN_APP_NAMES:
        return KNOWN_APP_NAMES[key]
    name = process_name.strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    name = name.replace("_", " ").replace("-", " ").strip()
    return (name[:1].upper() + name[1:]) if name else "Unknown"


def categorize_app(process_name: str) -> str:
    """Buckets a raw process name into one of a small set of categories.
    Anything not explicitly mapped falls into 'Other' (this includes
    browsers, office apps, and anything unrecognized, by design)."""
    key = (process_name or "").strip().lower()
    return APP_CATEGORIES.get(key, "Other")


# ============================================================================
# "Not Responding" detection - finds hung top-level windows the same way
# Task Manager does (IsHungAppWindow) and can force-close their process.
# ============================================================================

def get_not_responding_apps() -> list:
    """Returns a list of {hwnd, pid, process_name, title} for every visible
    top-level window Windows currently considers hung."""
    if not WINDOWS:
        return []

    results = {}

    def _callback(hwnd, _extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            if not ctypes.windll.user32.IsHungAppWindow(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in results:
                return True
            try:
                process_name = psutil.Process(pid).name()
            except Exception:
                process_name = "unknown.exe"
            results[pid] = {
                "hwnd": hwnd, "pid": pid,
                "process_name": process_name,
                "title": win32gui.GetWindowText(hwnd),
            }
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        pass
    return list(results.values())


def force_close_process(pid: int) -> bool:
    """Force-terminates a process (and its children) the way Task Manager's
    'End task' does for a hung app."""
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        proc.kill()
        return True
    except Exception:
        return False


# ============================================================================
# Custom alerts - user-defined threshold alerts on top of the built-in
# CPU / anomaly ones.
# ============================================================================

def get_custom_alert_metric_value(metric: str, process_name: str = "") -> float:
    """Returns the current value for a custom alert's chosen metric, or
    None if it can't be read right now (e.g. process not running)."""
    try:
        if metric == "cpu_total":
            return psutil.cpu_percent(interval=None)
        if metric == "ram_total":
            return psutil.virtual_memory().percent
        if metric == "disk_total":
            return psutil.disk_usage(os.path.splitdrive(sys.executable)[0] + "\\").percent
        if metric == "battery_percent":
            battery = psutil.sensors_battery()
            return battery.percent if battery else None
        if metric in ("process_cpu", "process_ram"):
            target = (process_name or "").strip().lower()
            if not target:
                return None
            total = 0.0
            found = False
            for proc in psutil.process_iter(["name"]):
                try:
                    if (proc.info["name"] or "").lower() != target:
                        continue
                    found = True
                    if metric == "process_cpu":
                        total += proc.cpu_percent(interval=None)
                    else:
                        total += proc.memory_info().rss / (1024 * 1024)
                except Exception:
                    continue
            return total if found else None
    except Exception:
        return None
    return None


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
# System monitor: one shared sampler for CPU/RAM history. The tray icon
# (tooltip + anomaly detection) and the Screen Time window (sparklines)
# both subscribe to it, so the same psutil calls aren't duplicated.
# ============================================================================

class SystemMonitor(QObject):
    """Samples system stats on a timer and emits them, along with a
    rolling history of CPU/RAM used for the sparkline graphs and the
    CPU anomaly baseline."""

    sampled = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.cpu_history = deque(maxlen=BASELINE_WINDOW)
        self.ram_history = deque(maxlen=BASELINE_WINDOW)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sample)
        self._timer.start(HISTORY_POLL_MS)

    def _sample(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        self.cpu_history.append(cpu)
        self.ram_history.append(ram)

        self.sampled.emit({
            "cpu": cpu,
            "ram": ram,
            "cpu_history": list(self.cpu_history),
            "ram_history": list(self.ram_history),
        })

    def cpu_baseline(self):
        """Return (mean, stdev) of recent CPU history, or (None, None)
        if there isn't enough history yet to judge an anomaly."""
        if len(self.cpu_history) < ANOMALY_MIN_SAMPLES:
            return None, None
        mean = statistics.mean(self.cpu_history)
        stdev = statistics.pstdev(self.cpu_history)
        return mean, stdev


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
QLabel#SectionHeader {
    color: #cccccc;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#BigStat {
    color: #4fd1c5;
    font-size: 22pt;
    font-weight: bold;
}
QLabel#Insight {
    color: #f6ad55;
    font-size: 9pt;
    font-style: italic;
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
    background-color: rgba(20, 20, 20, 238);
    color: #e6e6e6;
    font-size: 9pt;
    border: 1px solid rgba(255, 255, 255, 20);
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 20px 6px 12px;
    border-radius: 5px;
    margin: 1px 2px;
}
QMenu::item:selected {
    background-color: rgba(79, 209, 197, 32);
    color: #4fd1c5;
}
QMenu::item:disabled {
    color: #666666;
}
QMenu::separator {
    height: 1px;
    background: rgba(255, 255, 255, 16);
    margin: 4px 8px;
}
QMenu::indicator {
    width: 12px;
    height: 12px;
}
"""


# ============================================================================
# Small reusable widgets shared by the Screen Time and Weekly Report windows
# ============================================================================

class SparklineWidget(QWidget):
    """A tiny live line-graph (last N samples) with a label + current
    value drawn in the corner. Used for the live CPU/RAM history."""

    def __init__(self, label: str, color: str = "#4fd1c5", max_value: float = 100.0, parent=None):
        super().__init__(parent)
        self.label = label
        self.color = QColor(color)
        self.max_value = max_value
        self.values = []
        self.setMinimumHeight(42)
        self.setMinimumWidth(100)

    def set_values(self, values):
        self.values = values
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor("#1a1a1a"))

        if len(self.values) >= 2:
            w, h = rect.width(), rect.height()
            step = w / max(len(self.values) - 1, 1)
            pen = QPen(self.color)
            pen.setWidth(2)
            painter.setPen(pen)
            points = [
                QPointF(i * step, h - (min(v, self.max_value) / self.max_value) * (h - 4) - 2)
                for i, v in enumerate(self.values)
            ]
            for a, b in zip(points, points[1:]):
                painter.drawLine(a, b)

        painter.setPen(QColor("#888888"))
        painter.setFont(QFont("Segoe UI", 7))
        current = f"{self.values[-1]:.0f}%" if self.values else "--"
        painter.drawText(4, 12, f"{self.label}: {current}")


def populate_usage_list_widget(list_widget: QListWidget, apps: dict):
    """Fill a QListWidget with one progress-bar row per application, used by
    both the Productivity Analytics window (single day) and the Weekly
    Report window (aggregated days). `apps` is keyed by raw process name;
    the label shown is the friendly Application name."""
    list_widget.clear()
    max_seconds = max(apps.values(), default=1) or 1
    for process_name, seconds in sorted(apps.items(), key=lambda kv: kv[1], reverse=True):
        item = QListWidgetItem()
        list_widget.addItem(item)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(6, 2, 6, 2)

        name_label = QLabel(friendly_app_name(process_name))
        name_label.setFixedWidth(120)

        bar = QProgressBar()
        bar.setRange(0, max_seconds)
        bar.setValue(seconds)
        bar.setFormat(format_duration(seconds))

        row_layout.addWidget(name_label)
        row_layout.addWidget(bar, stretch=1)
        item.setSizeHint(row.sizeHint())
        list_widget.setItemWidget(item, row)


# ============================================================================
# Screen time window (Qt, dark theme, with a date picker for history)
# ============================================================================

class ScreenTimeWindow(QWidget):
    """Opened on demand from the pet's tray menu or a left click on the
    tray icon. Owns its own tracking thread so it keeps recording usage
    even while hidden. A date picker lets you look back at any past day
    stored in the local database."""

    def __init__(self, monitor: "SystemMonitor"):
        super().__init__()
        self.monitor = monitor
        self.setWindowTitle("Productivity Analytics - CPU Pets")
        self.setFixedSize(320, 480)
        self.setWindowFlags(Qt.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Productivity Analytics")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        spark_row = QHBoxLayout()
        self.cpu_spark = SparklineWidget("CPU", color="#4fd1c5")
        self.ram_spark = SparklineWidget("RAM", color="#f6ad55")
        spark_row.addWidget(self.cpu_spark)
        spark_row.addWidget(self.ram_spark)
        layout.addLayout(spark_row)

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

        self.monitor.sampled.connect(self._on_monitor_sample)

    # ---------- Live sparklines ----------
    def _on_monitor_sample(self, data: dict):
        self.cpu_spark.set_values(data["cpu_history"][-SPARKLINE_POINTS:])
        self.ram_spark.set_values(data["ram_history"][-SPARKLINE_POINTS:])

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
        total_seconds = sum(apps.values())
        label = "Today" if day == date.today().isoformat() else self._format_day_label(day)
        self.total_label.setText(f"Total ({label}): {format_duration(total_seconds)}")
        populate_usage_list_widget(self.list_widget, apps)

    def closeEvent(self, event):
        """Hide instead of quitting; the pet's tray menu fully quits."""
        event.ignore()
        self.hide()

    def shutdown(self):
        self.tracker.stop()


# ============================================================================
# Weekly report window: a small analytics dashboard built from the same
# Productivity Analytics database (no separate tracking thread needed - it
# just queries on demand) - total time, top apps, week-over-week comparison,
# a category breakdown, and a one-line insight.
# ============================================================================

class WeeklyReportWindow(QWidget):
    """Shows this week's usage aggregated and compared against last week.
    Opened from the tray menu, or automatically pointed to via a tray
    notification once a week."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Weekly Report - CPU Pets")
        self.setFixedSize(340, 620)
        self.setWindowFlags(Qt.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("This Week")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        self.range_label = QLabel("")
        self.range_label.setObjectName("Muted")
        layout.addWidget(self.range_label)

        layout.addSpacing(6)

        total_caption = QLabel("Total active time")
        total_caption.setObjectName("SectionHeader")
        layout.addWidget(total_caption)

        self.total_value_label = QLabel("0m")
        self.total_value_label.setObjectName("BigStat")
        layout.addWidget(self.total_value_label)

        self.comparison_label = QLabel("")
        self.comparison_label.setObjectName("Muted")
        layout.addWidget(self.comparison_label)

        layout.addSpacing(10)

        most_used_caption = QLabel("Most used")
        most_used_caption.setObjectName("SectionHeader")
        layout.addWidget(most_used_caption)

        self.top_apps_list = QListWidget()
        self.top_apps_list.setFixedHeight(100)
        layout.addWidget(self.top_apps_list)

        layout.addSpacing(10)

        categories_caption = QLabel("App categories")
        categories_caption.setObjectName("SectionHeader")
        layout.addWidget(categories_caption)

        self.categories_list = QListWidget()
        self.categories_list.setFixedHeight(150)
        layout.addWidget(self.categories_list)

        layout.addSpacing(6)

        self.insight_label = QLabel("")
        self.insight_label.setObjectName("Insight")
        self.insight_label.setWordWrap(True)
        layout.addWidget(self.insight_label)

        layout.addStretch()

        hint = QLabel(f"Sums usage across the last {WEEKLY_REPORT_DAYS} days. "
                       "Refreshes each time you open it.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        end = date.today()
        start = end - timedelta(days=WEEKLY_REPORT_DAYS - 1)
        self.range_label.setText(f"{start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}")

        this_week = generate_weekly_report(WEEKLY_REPORT_DAYS)
        last_week = generate_previous_period_report(WEEKLY_REPORT_DAYS)

        total_this = sum(this_week.values())
        total_last = sum(last_week.values())
        self.total_value_label.setText(format_duration(total_this))

        total_change = percent_change(total_last, total_this)
        if total_change is None:
            self.comparison_label.setText("Compared to last week: not enough data yet.")
        else:
            sign = "+" if total_change >= 0 else ""
            self.comparison_label.setText(
                f"Compared to last week: {sign}{total_change:.0f}% total usage"
            )

        # ---- Most used (top 3) ----
        self.top_apps_list.clear()
        top_apps = sorted(this_week.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if not top_apps:
            self.top_apps_list.addItem(QListWidgetItem("No usage recorded yet."))
        for process_name, seconds in top_apps:
            self.top_apps_list.addItem(
                QListWidgetItem(f"{friendly_app_name(process_name)}    {format_duration(seconds)}")
            )

        # ---- App categories ----
        this_categories = categorize_totals(this_week)
        last_categories = categorize_totals(last_week)
        populate_usage_list_widget(self.categories_list, this_categories)

        # ---- One-line insight: the category you spent most time in this
        # week, and how that compares to last week ----
        if this_categories:
            top_category = max(this_categories, key=this_categories.get)
            top_cat_seconds = this_categories[top_category]
            cat_change = percent_change(last_categories.get(top_category, 0), top_cat_seconds)
            if cat_change is None:
                self.insight_label.setText(
                    f"You spent {format_duration(top_cat_seconds)} in "
                    f"{top_category.lower()} apps this week."
                )
            else:
                direction = "more" if cat_change >= 0 else "less"
                self.insight_label.setText(
                    f"You spent {abs(cat_change):.0f}% {direction} time in "
                    f"{top_category.lower()} apps this week."
                )
        else:
            self.insight_label.setText("Not enough data yet to generate an insight.")

    def closeEvent(self, event):
        """Hide instead of quitting; the pet's tray menu fully quits."""
        event.ignore()
        self.hide()


# ============================================================================
# Application Health window: crash/hang history per app, built from the
# app_health table. A "hang" is logged every time an app is newly detected
# as Not Responding; a "crash" is logged every time a hung app gets force-
# closed via the tray menu action (the most reliable proxy this app has for
# a real crash, since it doesn't hook into Windows Error Reporting).
# ============================================================================

class AppHealthWindow(QWidget):
    """Shows, per app, how many times it hung / had to be force-closed over
    the last WEEKLY_REPORT_DAYS days, plus a 'Most unstable apps' ranking."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Application Health - CPU Pets")
        self.setFixedSize(340, 480)
        self.setWindowFlags(Qt.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Application Health")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        self.range_label = QLabel("")
        self.range_label.setObjectName("Muted")
        layout.addWidget(self.range_label)

        layout.addSpacing(6)

        self.health_list = QListWidget()
        layout.addWidget(self.health_list, stretch=1)

        layout.addSpacing(6)

        unstable_caption = QLabel("Most unstable apps")
        unstable_caption.setObjectName("SectionHeader")
        layout.addWidget(unstable_caption)

        self.unstable_list = QListWidget()
        self.unstable_list.setFixedHeight(110)
        layout.addWidget(self.unstable_list)

        hint = QLabel(f"Based on the last {WEEKLY_REPORT_DAYS} days. A hang is logged the moment "
                       "Windows reports an app as Not Responding; a crash is logged whenever a hung "
                       "app has to be force-closed.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        end = date.today()
        start = end - timedelta(days=WEEKLY_REPORT_DAYS - 1)
        self.range_label.setText(f"{start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}")

        totals = get_app_health_totals(WEEKLY_REPORT_DAYS)

        self.health_list.clear()
        if not totals:
            self.health_list.addItem(QListWidgetItem("No hangs or crashes recorded - nice and stable!"))
        else:
            ranked = sorted(
                totals.items(),
                key=lambda kv: kv[1]["hangs"] + kv[1]["force_closes"],
                reverse=True,
            )
            for process_name, counts in ranked:
                name = friendly_app_name(process_name)
                crash_mark = "\u2713" if counts["force_closes"] == 0 else "\u26a0"
                hang_mark = "\u2713" if counts["hangs"] == 0 else "\u26a0"
                self.health_list.addItem(QListWidgetItem(name))
                self.health_list.addItem(
                    QListWidgetItem(f"   {crash_mark} {counts['force_closes']} crashes")
                )
                self.health_list.addItem(
                    QListWidgetItem(f"   {hang_mark} {counts['hangs']} hangs this week")
                )

        # ---- Most unstable apps (top 5 by total incidents) ----
        self.unstable_list.clear()
        ranked_unstable = sorted(
            totals.items(),
            key=lambda kv: kv[1]["hangs"] + kv[1]["force_closes"],
            reverse=True,
        )[:5]
        if not ranked_unstable:
            self.unstable_list.addItem(QListWidgetItem("Nothing to show yet."))
        for i, (process_name, _counts) in enumerate(ranked_unstable, start=1):
            self.unstable_list.addItem(QListWidgetItem(f"{i}. {friendly_app_name(process_name)}"))

    def closeEvent(self, event):
        """Hide instead of quitting; the pet's tray menu fully quits."""
        event.ignore()
        self.hide()


# ============================================================================
# Custom Alerts UI
# ============================================================================

class CustomAlertDialog(QDialog):
    """Add/Edit dialog for a single custom alert."""

    def __init__(self, parent=None, alert: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Custom Alert")
        self.setMinimumWidth(320)
        self._editing = alert is not None
        alert = alert or {}

        form = QFormLayout(self)

        self.name_edit = QLineEdit(alert.get("name", ""))
        self.name_edit.setPlaceholderText("e.g. Chrome using too much RAM")
        form.addRow("Name:", self.name_edit)

        self.metric_combo = QComboBox()
        for key, label in CUSTOM_ALERT_METRICS.items():
            self.metric_combo.addItem(label, key)
        idx = self.metric_combo.findData(alert.get("metric", "cpu_total"))
        self.metric_combo.setCurrentIndex(max(idx, 0))
        self.metric_combo.currentIndexChanged.connect(self._update_visibility)
        form.addRow("Watch:", self.metric_combo)

        self.process_edit = QLineEdit(alert.get("process_name", ""))
        self.process_edit.setPlaceholderText("exact process name, e.g. chrome.exe")
        form.addRow("App (.exe name):", self.process_edit)

        self.operator_combo = QComboBox()
        for key, label in CUSTOM_ALERT_OPERATORS.items():
            self.operator_combo.addItem(label, key)
        idx = self.operator_combo.findData(alert.get("operator", "above"))
        self.operator_combo.setCurrentIndex(max(idx, 0))
        form.addRow("Condition:", self.operator_combo)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0, 999999)
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setValue(alert.get("threshold", 80.0))
        form.addRow("Threshold:", self.threshold_spin)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 3600)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(alert.get("duration_s", 0))
        self.duration_spin.setToolTip("How long the condition must stay true before alerting. 0 = alert instantly.")
        form.addRow("Sustained for:", self.duration_spin)

        self.message_edit = QLineEdit(alert.get("message", ""))
        self.message_edit.setPlaceholderText("Optional custom notification text")
        form.addRow("Message:", self.message_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._id = alert.get("id") or uuid.uuid4().hex
        self.result_alert = None
        self._update_visibility()

    def _update_visibility(self):
        metric = self.metric_combo.currentData()
        needs_process = metric in ("process_cpu", "process_ram")
        self.process_edit.setEnabled(needs_process)
        if metric == "process_cpu":
            self.threshold_spin.setSuffix(" %")
        elif metric == "process_ram":
            self.threshold_spin.setSuffix(" MB")
        elif metric == "battery_percent":
            self.threshold_spin.setSuffix(" %")
        else:
            self.threshold_spin.setSuffix(" %")

    def _on_accept(self):
        metric = self.metric_combo.currentData()
        if metric in ("process_cpu", "process_ram") and not self.process_edit.text().strip():
            QMessageBox.warning(self, "Missing app name", "Enter the .exe name of the app to watch.")
            return
        name = self.name_edit.text().strip() or CUSTOM_ALERT_METRICS[metric]
        self.result_alert = {
            "id": self._id,
            "name": name,
            "metric": metric,
            "process_name": self.process_edit.text().strip(),
            "operator": self.operator_combo.currentData(),
            "threshold": self.threshold_spin.value(),
            "duration_s": self.duration_spin.value(),
            "message": self.message_edit.text().strip(),
            "enabled": True,
        }
        self.accept()


class CustomAlertsWindow(QWidget):
    """Manage the user's custom alerts: add, edit, delete, enable/disable."""

    def __init__(self, get_alerts, set_alerts, parent=None):
        super().__init__(parent)
        self._get_alerts = get_alerts
        self._set_alerts = set_alerts
        self.setWindowTitle("Custom Alerts - CPU Pets")
        self.setFixedSize(360, 420)
        self.setWindowFlags(Qt.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("Custom Alerts")
        title.setObjectName("Title")
        layout.addWidget(title)

        hint = QLabel("Get notified when something you choose crosses a threshold.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.list_widget = QListWidget()
        self.list_widget.itemChanged.connect(self._on_item_toggled)
        layout.addWidget(self.list_widget, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_alert)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_alert)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_alert)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()

    def _refresh(self):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for alert in self._get_alerts():
            op = CUSTOM_ALERT_OPERATORS.get(alert["operator"], alert["operator"])
            metric_label = CUSTOM_ALERT_METRICS.get(alert["metric"], alert["metric"])
            unit = "MB" if alert["metric"] == "process_ram" else "%"
            summary = f"{metric_label} {op} {alert['threshold']:.0f}{unit}"
            item = QListWidgetItem(f"{alert['name']}\n{summary}")
            item.setData(Qt.UserRole, alert["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if alert.get("enabled", True) else Qt.Unchecked)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

    def _on_item_toggled(self, item: QListWidgetItem):
        alerts = self._get_alerts()
        alert_id = item.data(Qt.UserRole)
        for alert in alerts:
            if alert["id"] == alert_id:
                alert["enabled"] = item.checkState() == Qt.Checked
        self._set_alerts(alerts)

    def _add_alert(self):
        dialog = CustomAlertDialog(self)
        if dialog.exec_() == QDialog.Accepted and dialog.result_alert:
            alerts = self._get_alerts()
            alerts.append(dialog.result_alert)
            self._set_alerts(alerts)
            self._refresh()

    def _selected_id(self):
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _edit_alert(self):
        alert_id = self._selected_id()
        if not alert_id:
            return
        alerts = self._get_alerts()
        existing = next((a for a in alerts if a["id"] == alert_id), None)
        if not existing:
            return
        dialog = CustomAlertDialog(self, existing)
        if dialog.exec_() == QDialog.Accepted and dialog.result_alert:
            alerts = [dialog.result_alert if a["id"] == alert_id else a for a in alerts]
            self._set_alerts(alerts)
            self._refresh()

    def _delete_alert(self):
        alert_id = self._selected_id()
        if not alert_id:
            return
        alerts = [a for a in self._get_alerts() if a["id"] != alert_id]
        self._set_alerts(alerts)
        self._refresh()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ============================================================================
# Reminders UI (add / edit / delete / repeat, similar to a phone alarm app)
# ============================================================================

class ReminderDialog(QDialog):
    def __init__(self, parent=None, reminder: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Reminder")
        self.setMinimumWidth(320)
        reminder = reminder or {}

        form = QFormLayout(self)

        self.title_edit = QLineEdit(reminder.get("title", ""))
        self.title_edit.setPlaceholderText("What's this reminder for?")
        form.addRow("Title:", self.title_edit)

        self.datetime_edit = QDateTimeEdit()
        self.datetime_edit.setCalendarPopup(True)
        self.datetime_edit.setDisplayFormat("yyyy-MM-dd hh:mm")
        if reminder.get("remind_at"):
            dt = QDateTime.fromString(reminder["remind_at"], Qt.ISODate)
            self.datetime_edit.setDateTime(dt)
        else:
            self.datetime_edit.setDateTime(QDateTime.currentDateTime().addSecs(300))
        form.addRow("Date & time:", self.datetime_edit)

        self.repeat_combo = QComboBox()
        self.repeat_combo.addItem("Never", REMINDER_REPEAT_NONE)
        self.repeat_combo.addItem("Every day", REMINDER_REPEAT_DAILY)
        self.repeat_combo.addItem("Weekly on selected days", REMINDER_REPEAT_WEEKLY)
        self.repeat_combo.addItem("Every month (same date)", REMINDER_REPEAT_MONTHLY)
        idx = self.repeat_combo.findData(reminder.get("repeat_mode", REMINDER_REPEAT_NONE))
        self.repeat_combo.setCurrentIndex(max(idx, 0))
        self.repeat_combo.currentIndexChanged.connect(self._update_weekday_visibility)
        form.addRow("Repeat:", self.repeat_combo)

        self.weekday_checks = []
        weekday_row = QHBoxLayout()
        selected = set(reminder.get("weekdays", "").split(",")) if reminder.get("weekdays") else set()
        for i, label in enumerate(WEEKDAY_LABELS):
            cb = QCheckBox(label)
            cb.setChecked(str(i) in selected)
            weekday_row.addWidget(cb)
            self.weekday_checks.append(cb)
        self.weekday_widget = QWidget()
        self.weekday_widget.setLayout(weekday_row)
        form.addRow("Days:", self.weekday_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._id = reminder.get("id") or uuid.uuid4().hex
        self.result_reminder = None
        self._update_weekday_visibility()

    def _update_weekday_visibility(self):
        self.weekday_widget.setVisible(self.repeat_combo.currentData() == REMINDER_REPEAT_WEEKLY)

    def _on_accept(self):
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "Missing title", "Give the reminder a title.")
            return
        weekdays = ",".join(str(i) for i, cb in enumerate(self.weekday_checks) if cb.isChecked())
        if self.repeat_combo.currentData() == REMINDER_REPEAT_WEEKLY and not weekdays:
            weekdays = str(self.datetime_edit.dateTime().date().dayOfWeek() - 1)
        self.result_reminder = {
            "id": self._id,
            "title": title,
            "remind_at": self.datetime_edit.dateTime().toString(Qt.ISODate),
            "repeat_mode": self.repeat_combo.currentData(),
            "weekdays": weekdays,
            "enabled": True,
        }
        self.accept()


class RemindersWindow(QWidget):
    """Add / edit / delete / repeat reminders for any date & time."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reminders - CPU Pets")
        self.setFixedSize(360, 420)
        self.setWindowFlags(Qt.Window)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("Reminders")
        title.setObjectName("Title")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        self.list_widget.itemChanged.connect(self._on_item_toggled)
        layout.addWidget(self.list_widget, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_reminder)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._edit_reminder)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_reminder)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for reminder in get_all_reminders():
            dt = QDateTime.fromString(reminder["remind_at"], Qt.ISODate)
            when = dt.toString("MMM d, yyyy hh:mm")
            repeat_label = {
                REMINDER_REPEAT_NONE: "Once",
                REMINDER_REPEAT_DAILY: "Daily",
                REMINDER_REPEAT_WEEKLY: "Weekly",
                REMINDER_REPEAT_MONTHLY: "Monthly",
            }.get(reminder["repeat_mode"], "Once")
            item = QListWidgetItem(f"{reminder['title']}\n{when} - {repeat_label}")
            item.setData(Qt.UserRole, reminder["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if reminder["enabled"] else Qt.Unchecked)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

    def _on_item_toggled(self, item: QListWidgetItem):
        reminders = get_all_reminders()
        reminder_id = item.data(Qt.UserRole)
        reminder = next((r for r in reminders if r["id"] == reminder_id), None)
        if reminder:
            reminder["enabled"] = item.checkState() == Qt.Checked
            save_reminder(reminder)

    def _add_reminder(self):
        dialog = ReminderDialog(self)
        if dialog.exec_() == QDialog.Accepted and dialog.result_reminder:
            save_reminder(dialog.result_reminder)
            self.refresh()

    def _selected_id(self):
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _edit_reminder(self):
        reminder_id = self._selected_id()
        if not reminder_id:
            return
        existing = next((r for r in get_all_reminders() if r["id"] == reminder_id), None)
        if not existing:
            return
        dialog = ReminderDialog(self, existing)
        if dialog.exec_() == QDialog.Accepted and dialog.result_reminder:
            save_reminder(dialog.result_reminder)
            self.refresh()

    def _delete_reminder(self):
        reminder_id = self._selected_id()
        if not reminder_id:
            return
        delete_reminder(reminder_id)
        self.refresh()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ============================================================================
# CPU Pets: the core tray pet (QSystemTrayIcon based, custom dark menu)
# ============================================================================

class CpuPetTray(QSystemTrayIcon):
    def __init__(self, screen_time_window: ScreenTimeWindow, app: QApplication, monitor: SystemMonitor):
        super().__init__()
        self.screen_time_window = screen_time_window
        self.app = app
        self.monitor = monitor
        self.weekly_report_window = None
        self.custom_alerts_window = None
        self.reminders_window = None
        self.app_health_window = None

        self.base = Path(__file__).resolve().parent
        self.frames = {animal: {"light": [], "dark": []} for animal in ANIMALS}
        self._load_all_frames_or_fail()

        self._paused = False
        self._cpu_alert_notified = False
        self.cpu_alert_enabled = True
        self._anomaly_notified = False
        self.anomaly_alert_enabled = True
        self.last_weekly_report_date = ""
        self.current_animal = DEFAULT_ANIMAL

        self.custom_alerts = []          # list of dicts, see CUSTOM_ALERT_METRICS
        self._custom_alert_state = {}    # id -> {"since": ts or None, "notified": bool}
        self.not_responding_enabled = True
        self._known_hung_pids = set()

        self.load_settings()

        self.current_theme = get_windows_app_theme()
        self._idx = 0
        self._cpu_smooth = psutil.cpu_percent(interval=None)

        self.setIcon(self._get_colored_icon(0))
        self._build_menu()
        self.activated.connect(self._on_activated)
        self.monitor.sampled.connect(self._on_monitor_sample)

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

        self._weekly_timer = QTimer(self)
        self._weekly_timer.timeout.connect(self._check_weekly_report)
        self._weekly_timer.start(WEEKLY_REPORT_CHECK_INTERVAL_MS)
        self._check_weekly_report()

        self._custom_alert_timer = QTimer(self)
        self._custom_alert_timer.timeout.connect(self._check_custom_alerts)
        self._custom_alert_timer.start(CUSTOM_ALERT_CHECK_INTERVAL_MS)

        self._reminder_timer = QTimer(self)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start(REMINDER_CHECK_INTERVAL_MS)
        self._check_reminders()

        self._not_responding_timer = QTimer(self)
        self._not_responding_timer.timeout.connect(self._check_not_responding)
        self._not_responding_timer.start(NOT_RESPONDING_CHECK_INTERVAL_MS)

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
                "anomaly_alert_enabled": self.anomaly_alert_enabled,
                "last_weekly_report_date": self.last_weekly_report_date,
                "custom_alerts": self.custom_alerts,
                "not_responding_enabled": self.not_responding_enabled,
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
                self.anomaly_alert_enabled = data.get("anomaly_alert_enabled", True)
                self.last_weekly_report_date = data.get("last_weekly_report_date", "")
                self.custom_alerts = data.get("custom_alerts", [])
                self.not_responding_enabled = data.get("not_responding_enabled", True)
                if data.get("run_on_startup", False):
                    set_run_on_startup(True)
            except Exception as e:
                print("Failed to load settings:", e)

    # ---------- Menu (custom dark, translucent, rounded-corner style) ----------
    # Reorganized into a small number of logical groups (Animal / Monitor /
    # Alerts / Settings) instead of a long flat list, so related toggles and
    # windows live together. Compact modern look: short labels (no emoji -
    # they render inconsistently across systems and read as clutter, not
    # polish), tight padding/radius instead of the default oversized QMenu
    # spacing, and a translucent rounded popup. The OS still draws its own
    # soft drop shadow behind it. Color palette itself is untouched.
    @staticmethod
    def _make_menu_modern(menu: QMenu):
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)

    def _build_menu(self):
        menu = QMenu()
        self._make_menu_modern(menu)

        # ---- Quick, most-used toggle stays at the very top ----
        self.pause_action = QAction("Pause", menu, checkable=True)
        self.pause_action.toggled.connect(self._on_pause_toggled)
        menu.addAction(self.pause_action)

        menu.addSeparator()

        # ---- Animal ----
        animal_menu = menu.addMenu("Animal")
        self._make_menu_modern(animal_menu)
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

        # ---- Monitor: everything that shows you data about your PC ----
        monitor_menu = menu.addMenu("Monitor")
        self._make_menu_modern(monitor_menu)

        screen_time_action = QAction("Productivity Analytics", monitor_menu)
        screen_time_action.triggered.connect(self._show_screen_time)
        monitor_menu.addAction(screen_time_action)

        weekly_report_action = QAction("Weekly Report", monitor_menu)
        weekly_report_action.triggered.connect(self._show_weekly_report)
        monitor_menu.addAction(weekly_report_action)

        app_health_action = QAction("App Health", monitor_menu)
        app_health_action.triggered.connect(self._show_app_health)
        monitor_menu.addAction(app_health_action)

        # ---- Alerts: everything that notifies you about something ----
        alerts_menu = menu.addMenu("Alerts")
        self._make_menu_modern(alerts_menu)

        self.cpu_alert_action = QAction("CPU 100% Alert", alerts_menu, checkable=True)
        self.cpu_alert_action.setChecked(self.cpu_alert_enabled)
        self.cpu_alert_action.toggled.connect(self._on_cpu_alert_toggled)
        alerts_menu.addAction(self.cpu_alert_action)

        self.anomaly_alert_action = QAction("Anomaly Alerts", alerts_menu, checkable=True)
        self.anomaly_alert_action.setChecked(self.anomaly_alert_enabled)
        self.anomaly_alert_action.toggled.connect(self._on_anomaly_alert_toggled)
        alerts_menu.addAction(self.anomaly_alert_action)

        self.not_responding_toggle_action = QAction("Hang Alerts", alerts_menu, checkable=True)
        self.not_responding_toggle_action.setChecked(self.not_responding_enabled)
        self.not_responding_toggle_action.toggled.connect(self._on_not_responding_toggled)
        alerts_menu.addAction(self.not_responding_toggle_action)

        alerts_menu.addSeparator()

        custom_alerts_action = QAction("Custom Alerts...", alerts_menu)
        custom_alerts_action.triggered.connect(self._show_custom_alerts)
        alerts_menu.addAction(custom_alerts_action)

        # ---- Close Hung Apps: pulled out to the top level (not buried in
        # Alerts) since it's an action you need to find fast ----
        close_not_responding_action = QAction("Close Hung Apps", menu)
        close_not_responding_action.triggered.connect(self._close_not_responding_apps)
        menu.addAction(close_not_responding_action)

        # ---- Reminders: distinct enough from Alerts to stand on its own ----
        reminders_action = QAction("Reminders", menu)
        reminders_action.triggered.connect(self._show_reminders)
        menu.addAction(reminders_action)

        menu.addSeparator()

        # ---- Settings ----
        settings_menu = menu.addMenu("Settings")
        self._make_menu_modern(settings_menu)
        self.startup_action = QAction("Run on Startup", settings_menu, checkable=True)
        self.startup_action.setChecked(is_run_on_startup())
        self.startup_action.toggled.connect(self._on_startup_toggled)
        settings_menu.addAction(self.startup_action)

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

    def _on_anomaly_alert_toggled(self, checked):
        self.anomaly_alert_enabled = checked
        if not checked:
            self._anomaly_notified = False
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

    def _show_weekly_report(self, _checked=False):
        if self.weekly_report_window is None:
            self.weekly_report_window = WeeklyReportWindow()
        w = self.weekly_report_window
        w.refresh()
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _show_app_health(self, _checked=False):
        if self.app_health_window is None:
            self.app_health_window = AppHealthWindow()
        w = self.app_health_window
        w.refresh()
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _show_custom_alerts(self, _checked=False):
        if self.custom_alerts_window is None:
            self.custom_alerts_window = CustomAlertsWindow(
                get_alerts=lambda: self.custom_alerts,
                set_alerts=self._set_custom_alerts,
            )
        w = self.custom_alerts_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _set_custom_alerts(self, alerts: list):
        self.custom_alerts = alerts
        # Drop runtime state for any alert that no longer exists.
        live_ids = {a["id"] for a in alerts}
        self._custom_alert_state = {
            k: v for k, v in self._custom_alert_state.items() if k in live_ids
        }
        self.save_settings()

    def _show_reminders(self, _checked=False):
        if self.reminders_window is None:
            self.reminders_window = RemindersWindow()
        w = self.reminders_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _close_not_responding_apps(self, _checked=False):
        """Force-close every currently hung app in one click, the same
        way Task Manager's 'End task' does - no list shown first."""
        if not WINDOWS:
            return

        apps = get_not_responding_apps()
        if not apps:
            try:
                self.showMessage(
                    APP_NAME,
                    "No apps are currently not responding.",
                    QSystemTrayIcon.Information,
                    3000,
                )
            except Exception as e:
                self._log_alert_error(e)
            return

        closed_names = []
        for app in apps:
            if force_close_process(app["pid"]):
                closed_names.append(app["process_name"])
                record_app_health_event(app["process_name"], force_close=True)
            self._known_hung_pids.discard(app["pid"])

        if closed_names:
            try:
                self.showMessage(
                    APP_NAME,
                    "Closed: " + ", ".join(closed_names),
                    QSystemTrayIcon.Information,
                    4000,
                )
            except Exception as e:
                self._log_alert_error(e)

    def _on_not_responding_toggled(self, checked):
        self.not_responding_enabled = checked
        self.save_settings()

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

    # ---------- Tooltip: CPU, RAM, and system uptime ----------
    def _update_tooltip(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        uptime = format_duration(time.time() - psutil.boot_time())
        self.setToolTip(f"{APP_NAME}\nCPU: {cpu:.0f}%   RAM: {ram:.0f}%\nUptime: {uptime}")

    # ---------- Shared system monitor: feeds the anomaly check ----------
    def _on_monitor_sample(self, data: dict):
        mean, stdev = self.monitor.cpu_baseline()
        self._check_cpu_anomaly(data["cpu"], mean, stdev)

    def _check_cpu_anomaly(self, instant_cpu, mean, stdev):
        """Flag a CPU spike that's unusual relative to its own recent
        baseline, rather than only alerting at a fixed 100% threshold."""
        if not self.anomaly_alert_enabled or mean is None:
            return

        effective_std = max(stdev, ANOMALY_MIN_STD)
        z_score = (instant_cpu - mean) / effective_std

        if instant_cpu >= ANOMALY_MIN_ABSOLUTE_CPU and z_score >= ANOMALY_Z_THRESHOLD:
            if not self._anomaly_notified:
                try:
                    self.showMessage(
                        APP_NAME,
                        f"Unusual CPU spike: {instant_cpu:.0f}% (recent baseline ~{mean:.0f}%)",
                        QSystemTrayIcon.Warning,
                        5000,
                    )
                except Exception as e:
                    self._log_alert_error(e)
                self._anomaly_notified = True
        elif z_score < ANOMALY_RESET_Z:
            self._anomaly_notified = False

    # ---------- Weekly report: auto-notify once a week ----------
    def _check_weekly_report(self):
        today = date.today()
        if today.weekday() != WEEKLY_REPORT_WEEKDAY:
            return
        today_iso = today.isoformat()
        if self.last_weekly_report_date == today_iso:
            return

        self.last_weekly_report_date = today_iso
        self.save_settings()
        try:
            self.showMessage(
                APP_NAME,
                "Your weekly usage report is ready - open it from the tray menu.",
                QSystemTrayIcon.Information,
                5000,
            )
        except Exception as e:
            self._log_alert_error(e)

    # ---------- Custom alerts ----------
    def _check_custom_alerts(self):
        now = time.time()
        for alert in self.custom_alerts:
            if not alert.get("enabled", True):
                continue

            value = get_custom_alert_metric_value(alert["metric"], alert.get("process_name", ""))
            state = self._custom_alert_state.setdefault(alert["id"], {"since": None, "notified": False})

            if value is None:
                state["since"] = None
                continue

            triggered = (value > alert["threshold"]) if alert["operator"] == "above" else (value < alert["threshold"])

            if not triggered:
                state["since"] = None
                state["notified"] = False
                continue

            if state["since"] is None:
                state["since"] = now

            if not state["notified"] and (now - state["since"]) >= alert.get("duration_s", 0):
                message = alert.get("message") or f"{alert['name']}: currently {value:.1f}"
                try:
                    self.showMessage(APP_NAME, message, QSystemTrayIcon.Warning, 5000)
                except Exception as e:
                    self._log_alert_error(e)
                state["notified"] = True

    # ---------- Reminders ----------
    def _check_reminders(self):
        now = datetime.now()
        for reminder in get_all_reminders():
            if not reminder["enabled"]:
                continue
            try:
                remind_at = datetime.fromisoformat(reminder["remind_at"])
            except ValueError:
                continue
            if remind_at > now:
                continue

            try:
                self.showMessage(APP_NAME, f"Reminder: {reminder['title']}", QSystemTrayIcon.Information, 8000)
            except Exception as e:
                self._log_alert_error(e)

            if reminder["repeat_mode"] == REMINDER_REPEAT_NONE:
                reminder["enabled"] = False
            else:
                next_at = compute_next_occurrence(remind_at, reminder["repeat_mode"], reminder["weekdays"])
                reminder["remind_at"] = next_at.isoformat()
            save_reminder(reminder)

            if self.reminders_window is not None and self.reminders_window.isVisible():
                self.reminders_window.refresh()

    # ---------- Not Responding ----------
    def _check_not_responding(self):
        if not self.not_responding_enabled or not WINDOWS:
            return

        apps = get_not_responding_apps()
        current_pids = {a["pid"] for a in apps}
        new_pids = current_pids - self._known_hung_pids
        self._known_hung_pids = current_pids

        for app in apps:
            if app["pid"] in new_pids:
                record_app_health_event(app["process_name"], hang=True)
                try:
                    self.showMessage(
                        APP_NAME,
                        f"{app['process_name']} is not responding. Use 'Close Not Responding Apps' in the tray menu to force-close it.",
                        QSystemTrayIcon.Warning,
                        6000,
                    )
                except Exception as e:
                    self._log_alert_error(e)

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


# ============================================================================
# Entry point
# ============================================================================

def main():
    hide_and_detach_console()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_QSS)

    monitor = SystemMonitor()  # noqa: F841 (kept alive by reference + signal connections)
    screen_time_window = ScreenTimeWindow(monitor)
    tray = CpuPetTray(screen_time_window, app, monitor)  # noqa: F841 (kept alive by reference)

    app.aboutToQuit.connect(screen_time_window.shutdown)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
