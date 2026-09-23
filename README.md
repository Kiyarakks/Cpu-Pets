# CPU Pets 🐾

**CPU Pets** is a fun and lightweight Windows tray application that shows animated pets (Cat, Parrot, Horse) in your system tray.
The animation speed changes dynamically based on your **CPU usage**, making it both entertaining and a subtle system monitor.

It has grown from a simple screen‑time tracker into a small suite of tray‑based tools: **Productivity Analytics** with week‑over‑week insights, **Custom Alerts** on any system metric, **Reminders**, automatic **Not Responding / hung‑app** detection with one‑click force‑close, an **Application Health** history, a **Health Check** score with a real **Crash & Restart History** pulled from the Windows Event Log, **Battery Health**, a **Quick Flyout** on left‑click, and **built‑in self‑updates** — all from the same tray icon.

---

## 🐾 Demo

![Demo](/Docs/demo.gif)

## ✨ Features

### 🐱 The Pet (tray icon)

- 🖼️ **Animated Tray Icons** – Pets move smoothly in the tray area.
- ⚡ **CPU-Based Animation** – The higher your CPU usage, the slower the pet moves.
- 🎨 **Automatic Light & Dark Theme** – Detects the current Windows theme and recolors the pet icon (white on dark theme, black on light theme) automatically — no manual switch needed.
- 🚨 **CPU 100% Alert** – Shows a one-time tray notification when CPU usage hits 100%, and won't notify again until usage drops back below 90% and spikes again.
- 📈 **Anomaly Detection Alert** – Learns a rolling baseline (mean/standard deviation) of your normal CPU usage and warns you when it spikes well above that baseline — not just at a fixed 100% — so unusual activity gets caught even if it never hits the ceiling. Ignores small spikes below a minimum absolute CPU% so idle machines don't trigger false alarms.
- 🐾 **Multiple Pets** – Choose between Cat, Parrot, and Horse.
- 🖥️ **Live Tooltip** – Hover over the icon to see current CPU usage, RAM usage, and system uptime at a glance.
- 🎛️ **Redesigned Tray Menu** – Right-click opens a compact, translucent, rounded-corner dark menu (not the native Windows menu), reorganized into logical groups instead of one long list:

  - **Animal ▸** – switch between Cat / Parrot / Horse
  - **Monitor ▸** – Productivity Analytics, Weekly Report, App Health
  - **Alerts ▸** – CPU 100% Alert, Anomaly Alerts, Hang Alerts, Custom Alerts...
  - **Close Hung Apps** – force-close every currently unresponsive app in one click (kept at the top level, not buried in Alerts, since it's an action you need fast)
  - **Reminders**
  - **Health Check** – opens the new Health Check window (top level, since it's the headline feature)
  - **Settings ▸** – Run on Startup, App Categories..., Crash & Restart History..., Battery Health..., Check for Updates...
  - **About**
  - **Quit**

  > Note: the previous "Pause" action was removed in this version; the animation always runs.

- 🪟 **Custom Frameless Windows** – Every secondary window and dialog (Productivity Analytics, Weekly Report, Health Check, Crash & Restart History, Battery Health, Custom Alerts, Reminders, App Categories, About, Update, and the add/edit dialogs) now draws its **own** dark caption bar with a title, minimize (where it makes sense), and close button. Windows never shows its default white title bar. The bar is draggable to move the window, and the close button always routes through the window's own close/reject behavior (so "hide instead of quit" still works).
- 🔄 **Run on Startup** – Optional auto-start with Windows.
- 💾 **Persistent Settings** – Saves your chosen animal, alert preferences, and startup preference in `settings.json`.

### ⚡ Quick Flyout (new)

- Left-clicking the tray icon now opens a small borderless **Quick Flyout** anchored above the icon (Windows-style popup) instead of toggling the Productivity Analytics window.
- Shows live **CPU %**, **RAM %**, **Uptime**, and the **top 3 busiest processes** (with CPU % and memory MB), refreshed every 1.5 seconds.
- Three shortcut buttons at the bottom: **Screen Time**, **Health**, **Report** — click one to jump straight to the heavy window (the flyout closes itself).
- Clicking anywhere outside the flyout closes it (it's a `Qt.Popup`).
- The flyout is positioned above the tray icon when the taskbar is at the bottom, or below it if the taskbar is at the top; it is clamped to the screen so it never goes off-edge.
- **Double-click** on the tray icon still toggles the full Productivity Analytics window (the old left-click behavior).

### 📊 Productivity Analytics

- 📊 Tracks how long each application is the active (foreground) window.
- 😴 Automatically skips counting time while the system is idle (no keyboard/mouse input).
- 🗂️ **Long-term history** – Usage is stored in a local SQLite database (`screen_time.db`), one entry per day per app, so your history builds up indefinitely (months, even years) instead of being overwritten every day.
- 📅 **Date picker** – Browse today's usage or jump back to any previous day that has recorded data.
- 🏷️ **Friendly application names** – Raw process names like `chrome.exe` or `code.exe` are shown as readable names ("Chrome", "VS Code", etc.) throughout the UI. The raw process name is still what's stored in the database, so history stays consistent even for apps not yet in the friendly-name list.
- 🖱️ Opens from the pet's tray menu ("Monitor ▸ Productivity Analytics") or with a **double-click** on the tray icon; closing the window just hides it, tracking keeps running in the background.

### 📅 Weekly Report — analytics dashboard

The weekly report is a small dashboard:

- **Total active time** for the week, with a **week-over-week comparison** (e.g. "+8% total usage compared to last week").
- **Most used** – your top 3 apps by time, by friendly name.
- **App categories** – time grouped into Development / Communication / Entertainment / Other, so you can see *what kind* of usage grew, not just which app.
- **Auto-generated insight** – a one-line sentence such as *"You spent 34% more time in development apps this week"*, based on whichever category you spent the most time in and how it changed versus the previous week.

### 🩺 Application Health — crash & hang history

An "App Health" window (Monitor ▸ App Health) tracks each app's stability over the last 7 days:

- **Hangs** – incremented every time Windows reports an app as Not Responding.
- **Crashes** – incremented every time a hung app has to be force-closed via the tray menu (the closest reliable signal available without hooking into Windows Error Reporting).
- **Most unstable apps** – a ranked top-5 list by total incidents (hangs + crashes).

### 🩻 Health Check (new)

The headline feature of this version. Health Check produces a **single 0–100 score** with a grade (Excellent / Good / Needs attention / Poor) plus a scrollable list of findings, each showing the points it cost and (where relevant) a link into the detail window.

- Opens from the top level of the tray menu (**Health Check**), or from the flyout's **Health** button.
- Combines several independent inputs:
  - **Unexpected restarts** (blue screens, forced power-off, unexpected shutdown) in the last 30 days — the biggest single penalty.
  - **Hardware errors** reported by Windows (WHEA-Logger).
  - **Disk errors** and NTFS corruption.
  - **Graphics driver resets** (Display event 4101).
  - **App crashes and hangs** in the last 7 days.
  - **Uptime** (very long uptimes are flagged, since Fast Startup can leave a machine running for a month without ever really restarting).
  - **Free disk space** on every fixed drive — <5% free is a hard penalty, 5–10% and 10–15% are smaller warnings.
  - **Average RAM pressure** over the last few minutes (from the shared `SystemMonitor`).
  - **Startup programs count** (registry Run keys on HKLM/HKCU + Startup folders, excluding this app itself).
  - **Pending Windows reboot** (CBS or Windows Update RebootRequired).
  - **Battery wear** (design vs. current full-charge capacity, from `powercfg`).
- Findings from the last week count fully toward the score; older ones (up to 30 days) count half. Dismissed incidents do **not** count.
- The score is fully explainable — every finding is listed with the points it cost, and findings are sorted by severity (bad → warn → info → ok).
- A **Reset...** menu lets you clear history older than 7 days, clear all history, or open the full history to review first. Clearing also permanently ignores those specific Windows Event Log entries (see "Crash & Restart History" below), so a later scan can't bring them back.
- Refresh runs on a background thread (`BackgroundTask`), so the UI never blocks.

### 💥 Crash & Restart History (new)

A real, Event-Log-sourced history of every crash and unexpected restart on the machine, opened from Settings ▸ **Crash & Restart History...** (and linked from the Health Check findings).

- Reads the Windows Event Log directly via the built-in **`wevtutil.exe`** — no extra Python packages needed.
- Queries multiple sources (Kernel-Power 41, EventLog 6008, WER-SystemErrorReporting 1001, BugCheck 1001, Display 4101, WHEA-Logger, disk, Ntfs, Application Error 1000, Application Hang 1002) and clusters events that occur within 10 minutes of each other into a **single incident** — a single blue screen would otherwise appear as 2–3 separate events.
- Recognizes a curated set of common **stop codes** (0xA IRQL_NOT_LESS_OR_EQUAL, 0x1A MEMORY_MANAGEMENT, 0x3B SYSTEM_SERVICE_EXCEPTION, 0x7E SYSTEM_THREAD_EXCEPTION_NOT_HANDLED, 0xD1 DRIVER_IRQL_NOT_LESS_OR_EQUAL, 0x116/0x117/0x141 VIDEO_TDR, 0x124 WHEA_UNCORRECTABLE_ERROR, 0x133 DPC_WATCHDOG_VIOLATION, 0x139 KERNEL_SECURITY_CHECK_FAILURE, etc.) and explains in plain English what each one usually points to.
- Distinguishes **blue screen**, **forced power-off** (power button held down — usually a freeze), and **unexpected shutdown** (no stop code recorded — usually power loss or a hard reset).
- Other events become one incident each: **app crash**, **app hang**, **GPU reset**, **hardware error**, **disk error**.
- **Heartbeat attachment**: the app records a lightweight "I'm alive, and here's what's running" row every 20 seconds (see "Heartbeat" below). When a restart incident is stored, the app looks up the most recent heartbeat within 2 hours before the crash and appends it to the incident details — so you can see *what the PC was doing right before it died* (CPU%, RAM%, busiest app, biggest memory user).
- Every incident has a **Dismiss** / **Restore** toggle: dismissing excludes it from the Health Check score but keeps it visible (greyed out) in history. Nothing disappears silently.
- **Clear All...** deletes everything and recalculates the score. Because Windows has no supported way to delete a single Event Log entry (`wevtutil` can only clear an entire channel), clearing an incident also stores its `event_key` in a `reliability_ignored` table so future scans skip that exact record. A genuinely new problem always gets a new record id, so it's never affected.
- The scan runs on a 30-minute timer, on a background thread, and once shortly after startup. Newly-found restart incidents raise a tray notification.

### 🔋 Battery Health (new)

Opened from Settings ▸ **Battery Health...** (and linked from the Health Check findings).

- Reads Windows' own battery report via **`powercfg /batteryreport /output ... /xml`** and parses it — no extra Python packages needed.
- Shows **design capacity**, **current full-charge capacity**, **cycle count**, and a battery label, plus a large **health %** (full ÷ design, clamped to 0–100) with a grade (Excellent / Good / Worn / Poor - consider replacing).
- Also shows the **live state** (percent and charging/on battery/plugged in).
- Snapshots are saved once per day (up to the last 120 days) so the trend builds up over time.
- On a desktop with no battery, the window says so clearly and disables the "Run report" button instead of erroring.
- The report runs on a background thread; a report is automatically generated once on first launch (after a short delay) and refreshed every 6 hours.

### 🔔 Custom Alerts

Define your own alert rules from Alerts ▸ Custom Alerts:

- Watch **Total CPU %**, **Total RAM %**, **Disk usage % (C:)**, **Battery %**, or a **specific app's CPU %** / **RAM (MB)**.
- Trigger when the value **goes above** or **drops below** a threshold you set.
- Optionally require the condition to be **sustained** for N seconds before alerting (to avoid noise from brief spikes).
- Optional custom notification message.
- Multiple alerts can be active at once; each is checked independently on a short timer.

### ⏰ Reminders

Simple date/time reminders that fire as tray notifications, opened from the tray menu:

- One-off reminders, or repeating **daily**, **weekly** (on chosen weekdays), or **monthly** (same date each month).
- Stored in the same local database as everything else, so they persist across restarts.

### 🥊 Not Responding / Hung App Detection

- Detects hung top-level windows the same way Windows itself does, and polls periodically.
- When a new hang is detected, you get a tray notification (if "Hang Alerts" is enabled).
- **Close Hung Apps** (top level of the tray menu) force-closes *every* currently unresponsive app in a single click — no list to browse first, just like clicking "End Task" in Task Manager for everything that's frozen at once.
- Every hang and every forced close is logged for the Application Health window described above.

### 🫀 Heartbeat (background, powers Crash & Restart History)

- Once every **20 seconds**, the app writes one row per boot to the `heartbeat` table: current CPU%, RAM%, the busiest process (name + CPU%) and the biggest memory user (name + MB).
- To keep its own footprint small, the heartbeat **only scans process memory in 1 out of every 6 ticks** (~every 2 minutes); the other ticks do the cheap CPU-only scan and keep the previous RAM figure. This makes the "top RAM user" attached to a crash accurate to within a couple of minutes rather than to the exact last heartbeat — an acceptable trade-off since it's only ever shown as a rough clue.
- The heartbeat table keeps the last 20 boots and drops older ones automatically.
- When a restart incident is stored (from the Event Log scan), the app looks up the heartbeat closest to the crash and appends it to the incident's details.

---

## ⚙️ How It Works

- The whole app runs on **PyQt5** (a single `QApplication` event loop) — the pet's tray icon, its dark context menu, the quick flyout, and every window are all part of the same process.
- The app loads `.ico` frames for each pet and theme. If frames are missing for a given animal/theme, a simple default placeholder icon is used instead.
- Animation speed is calculated from real-time CPU usage (via `psutil`) and driven by a `QTimer` that reschedules itself with a variable delay each frame.
- A `SystemMonitor` samples CPU/RAM on a timer, keeping a rolling history used both for the sparkline graphs and as the baseline (mean/standard deviation) for the anomaly detection alert. It also drives the **heartbeat** timer that powers Crash & Restart History.
- `BackgroundTask` is a tiny helper that runs any callable on a daemon thread and delivers its result back on the Qt thread via a signal. It's used by every slow job: reliability scan, battery report, update check, update install, and the heartbeat's process scan.
- Custom Alerts are evaluated on their own timer against whatever metric, operator, threshold, and optional sustained-duration each rule specifies.
- Reminders are checked periodically; repeating reminders compute their next occurrence (daily / specific weekdays / monthly) once they fire.
- Not Responding detection polls for hung top-level windows; a newly-hung app logs a "hang" event, and force-closing it (via the tray menu) logs a "crash" event — both feed the Application Health window.
- A dedicated background thread (`QThread`) polls the active foreground window for Productivity Analytics, writing accumulated seconds straight into the SQLite database so nothing is lost between restarts or across days.
- The custom dark tray menu is a translucent, rounded, frameless popup (`WA_TranslucentBackground` + `FramelessWindowHint`) built from a small set of grouped submenus instead of one long flat list.
- Every secondary window and dialog uses a small `FramelessWindow` / `FramelessDialog` base class that draws its own **dark caption bar** (title, drag-to-move, minimize where applicable, close that routes through `close()` / `reject()`), so Windows never draws its default white chrome.
- Reliability scanning shells out to the built-in **`wevtutil.exe`** (no extra dependencies) and parses the XML with `xml.etree.ElementTree`, namespace-agnostically.
- Battery health shells out to the built-in **`powercfg.exe`** and parses its XML report the same way.
- The self-updater uses only the standard library (`urllib.request`, `hashlib`, `subprocess`) against the GitHub Releases API.
- User preferences (selected animal, alert toggles, run-on-startup, custom alerts, category overrides, last weekly-report date) are stored in `settings.json`; usage history, reminders, app-health counters, reliability incidents, heartbeats, and battery snapshots all live in `screen_time.db`.
- The system theme is checked continuously and applied automatically — there is no manual theme override.
- `--wait-pid <pid>` is a hidden command-line flag used only by the self-updater restart path; it makes a fresh copy of the app wait for the old one to exit before taking over.

---

## 📂 Project Structure
CPU_Pets/
│── cat/
│ ├── light/
│ └── dark/
│── parrot/
│ ├── light/
│ └── dark/
│── horse/
│ ├── light/
│ └── dark/
│── main.pyw


Each folder contains `.ico` files for animation frames. Icons can be plain silhouettes — the app tints them white or black automatically depending on the active Windows theme.

At runtime, the app creates a data folder under `%APPDATA%\CPU_Pets\` containing:

%APPDATA%\CPU_Pets
│── settings.json (pet preferences, custom alerts, category overrides, alert toggles)
│── screen_time.db (SQLite database: usage history, reminders, app-health counters,
│ reliability incidents, ignored event keys, heartbeats, battery snapshots)
│── battery_report.xml (temporary file written by powercfg, re-read on each report run)
│── main.pyw.bak (created only after a self-update; the previous version of the script)
│── alert_error.log (created only if a background job or notification fails)


### Database tables inside `screen_time.db`

| Table | Purpose |
|---|---|
| `usage` | Per-day, per-process foreground seconds (Productivity Analytics). |
| `reminders` | One-off and repeating reminders. |
| `app_health` | Per-day hang / force-close counters per process (Application Health). |
| `reliability_events` | Crash / restart / hardware / disk / GPU / app-crash incidents from the Event Log (Crash & Restart History). |
| `reliability_ignored` | Event keys the user has cleared, so a later scan skips them. |
| `heartbeat` | Per-boot "last sign of life" rows (CPU, RAM, top CPU process, top RAM process). |
| `battery_health` | One battery snapshot per day (design mAh, full-charge mAh, cycle count, label). |

---

## 🚀 Usage

1. Run the application (`main.pyw`, or a packaged `.exe`).
2. For Download .exe file click [CpuPets](https://github.com/Kiyarakks/Cpu-Pets/releases/download/v2.1.0/CpuPets.v2.1.0.exe)
3. A pet icon will appear in your **system tray**.
4. **Left-click** the icon for the Quick Flyout (live stats + top processes + shortcuts).
5. **Double-click** the icon to toggle Productivity Analytics.
6. **Right-click** the icon to open the full menu (Animal / Monitor / Alerts / Close Hung Apps / Reminders / Health Check / Settings / About / Quit).

---

## 🛠️ Requirements (not for .exe file)

- Python 3.8+
- Dependencies:
pip install psutil pillow PyQt5 pywin32


No additional packages are needed for the new features: Crash & Restart History uses the built-in `wevtutil.exe`, Battery Health uses the built-in `powercfg.exe`, and the self-updater uses only the standard library.

---

## 📌 Notes

- Works on **Windows only** (uses `winreg` for startup registry and theme detection, `pywin32` for foreground-window/idle/hung-window detection, `wevtutil.exe` for the Event Log, and `powercfg.exe` for the battery report).
- If `.ico` frames are missing for an animal/theme, the app falls back to a default placeholder icon instead of crashing.
- Tray notifications use Qt's native `QSystemTrayIcon.showMessage()` — if one ever silently fails, check `alert_error.log`.
- "Crash" counts in Application Health are a practical proxy (apps force-closed after hanging), not real Windows Error Reporting crash data — there's no hook into WER. The **Crash & Restart History** window, however, *is* sourced from the real Windows Event Log.
- Clearing a crash/restart incident does **not** delete the underlying Windows Event Log entry — Windows does not support deleting single entries (`wevtutil` can only clear an entire channel). Instead, the app remembers that specific `event_key` in `reliability_ignored` so future scans skip it. A genuinely new problem always gets a new record id and is detected normally.
- Self-update is only possible when the app is running from a writable `.py` / `.pyw` script (not a packaged `.exe`, not a read-only folder). When blocked, the update dialog still offers a link to the release page.
- The heartbeat only scans process memory every ~2 minutes, so the "top RAM user" attached to a crash is a rough clue, not an exact reading at the moment of failure.
- All history (usage, reminders, app health, reliability incidents, heartbeats, battery snapshots) is kept for a bounded but long time by default (reliability events up to 180 days, heartbeat up to 20 boots, battery snapshots up to 120 days). Delete `screen_time.db` under `%APPDATA%\CPU_Pets\` if you ever want to reset everything.
