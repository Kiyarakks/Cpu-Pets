# CPU Pets 🐾

**CPU Pets** is a fun and lightweight Windows tray application that shows animated pets (Cat, Parrot, Horse) in your system tray.
The animation speed changes dynamically based on your **CPU usage**, making it both entertaining and a subtle system monitor.

It has grown from a simple screen‑time tracker into a small suite of tray‑based tools: **Productivity Analytics** with week‑over‑week insights, **Custom Alerts** on any system metric, **Reminders**, automatic **Not Responding / hung‑app** detection with one‑click force‑close, and an **Application Health** history — all from the same tray icon.

---

## 🐾 Demo

![Demo](/Docs/demo.gif)

## ✨ Features

### 🐱 The Pet (tray icon)

- 🖼️ **Animated Tray Icons** – Pets move smoothly in the tray area.
- ⚡ **CPU-Based Animation** – The higher your CPU usage, the slower the pet moves.
- 🎨 **Automatic Light & Dark Theme** – Detects the current Windows theme and recolors the pet icon (white on dark theme, black on light theme) automatically — no manual switch needed.
- 🚨 **CPU 100% Alert** – Shows a one-time tray notification when CPU usage hits 100%, and won't notify again until usage drops back below 90% and spikes again.
- 📈 **Anomaly Detection Alert** *(new)* – Learns a rolling baseline (mean/standard deviation) of your normal CPU usage and warns you when it spikes well above that baseline — not just at a fixed 100% — so unusual activity gets caught even if it never hits the ceiling. Ignores small spikes below a minimum absolute CPU% so idle machines don't trigger false alarms.
- 🐾 **Multiple Pets** – Choose between Cat, Parrot, and Horse.
- 🖥️ **Live Tooltip** – Hover over the icon to see current CPU usage, RAM usage, and system uptime at a glance.
- 🎛️ **Redesigned Tray Menu** *(reworked)* – Right-click opens a compact, translucent, rounded-corner dark menu (not the native Windows menu), reorganized into logical groups instead of one long list:

  - **Pause** – pause/resume the pet's animation
  - **Animal ▸** – switch between Cat / Parrot / Horse
  - **Monitor ▸** – Productivity Analytics, Weekly Report, App Health
  - **Alerts ▸** – CPU 100% Alert, Anomaly Alerts, Hang Alerts, Custom Alerts...
  - **Close Hung Apps** – force-close every currently unresponsive app in one click (kept at the top level, not buried in Alerts, since it's an action you need fast)
  - **Reminders**
  - **Settings ▸** – Run on Startup
  - **Quit**

- 🔄 **Run on Startup** – Optional auto-start with Windows.
- 💾 **Persistent Settings** – Saves your chosen animal, alert preferences, and startup preference in `settings.json`.

### 📊 Productivity Analytics (formerly "Screen Time")

- 📊 Tracks how long each application is the active (foreground) window.
- 😴 Automatically skips counting time while the system is idle (no keyboard/mouse input).
- 🗂️ **Long-term history** – Usage is stored in a local SQLite database (`screen_time.db`), one entry per day per app, so your history builds up indefinitely (months, even years) instead of being overwritten every day.
- 📅 **Date picker** – Browse today's usage or jump back to any previous day that has recorded data.
- 🏷️ **Friendly application names** *(new)* – Raw process names like `chrome.exe` or `code.exe` are shown as readable names ("Chrome", "VS Code", etc.) throughout the UI. The raw process name is still what's stored in the database, so history stays consistent even for apps not yet in the friendly-name list.
- 🖱️ Opens from the pet's tray menu ("Monitor ▸ Productivity Analytics") or with a single left click on the tray icon; closing the window just hides it, tracking keeps running in the background.

### 📅 Weekly Report — now a real analytics dashboard *(new)*

The old weekly report was just a flat list of totals per app. It's now a small dashboard:

- **Total active time** for the week, with a **week-over-week comparison** (e.g. "+8% total usage compared to last week").
- **Most used** – your top 3 apps by time, by friendly name.
- **App categories** – time grouped into Development / Communication / Entertainment / Other, so you can see *what kind* of usage grew, not just which app.
- **Auto-generated insight** – a one-line sentence such as *"You spent 34% more time in development apps this week"*, based on whichever category you spent the most time in and how it changed versus the previous week.

### 🩺 Application Health — crash & hang history *(new)*

A new "App Health" window (Monitor ▸ App Health) tracks each app's stability over the last 7 days:

- **Hangs** – incremented every time Windows reports an app as Not Responding.
- **Crashes** – incremented every time a hung app has to be force-closed via the tray menu (the closest reliable signal available without hooking into Windows Error Reporting).
- **Most unstable apps** – a ranked top-5 list by total incidents (hangs + crashes).

### 🔔 Custom Alerts *(new)*

Define your own alert rules from Alerts ▸ Custom Alerts:

- Watch **Total CPU %**, **Total RAM %**, **Disk usage % (C:)**, **Battery %**, or a **specific app's CPU %** / **RAM (MB)**.
- Trigger when the value **goes above** or **drops below** a threshold you set.
- Optionally require the condition to be **sustained** for N seconds before alerting (to avoid noise from brief spikes).
- Optional custom notification message.
- Multiple alerts can be active at once; each is checked independently on a short timer.

### ⏰ Reminders *(new)*

Simple date/time reminders that fire as tray notifications, opened from the tray menu:

- One-off reminders, or repeating **daily**, **weekly** (on chosen weekdays), or **monthly** (same date each month).
- Stored in the same local database as everything else, so they persist across restarts.

### 🥊 Not Responding / Hung App Detection *(reworked)*

- Detects hung top-level windows the same way Windows itself does, and polls periodically.
- When a new hang is detected, you get a tray notification (if "Hang Alerts" is enabled).
- **Close Hung Apps** (top level of the tray menu) force-closes *every* currently unresponsive app in a single click — no list to browse first, just like clicking "End Task" in Task Manager for everything that's frozen at once.
- Every hang and every forced close is logged for the Application Health window described above.

---

## ⚙️ How It Works

- The whole app runs on **PyQt5** (a single `QApplication` event loop) — the pet's tray icon, its dark context menu, and every window (Productivity Analytics, Weekly Report, App Health, Custom Alerts, Reminders) are all part of the same process.
- The app loads `.ico` frames for each pet and theme. If frames are missing for a given animal/theme, a simple default placeholder icon is used instead.
- Animation speed is calculated from real-time CPU usage (via `psutil`) and driven by a `QTimer` that reschedules itself with a variable delay each frame.
- A `SystemMonitor` samples CPU/RAM on a timer, keeping a rolling history used both for the sparkline graphs and as the baseline (mean/standard deviation) for the anomaly detection alert.
- Custom Alerts are evaluated on their own timer against whatever metric, operator, threshold, and optional sustained-duration each rule specifies.
- Reminders are checked periodically; repeating reminders compute their next occurrence (daily / specific weekdays / monthly) once they fire.
- Not Responding detection polls for hung top-level windows; a newly-hung app logs a "hang" event, and force-closing it (via the tray menu) logs a "crash" event — both feed the Application Health window.
- A dedicated background thread (`QThread`) polls the active foreground window for Productivity Analytics, writing accumulated seconds straight into the SQLite database so nothing is lost between restarts or across days.
- The custom dark tray menu is a translucent, rounded, frameless popup (`WA_TranslucentBackground` + `FramelessWindowHint`) built from a small set of grouped submenus instead of one long flat list; the underlying color palette is unchanged, just tighter and glassier.
- User preferences (selected animal, alert toggles, run-on-startup) are stored in `settings.json`; usage history, reminders, and app-health counters all live in `screen_time.db`.
- The system theme is checked continuously and applied automatically — there is no manual theme override.

---

## 📂 Project Structure

```
CPU_Pets/
│── cat/
│   ├── light/
│   └── dark/
│── parrot/
│   ├── light/
│   └── dark/
│── horse/
│   ├── light/
│   └── dark/
│── main.pyw
```

Each folder contains `.ico` files for animation frames. Icons can be plain silhouettes — the app tints them white or black automatically depending on the active Windows theme.

At runtime, the app creates a data folder under `%APPDATA%\CPU_Pets\` containing:

```
%APPDATA%\CPU_Pets\
│── settings.json        (pet preferences: animal, alert toggles, run on startup)
│── screen_time.db        (SQLite database: usage history, reminders, and app-health/hang-crash counters)
│── alert_error.log       (created only if a notification fails to send)
```

---

## 🚀 Usage

1. Run the application (`main.pyw`, or a packaged `.exe`).
2. For Download .exe file click [CpuPets](https://github.com/Kiyarakks/Cpu-Pets/releases/download/v2.1.0/CpuPets.v2.1.0.exe)
3. A pet icon will appear in your **system tray**.
4. Right-click the icon to open the menu and configure settings, or left-click to quickly open Productivity Analytics.

---

## 🛠️ Requirements (not for .exe file)

- Python 3.8+
- Dependencies (unchanged from the previous version):

```
  pip install psutil pillow PyQt5 pywin32
```

---

## 📌 Notes

- Works on **Windows only** (uses `winreg` for startup registry and theme detection, and `pywin32` for foreground-window/idle/hung-window detection).
- If `.ico` frames are missing for an animal/theme, the app falls back to a default placeholder icon instead of crashing.
- Tray notifications use Qt's native `QSystemTrayIcon.showMessage()` — if one ever silently fails, check `alert_error.log`.
- "Crash" counts in Application Health are a practical proxy (apps force-closed after hanging), not real Windows Error Reporting crash data — there's no hook into WER.
- All history (usage, reminders, app health) is kept forever by default; delete `screen_time.db` under `%APPDATA%\CPU_Pets\` if you ever want to reset everything.
