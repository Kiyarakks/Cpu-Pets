import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psutil
from PIL import Image, ImageDraw

from PyQt5.QtCore import Qt, QDateTime, QObject, QPoint, QPointF, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QCheckBox, QComboBox, QDateTimeEdit,
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QSystemTrayIcon, QVBoxLayout, QWidget,
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

SCRIPT_PATH = Path(sys.argv[0] if not getattr(sys, "frozen", False) else sys.executable).resolve()
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


# ============================================================================
# Shared configuration
# ============================================================================

APP_NAME = "CPU Pets"
APP_VERSION = "2.2.0"
APP_REPO_URL = "https://github.com/Kiyarakks/Cpu-Pets"
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ---- Reliability tracking (real BSOD / hang / unexpected-restart history) ----
RELIABILITY_LOOKBACK_DAYS = 30       # how far back Health Check looks
RELIABILITY_KEEP_DAYS = 180          # how long incidents stay in the DB
RELIABILITY_CLUSTER_MINUTES = 10     # events this close together = one incident
RELIABILITY_SCAN_INTERVAL_MS = 30 * 60 * 1000   # re-scan the Event Log every 30 min
RESTART_KINDS = ("bsod", "forced_poweroff", "unexpected_shutdown")

# ---- Heartbeat (what the PC was doing right before a crash) ----
HEARTBEAT_INTERVAL_MS = 20 * 1000
HEARTBEAT_MAX_GAP_HOURS = 2          # ignore a heartbeat older than this vs. the crash
HEARTBEAT_KEEP_BOOTS = 20            # keep this many past boots of heartbeat rows

# ---- Battery health ----
BATTERY_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000  # refresh the report every 6 hours

# ---- Updates ----
UPDATE_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000  # once a day
UPDATE_ASSET_NAME = "main.pyw"
UPDATE_HTTP_TIMEOUT_S = 20
UPDATE_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

# ---- Quick flyout ----
FLYOUT_REFRESH_MS = 1500

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

# ---- App categories ----
DEFAULT_CATEGORY = "Other"     # anything with no built-in or user-chosen category
MAX_CATEGORY_NAME_LEN = 20     # keeps names readable in the report list
BUILTIN_CATEGORIES = (
    "Development", "Communication", "Browsing", "Gaming",
    "Entertainment", "Productivity", "Creative", "Utilities",
)

# ---- Single instance ----
SINGLE_INSTANCE_MUTEX_NAME = "Local\\CPU_Pets_SingleInstance"  # "Local\\" = per Windows session
ERROR_ALREADY_EXISTS = 183


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


_SINGLE_INSTANCE_HANDLE = None  # kept for the whole process lifetime on purpose


def acquire_single_instance() -> bool:
    """Returns True if this is the only running copy of the app, False if
    another copy already owns the mutex. A named Windows mutex is released
    automatically by the OS when the owning process ends - even after a
    crash - so there is no stale lock file to clean up. If anything goes
    wrong creating it, we allow the app to start rather than block it."""
    global _SINGLE_INSTANCE_HANDLE
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _SINGLE_INSTANCE_HANDLE = handle
        return True
    except Exception:
        return True


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reliability_events (
            event_key TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            logged_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            process_name TEXT NOT NULL DEFAULT '',
            dismissed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(reliability_events)")}
    if "dismissed" not in existing_columns:  # upgrading a database created before this column existed
        conn.execute("ALTER TABLE reliability_events ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reliability_ignored (
            event_key TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            ignored_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS heartbeat (
            boot_time INTEGER PRIMARY KEY,
            last_ts TEXT NOT NULL,
            cpu REAL NOT NULL DEFAULT 0,
            ram REAL NOT NULL DEFAULT 0,
            top_cpu_name TEXT NOT NULL DEFAULT '',
            top_cpu_pct REAL NOT NULL DEFAULT 0,
            top_ram_name TEXT NOT NULL DEFAULT '',
            top_ram_mb REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS battery_health (
            day TEXT PRIMARY KEY,
            design_mwh INTEGER NOT NULL DEFAULT 0,
            full_mwh INTEGER NOT NULL DEFAULT 0,
            cycles INTEGER NOT NULL DEFAULT 0,
            label TEXT NOT NULL DEFAULT ''
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


def get_all_time_usage() -> dict:
    """{process_name: total_seconds} across the whole history, biggest
    first. Used by the category editor to list every app ever seen."""
    conn = _open_db_connection()
    try:
        rows = conn.execute(
            "SELECT process_name, SUM(seconds) AS total FROM usage "
            "GROUP BY process_name ORDER BY total DESC"
        ).fetchall()
        return {name: int(total or 0) for name, total in rows}
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
    "vivaldi.exe": "Vivaldi",
    "cursor.exe": "Cursor",
    "pwsh.exe": "PowerShell 7",
    "steamwebhelper.exe": "Steam",
    "epicgameslauncher.exe": "Epic Games",
    "battle.net.exe": "Battle.net",
    "leagueclient.exe": "League of Legends",
    "league of legends.exe": "League of Legends",
    "valorant-win64-shipping.exe": "Valorant",
    "cs2.exe": "Counter-Strike 2",
    "dota2.exe": "Dota 2",
    "obs64.exe": "OBS Studio",
    "notion.exe": "Notion",
    "obsidian.exe": "Obsidian",
    "taskmgr.exe": "Task Manager",
    "mspaint.exe": "Paint",
}

# Built-in mapping, grouped by category to keep it easy to extend. Users can
# override any app (or add new categories) from the "App Categories" window;
# those choices live in settings.json and win over this table.
_CATEGORY_APPS = {
    "Development": (
        "code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
        "webstorm64.exe", "clion64.exe", "rider64.exe", "studio64.exe",
        "sublime_text.exe", "notepad++.exe", "cmd.exe", "powershell.exe",
        "pwsh.exe", "windowsterminal.exe", "mintty.exe", "python.exe",
        "pythonw.exe", "githubdesktop.exe", "postman.exe",
    ),
    "Communication": (
        "discord.exe", "slack.exe", "teams.exe", "ms-teams.exe", "outlook.exe",
        "olk.exe", "whatsapp.exe", "telegram.exe", "signal.exe", "zoom.exe",
        "skype.exe", "viber.exe", "thunderbird.exe",
    ),
    "Browsing": (
        "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
        "vivaldi.exe", "iexplore.exe", "chromium.exe",
    ),
    "Gaming": (
        "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
        "battle.net.exe", "eadesktop.exe", "origin.exe", "upc.exe",
        "ubisoftconnect.exe", "galaxyclient.exe", "xboxpcapp.exe",
        "riotclientservices.exe", "valorant-win64-shipping.exe",
        "leagueclient.exe", "league of legends.exe", "cs2.exe", "csgo.exe",
        "dota2.exe", "fortniteclient-win64-shipping.exe", "gta5.exe",
        "robloxplayerbeta.exe", "minecraftlauncher.exe", "r5apex.exe",
        "overwatch.exe", "eldenring.exe", "cyberpunk2077.exe",
        "rocketleague.exe", "tslgame.exe", "genshinimpact.exe",
    ),
    "Entertainment": (
        "spotify.exe", "vlc.exe", "potplayermini64.exe", "potplayermini.exe",
        "mpc-hc64.exe", "wmplayer.exe", "itunes.exe", "musicbee.exe", "kodi.exe",
    ),
    "Productivity": (
        "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe",
        "acrobat.exe", "acrord32.exe", "notion.exe", "obsidian.exe",
        "evernote.exe", "soffice.exe", "soffice.bin",
    ),
    "Creative": (
        "photoshop.exe", "illustrator.exe", "blender.exe",
        "adobe premiere pro.exe", "afterfx.exe", "figma.exe", "resolve.exe",
        "obs64.exe", "audacity.exe", "krita.exe", "inkscape.exe",
    ),
    "Utilities": (
        "explorer.exe", "notepad.exe", "taskmgr.exe", "mspaint.exe",
        "calculatorapp.exe", "snippingtool.exe", "systemsettings.exe",
        "regedit.exe", "winrar.exe", "7zfm.exe",
    ),
}

APP_CATEGORIES = {
    exe: category for category, exes in _CATEGORY_APPS.items() for exe in exes
}

# {process_name: category} chosen by the user. Only differences from the
# built-in mapping are stored. Loaded from settings.json at startup.
USER_CATEGORY_OVERRIDES = {}


def _clean_category_name(name) -> str:
    return " ".join(str(name or "").split())[:MAX_CATEGORY_NAME_LEN].strip()


def normalize_process_name(name) -> str:
    """'Game' / ' GAME.EXE ' -> 'game.exe'. Empty string if nothing usable."""
    name = str(name or "").strip().lower()
    if not name:
        return ""
    return name if "." in name else name + ".exe"


def set_user_category_overrides(overrides: dict):
    """Replace the in-memory user overrides (e.g. after loading settings)."""
    # Case-insensitive names ("gaming" == "Gaming") collapse to one spelling:
    # built-in names win, otherwise the first spelling seen.
    canonical = {n.lower(): n for n in BUILTIN_CATEGORIES}
    canonical[DEFAULT_CATEGORY.lower()] = DEFAULT_CATEGORY

    cleaned = {}
    for process_name, category in (overrides or {}).items():
        process_name = normalize_process_name(process_name)
        category = _clean_category_name(category)
        if process_name and category:
            cleaned[process_name] = canonical.setdefault(category.lower(), category)
    USER_CATEGORY_OVERRIDES.clear()
    USER_CATEGORY_OVERRIDES.update(cleaned)


def get_all_category_names() -> list:
    """Built-in categories, then any custom ones the user created, then
    'Other' last."""
    names = list(BUILTIN_CATEGORIES)
    known = {n.lower() for n in names} | {DEFAULT_CATEGORY.lower()}
    for name in sorted(set(USER_CATEGORY_OVERRIDES.values()), key=str.lower):
        if name.lower() not in known:
            names.append(name)
            known.add(name.lower())
    names.append(DEFAULT_CATEGORY)
    return names


def normalize_category_name(name) -> str:
    """Cleans a typed category name and snaps it to the exact spelling of an
    existing category if one matches ignoring case ('gaming' -> 'Gaming')."""
    cleaned = _clean_category_name(name)
    for existing in get_all_category_names():
        if existing.lower() == cleaned.lower():
            return existing
    return cleaned


def default_category_for(process_name: str) -> str:
    """The built-in category, ignoring any user override."""
    return APP_CATEGORIES.get((process_name or "").strip().lower(), DEFAULT_CATEGORY)


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
    """The user's own choice if there is one, otherwise the built-in
    category, otherwise 'Other'. Categories are resolved at display time, so
    changing one also re-labels all past usage."""
    key = (process_name or "").strip().lower()
    return USER_CATEGORY_OVERRIDES.get(key) or APP_CATEGORIES.get(key, DEFAULT_CATEGORY)


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
# Shared helpers for the reliability / battery / update features
# ============================================================================

def log_error(context: str, err) -> None:
    """The console is hidden, so background problems go to the log file."""
    try:
        with open(ALERT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {context}: {err}\n")
    except Exception:
        pass


def run_hidden(args, timeout: int = 30):
    """Runs a console tool without flashing a window. Returns the finished
    process (stdout/stderr as bytes) or None if it could not be run. stdin is
    pointed at DEVNULL because pythonw has no valid stdin handle."""
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        log_error(f"run {args[0]}", e)
        return None


class BackgroundTask(QObject):
    """Runs a plain function on a daemon thread and hands its return value
    back on the Qt thread through `done`. Daemon threads mean a slow job can
    never keep the app from quitting. If the function raises, `done` gets
    None (and the error is logged)."""

    done = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self.running = False

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            result = self._fn()
        except Exception as e:
            log_error("background task", e)
            result = None
        self.running = False
        self.done.emit(result)


def _to_int(value) -> int:
    """'209' -> 209, '0xd1' -> 209, anything unreadable -> 0."""
    text = str(value if value is not None else "").strip()
    try:
        return int(text)
    except ValueError:
        try:
            return int(text, 16)
        except ValueError:
            return 0


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def format_event_time(iso_text: str) -> str:
    try:
        moment = datetime.fromisoformat(iso_text)
    except (TypeError, ValueError):
        return str(iso_text)
    fmt = "%b %d, %H:%M" if moment.year == date.today().year else "%b %d %Y, %H:%M"
    return moment.strftime(fmt)


def format_ago(moment: datetime) -> str:
    seconds = max(0, int((datetime.now() - moment).total_seconds()))
    return "just now" if seconds < 60 else format_duration(seconds) + " ago"


# ============================================================================
# Reliability: a real crash / unexpected-restart history built from the
# Windows Event Log (via the built-in wevtutil tool - no extra packages),
# plus the app's own "heartbeat" so it can say what the PC was doing right
# before it died.
# ============================================================================

# Kind -> (label, severity). Severity picks the colour in the UI.
KIND_INFO = {
    "bsod": ("Blue screen", "bad"),
    "forced_poweroff": ("Forced power-off", "bad"),
    "unexpected_shutdown": ("Unexpected shutdown", "bad"),
    "hardware_error": ("Hardware error", "bad"),
    "disk_error": ("Disk error", "bad"),
    "gpu_reset": ("Graphics driver reset", "warn"),
    "app_crash": ("App crash", "info"),
    "app_hang": ("App hang", "info"),
}
SEVERITY_COLORS = {"bad": "#fc8181", "warn": "#f6ad55", "info": "#a0aec0", "ok": "#68d391"}

# Bugcheck (STOP) codes people actually run into: code -> (name, likely area).
STOP_CODES = {
    0x0A: ("IRQL_NOT_LESS_OR_EQUAL", "a faulty driver or bad RAM"),
    0x1A: ("MEMORY_MANAGEMENT", "bad RAM or a driver corrupting memory"),
    0x1E: ("KMODE_EXCEPTION_NOT_HANDLED", "a faulty driver"),
    0x24: ("NTFS_FILE_SYSTEM", "a disk or file-system problem"),
    0x3B: ("SYSTEM_SERVICE_EXCEPTION", "a faulty driver (often graphics or security software)"),
    0x50: ("PAGE_FAULT_IN_NONPAGED_AREA", "bad RAM or a faulty driver"),
    0x51: ("REGISTRY_ERROR", "a corrupted registry or a failing disk"),
    0x77: ("KERNEL_STACK_INPAGE_ERROR", "a failing disk/SSD or a loose cable"),
    0x7A: ("KERNEL_DATA_INPAGE_ERROR", "a failing disk/SSD, a loose cable, or bad RAM"),
    0x7B: ("INACCESSIBLE_BOOT_DEVICE", "a storage driver or boot-disk problem"),
    0x7E: ("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", "a faulty driver"),
    0x7F: ("UNEXPECTED_KERNEL_MODE_TRAP", "a hardware fault (RAM/CPU) or an unstable overclock"),
    0x9F: ("DRIVER_POWER_STATE_FAILURE", "a driver that failed during sleep, wake or shutdown"),
    0xA0: ("INTERNAL_POWER_ERROR", "a power-management driver or firmware problem"),
    0xBE: ("ATTEMPTED_WRITE_TO_READONLY_MEMORY", "a faulty driver"),
    0xC2: ("BAD_POOL_CALLER", "a faulty driver"),
    0xC4: ("DRIVER_VERIFIER_DETECTED_VIOLATION", "a driver flagged by Driver Verifier"),
    0xC5: ("DRIVER_CORRUPTED_EXPOOL", "a faulty driver"),
    0xD1: ("DRIVER_IRQL_NOT_LESS_OR_EQUAL", "a faulty driver (often network or graphics)"),
    0xEA: ("THREAD_STUCK_IN_DEVICE_DRIVER", "a graphics driver or GPU that stopped responding"),
    0xEF: ("CRITICAL_PROCESS_DIED", "a critical Windows process being killed (corruption or a bad driver)"),
    0xF4: ("CRITICAL_OBJECT_TERMINATION", "a critical process ending - often a failing disk"),
    0x101: ("CLOCK_WATCHDOG_TIMEOUT", "a CPU that stopped responding (hardware, firmware or overclock)"),
    0x109: ("CRITICAL_STRUCTURE_CORRUPTION", "a faulty driver or bad RAM"),
    0x116: ("VIDEO_TDR_FAILURE", "the graphics driver or GPU"),
    0x117: ("VIDEO_TDR_TIMEOUT_DETECTED", "the graphics driver or GPU"),
    0x119: ("VIDEO_SCHEDULER_INTERNAL_ERROR", "the graphics driver or GPU"),
    0x124: ("WHEA_UNCORRECTABLE_ERROR", "a hardware fault (CPU, RAM, motherboard or power supply)"),
    0x133: ("DPC_WATCHDOG_VIOLATION", "a driver or SSD firmware that stalled the system"),
    0x139: ("KERNEL_SECURITY_CHECK_FAILURE", "corrupted data from a faulty driver or bad RAM"),
    0x13A: ("KERNEL_MODE_HEAP_CORRUPTION", "a faulty driver"),
    0x141: ("VIDEO_ENGINE_TIMEOUT_DETECTED", "the graphics driver or GPU"),
    0x154: ("UNEXPECTED_STORE_EXCEPTION", "a failing disk/SSD or security software"),
}


def describe_stop_code(code: int):
    """-> (name, likely_area). Unknown codes still get a readable name."""
    return STOP_CODES.get(code, (f"STOP 0x{code:X}", ""))


# (channel, provider, event ids, levels) - one small wevtutil query each, so
# a problem with one source never hides the others.
RELIABILITY_QUERIES = (
    ("System", "Microsoft-Windows-Kernel-Power", (41,), None),
    ("System", "EventLog", (6008,), None),
    ("System", "Microsoft-Windows-WER-SystemErrorReporting", (1001,), None),
    ("System", "BugCheck", (1001,), None),
    ("System", "Display", (4101,), None),
    ("System", "Microsoft-Windows-WHEA-Logger", None, (1, 2)),
    ("System", "disk", (7, 11, 51), None),
    ("System", "Ntfs", (55,), None),
    ("Application", "Application Error", (1000,), None),
    ("Application", "Application Hang", (1002,), None),
)

_EVT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

# Events that together describe "the PC restarted without shutting down".
_RESTART_SOURCES = {
    ("Microsoft-Windows-Kernel-Power", 41),
    ("EventLog", 6008),
    ("Microsoft-Windows-WER-SystemErrorReporting", 1001),
    ("BugCheck", 1001),
}


def build_event_xpath(provider, ids=None, levels=None, days=RELIABILITY_LOOKBACK_DAYS) -> str:
    parts = [f"Provider[@Name='{provider}']"]
    if ids:
        parts.append("(" + " or ".join(f"EventID={i}" for i in ids) + ")")
    if levels:
        parts.append("(" + " or ".join(f"Level={lv}" for lv in levels) + ")")
    parts.append(f"TimeCreated[timediff(@SystemTime) <= {int(days * 24 * 3600 * 1000)}]")
    return "*[System[" + " and ".join(parts) + "]]"


def parse_event_time(value: str):
    """'2026-09-20T18:03:11.123456700Z' (UTC) -> local naive datetime."""
    try:
        utc = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return utc.astimezone().replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def parse_event_xml(text: str) -> list:
    """wevtutil prints one <Event> per record with no root element, so wrap
    the output before parsing. Returns a list of plain dicts."""
    try:
        root = ET.fromstring("<Events>" + text + "</Events>")
    except ET.ParseError:
        return []

    events = []
    for ev in root.iter(_EVT_NS + "Event"):
        system = ev.find(_EVT_NS + "System")
        if system is None:
            continue
        provider = system.find(_EVT_NS + "Provider")
        time_el = system.find(_EVT_NS + "TimeCreated")
        id_el = system.find(_EVT_NS + "EventID")
        if provider is None or time_el is None or id_el is None:
            continue
        record_el = system.find(_EVT_NS + "EventRecordID")
        level_el = system.find(_EVT_NS + "Level")

        data, named = [], {}
        event_data = ev.find(_EVT_NS + "EventData")
        if event_data is not None:
            for item in event_data.findall(_EVT_NS + "Data"):
                value = (item.text or "").strip()
                data.append(value)
                if item.get("Name"):
                    named[item.get("Name")] = value

        events.append({
            "provider": provider.get("Name", ""),
            "event_id": _to_int(id_el.text),
            "record_id": _to_int(record_el.text) if record_el is not None else 0,
            "level": _to_int(level_el.text) if level_el is not None else 0,
            "time": parse_event_time(time_el.get("SystemTime", "")),
            "data": data,
            "named": named,
        })
    return events


def query_event_log(channel: str, xpath: str, max_events: int = 500) -> list:
    result = run_hidden(
        ["wevtutil.exe", "qe", channel, "/q:" + xpath, "/f:xml", "/rd:true", f"/c:{max_events}"],
        timeout=60,
    )
    if result is None or result.returncode != 0 or not result.stdout:
        return []
    return parse_event_xml(result.stdout.decode("utf-8", errors="replace"))


def _event_data(event: dict, index: int) -> str:
    return event["data"][index] if index < len(event["data"]) else ""


def analyze_reliability_events(events: list) -> list:
    """Turns raw Windows events into incidents. A blue screen writes several
    events at the next boot (Kernel-Power 41, BugCheck 1001, EventLog 6008),
    so events close together are merged into ONE incident:
      * a stop code was recorded          -> blue screen
      * power button held down            -> forced power-off (usually a freeze)
      * neither                           -> unexpected shutdown
    Other events (app crash/hang, GPU reset, hardware and disk errors) become
    one incident each."""
    incidents = []

    # ---------- restarts / blue screens ----------
    restart_events = sorted(
        (e for e in events if (e["provider"], e["event_id"]) in _RESTART_SOURCES and e["time"]),
        key=lambda e: e["time"],
    )
    clusters = []
    for e in restart_events:
        if clusters and e["time"] - clusters[-1][-1]["time"] <= timedelta(minutes=RELIABILITY_CLUSTER_MINUTES):
            clusters[-1].append(e)
        else:
            clusters.append([e])

    for cluster in clusters:
        stop_code, params, dump, power_button = 0, [], "", False
        for e in cluster:
            named = e["named"]
            if e["provider"] == "Microsoft-Windows-Kernel-Power":
                code = _to_int(named.get("BugcheckCode", 0))
                if code and not stop_code:
                    stop_code = code
                if not params:
                    params = [named.get(f"BugcheckParameter{i}", "") for i in range(1, 5)]
                if (_to_int(named.get("PowerButtonTimestamp", 0))
                        or str(named.get("LongPowerButtonPressDetected", "")).lower() == "true"):
                    power_button = True
            elif e["event_id"] == 1001:
                match = re.search(r"0x[0-9a-fA-F]+", " ".join(e["data"]))
                if match and int(match.group(0), 16):
                    stop_code = int(match.group(0), 16)  # the 1001 event is the authoritative source
                for item in e["data"]:
                    if item.lower().endswith(".dmp"):
                        dump = item

        logged_at = cluster[0]["time"].isoformat(timespec="seconds")
        if stop_code:
            name, hint = describe_stop_code(stop_code)
            kind = "bsod"
            title = f"Blue screen: {name} (0x{stop_code:X})"
            lines = [f"Stop code: 0x{stop_code:08X} ({name})"]
            if hint:
                lines.append(f"This kind of crash usually points to {hint}.")
            if any(params):
                lines.append("Parameters: " + ", ".join(p for p in params if p))
            if dump:
                lines.append(f"Memory dump: {dump}")
            lines.append("Windows restarted itself after the crash.")
        elif power_button:
            kind = "forced_poweroff"
            title = "Forced power-off (the PC most likely froze)"
            lines = [
                "The power button was held down to turn the PC off, and Windows did not record a crash code.",
                "That almost always means the system was frozen or hung and could not recover on its own.",
            ]
        else:
            kind = "unexpected_shutdown"
            title = "Unexpected shutdown / restart"
            lines = [
                "Windows was not shut down cleanly and recorded no crash code.",
                "Typical causes: power loss (charger, battery or power supply), a hard freeze followed by the "
                "reset button, or a hardware fault.",
            ]

        incidents.append({
            "event_key": f"restart:{cluster[0]['provider']}:{cluster[0]['record_id']}",
            "logged_at": logged_at,
            "occurred_at": logged_at,
            "kind": kind,
            "title": title,
            "details": "\n".join(lines),
            "process_name": "",
        })

    # ---------- everything else: one event = one incident ----------
    for e in events:
        if not e["time"] or (e["provider"], e["event_id"]) in _RESTART_SOURCES:
            continue
        moment = e["time"].isoformat(timespec="seconds")
        provider, event_id = e["provider"], e["event_id"]
        base = {
            "event_key": f"{provider}:{e['record_id']}:{event_id}",
            "logged_at": moment,
            "occurred_at": moment,
            "process_name": "",
        }

        if provider == "Application Error" and event_id == 1000:
            app = _event_data(e, 0) or "An app"
            lines = []
            if _event_data(e, 3):
                lines.append(f"Faulting module: {_event_data(e, 3)}")
            if _event_data(e, 6):
                lines.append(f"Exception code: {_event_data(e, 6)}")
            if _event_data(e, 10):
                lines.append(f"Program: {_event_data(e, 10)}")
            incidents.append({**base, "kind": "app_crash", "process_name": app.lower(),
                              "title": f"{friendly_app_name(app)} crashed",
                              "details": "\n".join(lines) or "Windows recorded an application crash."})
        elif provider == "Application Hang" and event_id == 1002:
            app = _event_data(e, 0) or "An app"
            incidents.append({**base, "kind": "app_hang", "process_name": app.lower(),
                              "title": f"{friendly_app_name(app)} stopped responding",
                              "details": "Windows recorded that this program hung and had to be closed."})
        elif provider == "Display" and event_id == 4101:
            incidents.append({**base, "kind": "gpu_reset",
                              "title": "Graphics driver stopped responding and recovered",
                              "details": f"Driver: {_event_data(e, 0) or 'unknown'}\n"
                                         "Windows reset the graphics driver. Repeated resets point to a driver "
                                         "problem or an unstable GPU or power supply."})
        elif provider == "Microsoft-Windows-WHEA-Logger":
            incidents.append({**base, "kind": "hardware_error",
                              "title": "Hardware error reported by Windows (WHEA)",
                              "details": f"WHEA-Logger event {event_id}.\n"
                                         "These come from the CPU, RAM, motherboard or power supply. If they "
                                         "repeat, run the Windows Memory Diagnostic and update the BIOS."})
        elif provider == "disk":
            names = {7: "bad block", 11: "controller error", 51: "paging error"}
            incidents.append({**base, "kind": "disk_error",
                              "title": f"Disk error: {names.get(event_id, 'error')} (event {event_id})",
                              "details": f"Device: {_event_data(e, 0) or 'unknown'}\n"
                                         "Back up important files, then check the drive and its cable."})
        elif provider == "Ntfs":
            incidents.append({**base, "kind": "disk_error",
                              "title": "NTFS file-system corruption detected (event 55)",
                              "details": "Run 'chkdsk /scan' on the affected drive and back up important files."})

    return incidents


def find_heartbeat_before(conn, when: datetime):
    """The app's last heartbeat before `when`, i.e. the last sign of life of
    the boot that crashed. Returns None if there is no trustworthy one."""
    row = conn.execute(
        "SELECT boot_time, last_ts, cpu, ram, top_cpu_name, top_cpu_pct, top_ram_name, top_ram_mb "
        "FROM heartbeat WHERE last_ts <= ? ORDER BY last_ts DESC LIMIT 1",
        (when.isoformat(timespec="seconds"),),
    ).fetchone()
    if not row:
        return None
    keys = ("boot_time", "last_ts", "cpu", "ram", "top_cpu_name", "top_cpu_pct", "top_ram_name", "top_ram_mb")
    beat = dict(zip(keys, row))
    try:
        beat_time = datetime.fromisoformat(beat["last_ts"])
    except ValueError:
        return None
    if when - beat_time > timedelta(hours=HEARTBEAT_MAX_GAP_HOURS):
        return None  # too old to be the session that crashed
    if beat["boot_time"] >= when.timestamp() - 60:
        return None  # belongs to the boot that logged the event, not the one that died
    return beat


def describe_heartbeat(beat: dict, logged_at: datetime) -> list:
    """Human-readable 'what was the PC doing just before it died' lines."""
    lines = [
        f"Last sign of life: {format_event_time(beat['last_ts'])} "
        f"(the restart was logged at {logged_at.strftime('%H:%M')})",
        f"At that moment: CPU {beat['cpu'] or 0:.0f}%, RAM {beat['ram'] or 0:.0f}%",
    ]
    if beat["top_cpu_name"] and beat["top_cpu_pct"] >= 1:
        lines.append(f"Busiest app: {beat['top_cpu_name']} ({beat['top_cpu_pct']:.0f}% CPU)")
    if beat["top_ram_name"]:
        lines.append(f"Biggest memory user: {beat['top_ram_name']} ({beat['top_ram_mb']:,.0f} MB)")
    if (beat["ram"] or 0) >= 95:
        lines.append("Insight: RAM was almost full - a memory-hungry app may have frozen the system.")
    elif (beat["cpu"] or 0) >= 95:
        lines.append("Insight: the CPU was maxed out right before the failure.")
    lines.append(f"(The app checks every {HEARTBEAT_INTERVAL_MS // 1000} seconds, so this is up to that much earlier.)")
    return lines


def _ignore_event_keys(conn, rows):
    """rows: iterable of (event_key, occurred_at). Marks these specific
    Windows Event Log entries as permanently ignored, so a future scan that
    finds the exact same entry still sitting in the Event Log (Windows has
    no supported way to delete a single entry - wevtutil can only clear an
    entire channel) will skip it instead of re-adding it. A genuinely new
    problem always gets a new record id, so it is never affected by this."""
    if not rows:
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO reliability_ignored (event_key, occurred_at, ignored_at) VALUES (?,?,?)",
        [(key, occurred_at, now) for key, occurred_at in rows],
    )
    # No point remembering an ignored entry once it has aged out of the scan
    # window anyway - it could never reappear, so keep this table small.
    cutoff = (datetime.now() - timedelta(days=RELIABILITY_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM reliability_ignored WHERE occurred_at < ?", (cutoff,))


def store_reliability_incidents(conn, incidents: list) -> list:
    """Saves incidents that are not stored yet and returns just the new ones.
    Restart incidents also get the pre-crash heartbeat attached. Incidents
    whose event_key was reset away by the user are skipped entirely, even
    though the underlying Windows Event Log entry is still there."""
    if incidents:
        ignored_keys = {row[0] for row in conn.execute("SELECT event_key FROM reliability_ignored")}
        incidents = [inc for inc in incidents if inc["event_key"] not in ignored_keys]
    new_items = []
    restart_kinds = RESTART_KINDS
    for inc in incidents:
        if inc["kind"] in restart_kinds:
            duplicate = conn.execute(
                "SELECT 1 FROM reliability_events "
                "WHERE kind IN (?, ?, ?) AND ABS(strftime('%s', logged_at) - strftime('%s', ?)) < ? LIMIT 1",
                (*restart_kinds, inc["logged_at"], RELIABILITY_CLUSTER_MINUTES * 60),
            ).fetchone()
            if duplicate:
                continue
            logged = datetime.fromisoformat(inc["logged_at"])
            beat = find_heartbeat_before(conn, logged)
            if beat:
                inc = dict(inc)
                inc["occurred_at"] = beat["last_ts"]
                inc["details"] = inc["details"] + "\n\n" + "\n".join(describe_heartbeat(beat, logged))
            else:
                inc = dict(inc)
                inc["details"] += (
                    f"\n\nLogged by Windows at {logged.strftime('%H:%M')} on the next start "
                    "(this app was not running before the crash, so the exact time and load are unknown)."
                )
        cursor = conn.execute(
            "INSERT OR IGNORE INTO reliability_events "
            "(event_key, occurred_at, logged_at, kind, title, details, process_name) VALUES (?,?,?,?,?,?,?)",
            (inc["event_key"], inc["occurred_at"], inc["logged_at"], inc["kind"],
             inc["title"], inc["details"], inc["process_name"]),
        )
        if cursor.rowcount:
            new_items.append(inc)
    conn.commit()
    return new_items


def scan_reliability_log(days: int = RELIABILITY_LOOKBACK_DAYS) -> list:
    """Reads the Windows Event Log and stores anything new. Slow (several
    wevtutil calls) - always run it off the UI thread. Returns the newly
    stored incidents."""
    if os.name != "nt":
        return []
    events = []
    for channel, provider, ids, levels in RELIABILITY_QUERIES:
        events.extend(query_event_log(channel, build_event_xpath(provider, ids, levels, days)))
    incidents = analyze_reliability_events(events)

    conn = _open_db_connection()
    try:
        new_items = store_reliability_incidents(conn, incidents)
        cutoff = (datetime.now() - timedelta(days=RELIABILITY_KEEP_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM reliability_events WHERE logged_at < ?", (cutoff,))
        conn.commit()
        return new_items
    finally:
        conn.close()


_EVENT_COLUMNS = ("event_key", "occurred_at", "logged_at", "kind", "title", "details", "process_name", "dismissed")


def get_reliability_events(days=None, kinds=None, limit: int = 300, include_dismissed: bool = True) -> list:
    """include_dismissed=False is what Health Check's scoring uses, so a
    dismissed incident no longer costs points; the history window still
    shows dismissed items (greyed out) so nothing just disappears silently."""
    conn = _open_db_connection()
    try:
        sql = "SELECT " + ", ".join(_EVENT_COLUMNS) + " FROM reliability_events"
        clauses, params = [], []
        if days is not None:
            clauses.append("logged_at >= ?")
            params.append((datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
        if kinds:
            clauses.append("kind IN (" + ",".join("?" * len(kinds)) + ")")
            params.extend(kinds)
        if not include_dismissed:
            clauses.append("dismissed = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)
        return [dict(zip(_EVENT_COLUMNS, row)) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def count_reliability_events(days: int, include_dismissed: bool = True) -> dict:
    """{kind: how many} for the last `days` days."""
    conn = _open_db_connection()
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        sql = "SELECT kind, COUNT(*) FROM reliability_events WHERE logged_at >= ?"
        params = [since]
        if not include_dismissed:
            sql += " AND dismissed = 0"
        sql += " GROUP BY kind"
        rows = conn.execute(sql, params).fetchall()
        return {kind: count for kind, count in rows}
    finally:
        conn.close()


def set_reliability_events_dismissed(event_keys: list, dismissed: bool) -> int:
    """Marks the given incidents dismissed/undismissed. Returns how many rows
    changed. Dismissing does not delete anything - it just excludes that
    incident from the Health Check score while keeping it visible in history."""
    if not event_keys:
        return 0
    conn = _open_db_connection()
    try:
        placeholders = ",".join("?" * len(event_keys))
        cursor = conn.execute(
            f"UPDATE reliability_events SET dismissed = ? WHERE event_key IN ({placeholders})",
            [1 if dismissed else 0, *event_keys],
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def clear_reliability_events(days=None) -> int:
    """Deletes reliability incidents outright (not just dismisses them) and
    permanently ignores their event_key (see _ignore_event_keys), so a
    later scan will not bring the exact same Windows Event Log entries back.
    days=None clears everything; days=N clears only incidents from the last
    N days. Returns how many rows were deleted."""
    conn = _open_db_connection()
    try:
        if days is None:
            rows = conn.execute("SELECT event_key, occurred_at FROM reliability_events").fetchall()
            cursor = conn.execute("DELETE FROM reliability_events")
        else:
            since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
            rows = conn.execute(
                "SELECT event_key, occurred_at FROM reliability_events WHERE logged_at >= ?", (since,)
            ).fetchall()
            cursor = conn.execute("DELETE FROM reliability_events WHERE logged_at >= ?", (since,))
        _ignore_event_keys(conn, rows)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# ---------------- heartbeat: the app's own black-box recorder ----------------

# Names that show CPU time but are not something the user can act on.
_IGNORED_PROCESSES = frozenset({"system", "registry", "memory compression", "secure system", "system idle process"})


def sample_process_usage(light: bool = False):
    """-> ({name: cpu % of the whole machine}, {name: memory in MB}) with all
    processes of the same name added together (Chrome has dozens).

    `light=True` skips memory_info() (the more expensive per-process call).
    Used by the frequent heartbeat timer to keep its own footprint small;
    the full version is used where RAM actually matters (Health Check, the
    flyout's top-processes list, and the heartbeat's own periodic full scan)."""
    cpu_by_name, ram_by_name = {}, {}
    cores = psutil.cpu_count() or 1
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name or proc.info.get("pid") == 0:
                continue
        except Exception:
            continue
        try:
            cpu = proc.cpu_percent(interval=None) / cores
        except Exception:
            cpu = 0.0
        cpu_by_name[name] = cpu_by_name.get(name, 0.0) + cpu
        if not light:
            try:
                mb = proc.memory_info().rss / (1024 * 1024)
            except Exception:
                mb = 0.0
            ram_by_name[name] = ram_by_name.get(name, 0.0) + mb
    return cpu_by_name, ram_by_name


def get_top_processes(count: int = 3) -> list:
    """The busiest apps right now: [{'name', 'cpu', 'mb'}], CPU first."""
    cpu_by_name, ram_by_name = sample_process_usage()
    names = [n for n in cpu_by_name if n not in _IGNORED_PROCESSES]
    names.sort(key=lambda n: (cpu_by_name[n], ram_by_name.get(n, 0.0)), reverse=True)
    return [{"name": n, "cpu": cpu_by_name[n], "mb": ram_by_name.get(n, 0.0)} for n in names[:count]]


# Heartbeats fire every HEARTBEAT_INTERVAL_MS (20s by default). Scanning
# memory_info() for every process on every single tick was the main source
# of this app's own memory/handle churn, so only 1 in HEARTBEAT_FULL_SCAN_EVERY
# heartbeats does the heavier RAM-included scan; the rest do the cheap
# CPU-only version and keep whatever RAM figure is already stored. This makes
# the "top RAM user" attached to a crash accurate to within a couple of
# minutes rather than the exact last heartbeat - an acceptable trade-off
# since it is only ever shown as a rough clue, not an exact reading.
HEARTBEAT_FULL_SCAN_EVERY = 6  # every ~2 minutes at the default interval
_heartbeat_tick = {"count": 0}


def write_heartbeat(boot_id: int, cpu: float, ram: float):
    _heartbeat_tick["count"] += 1
    light = (_heartbeat_tick["count"] % HEARTBEAT_FULL_SCAN_EVERY) != 0
    cpu_by_name, ram_by_name = sample_process_usage(light=light)
    top_cpu = max(((n, v) for n, v in cpu_by_name.items() if n not in _IGNORED_PROCESSES),
                  key=lambda kv: kv[1], default=("", 0.0))
    top_ram = max(((n, v) for n, v in ram_by_name.items() if n not in _IGNORED_PROCESSES),
                  key=lambda kv: kv[1], default=("", 0.0))
    conn = _open_db_connection()
    try:
        if light:
            conn.execute(
                "INSERT INTO heartbeat (boot_time, last_ts, cpu, ram, top_cpu_name, top_cpu_pct, top_ram_name, top_ram_mb) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(boot_time) DO UPDATE SET last_ts=excluded.last_ts, cpu=excluded.cpu, ram=excluded.ram, "
                "top_cpu_name=excluded.top_cpu_name, top_cpu_pct=excluded.top_cpu_pct",
                (boot_id, datetime.now().isoformat(timespec="seconds"), cpu, ram,
                 top_cpu[0], top_cpu[1], "", 0.0),
            )
        else:
            conn.execute(
                "INSERT INTO heartbeat (boot_time, last_ts, cpu, ram, top_cpu_name, top_cpu_pct, top_ram_name, top_ram_mb) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(boot_time) DO UPDATE SET last_ts=excluded.last_ts, cpu=excluded.cpu, ram=excluded.ram, "
                "top_cpu_name=excluded.top_cpu_name, top_cpu_pct=excluded.top_cpu_pct, "
                "top_ram_name=excluded.top_ram_name, top_ram_mb=excluded.top_ram_mb",
                (boot_id, datetime.now().isoformat(timespec="seconds"), cpu, ram,
                 top_cpu[0], top_cpu[1], top_ram[0], top_ram[1]),
            )
        conn.execute(
            "DELETE FROM heartbeat WHERE boot_time NOT IN "
            "(SELECT boot_time FROM heartbeat ORDER BY boot_time DESC LIMIT ?)",
            (HEARTBEAT_KEEP_BOOTS,),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Health Check: one 0-100 score from things that can be measured locally
# ============================================================================

def get_fixed_drive_usage() -> list:
    drives = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return drives
    for part in partitions:
        opts = (part.opts or "").lower()
        if os.name == "nt" and "fixed" not in opts:
            continue  # skip CD drives, card readers, USB sticks
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        if usage.total:
            drives.append({
                "mount": part.mountpoint,
                "total": usage.total,
                "free": usage.free,
                "percent_free": usage.free / usage.total * 100.0,
            })
    return drives


def count_startup_entries() -> int:
    """Programs registered to start with Windows (Run keys + Startup folders),
    not counting this app itself."""
    total = 0
    if winreg:
        views = (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0))
        targets = (
            (winreg.HKEY_CURRENT_USER, 0),
            (winreg.HKEY_LOCAL_MACHINE, views[1]),
            (winreg.HKEY_LOCAL_MACHINE, views[2]),
        )
        for hive, view in targets:
            try:
                with winreg.OpenKey(hive, STARTUP_REG_PATH, 0, winreg.KEY_READ | view) as key:
                    index = 0
                    while True:
                        try:
                            name, _value, _kind = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        index += 1
                        if name != APP_NAME:
                            total += 1
            except OSError:
                continue
    for env_name in ("APPDATA", "PROGRAMDATA"):
        base = os.getenv(env_name)
        if not base:
            continue
        folder = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        try:
            total += sum(1 for p in folder.iterdir() if p.name.lower() != "desktop.ini")
        except OSError:
            continue
    return total


def is_reboot_pending() -> bool:
    """True if Windows says it is waiting for a restart (updates/installs)."""
    if not winreg:
        return False
    for path in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path):
                return True
        except OSError:
            continue
    return False


def health_grade(score: int) -> str:
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 50:
        return "Needs attention"
    return "Poor"


def health_color(score: int) -> str:
    if score >= 90:
        return "#68d391"
    if score >= 75:
        return "#4fd1c5"
    if score >= 50:
        return "#f6ad55"
    return "#fc8181"


def _age_weight(logged_at: str, now: datetime) -> float:
    """Problems from the last week count fully, older ones (up to 30 days) half."""
    try:
        age_days = (now - datetime.fromisoformat(logged_at)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 0.5
    return 1.0 if age_days <= 7 else 0.5


def gather_health_inputs(monitor=None) -> dict:
    ram_avg = None
    if monitor is not None and len(monitor.ram_history) >= ANOMALY_MIN_SAMPLES:
        ram_avg = statistics.mean(monitor.ram_history)
    return {
        "now": datetime.now(),
        "events": get_reliability_events(days=RELIABILITY_LOOKBACK_DAYS, limit=1000, include_dismissed=False),
        "uptime_days": (time.time() - psutil.boot_time()) / 86400,
        "drives": get_fixed_drive_usage(),
        "ram_avg": ram_avg,
        "startup_count": count_startup_entries(),
        "reboot_pending": is_reboot_pending(),
        "battery": get_latest_battery_snapshot(),
    }


def evaluate_health(inputs: dict) -> dict:
    """Pure function: inputs in, {'score', 'grade', 'findings', ...} out.
    Every finding carries the points it cost, so the score is explainable."""
    now = inputs.get("now") or datetime.now()
    events = inputs.get("events") or []
    findings = []

    def add(severity, title, detail="", penalty=0, tab=None):
        findings.append({"severity": severity, "title": title, "detail": detail,
                         "penalty": int(round(penalty)), "tab": tab})

    def of_kind(*kinds):
        return [e for e in events if e["kind"] in kinds]

    def weight(*kinds):
        return sum(_age_weight(e["logged_at"], now) for e in of_kind(*kinds))

    # ---- restarts and blue screens ----
    bsod, forced, unexpected = of_kind("bsod"), of_kind("forced_poweroff"), of_kind("unexpected_shutdown")
    if bsod or forced or unexpected:
        penalty = min(45, 15 * weight("bsod") + 10 * weight("forced_poweroff") + 8 * weight("unexpected_shutdown"))
        parts = []
        if bsod:
            parts.append(_plural(len(bsod), "blue screen"))
        if forced:
            parts.append(_plural(len(forced), "forced power-off"))
        if unexpected:
            parts.append(_plural(len(unexpected), "unexpected shutdown"))
        latest = max(bsod + forced + unexpected, key=lambda e: e["logged_at"])
        recent = any(_age_weight(e["logged_at"], now) == 1.0 for e in bsod + forced + unexpected)
        add("bad" if recent else "warn",
            f"Unexpected restarts in the last {RELIABILITY_LOOKBACK_DAYS} days: " + ", ".join(parts),
            f"Most recent: {format_event_time(latest['logged_at'])} - {latest['title']}. Update graphics, chipset "
            "and network drivers; if it keeps happening run the Windows Memory Diagnostic and check the power supply.",
            penalty, "reliability")
    else:
        add("ok", f"No blue screens or unexpected restarts in the last {RELIABILITY_LOOKBACK_DAYS} days")

    # ---- hardware and disk errors ----
    hardware, disk = of_kind("hardware_error"), of_kind("disk_error")
    if hardware:
        add("bad", f"Windows logged {_plural(len(hardware), 'hardware error')}",
            "These come from the CPU, RAM, motherboard or power supply. Run the Windows Memory Diagnostic and "
            "update the BIOS/firmware.", min(25, 10 * weight("hardware_error")), "reliability")
    if disk:
        add("bad", f"Windows logged {_plural(len(disk), 'disk error')}",
            "Back up important files now, then check the drive ('chkdsk /scan') and its cable or SSD tool.",
            min(24, 8 * weight("disk_error")), "reliability")
    if not hardware and not disk:
        add("ok", "No hardware or disk errors logged")

    gpu = of_kind("gpu_reset")
    if gpu:
        add("warn", f"The graphics driver reset {_plural(len(gpu), 'time')}",
            "Update the graphics driver. If it keeps happening, check GPU stability and the power supply.",
            min(12, 4 * weight("gpu_reset")), "reliability")

    # ---- app crashes / hangs (last 7 days) ----
    recent_apps = [e for e in of_kind("app_crash", "app_hang") if _age_weight(e["logged_at"], now) == 1.0]
    if recent_apps:
        counts = {}
        for e in recent_apps:
            counts[e["process_name"]] = counts.get(e["process_name"], 0) + 1
        worst, worst_count = max(counts.items(), key=lambda kv: kv[1])
        add("warn" if len(recent_apps) >= 5 else "info",
            f"{_plural(len(recent_apps), 'app crash/hang event')} in the last 7 days",
            f"Most frequent: {friendly_app_name(worst)} ({worst_count}x). Update or reinstall it if it keeps failing.",
            min(10, (len(recent_apps) + 2) // 3), "reliability")
    else:
        add("ok", "No app crashes or hangs in the last 7 days")

    # ---- uptime ----
    days = inputs.get("uptime_days")
    if days is not None:
        if days >= 30:
            add("warn", f"The PC has run {days:.0f} days without a restart",
                "Restart it (choose Restart, not Shut down - Fast Startup keeps the old session alive).", 8)
        elif days >= 14:
            add("info", f"The PC has run {days:.0f} days without a restart",
                "A restart clears memory leaks and applies pending updates.", 4)
        else:
            add("ok", f"Uptime is {format_duration(days * 86400)}")

    # ---- disk space ----
    drives = [d for d in (inputs.get("drives") or []) if d.get("total")]
    if drives:
        def drive_penalty(d):
            free_gb = d["free"] / (1024 ** 3)
            if d["percent_free"] < 5 or free_gb < 2:
                return 15
            if d["percent_free"] < 10:
                return 8
            if d["percent_free"] < 15:
                return 3
            return 0

        low = sorted((d for d in drives if drive_penalty(d)), key=lambda d: d["percent_free"])
        if low:
            for index, d in enumerate(low):
                penalty = drive_penalty(d)
                add("bad" if penalty >= 8 else "warn",
                    f"{d['mount']} is almost full: {d['percent_free']:.0f}% free ({d['free'] / 1024 ** 3:.1f} GB)",
                    "Free up space (Settings > System > Storage) - a full system drive slows Windows and updates.",
                    penalty if index == 0 else 0)
        else:
            lowest = min(drives, key=lambda d: d["percent_free"])
            add("ok", f"Disk space is fine (lowest: {lowest['mount']} {lowest['percent_free']:.0f}% free)")

    # ---- memory pressure ----
    ram_avg = inputs.get("ram_avg")
    if ram_avg is not None:
        if ram_avg >= 90:
            add("bad", f"Memory is under heavy pressure (average {ram_avg:.0f}% over the last minutes)",
                "Close heavy apps or browser tabs; consider more RAM if this is your normal workload.", 8)
        elif ram_avg >= 80:
            add("warn", f"Memory use is high (average {ram_avg:.0f}%)",
                "Closing unused apps and browser tabs will help.", 4)
        else:
            add("ok", f"Memory use is comfortable (average {ram_avg:.0f}%)")

    # ---- startup programs ----
    startup = inputs.get("startup_count")
    if startup is not None:
        if startup > 25:
            add("warn", f"{startup} programs start with Windows",
                "Disable the ones you do not need (Task Manager > Startup apps) for a faster boot.", 6)
        elif startup > 15:
            add("info", f"{startup} programs start with Windows",
                "Disabling unneeded ones (Task Manager > Startup apps) speeds up the boot.", 3)
        else:
            add("ok", f"{startup} programs start with Windows")

    if inputs.get("reboot_pending"):
        add("warn", "Windows is waiting for a restart",
            "An update or installation needs a restart to finish.", 3)

    # ---- battery ----
    battery = inputs.get("battery")
    if battery and battery.get("design_mwh"):
        health = battery_health_percent(battery["design_mwh"], battery["full_mwh"])
        wear = 100 - health
        if wear >= 40:
            add("bad", f"The battery has lost {wear:.0f}% of its capacity",
                "Runtime is much shorter than new; consider replacing the battery.", 8, "battery")
        elif wear >= 25:
            add("warn", f"The battery has lost {wear:.0f}% of its capacity",
                "It is noticeably worn.", 4, "battery")
        elif wear >= 15:
            add("info", f"The battery has lost {wear:.0f}% of its capacity", "Normal wear for an older battery.", 1, "battery")
        else:
            add("ok", f"The battery holds {health:.0f}% of its original capacity", tab="battery")

    rank = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    findings.sort(key=lambda f: rank[f["severity"]])
    score = max(0, 100 - sum(f["penalty"] for f in findings))
    return {
        "score": score,
        "grade": health_grade(score),
        "findings": findings,
        "issues": sum(1 for f in findings if f["severity"] != "ok"),
        "passed": sum(1 for f in findings if f["severity"] == "ok"),
    }


# ============================================================================
# Battery health: powercfg's battery report -> design vs. real capacity
# ============================================================================

def get_battery_status():
    """Live battery state, or None on a desktop PC."""
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return None
    if battery is None:
        return None
    secs = battery.secsleft
    if battery.power_plugged:
        state = "plugged in" if battery.percent >= 99 else "charging"
    elif secs is not None and secs > 0:
        state = f"on battery, about {format_duration(secs)} left"
    else:
        state = "on battery"
    return {"percent": float(battery.percent), "plugged": battery.power_plugged, "state": state}


def battery_health_percent(design_mwh: float, full_mwh: float) -> float:
    if not design_mwh:
        return 0.0
    return max(0.0, min(100.0, full_mwh / design_mwh * 100.0))


def battery_grade(health: float) -> str:
    if health >= 90:
        return "Excellent"
    if health >= 80:
        return "Good"
    if health >= 60:
        return "Worn"
    return "Poor - consider replacing"


def parse_battery_report(xml_bytes: bytes) -> dict:
    """Reads the design / full-charge capacity out of `powercfg /batteryreport
    /xml`. Namespace-agnostic on purpose so small format differences between
    Windows versions do not break it."""
    root = ET.fromstring(xml_bytes)

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    design = full = cycles = found = 0
    label = ""
    for el in root.iter():
        if local(el.tag) != "Battery":
            continue
        fields = {local(child.tag): (child.text or "").strip() for child in el}
        battery_design = _to_int(fields.get("DesignCapacity", 0))
        if battery_design <= 0:
            continue
        found += 1
        design += battery_design
        full += _to_int(fields.get("FullChargeCapacity", 0))
        cycles = max(cycles, _to_int(fields.get("CycleCount", 0)))
        if not label:
            label = " ".join(x for x in (fields.get("Manufacturer", ""), fields.get("Id", "")) if x)
    if not found:
        return {"ok": False, "error": "The report contains no battery capacity data."}
    return {"ok": True, "design_mwh": design, "full_mwh": full, "cycles": cycles, "label": label}


def save_battery_snapshot(design_mwh: int, full_mwh: int, cycles: int, label: str = ""):
    conn = _open_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO battery_health (day, design_mwh, full_mwh, cycles, label) VALUES (?,?,?,?,?)",
            (date.today().isoformat(), int(design_mwh), int(full_mwh), int(cycles), label),
        )
        conn.commit()
    finally:
        conn.close()


def get_battery_snapshots(limit: int = 120) -> list:
    """Oldest first, so it can be drawn as a trend."""
    conn = _open_db_connection()
    try:
        rows = conn.execute(
            "SELECT day, design_mwh, full_mwh, cycles, label FROM battery_health ORDER BY day DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    keys = ("day", "design_mwh", "full_mwh", "cycles", "label")
    return [dict(zip(keys, row)) for row in reversed(rows)]


def get_latest_battery_snapshot():
    snapshots = get_battery_snapshots(limit=1)
    return snapshots[-1] if snapshots else None


def run_battery_report_job() -> dict:
    """Slow (a few seconds) - run off the UI thread."""
    if os.name != "nt":
        return {"ok": False, "error": "Battery reports are only available on Windows."}
    out_path = APP_DATA_DIR / "battery_report.xml"
    try:
        out_path.unlink()
    except OSError:
        pass
    result = run_hidden(["powercfg.exe", "/batteryreport", "/output", str(out_path), "/xml"], timeout=90)
    if result is None or not out_path.exists():
        return {"ok": False, "error": "Windows could not create the battery report (powercfg failed)."}
    try:
        data = parse_battery_report(out_path.read_bytes())
    except (ET.ParseError, OSError) as e:
        return {"ok": False, "error": f"The battery report could not be read ({e})."}
    if data["ok"]:
        save_battery_snapshot(data["design_mwh"], data["full_mwh"], data["cycles"], data["label"])
    return data


# ============================================================================
# Updates: check GitHub releases and (optionally) install the new main.pyw
# ============================================================================

def parse_version(text) -> tuple:
    """'v2.3.1-beta' -> (2, 3, 1). Empty tuple if there are no digits."""
    match = re.search(r"\d+(?:\.\d+)*", str(text or ""))
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def is_newer_version(remote, local) -> bool:
    a, b = parse_version(remote), parse_version(local)
    if not a:
        return False
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def github_repo_from_url(url: str):
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url or "")
    if not match:
        return None
    return match.group(1), match.group(2).removesuffix(".git")


def _http_get(url: str, accept: str, limit: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": f"CPU-Pets/{APP_VERSION}"}
    )
    with urllib.request.urlopen(request, timeout=UPDATE_HTTP_TIMEOUT_S) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The download is larger than expected.")
    return data


def pick_update_asset(assets: list):
    """The file to install: 'main.pyw' if attached, else any other .pyw."""
    usable = [a for a in assets if a.get("browser_download_url") and a.get("name")]
    for asset in usable:
        if asset["name"].lower() == UPDATE_ASSET_NAME:
            return asset
    for asset in usable:
        if asset["name"].lower().endswith(".pyw"):
            return asset
    return None


def extract_sha256(text: str, asset_name: str = "") -> str:
    """Finds a published SHA-256 in release notes / a .sha256 file. Only lines
    that mention 'sha' or the file name count, so random hashes are ignored."""
    for line in (text or "").splitlines():
        match = re.search(r"\b([0-9a-fA-F]{64})\b", line)
        if match and ("sha" in line.lower() or (asset_name and asset_name.lower() in line.lower())):
            return match.group(1).lower()
    return ""


def fetch_latest_release(repo_url: str = APP_REPO_URL) -> dict:
    repo = github_repo_from_url(repo_url)
    if not repo:
        return {"status": "error", "error": "The repository address is not a GitHub URL."}
    api_url = f"https://api.github.com/repos/{repo[0]}/{repo[1]}/releases/latest"
    try:
        payload = json.loads(_http_get(api_url, "application/vnd.github+json", 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "no_release"}
        return {"status": "error", "error": f"GitHub answered with HTTP {e.code}."}
    except Exception as e:
        return {"status": "error", "error": f"Could not reach GitHub ({e.__class__.__name__})."}

    tag = payload.get("tag_name") or ""
    body = payload.get("body") or ""
    assets = payload.get("assets") or []
    asset = pick_update_asset(assets)
    sha256 = ""
    if asset:
        sha256 = extract_sha256(body, asset["name"])
        if not sha256:
            for other in assets:
                if other.get("name", "").lower() == asset["name"].lower() + ".sha256" and other.get("browser_download_url"):
                    try:
                        sha256 = extract_sha256(
                            _http_get(other["browser_download_url"], "application/octet-stream", 4096)
                            .decode("utf-8", errors="replace"),
                            asset["name"],
                        ) or re.sub(r"[^0-9a-fA-F]", "", _http_get(
                            other["browser_download_url"], "application/octet-stream", 4096
                        ).decode("utf-8", errors="replace"))[:64].lower()
                    except Exception:
                        sha256 = ""
                    break
    return {
        "status": "ok",
        "version": tag.lstrip("vV"),
        "tag": tag,
        "html_url": payload.get("html_url") or repo_url,
        "notes": body.strip()[:4000],
        "asset_name": asset["name"] if asset else "",
        "asset_url": asset["browser_download_url"] if asset else "",
        "sha256": sha256 if len(sha256) == 64 else "",
    }


def check_for_update() -> dict:
    """{'status': 'available' | 'up_to_date' | 'no_release' | 'error', ...}"""
    info = fetch_latest_release()
    if info.get("status") != "ok":
        return info
    info["status"] = "available" if is_newer_version(info["version"], APP_VERSION) else "up_to_date"
    return info


def self_update_blocker() -> str:
    """Empty string if this copy of the app can replace its own file."""
    if getattr(sys, "frozen", False):
        return "This build is a packaged program and cannot replace itself."
    if SCRIPT_PATH.suffix.lower() not in (".py", ".pyw"):
        return "The app is not running from a script file."
    if not (os.access(SCRIPT_PATH, os.W_OK) and os.access(SCRIPT_PATH.parent, os.W_OK)):
        return "The program folder is read-only."
    return ""


def update_install_blocker(info: dict) -> str:
    reason = self_update_blocker()
    if reason:
        return reason
    if not info.get("asset_url"):
        return f"This release has no {UPDATE_ASSET_NAME} file attached."
    return ""


def verify_update_payload(data: bytes, expected_version: str, sha256: str = "") -> tuple:
    """Refuses anything that is not a valid, matching copy of this app."""
    if len(data) < 1000:
        return False, "The downloaded file is too small to be the app."
    if sha256 and hashlib.sha256(data).hexdigest().lower() != sha256.lower():
        return False, "Checksum mismatch - the download was not installed."
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False, "The downloaded file is not valid text."
    try:
        compile(text, UPDATE_ASSET_NAME, "exec")
    except SyntaxError as e:
        return False, f"The downloaded file is not valid Python ({e.msg}, line {e.lineno})."
    if f'APP_NAME = "{APP_NAME}"' not in text:
        return False, f"The downloaded file does not look like {APP_NAME}."
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match or parse_version(match.group(1)) != parse_version(expected_version):
        return False, "The version inside the file does not match the release, so it was not installed."
    return True, ""


def install_update_file(script_path, data: bytes) -> Path:
    """Backs up the current file (main.pyw.bak) and swaps the new one in with
    an atomic rename. Returns the backup path."""
    script_path = Path(script_path)
    backup = script_path.with_name(script_path.name + ".bak")
    shutil.copy2(script_path, backup)
    staging = script_path.with_name(script_path.name + ".new")
    staging.write_bytes(data)
    os.replace(staging, script_path)
    return backup


def perform_update_install(info: dict) -> tuple:
    """Download + verify + install. Returns (ok, message). Off the UI thread."""
    reason = update_install_blocker(info)
    if reason:
        return False, reason
    try:
        data = _http_get(info["asset_url"], "application/octet-stream", UPDATE_MAX_DOWNLOAD_BYTES)
        ok, message = verify_update_payload(data, info["version"], info.get("sha256", ""))
        if not ok:
            return False, message
        backup = install_update_file(SCRIPT_PATH, data)
    except Exception as e:
        log_error("update install", e)
        return False, f"The update could not be installed ({e.__class__.__name__}: {e})."
    return True, f"Updated to {info['version']} (the old version is kept as {backup.name})."


def restart_command() -> list:
    """Command line that starts a fresh copy of the app. --wait-pid makes the
    new copy wait until this one has exited (so the single-instance lock is free)."""
    args = ["--wait-pid", str(os.getpid())]
    if getattr(sys, "frozen", False):
        return [sys.executable] + args
    python = sys.executable
    if os.name == "nt" and python.lower().endswith("python.exe"):
        windowless = python[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(windowless):
            python = windowless
    return [python, str(SCRIPT_PATH)] + args


def spawn_restart() -> bool:
    try:
        subprocess.Popen(
            restart_command(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
            close_fds=True,
        )
        return True
    except Exception as e:
        log_error("restart", e)
        return False


def wait_for_previous_instance(argv, timeout_s: float = 20.0):
    """Used right after an update restart: wait for the old process to exit."""
    if "--wait-pid" not in argv:
        return
    try:
        pid = int(argv[argv.index("--wait-pid") + 1])
    except (IndexError, ValueError):
        return
    deadline = time.time() + timeout_s
    while psutil.pid_exists(pid) and time.time() < deadline:
        time.sleep(0.2)



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
        self.last_cpu = 0.0
        self.last_ram = 0.0
        self.boot_id = int(psutil.boot_time())

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sample)
        self._timer.start(HISTORY_POLL_MS)

        # Heartbeat: a lightweight "still alive, here's what was busy" record
        # used to reconstruct what the PC was doing right before a crash.
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._write_heartbeat)
        self._heartbeat_timer.start(HEARTBEAT_INTERVAL_MS)
        QTimer.singleShot(3000, self._write_heartbeat)  # first one shortly after startup

    def _sample(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        self.last_cpu, self.last_ram = cpu, ram
        self.cpu_history.append(cpu)
        self.ram_history.append(ram)

        self.sampled.emit({
            "cpu": cpu,
            "ram": ram,
            "cpu_history": list(self.cpu_history),
            "ram_history": list(self.ram_history),
        })

    def _write_heartbeat(self):
        # Sampling every process's CPU/RAM is too slow for the UI thread on
        # a busy machine, so it runs in the background; self.last_cpu/ram are
        # already cheap and up to date from the regular 1s sampler.
        task = BackgroundTask(self._write_heartbeat_job, self)
        task.done.connect(lambda _result, t=task: None)
        task.start()

    def _write_heartbeat_job(self):
        write_heartbeat(self.boot_id, self.last_cpu, self.last_ram)

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
#FramelessWindow {
    border: 1px solid #2a2a2a;
}
#TitleBar {
    background-color: #1a1a1a;
    border-bottom: 1px solid #2a2a2a;
}
#TitleBarLabel {
    color: #4fd1c5;
    font-weight: 600;
    font-size: 9pt;
}
#TitleBarButton {
    background: transparent;
    border: none;
    border-radius: 0px;
    color: #cccccc;
    font-size: 10pt;
    padding: 0px;
}
#TitleBarButton:hover {
    background-color: #2a2a2a;
}
#TitleBarCloseButton {
    background: transparent;
    border: none;
    border-radius: 0px;
    color: #cccccc;
    font-size: 10pt;
    padding: 0px;
}
#TitleBarCloseButton:hover {
    background-color: #e53e3e;
    color: #ffffff;
}
#FramelessBody {
    background-color: #121212;
}
"""


# ============================================================================
# Custom title bar: every secondary window in this app is frameless and
# draws its own caption bar in the same dark theme, so Windows never shows
# its default white title bar / min / close buttons. Dragging the bar moves
# the window; the close button always calls the window's own close()/
# reject() so existing closeEvent()/hide-instead-of-quit logic still runs.
# ============================================================================

TITLE_BAR_HEIGHT = 32


class TitleBar(QWidget):
    def __init__(self, title: str, window, show_minimize: bool = True, on_close=None):
        super().__init__(window)
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self._window = window
        self._drag_offset = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(0)

        self.label = QLabel(title)
        self.label.setObjectName("TitleBarLabel")
        layout.addWidget(self.label)
        layout.addStretch()

        if show_minimize:
            self.min_btn = QPushButton("—")
            self.min_btn.setObjectName("TitleBarButton")
            self.min_btn.setFixedSize(40, TITLE_BAR_HEIGHT)
            self.min_btn.setCursor(Qt.ArrowCursor)
            self.min_btn.clicked.connect(window.showMinimized)
            layout.addWidget(self.min_btn)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("TitleBarCloseButton")
        self.close_btn.setFixedSize(40, TITLE_BAR_HEIGHT)
        self.close_btn.setCursor(Qt.ArrowCursor)
        self.close_btn.clicked.connect(on_close or window.close)
        layout.addWidget(self.close_btn)

    def setTitle(self, title: str):
        self.label.setText(title)

    # ---------- Drag-to-move ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self._window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPos() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None


class FramelessWindow(QWidget):
    """Base class for the app's secondary top-level windows (Productivity
    Analytics, Weekly Report, Health Check, etc). Subclasses build their
    UI into self.body instead of self, and use self.set_content_fixed_size()
    in place of setFixedSize() so the extra title-bar height is accounted
    for automatically."""

    def __init__(self, title: str, show_minimize: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setObjectName("FramelessWindow")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self.title_bar = TitleBar(title, self, show_minimize=show_minimize)
        outer.addWidget(self.title_bar)

        self.body = QWidget()
        self.body.setObjectName("FramelessBody")
        outer.addWidget(self.body, stretch=1)

    def set_content_fixed_size(self, width: int, height: int):
        self.setFixedSize(width, height + TITLE_BAR_HEIGHT)


class FramelessDialog(QDialog):
    """Base class for the app's modal dialogs (Custom Alert, Reminder,
    Update, About). Same idea as FramelessWindow, but the close button
    calls reject() by default, matching QDialog's usual close behaviour,
    and there is no minimize button since these are modal."""

    def __init__(self, title: str, parent=None, show_minimize: bool = False):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setObjectName("FramelessWindow")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        self.title_bar = TitleBar(title, self, show_minimize=show_minimize, on_close=self.reject)
        outer.addWidget(self.title_bar)

        self.body = QWidget()
        self.body.setObjectName("FramelessBody")
        outer.addWidget(self.body, stretch=1)


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

class ScreenTimeWindow(FramelessWindow):
    """Opened on demand from the pet's tray menu or a left click on the
    tray icon. Owns its own tracking thread so it keeps recording usage
    even while hidden. A date picker lets you look back at any past day
    stored in the local database."""

    def __init__(self, monitor: "SystemMonitor"):
        super().__init__("Productivity Analytics - CPU Pets")
        self.monitor = monitor
        self.set_content_fixed_size(320, 480)

        layout = QVBoxLayout(self.body)
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

class WeeklyReportWindow(FramelessWindow):
    """Shows this week's usage aggregated and compared against last week.
    Opened from the tray menu, or automatically pointed to via a tray
    notification once a week."""

    def __init__(self):
        super().__init__("Weekly Report - CPU Pets")
        self.set_content_fixed_size(340, 620)

        layout = QVBoxLayout(self.body)
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

class AppHealthWindow(FramelessWindow):
    """Shows, per app, how many times it hung / had to be force-closed over
    the last WEEKLY_REPORT_DAYS days, plus a 'Most unstable apps' ranking."""

    def __init__(self):
        super().__init__("Application Health - CPU Pets")
        self.set_content_fixed_size(340, 480)

        layout = QVBoxLayout(self.body)
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

class CustomAlertDialog(FramelessDialog):
    """Add/Edit dialog for a single custom alert."""

    def __init__(self, parent=None, alert: dict = None):
        super().__init__("Custom Alert", parent=parent)
        self.setMinimumWidth(320)
        self._editing = alert is not None
        alert = alert or {}

        form = QFormLayout(self.body)

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


class CustomAlertsWindow(FramelessWindow):
    """Manage the user's custom alerts: add, edit, delete, enable/disable."""

    def __init__(self, get_alerts, set_alerts, parent=None):
        super().__init__("Custom Alerts - CPU Pets", parent=parent)
        self._get_alerts = get_alerts
        self._set_alerts = set_alerts
        self.set_content_fixed_size(360, 420)

        layout = QVBoxLayout(self.body)
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

class ReminderDialog(FramelessDialog):
    def __init__(self, parent=None, reminder: dict = None):
        super().__init__("Reminder", parent=parent)
        self.setMinimumWidth(320)
        reminder = reminder or {}

        form = QFormLayout(self.body)

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


class RemindersWindow(FramelessWindow):
    """Add / edit / delete / repeat reminders for any date & time."""

    def __init__(self, parent=None):
        super().__init__("Reminders - CPU Pets", parent=parent)
        self.set_content_fixed_size(360, 420)

        layout = QVBoxLayout(self.body)
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
# App category editor: lets the user decide which category each app belongs
# to (including brand-new custom categories). Only the *differences* from the
# built-in mapping are saved, and because categories are resolved when the
# reports are drawn, a change re-labels past usage too.
# ============================================================================

class CategoryEditorWindow(FramelessWindow):
    """Assign apps to categories. Opened from Settings in the tray menu."""

    def __init__(self, on_change, parent=None):
        super().__init__("App Categories - CPU Pets", parent=parent)
        self._on_change = on_change  # called after every change (save + refresh)
        self.set_content_fixed_size(380, 540)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("App Categories")
        title.setObjectName("Title")
        layout.addWidget(title)

        hint = QLabel(
            "Select one or more apps, pick a category and press Assign. "
            "Your choices override the built-in ones and also apply to past usage."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search apps...")
        self.search_edit.textChanged.connect(lambda _text: self._refresh())
        self.only_other_check = QCheckBox("Only 'Other'")
        self.only_other_check.toggled.connect(lambda _checked: self._refresh())
        filter_row.addWidget(self.search_edit, stretch=1)
        filter_row.addWidget(self.only_other_check)
        layout.addLayout(filter_row)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        layout.addWidget(self.list_widget, stretch=1)

        assign_row = QHBoxLayout()
        self.category_combo = QComboBox()
        assign_btn = QPushButton("Assign")
        assign_btn.clicked.connect(self._on_assign_clicked)
        assign_row.addWidget(self.category_combo, stretch=1)
        assign_row.addWidget(assign_btn)
        layout.addLayout(assign_row)

        extra_row = QHBoxLayout()
        new_category_btn = QPushButton("New Category...")
        new_category_btn.clicked.connect(self._on_new_category_clicked)
        add_app_btn = QPushButton("Add App...")
        add_app_btn.clicked.connect(self._on_add_app_clicked)
        reset_btn = QPushButton("Reset to Default")
        reset_btn.clicked.connect(self._on_reset_clicked)
        extra_row.addWidget(new_category_btn)
        extra_row.addWidget(add_app_btn)
        extra_row.addWidget(reset_btn)
        layout.addLayout(extra_row)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()

    # ---------- Helpers ----------
    def _selected_processes(self) -> list:
        return [item.data(Qt.UserRole) for item in self.list_widget.selectedItems()]

    def _refresh_category_combo(self):
        current = self.category_combo.currentText()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItems(get_all_category_names())
        index = self.category_combo.findText(current)
        if index >= 0:
            self.category_combo.setCurrentIndex(index)
        self.category_combo.blockSignals(False)

    def _refresh(self, reselect=None):
        if reselect is None:
            reselect = set(self._selected_processes())

        usage = get_all_time_usage()
        processes = set(usage) | set(USER_CATEGORY_OVERRIDES)
        needle = self.search_edit.text().strip().lower()
        only_other = self.only_other_check.isChecked()

        self.list_widget.clear()
        for process_name in sorted(processes, key=lambda p: (-usage.get(p, 0), p)):
            category = categorize_app(process_name)
            friendly = friendly_app_name(process_name)
            if only_other and category != DEFAULT_CATEGORY:
                continue
            if needle and needle not in friendly.lower() and needle not in process_name:
                continue

            text = f"{friendly}   \u2192   {category}"
            if process_name in USER_CATEGORY_OVERRIDES:
                text += "   (custom)"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, process_name)
            seconds = usage.get(process_name, 0)
            tip = process_name
            if seconds:
                tip += f"\nTotal tracked: {format_duration(seconds)}"
            item.setToolTip(tip)
            self.list_widget.addItem(item)
            if process_name in reselect:
                item.setSelected(True)

        self._refresh_category_combo()

    def _commit(self, overrides: dict, reselect=None):
        set_user_category_overrides(overrides)
        self._on_change()
        self._refresh(reselect=reselect)

    def _assign(self, processes, category: str):
        overrides = dict(USER_CATEGORY_OVERRIDES)
        for process_name in processes:
            if category == default_category_for(process_name):
                # Same as the built-in choice, so no override needs storing.
                overrides.pop(process_name, None)
            else:
                overrides[process_name] = category
        self._commit(overrides, reselect=set(processes))

    def _need_selection(self, message: str) -> list:
        processes = self._selected_processes()
        if not processes:
            QMessageBox.information(self, APP_NAME, message)
        return processes

    # ---------- Button handlers ----------
    def _on_assign_clicked(self, _checked=False):
        processes = self._need_selection("Select one or more apps first.")
        if processes:
            self._assign(processes, self.category_combo.currentText())

    def _on_new_category_clicked(self, _checked=False):
        processes = self._need_selection(
            "Select one or more apps first - they will be moved into the new category."
        )
        if not processes:
            return
        text, ok = QInputDialog.getText(self, "New Category", "Category name:")
        if not ok:
            return
        name = normalize_category_name(text)
        if not name:
            return
        self._assign(processes, name)
        index = self.category_combo.findText(name)
        if index >= 0:
            self.category_combo.setCurrentIndex(index)

    def _on_add_app_clicked(self, _checked=False):
        """For apps that have not been tracked yet (e.g. a game you have not
        played since installing this)."""
        category = self.category_combo.currentText()
        text, ok = QInputDialog.getText(
            self, "Add App",
            f"Process name (for example game.exe).\nIt will be added to: {category}",
        )
        if not ok:
            return
        process_name = normalize_process_name(text)
        if not process_name:
            return
        if (process_name not in USER_CATEGORY_OVERRIDES
                and default_category_for(process_name) == category):
            QMessageBox.information(
                self, APP_NAME, f"{process_name} is already in {category} by default."
            )
            return
        self._assign([process_name], category)

    def _on_reset_clicked(self, _checked=False):
        processes = [p for p in self._selected_processes() if p in USER_CATEGORY_OVERRIDES]
        if not processes:
            QMessageBox.information(
                self, APP_NAME, "Select one or more apps marked (custom) to reset them."
            )
            return
        remaining = {k: v for k, v in USER_CATEGORY_OVERRIDES.items() if k not in processes}
        self._commit(remaining, reselect=set(processes))


# ============================================================================
# Health Check window: one score, explained by a list of findings, each
# tagged with which tab (Reliability / Battery) has more detail.
# ============================================================================

class HealthCheckWindow(FramelessWindow):
    """Runs evaluate_health() on demand and shows the score plus every
    finding that fed into it. The reliability scan and battery report that
    feed this run in the background on their own timers; this window just
    reads whatever has been gathered so far and can also nudge a refresh."""

    def __init__(self, monitor: "SystemMonitor", open_reliability, open_battery):
        super().__init__("Health Check - CPU Pets")
        self._monitor = monitor
        self._open_reliability = open_reliability
        self._open_battery = open_battery
        self._job = None

        self.set_content_fixed_size(380, 520)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Health Check")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        self.reset_btn = QPushButton("Reset...")
        self.reset_btn.setToolTip("Clear old crash/restart history that is no longer relevant")
        self.reset_btn.clicked.connect(self._show_reset_menu)
        header.addWidget(self.reset_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        score_row = QHBoxLayout()
        self.score_label = QLabel("--")
        self.score_label.setStyleSheet("font-size: 32pt; font-weight: bold;")
        score_row.addWidget(self.score_label)
        score_col = QVBoxLayout()
        self.grade_label = QLabel("Checking...")
        self.grade_label.setObjectName("SectionHeader")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Muted")
        score_col.addWidget(self.grade_label)
        score_col.addWidget(self.summary_label)
        score_row.addLayout(score_col)
        score_row.addStretch()
        layout.addLayout(score_row)

        layout.addSpacing(4)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.findings_holder = QWidget()
        self.findings_layout = QVBoxLayout(self.findings_holder)
        self.findings_layout.setContentsMargins(0, 0, 0, 0)
        self.findings_layout.setSpacing(6)
        self.findings_layout.addStretch()
        self.scroll.setWidget(self.findings_holder)
        layout.addWidget(self.scroll, stretch=1)

    def showEvent(self, event):
        super().showEvent(event)
        if self.score_label.text() == "--":
            self.refresh()

    def refresh(self):
        self.refresh_btn.setEnabled(False)
        self.grade_label.setText("Checking...")
        self._job = BackgroundTask(lambda: evaluate_health(gather_health_inputs(self._monitor)), self)
        self._job.done.connect(self._on_result)
        self._job.start()

    def _on_result(self, result):
        self.refresh_btn.setEnabled(True)
        if not result:
            self.grade_label.setText("Could not run the check")
            return

        self.score_label.setText(str(result["score"]))
        self.score_label.setStyleSheet(f"font-size: 32pt; font-weight: bold; color: {health_color(result['score'])};")
        self.grade_label.setText(result["grade"])
        self.summary_label.setText(f"{result['passed']} passed  \u00b7  {result['issues']} to review")

        while self.findings_layout.count() > 1:
            item = self.findings_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for finding in result["findings"]:
            self.findings_layout.insertWidget(self.findings_layout.count() - 1, self._finding_row(finding))

    def _finding_row(self, finding: dict) -> QWidget:
        icons = {"bad": "\u2717", "warn": "\u26a0", "info": "\u2139", "ok": "\u2713"}
        row = QWidget()
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)

        top = QHBoxLayout()
        mark = QLabel(icons.get(finding["severity"], "\u2022"))
        mark.setStyleSheet(f"color: {SEVERITY_COLORS.get(finding['severity'], '#a0aec0')}; font-weight: bold;")
        mark.setFixedWidth(18)
        top.addWidget(mark)
        text = finding["title"] + (f"  (-{finding['penalty']})" if finding["penalty"] else "")
        title_label = QLabel(text)
        title_label.setWordWrap(True)
        top.addWidget(title_label, stretch=1)
        v.addLayout(top)

        if finding.get("detail"):
            detail = QLabel(finding["detail"])
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            detail.setContentsMargins(24, 0, 0, 0)
            v.addWidget(detail)

        if finding.get("tab") == "reliability":
            link = QPushButton("View crash history \u2192")
            link.setFlat(True)
            link.setStyleSheet("color: #4fd1c5; text-align: left; border: none;")
            link.setContentsMargins(24, 0, 0, 0)
            link.clicked.connect(self._open_reliability)
            v.addWidget(link)
        elif finding.get("tab") == "battery":
            link = QPushButton("View battery health \u2192")
            link.setFlat(True)
            link.setStyleSheet("color: #4fd1c5; text-align: left; border: none;")
            link.clicked.connect(self._open_battery)
            v.addWidget(link)

        return row

    # ---------- Reset ----------
    def _show_reset_menu(self, _checked=False):
        menu = QMenu(self)
        clear_old = menu.addAction("Clear history older than 7 days")
        clear_all = menu.addAction("Clear all crash/restart history")
        menu.addSeparator()
        open_history = menu.addAction("Open full history to review first...")
        chosen = menu.exec_(self.reset_btn.mapToGlobal(self.reset_btn.rect().bottomLeft()))
        if chosen == clear_old:
            self._confirm_and_clear(days=7)
        elif chosen == clear_all:
            self._confirm_and_clear(days=None)
        elif chosen == open_history:
            self._open_reliability()

    def _confirm_and_clear(self, days):
        question = (
            "This removes all recorded crash/restart history and recalculates your score.\n\n"
            "These items are also marked so a future scan will not bring them back, even though "
            "Windows itself keeps the original log entries (Windows only lets you clear an entire "
            "log, not single entries) - any new problem is still detected normally."
            if days is None else
            f"This removes crash/restart history from the last {days} days and recalculates your score.\n\n"
            "These items are also marked so a future scan will not bring them back, even though "
            "Windows itself keeps the original log entries (Windows only lets you clear an entire "
            "log, not single entries) - any new problem is still detected normally."
        )
        confirm = QMessageBox.question(
            self, "Reset Health History", question,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        removed = clear_reliability_events(days=days)
        self.summary_label.setText(f"Cleared {_plural(removed, 'incident')}. Recalculating...")
        self.refresh()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ============================================================================
# Reliability window: real BSOD / hang / unexpected-restart history, pulled
# from the Windows Event Log, with the app's own heartbeat filled in so it
# can say what was running right before the crash.
# ============================================================================

class ReliabilityWindow(FramelessWindow):
    """Lists incidents, newest first, each expandable via tooltip-free detail
    labels (no click-to-expand - simplest to get right and to test)."""

    def __init__(self):
        super().__init__("Crash & Restart History - CPU Pets")
        self._job = None
        self.set_content_fixed_size(420, 520)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Crash & Restart History")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        self.clear_btn = QPushButton("Clear All...")
        self.clear_btn.setToolTip("Delete this history and recalculate the Health Check score")
        self.clear_btn.clicked.connect(self._clear_all)
        header.addWidget(self.clear_btn)
        self.scan_btn = QPushButton("Scan now")
        self.scan_btn.clicked.connect(self.rescan)
        header.addWidget(self.scan_btn)
        layout.addLayout(header)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.list_holder = QWidget()
        self.list_layout = QVBoxLayout(self.list_holder)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch()
        self.scroll.setWidget(self.list_holder)
        layout.addWidget(self.scroll, stretch=1)

        hint = QLabel(
            f"Read from the Windows Event Log (last {RELIABILITY_LOOKBACK_DAYS} days). Blue screens and forced "
            "restarts also show what this app last saw running, when it was open at the time. Dismissed items are "
            "excluded from the Health Check score but stay listed here."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def showEvent(self, event):
        super().showEvent(event)
        self._render(get_reliability_events(days=RELIABILITY_LOOKBACK_DAYS))

    def _clear_all(self, _checked=False):
        confirm = QMessageBox.question(
            self, "Clear Crash & Restart History",
            "This deletes all recorded incidents and recalculates your Health Check score.\n\n"
            "They are also marked so a future scan will not bring them back, even though Windows "
            "itself keeps the original log entries (Windows only lets you clear an entire log, not "
            "single entries) - any new problem is still detected normally.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        clear_reliability_events(days=None)
        self._render([])

    def _toggle_dismiss(self, event_key: str, dismissed: bool):
        set_reliability_events_dismissed([event_key], dismissed)
        self._render(get_reliability_events(days=RELIABILITY_LOOKBACK_DAYS))

    def rescan(self):
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning...")
        self._job = BackgroundTask(scan_reliability_log, self)
        self._job.done.connect(self._on_scanned)
        self._job.start()

    def _on_scanned(self, _new_items):
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("Scan now")
        self._render(get_reliability_events(days=RELIABILITY_LOOKBACK_DAYS))

    def _render(self, events: list):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        counts = {}
        for e in events:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        if not events:
            self.summary_label.setText(f"No incidents in the last {RELIABILITY_LOOKBACK_DAYS} days. Nice and stable!")
        else:
            parts = [f"{_plural(n, KIND_INFO.get(k, (k,))[0].lower())}" for k, n in counts.items()]
            self.summary_label.setText(", ".join(parts).capitalize())

        if not events:
            placeholder = QLabel("Nothing to show.")
            placeholder.setObjectName("Muted")
            self.list_layout.insertWidget(0, placeholder)
            return

        for event in events:
            self.list_layout.insertWidget(self.list_layout.count() - 1, self._event_card(event))

    def _event_card(self, event: dict) -> QWidget:
        label, severity = KIND_INFO.get(event["kind"], (event["kind"], "info"))
        dismissed = bool(event.get("dismissed"))
        border_color = "#4a4a4a" if dismissed else SEVERITY_COLORS[severity]
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setStyleSheet(
            f"QFrame {{ background-color: #1a1a1a; border-left: 3px solid {border_color}; "
            f"border-radius: 3px; {'opacity: 0.6;' if dismissed else ''} }}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(2)

        top = QHBoxLayout()
        tag = QLabel(label + ("  (dismissed)" if dismissed else ""))
        tag.setStyleSheet(f"color: {'#808080' if dismissed else SEVERITY_COLORS[severity]}; font-weight: bold;")
        top.addWidget(tag)
        top.addStretch()
        when = QLabel(format_event_time(event["occurred_at"]))
        when.setObjectName("Muted")
        top.addWidget(when)
        v.addLayout(top)

        title = QLabel(event["title"])
        title.setWordWrap(True)
        if dismissed:
            title.setObjectName("Muted")
        v.addWidget(title)

        if event.get("details"):
            detail = QLabel(event["details"])
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            v.addWidget(detail)

        actions = QHBoxLayout()
        actions.addStretch()
        toggle_btn = QPushButton("Restore" if dismissed else "Dismiss")
        toggle_btn.setFlat(True)
        toggle_btn.setStyleSheet("color: #4fd1c5; border: none;")
        toggle_btn.setToolTip(
            "Count this again toward the Health Check score" if dismissed
            else "Exclude this from the Health Check score without deleting it"
        )
        toggle_btn.clicked.connect(lambda _checked=False, key=event["event_key"], d=not dismissed: self._toggle_dismiss(key, d))
        actions.addWidget(toggle_btn)
        v.addLayout(actions)

        return card

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ============================================================================
# Battery health window
# ============================================================================

class BatteryHealthWindow(FramelessWindow):
    """Design vs. current full-charge capacity, from `powercfg
    /batteryreport`, plus the live charge state. Desktops (no battery) get a
    clear message instead of an error."""

    def __init__(self):
        super().__init__("Battery Health - CPU Pets")
        self._job = None
        self.set_content_fixed_size(340, 420)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Battery Health")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch()
        self.run_btn = QPushButton("Run report")
        self.run_btn.clicked.connect(self.run_report)
        header.addWidget(self.run_btn)
        layout.addLayout(header)

        self.status_label = QLabel("No report yet.")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addSpacing(6)

        self.health_label = QLabel("--")
        self.health_label.setStyleSheet("font-size: 28pt; font-weight: bold;")
        layout.addWidget(self.health_label)

        self.grade_label = QLabel("")
        self.grade_label.setObjectName("SectionHeader")
        layout.addWidget(self.grade_label)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("Muted")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        layout.addSpacing(6)
        self.live_label = QLabel("")
        self.live_label.setObjectName("Muted")
        layout.addWidget(self.live_label)

        layout.addStretch()
        hint = QLabel("Capacity data comes from Windows' own battery report and reflects normal wear over time.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def showEvent(self, event):
        super().showEvent(event)
        self._show_live()
        snapshot = get_latest_battery_snapshot()
        if snapshot:
            self._render(snapshot)
        elif get_battery_status() is None:
            self.status_label.setText("No battery was detected - this looks like a desktop PC.")
            self.run_btn.setEnabled(False)
        else:
            self.run_report()

    def _show_live(self):
        live = get_battery_status()
        self.live_label.setText(f"Right now: {live['percent']:.0f}%, {live['state']}" if live else "")

    def run_report(self):
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Running...")
        self.status_label.setText("Asking Windows for the battery report (a few seconds)...")
        self._job = BackgroundTask(run_battery_report_job, self)
        self._job.done.connect(self._on_report)
        self._job.start()

    def _on_report(self, result):
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run report")
        if not result or not result.get("ok"):
            error = (result or {}).get("error", "Unknown error.")
            self.status_label.setText(error)
            return
        self.status_label.setText(f"Report generated {datetime.now().strftime('%H:%M')}.")
        self._render({"design_mwh": result["design_mwh"], "full_mwh": result["full_mwh"],
                      "cycles": result["cycles"], "label": result["label"], "day": date.today().isoformat()})

    def _render(self, snapshot: dict):
        health = battery_health_percent(snapshot["design_mwh"], snapshot["full_mwh"])
        self.health_label.setText(f"{health:.0f}%")
        color = "#68d391" if health >= 80 else "#f6ad55" if health >= 60 else "#fc8181"
        self.health_label.setStyleSheet(f"font-size: 28pt; font-weight: bold; color: {color};")
        self.grade_label.setText(battery_grade(health) + " capacity remaining")
        self.bar.setValue(int(round(health)))
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")

        lines = [f"Design capacity: {snapshot['design_mwh']:,} mWh",
                 f"Current full charge: {snapshot['full_mwh']:,} mWh"]
        if snapshot.get("cycles"):
            lines.append(f"Cycle count: {snapshot['cycles']:,}")
        if snapshot.get("label"):
            lines.append(snapshot["label"])
        if snapshot.get("day"):
            lines.append(f"Report date: {snapshot['day']}")
        self.detail_label.setText("\n".join(lines))
        if not self.status_label.text():
            self.status_label.setText(f"Last report: {snapshot.get('day', '')}")

    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ============================================================================
# Update dialog
# ============================================================================

class UpdateDialog(FramelessDialog):
    """Shows what check_for_update() found and, if a newer main.pyw is
    attached to the release, offers to install it (with a restart)."""

    def __init__(self, info: dict, parent=None):
        super().__init__("Check for Updates - CPU Pets", parent=parent)
        self.info = info
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self.body)
        layout.setSpacing(8)

        status = info.get("status")
        if status == "available":
            title = QLabel(f"Version {info['version']} is available")
            title.setObjectName("Title")
            layout.addWidget(title)
            current = QLabel(f"You have {APP_VERSION}.")
            current.setObjectName("Muted")
            layout.addWidget(current)
            if info.get("notes"):
                notes = QLabel(info["notes"][:600])
                notes.setWordWrap(True)
                layout.addWidget(notes)
            self.blocker = update_install_blocker(info)
            if self.blocker:
                warn = QLabel(self.blocker)
                warn.setObjectName("Muted")
                warn.setWordWrap(True)
                layout.addWidget(warn)
        elif status == "up_to_date":
            title = QLabel("You're up to date")
            title.setObjectName("Title")
            layout.addWidget(title)
            layout.addWidget(QLabel(f"Version {APP_VERSION} is the latest release."))
            self.blocker = "up_to_date"
        elif status == "no_release":
            layout.addWidget(QLabel("No releases have been published to this repository yet."))
            self.blocker = "no_release"
        else:
            layout.addWidget(QLabel(info.get("error", "Could not check for updates.")))
            self.blocker = "error"

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Muted")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)

        buttons = QDialogButtonBox()
        self.install_btn = None
        if status == "available" and not self.blocker:
            self.install_btn = buttons.addButton("Install && Restart", QDialogButtonBox.AcceptRole)
            self.install_btn.clicked.connect(self._install)
        open_btn = buttons.addButton("Open release page", QDialogButtonBox.ActionRole)
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info.get("html_url", APP_REPO_URL))))
        buttons.addButton(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._job = None

    def _install(self):
        self.install_btn.setEnabled(False)
        self.progress_label.setText("Downloading and verifying...")
        self._job = BackgroundTask(lambda: perform_update_install(self.info), self)
        self._job.done.connect(self._on_installed)
        self._job.start()

    def _on_installed(self, result):
        ok, message = result if result else (False, "The update failed unexpectedly.")
        self.progress_label.setText(message)
        if ok:
            self.install_btn.setText("Restarting...")
            QTimer.singleShot(800, self._restart_now)
        else:
            self.install_btn.setEnabled(True)

    def _restart_now(self):
        if spawn_restart():
            QApplication.instance().quit()
        else:
            self.progress_label.setText(self.progress_label.text() + "\nCould not restart automatically - please reopen the app.")


# ============================================================================
# Quick flyout: a compact panel for the left-click on the tray icon, showing
# live stats and one-click access to the heavier windows.
# ============================================================================

class FlyoutWindow(QWidget):
    """A borderless popup anchored above the tray icon, closed by clicking
    anywhere outside it. Kept intentionally simple: live numbers plus
    shortcut buttons, no editing here."""

    def __init__(self, tray: "CpuPetTray"):
        super().__init__()
        self.tray = tray
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setFixedWidth(260)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("FlyoutCard")
        card.setStyleSheet(
            "#FlyoutCard { background-color: #1a1a1a; border: 1px solid #333333; border-radius: 8px; }"
        )
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        layout.addWidget(title)

        stats = QHBoxLayout()
        self.cpu_label = self._stat_label()
        self.ram_label = self._stat_label()
        stats.addWidget(self.cpu_label)
        stats.addWidget(self.ram_label)
        layout.addLayout(stats)

        self.uptime_label = QLabel("")
        self.uptime_label.setObjectName("Muted")
        layout.addWidget(self.uptime_label)

        top_caption = QLabel("Top processes")
        top_caption.setObjectName("SectionHeader")
        layout.addWidget(top_caption)
        self.top_holder = QVBoxLayout()
        self.top_holder.setSpacing(2)
        layout.addLayout(self.top_holder)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #333333;")
        layout.addWidget(line)

        buttons = QHBoxLayout()
        for text, handler in (
            ("Screen Time", self.tray._show_screen_time),
            ("Health", self.tray._show_health_check),
            ("Report", self.tray._show_weekly_report),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(self._trigger(handler))
            buttons.addWidget(btn)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.setInterval(FLYOUT_REFRESH_MS)

    def _stat_label(self) -> QLabel:
        label = QLabel("--")
        label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        return label

    def _trigger(self, handler):
        def run(_checked=False):
            self.close()
            handler()
        return run

    def _refresh(self):
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        self.cpu_label.setText(f"CPU {cpu:.0f}%")
        self.ram_label.setText(f"RAM {ram:.0f}%")
        self.uptime_label.setText(f"Uptime: {format_duration(time.time() - psutil.boot_time())}")

        while self.top_holder.count():
            item = self.top_holder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for proc in get_top_processes(3):
            if proc["cpu"] < 0.5 and proc["mb"] < 50:
                continue
            row = QLabel(f"{friendly_app_name(proc['name'])} \u2013 {proc['cpu']:.0f}% CPU, {proc['mb']:.0f} MB")
            row.setObjectName("Muted")
            self.top_holder.addWidget(row)
        if self.top_holder.count() == 0:
            placeholder = QLabel("Nothing heavy running right now.")
            placeholder.setObjectName("Muted")
            self.top_holder.addWidget(placeholder)

    def show_at(self, pos):
        self._refresh()
        self.move(pos)
        self.show()
        self.raise_()
        self.activateWindow()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)


# ============================================================================
# About dialog
# ============================================================================

class AboutDialog(FramelessDialog):
    """Simple About box: app name, version, short description, and a link
    to the repo. Opened from the tray menu."""

    def __init__(self, parent=None):
        super().__init__(f"About {APP_NAME}", parent=parent)
        self.setFixedWidth(320)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(6)

        name_label = QLabel(APP_NAME)
        name_label.setObjectName("Title")
        layout.addWidget(name_label)

        version_label = QLabel(f"Version {APP_VERSION}")
        version_label.setObjectName("Muted")
        layout.addWidget(version_label)

        layout.addSpacing(8)

        description = QLabel(
            "A lightweight Windows tray pet whose animation speed reacts to "
            "your CPU usage, bundled with Productivity Analytics, Weekly "
            "Reports, Custom Alerts, Reminders, and Not Responding / app "
            "health tracking."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        layout.addSpacing(8)

        link_label = QLabel(f'<a href="{APP_REPO_URL}" style="color:#4fd1c5;">{APP_REPO_URL}</a>')
        link_label.setOpenExternalLinks(False)
        link_label.linkActivated.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        layout.addWidget(link_label)

        layout.addSpacing(14)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


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
        self.category_editor_window = None
        self.about_dialog = None
        self.health_check_window = None
        self.reliability_window = None
        self.battery_window = None
        self.flyout = None
        self._last_update_info = None

        self.base = Path(__file__).resolve().parent
        self.frames = {animal: {"light": [], "dark": []} for animal in ANIMALS}
        self._load_all_frames_or_fail()

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

        self._reliability_timer = QTimer(self)
        self._reliability_timer.timeout.connect(self._scan_reliability)
        self._reliability_timer.start(RELIABILITY_SCAN_INTERVAL_MS)
        QTimer.singleShot(15000, self._scan_reliability)  # once shortly after startup

        self._battery_timer = QTimer(self)
        self._battery_timer.timeout.connect(self._refresh_battery_report)
        self._battery_timer.start(BATTERY_CHECK_INTERVAL_MS)
        if get_battery_status() is not None and get_latest_battery_snapshot() is None:
            QTimer.singleShot(20000, self._refresh_battery_report)

        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(lambda: self._check_for_updates(silent=True))
        self._update_timer.start(UPDATE_CHECK_INTERVAL_MS)
        QTimer.singleShot(10000, lambda: self._check_for_updates(silent=True))

        self.setVisible(True)
        self._animate_step()

    # ---------- Reliability / battery / update background jobs ----------
    def _scan_reliability(self):
        job = BackgroundTask(scan_reliability_log, self)
        job.done.connect(self._on_reliability_scanned)
        job.start()

    def _on_reliability_scanned(self, new_items):
        if not new_items:
            return
        restarts = [i for i in new_items if i["kind"] in RESTART_KINDS]
        if restarts:
            try:
                self.showMessage(
                    APP_NAME,
                    f"Found {_plural(len(restarts), 'unexpected restart')} in the Windows Event Log. "
                    "Open Health Check for details.",
                    QSystemTrayIcon.Warning,
                    6000,
                )
            except Exception as e:
                self._log_alert_error(e)

    def _refresh_battery_report(self):
        job = BackgroundTask(run_battery_report_job, self)
        job.done.connect(lambda _result: None)
        job.start()

    def _check_for_updates(self, silent: bool = False):
        job = BackgroundTask(check_for_update, self)
        job.done.connect(lambda result: self._on_update_checked(result, silent))
        job.start()

    def _on_update_checked(self, info, silent: bool):
        self._last_update_info = info
        if not info:
            return
        if info.get("status") == "available" and silent:
            try:
                self.showMessage(
                    APP_NAME,
                    f"Version {info['version']} is available (you have {APP_VERSION}). "
                    "Open Settings > Check for Updates to install.",
                    QSystemTrayIcon.Information,
                    6000,
                )
            except Exception as e:
                self._log_alert_error(e)
        elif not silent:
            UpdateDialog(info, parent=None).exec_()

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
                "category_overrides": dict(USER_CATEGORY_OVERRIDES),
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
                set_user_category_overrides(data.get("category_overrides", {}))
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

        # ---- Health Check: important enough to sit at the top level ----
        health_check_action = QAction("Health Check", menu)
        health_check_action.triggered.connect(self._show_health_check)
        menu.addAction(health_check_action)

        menu.addSeparator()

        # ---- Settings ----
        settings_menu = menu.addMenu("Settings")
        self._make_menu_modern(settings_menu)
        self.startup_action = QAction("Run on Startup", settings_menu, checkable=True)
        self.startup_action.setChecked(is_run_on_startup())
        self.startup_action.toggled.connect(self._on_startup_toggled)
        settings_menu.addAction(self.startup_action)

        categories_action = QAction("App Categories...", settings_menu)
        categories_action.triggered.connect(self._show_categories)
        settings_menu.addAction(categories_action)

        reliability_action = QAction("Crash && Restart History...", settings_menu)
        reliability_action.triggered.connect(self._show_reliability)
        settings_menu.addAction(reliability_action)

        battery_action = QAction("Battery Health...", settings_menu)
        battery_action.triggered.connect(self._show_battery)
        settings_menu.addAction(battery_action)

        settings_menu.addSeparator()

        update_action = QAction("Check for Updates...", settings_menu)
        update_action.triggered.connect(lambda: self._check_for_updates(silent=False))
        settings_menu.addAction(update_action)

        menu.addSeparator()

        about_action = QAction("About", menu)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)

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

    def _show_categories(self, _checked=False):
        if self.category_editor_window is None:
            self.category_editor_window = CategoryEditorWindow(
                on_change=self._on_categories_changed
            )
        w = self.category_editor_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _on_categories_changed(self):
        self.save_settings()
        # Categories are resolved at display time, so an open report just
        # needs to redraw to reflect the change.
        if self.weekly_report_window is not None and self.weekly_report_window.isVisible():
            self.weekly_report_window.refresh()

    def _show_health_check(self, _checked=False):
        if self.health_check_window is None:
            self.health_check_window = HealthCheckWindow(
                self.monitor, self._show_reliability, self._show_battery
            )
        w = self.health_check_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _show_reliability(self, _checked=False):
        if self.reliability_window is None:
            self.reliability_window = ReliabilityWindow()
        w = self.reliability_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _show_battery(self, _checked=False):
        if self.battery_window is None:
            self.battery_window = BatteryHealthWindow()
        w = self.battery_window
        w.showNormal()
        w.raise_()
        w.activateWindow()

    def _show_about(self, _checked=False):
        if self.about_dialog is None:
            self.about_dialog = AboutDialog()
        self.about_dialog.show()
        self.about_dialog.raise_()
        self.about_dialog.activateWindow()

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
        # Left click: quick flyout with live stats. Double-click: the full
        # Screen Time window. Right click opens the custom context menu.
        if reason == QSystemTrayIcon.Trigger:
            self._toggle_flyout()
        elif reason == QSystemTrayIcon.DoubleClick:
            if self.flyout is not None:
                self.flyout.close()
            w = self.screen_time_window
            if w.isVisible():
                w.hide()
            else:
                self._show_screen_time()

    def _toggle_flyout(self):
        if self.flyout is not None and self.flyout.isVisible():
            self.flyout.close()
            return
        if self.flyout is None:
            self.flyout = FlyoutWindow(self)
        self.flyout.adjustSize()
        flyout_width, flyout_height = self.flyout.width(), self.flyout.height()
        geometry = self.geometry()
        screen = QApplication.primaryScreen().availableGeometry()
        if geometry.isValid() and geometry.x() > 0:
            x = geometry.x() + geometry.width() // 2 - flyout_width // 2
            y = geometry.y() - flyout_height - 8
            if y < screen.top():  # taskbar on top: drop the flyout below the icon instead
                y = geometry.y() + geometry.height() + 8
        else:
            x = screen.right() - flyout_width - 12
            y = screen.bottom() - flyout_height - 12
        self.flyout.show_at(self._clamp_to_screen(x, y, flyout_width))

    @staticmethod
    def _clamp_to_screen(x, y, width):
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left() + 4, min(x, screen.right() - width - 4))
        y = max(screen.top() + 4, y)
        return QPoint(x, y)

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
    wait_for_previous_instance(sys.argv)  # only does anything right after a self-update restart

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_QSS)

    if not acquire_single_instance():
        QMessageBox.information(
            None, APP_NAME,
            f"{APP_NAME} is already running.\n\n"
            "Look for the pet icon in the system tray "
            "(it may be hidden under the ^ arrow).",
        )
        return

    monitor = SystemMonitor()  # noqa: F841 (kept alive by reference + signal connections)
    screen_time_window = ScreenTimeWindow(monitor)
    tray = CpuPetTray(screen_time_window, app, monitor)  # noqa: F841 (kept alive by reference)

    app.aboutToQuit.connect(screen_time_window.shutdown)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
