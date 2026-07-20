import os
import sys
import ctypes
import threading
import time
import json
from pathlib import Path
import psutil
from PIL import Image, ImageDraw, ImageFilter
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

# ---- CPU Alert Settings ----
CPU_ALERT_THRESHOLD = 100.0        # CPU percent that triggers the alert
CPU_ALERT_RESET_THRESHOLD = 90.0   # must drop below this before the alert can fire again
ALERT_TITLE = "CPU Pets"
ALERT_MESSAGE = "Your PC is using 100% of the CPU"

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
    """Get the current Windows app theme"""
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

        # CPU 100% alert flag (so the message is only shown once per spike)
        self._cpu_alert_notified = False

        # Load saved settings
        self.current_animal = DEFAULT_ANIMAL
        self.load_settings()
        
        # Get the current Windows theme
        self.current_theme = _get_windows_app_theme()
        self._last_theme_check = time.time()

        self._idx = 0
        self._cpu_smooth = psutil.cpu_percent(interval=None)

        # Build the initial icon
        icon_img = self._get_colored_frame(0)
        self.icon = pystray.Icon("CPU Pets", icon_img, "CPU Pets")
        self.icon.menu = self._build_menu()

        self._anim_thread = threading.Thread(target=self._animate, daemon=True)

    def _get_colored_frame(self, index):
        """Get the frame colored appropriately for the current theme"""
        frames = self.frames[self.current_animal][self.current_theme]
        if not frames or index >= len(frames):
            # Create a default icon
            img = Image.new('RGBA', (TRAY_ICON_SIZE, TRAY_ICON_SIZE), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            if self.current_theme == "dark":
                draw.ellipse([4, 4, 28, 28], fill=(255, 255, 255, 255))
            else:
                draw.ellipse([4, 4, 28, 28], fill=(0, 0, 0, 255))
            return img
        
        # Use the existing frame
        img = frames[index].copy()
        
        # If the theme is dark, recolor to white
        if self.current_theme == "dark":
            # Convert to a white-colored image
            result = Image.new('RGBA', img.size, (0, 0, 0, 0))
            # Extract the alpha channel
            if img.mode == 'RGBA':
                r, g, b, a = img.split()
            else:
                img = img.convert('RGBA')
                r, g, b, a = img.split()
            
            # Create a white image with the same transparency
            white = Image.new('RGBA', img.size, (255, 255, 255, 255))
            result = Image.composite(white, result, a)
            return result
        
        else:  # Light theme
            # Convert to a black-colored image
            result = Image.new('RGBA', img.size, (0, 0, 0, 0))
            if img.mode == 'RGBA':
                r, g, b, a = img.split()
            else:
                img = img.convert('RGBA')
                r, g, b, a = img.split()
            
            # Create a black image with the same transparency
            black = Image.new('RGBA', img.size, (0, 0, 0, 255))
            result = Image.composite(black, result, a)
            return result

    # ----------------- Settings -----------------
    def save_settings(self):
        try:
            data = {
                "animal": self.current_animal,
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
            # If the folder doesn't exist, use direct files instead
            paths = sorted(self.base.glob(f"{animal}_{theme}_*.ico"))
        
        if not paths:
            # If no files were found, create a default frame
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
                frames = self._load_frames_for(animal, theme)
                self.frames[animal][theme] = frames

    # ---------- Public controls ----------
    def start(self):
        self._anim_thread.start()
        self.icon.run()  # blocks

    def toggle_pause(self, _=None):
        self._paused = not self._paused

    def _toggle_startup(self, _=None):
        set_run_on_startup(not is_run_on_startup())
        self.save_settings()

    def set_animal(self, animal):
        if animal not in ANIMALS:
            return
        with self._lock:
            self.current_animal = animal
            self._idx = 0
            # Update the icon with the correct color
            try:
                self.icon.icon = self._get_colored_frame(0)
            except Exception:
                pass
        self.save_settings()

    def _update_theme(self):
        """Update the theme and icon based on the Windows theme"""
        new_theme = _get_windows_app_theme()
        if new_theme != self.current_theme:
            self.current_theme = new_theme
            with self._lock:
                self._idx = 0
                try:
                    self.icon.icon = self._get_colored_frame(0)
                except Exception:
                    pass

    def _quit(self, _=None):
        self._running = False
        self.save_settings()
        try:
            self.icon.visible = False
        except Exception:
            pass
        self.icon.stop()

    # ---------- CPU Alert ----------
    def _check_cpu_alert(self, instant_cpu):
        """If CPU has reached the alert threshold and we haven't notified yet in this spike, notify"""
        if instant_cpu >= CPU_ALERT_THRESHOLD:
            if not self._cpu_alert_notified:
                try:
                    self.icon.notify(ALERT_MESSAGE, ALERT_TITLE)
                except Exception as e:
                    self._log_alert_error(e)
                self._cpu_alert_notified = True
        elif instant_cpu < CPU_ALERT_RESET_THRESHOLD:
            # CPU has dropped enough; allow a new alert next time it spikes
            self._cpu_alert_notified = False

    def _log_alert_error(self, err):
        """Since the console is hidden, log any notify() error to a file so it can be checked"""
        try:
            log_path = SETTINGS_FILE.parent / "alert_error.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Notification failed: {err}\n")
        except Exception:
            pass

    # ---------- Animation ----------
    def _cpu_delay(self):
        instant = psutil.cpu_percent(interval=None)
        self._cpu_smooth = (SMOOTHING_ALPHA * instant) + ((1 - SMOOTHING_ALPHA) * self._cpu_smooth)
        factor = max(0.0, min(1.0, self._cpu_smooth / 100.0))
        # The alert is checked against the instant CPU value, not the smoothed one,
        # since smoothing means the value almost never reaches exactly 100
        self._check_cpu_alert(instant)
        return MIN_DELAY_S + (MAX_DELAY_S - MIN_DELAY_S) * factor

    def _animate(self):
        while self._running:
            if self._paused:
                time.sleep(0.2)
                self._update_theme()
                continue

            with self._lock:
                try:
                    # Get the frame with the correct color
                    colored_frame = self._get_colored_frame(self._idx)
                    self.icon.icon = colored_frame
                except Exception as e:
                    print(f"Animation error: {e}")
                    pass
                self._idx = (self._idx + 1) % len(self.frames[self.current_animal][self.current_theme])

            # Check the Windows theme
            self._update_theme()
            time.sleep(self._cpu_delay())

if __name__ == "__main__":
    _hide_and_detach_console()
    CpuPets().start()
