import os
import sys
import ctypes
import threading
import time
import json
from pathlib import Path
import psutil
from PIL import Image
import pystray
from pystray import MenuItem as Item, Menu

try:
    import winreg
except ImportError:
    winreg = None  

# ---- Settings ----
TRAY_ICON_SIZE = 32
MIN_DELAY_S = 0.05
MAX_DELAY_S = 0.50
SMOOTHING_ALPHA = 0.3
THEME_POLL_S = 2.0

ANIMALS = ("cat", "parrot", "horse")
DEFAULT_ANIMAL = "cat"
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "CPU Pets"


def get_settings_path():
    if os.name == "nt":  
        appdata = Path(os.getenv("APPDATA")) / "CPU_Pets"
        appdata.mkdir(exist_ok=True)
        return appdata / "settings.json"
    else:
        
        return Path.home() / ".cpu_pets_settings.json"

SETTINGS_FILE = get_settings_path()


def _hide_and_detach_console():
    try:
        GetConsoleWindow = ctypes.windll.kernel32.GetConsoleWindow
        ShowWindow = ctypes.windll.user32.ShowWindow
        FreeConsole = ctypes.windll.kernel32.FreeConsole
        SW_HIDE = 0
        hwnd = GetConsoleWindow()
        if hwnd:
            ShowWindow(hwnd, SW_HIDE)
            FreeConsole()
    except Exception:
        pass

def _get_windows_app_theme():
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(val) == 1 else "dark"
    except Exception:
        return "light"

def is_run_on_startup():
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

def set_run_on_startup(enable=True):
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

class CpuPets:
    def __init__(self):
        self.base = Path(__file__).parent
        self.frames = {animal: {"light": [], "dark": []} for animal in ANIMALS}
        self._load_all_frames_or_fail()

        self._lock = threading.Lock()
        self._running = True
        self._paused = False

        # بارگذاری تنظیمات ذخیره شده
        self.follow_system = True
        self.current_theme = _get_windows_app_theme()
        self.current_animal = DEFAULT_ANIMAL
        self.load_settings()

        self._last_theme_check = 0.0
        self._idx = 0
        self._cpu_smooth = psutil.cpu_percent(interval=None)

        icon_img = self.frames[self.current_animal][self.current_theme][0]
        self.icon = pystray.Icon("CPU Pets", icon_img, "CPU Pets")
        self.icon.menu = self._build_menu()

        self._anim_thread = threading.Thread(target=self._animate, daemon=True)

    # ----------------- Settings -----------------
    def save_settings(self):
        try:
            data = {
                "animal": self.current_animal,
                "theme": self.current_theme,
                "follow_system": self.follow_system,
                "run_on_startup": is_run_on_startup()
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
                self.current_theme = data.get("theme", _get_windows_app_theme())
                self.follow_system = data.get("follow_system", True)
                if data.get("run_on_startup", False):
                    set_run_on_startup(True)
            except Exception as e:
                print("Failed to load settings:", e)

    # ----------------- Menu -----------------
    def _build_menu(self):
        return Menu(
            Item("Pause", self.toggle_pause, default=False),
            Item(
                "Animal",
                Menu(
                    Item("Cat", lambda: self.set_animal("cat"), checked=lambda _: self.current_animal == "cat"),
                    Item("Parrot", lambda: self.set_animal("parrot"), checked=lambda _: self.current_animal == "parrot"),
                    Item("Horse", lambda: self.set_animal("horse"), checked=lambda _: self.current_animal == "horse"),
                )
            ),
            Item(
                "Theme",
                Menu(
                    Item("Follow system", self._toggle_follow_system, checked=lambda _: self.follow_system),
                    Item("Light", lambda: self.set_theme("light"),
                         checked=lambda _: (not self.follow_system and self.current_theme == "light")),
                    Item("Dark", lambda: self.set_theme("dark"),
                         checked=lambda _: (not self.follow_system and self.current_theme == "dark")),
                )
            ),
            Item(
                "Run on Startup",
                self._toggle_startup,
                checked=lambda _: is_run_on_startup()
            ),
            Item("Quit", self._quit)
        )

    # ---------- Loading frames ----------
    def _load_frames_for(self, animal, theme):
        frames = []
        folder = self.base / animal / theme
        paths = []
        if folder.exists():
            paths = sorted(folder.glob("*.ico"))
        else:
            paths = sorted(self.base.glob(f"{animal}_{theme}_*.ico"))
        for p in paths:
            try:
                img = Image.open(p).convert("RGBA").resize((TRAY_ICON_SIZE, TRAY_ICON_SIZE), Image.LANCZOS)
                frames.append(img)
            except Exception as e:
                print(f"Skipping {p.name}: {e}")
        return frames

    def _load_all_frames_or_fail(self):
        missing = []
        for animal in ANIMALS:
            for theme in ("light", "dark"):
                frames = self._load_frames_for(animal, theme)
                self.frames[animal][theme] = frames
                if not frames:
                    missing.append(f"{animal}/{theme}")
        if missing:
            details = ", ".join(missing)
            raise RuntimeError(
                "Missing icon frames for: " + details +
                "\nPut .ico files in '<animal>/<theme>/' folders (or use '<animal>_<theme>_*.ico' pattern)."
            )

    # ---------- Public controls ----------
    def start(self):
        self._anim_thread.start()
        self.icon.run()  # blocks

    def toggle_pause(self, _=None):
        self._paused = not self._paused

    def _toggle_follow_system(self, _=None):
        self.follow_system = not self.follow_system
        if self.follow_system:
            self.set_theme(_get_windows_app_theme())
        self.save_settings()

    def _toggle_startup(self, _=None):
        set_run_on_startup(not is_run_on_startup())
        self.save_settings()

    def set_theme(self, theme):
        if theme not in ("light", "dark"):
            return
        with self._lock:
            self.current_theme = theme
            self._idx = 0
            try:
                self.icon.icon = self.frames[self.current_animal][self.current_theme][0]
            except Exception:
                pass
        self.save_settings()

    def set_animal(self, animal):
        if animal not in ANIMALS:
            return
        with self._lock:
            self.current_animal = animal
            self._idx = 0
            try:
                self.icon.icon = self.frames[self.current_animal][self.current_theme][0]
            except Exception:
                pass
        self.save_settings()

    def _quit(self, _=None):
        self._running = False
        self.save_settings()  # save before exit
        try:
            self.icon.visible = False
        except Exception:
            pass
        self.icon.stop()

    # ---------- Animation ----------
    def _cpu_delay(self):
        instant = psutil.cpu_percent(interval=None)
        self._cpu_smooth = (SMOOTHING_ALPHA * instant) + ((1 - SMOOTHING_ALPHA) * self._cpu_smooth)
        factor = max(0.0, min(1.0, self._cpu_smooth / 100.0))
        return MIN_DELAY_S + (MAX_DELAY_S - MIN_DELAY_S) * factor

    def _check_auto_theme(self):
        now = time.time()
        if self.follow_system and (now - self._last_theme_check) >= THEME_POLL_S:
            self._last_theme_check = now
            themed = _get_windows_app_theme()
            if themed != self.current_theme:
                self.set_theme(themed)

    def _animate(self):
        while self._running:
            if self._paused:
                time.sleep(0.2)
                self._check_auto_theme()
                continue

            with self._lock:
                frames = self.frames[self.current_animal][self.current_theme]
                if not frames:
                    time.sleep(0.2)
                    continue
                try:
                    self.icon.icon = frames[self._idx]
                except Exception:
                    pass
                self._idx = (self._idx + 1) % len(frames)

            self._check_auto_theme()
            time.sleep(self._cpu_delay())

if __name__ == "__main__":
    _hide_and_detach_console()
    CpuPets().start()
