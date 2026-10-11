# CleanSweep

English | [简体中文](README.md)

A Windows desktop utility that lists every installed program, runs each program's **official uninstaller**, then automatically scans for and removes leftover files/folders and registry entries — and generates a cleanup report.

**Zero third-party runtime dependencies** (pure Python standard library + ctypes/Win32 API + tkinter). Packages into a single exe.

> Platform: Windows 10 / 11 ｜ Python 3.8+ to run from source ｜ License: MIT

## Introduction

Uninstalling software on Windows has two recurring problems: uninstallers that leave residue behind in `Program Files`, `AppData`, `ProgramData` and the registry; and "rogue" uninstallers that hang forever and spam dialogs.

CleanSweep follows a "official uninstall + fallback cleanup + full user control" pipeline:

1. Enumerates all installed programs from the 4 registry Uninstall keys (HKLM/HKCU × native/WOW6432Node);
2. Runs each program's **official uninstaller** first (MSI / Inno Setup / NSIS / arbitrary custom uninstallers);
3. The wait phase has a hard timeout, heartbeat logs and one-click force-kill — **rogue uninstallers cannot freeze it**;
4. After uninstalling, it scans for leftover candidates, deletes them (to the Recycle Bin by default) only after you review them item by item;
5. Everything is backed up automatically (`.reg` export + optional System Restore point) and a Markdown report is generated.

## Features

| Feature | Details |
| --- | --- |
| Program list | Name, publisher, install location, size (measured in background), install date |
| Sort & search | Click any column header to sort (size/date/risk default to descending), live search box |
| Thorough uninstall | Official uninstaller → leftover scan → item-by-item confirmation → delete → report |
| Leftover scan | `Program Files`, `Program Files (x86)`, `%LocalAppData%`, `%APPDATA%`, `%ProgramData%`, `%LocalAppData%\Programs`, Start Menu; registry `HKLM\SOFTWARE`, `HKLM\SOFTWARE\WOW6432Node`, `HKCU\Software` |
| Residue risk level | High/Medium/Low heuristic scoring, color-coded rows, red warning for high-risk apps before uninstalling |
| Anti-freeze design | 60-second hard timeout, 10-second heartbeat logs, "Force-kill uninstaller and continue" button, batched log flushing, cancellable scans (see below) |
| Backup & restore | Auto `reg export` before deleting any registry key (double-click a `.reg` to restore); optional System Restore point; files go to the Recycle Bin by default |
| Cleanup report | Markdown report listing every deleted file/folder (with file count, size) and every registry key plus backup location |

## The freeze bug: root cause and fix (v1.0.0 highlight)

### Symptom

Uninstalling certain rogue software (e.g. the Bilibili desktop client) froze the whole app — UI unresponsive, only killable from Task Manager.

### Root causes

Investigation confirmed three stacked problems; #1 was the core deadlock chain:

1. **Per-line logging blocked the worker thread.** Every log line was dispatched to the Tk main thread via `call_in_main()` and the worker blocked on `threading.Event.wait()`. As soon as the main thread got stuck in a modal dialog (e.g. a confirmation box hidden behind the rogue uninstaller's always-on-top window), the event queue stopped draining and the worker hung in `wait()` forever — a deadlock.
2. **Silent long waits.** Waiting 120 s for the uninstaller to exit plus 300 s polling the registry entry produced **zero log output** and offered no way to intervene. An uninstaller that never exits made the app look completely frozen.
3. **Unescapable launch phase.** `ShellExecuteExW` (with `SEE_MASK_NOASYNC`) blocks synchronously inside the worker thread while a UAC prompt or a malformed uninstaller is pending — even the Cancel button could not take effect.

### The fixes

| Problem | Fix |
| --- | --- |
| Logging deadlock | Logs go into a thread-safe buffer; the main thread **flushes in batches every 0.3 s**. Workers never block on logging — the deadlock chain is gone |
| Silent long waits | Hard **60-second timeout** for the uninstaller wait with a **heartbeat log every 10 seconds** (timestamped) |
| No way to intervene | New **"Force-kill uninstaller and continue"** button: within ≤1 s it runs `taskkill /F /T` on the uninstaller's process tree and jumps straight to the leftover scan; the timeout also force-kills automatically |
| Killing the wrong process | Before force-killing, candidate process names are derived from the uninstall command + top-level exes of the install folder, matched **exactly against currently running processes** via `tasklist`; system processes (`explorer.exe`, `svchost.exe`, `csrss.exe`…), browsers, Python and CleanSweep itself are on a hard-coded protected list and are **never killed**; if no name can be derived, force-kill is skipped and the user is told to close the uninstaller manually |
| `msiexec` special case | Killing a Windows Installer transaction can corrupt its database, so MSI waits are **never auto-killed**; force-killing an MSI requires a second confirmation and only targets that transaction's PID |
| Launch-phase blocking | `ShellExecuteExW` runs in a dedicated launcher thread so a stuck UAC prompt cannot freeze the flow |
| Uncancellable scans | `scan_filesystem` / `scan_registry` accept a cancel check — scans can be aborted at any time |
| Windows hidden behind others | Every dialog is raised and temporarily top-most, so a rogue uninstaller's window cannot hide it and fake a "hang" |

## Usage

1. On startup the app loads the program list and evaluates each program's residue risk in the background (rows color-coded: red = high, orange = medium, green = low).
2. Click a column header to sort by **size / date / risk / name / publisher**; the search box filters live.
3. Selecting a program shows details below (version, registry entry, uninstall command, risk reasons).
4. **Thorough uninstall**: confirmation dialog → (optional) restore point → official uninstaller (max 60 s wait, one-click force-kill) → leftover scan (cancellable) → review candidates item by item → choose "Recycle Bin" or "permanent delete" → report.
5. **Scan leftovers only**: lists residue candidates without running any uninstaller (useful for dead entries).
6. Reports live in `Documents\CleanSweepReports\<timestamp>_<app>\清理报告.md`; the `registry-backup\` folder next to it contains `.reg` backups of every deleted key (double-click to restore).

## Safety mechanisms

1. **Two-stage confirmation**: pre-uninstall confirmation + item-by-item review with a final confirmation (default check states can be changed).
2. **Hard path whitelist**: only content under standard software directories can ever be suggested; protected path segments (`Windows`, `Microsoft`, `Package Cache`, `Common Files`, `Users`…) are always rejected; user Desktop/Documents/Downloads are never scanned. On custom drives only the app's own install folder is allowed.
3. **Junction/symlink guard**: deletion removes the link itself, never follows into the target.
4. **Recycle Bin first**: files/folders go to the Recycle Bin unless you explicitly choose permanent deletion.
5. **Registry backup before delete**: keys are exported with `reg export` first; if the export fails the deletion is skipped; only keys under `HKLM\SOFTWARE`, `HKLM\SOFTWARE\WOW6432Node`, `HKCU\Software` are touched.
6. **System Restore point**: created via `Checkpoint-Computer` before uninstalling (admin required, optional).
7. **Protected process list** for force-kills: see the fixes table above.

## ⚠️ Risk warnings

- **Inherent risk of deletion**: files default to the Recycle Bin, but permanent deletion is unrecoverable; registry deletions have `.reg` backups — still confirm carefully.
- **Force-kill consequences**: unsaved data in killed processes is lost; killing `msiexec` can corrupt the Windows Installer database (double-confirmed by default and never auto-killed).
- **Heuristic matching**: leftover candidates are name-based; false negatives are expected and false positives are possible — read every path before checking it.
- **Restore point limits**: Windows only creates one restore point per 24 h; consider creating one manually before batch uninstalls.
- **Antivirus false positives**: unsigned PyInstaller exes are commonly flagged; run from source or sign the binary yourself.
- **Scope**: UWP/Store apps are not listed; do not use it to clean system components or drivers (hidden system entries are filtered by default).

## Run from source

```bat
git clone https://github.com/Yangxbao/CleanSweep.git
cd CleanSweep
run.bat
```

Or `python main.py`. A UAC prompt offers to relaunch as administrator — **recommended**, otherwise HKLM registry and Program Files cleanup is unavailable (limited mode still works; `python main.py --no-elevate` skips the prompt).

## Build the exe

Run `build.bat`, or manually:

```bat
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --uac-admin --name CleanSweep main.py
```

Output: `dist\CleanSweep.exe` (single file). `--windowed` hides the console; `--uac-admin` requests admin via UAC at launch; add `--icon=app.ico` for a custom icon.

## Project layout

```
CleanSweep/
├── main.py              # Entry point: elevation + GUI
├── run.bat              # Run from source
├── build.bat            # One-click build to dist\CleanSweep.exe
├── requirements.txt     # No third-party runtime deps
├── app/
│   ├── winutil.py       # Win32 API: elevation / ShellExecuteEx / recycle bin / junction / tasklist / taskkill
│   ├── matcher.py       # Heuristic name matching (software ↔ folders/registry keys)
│   ├── scanner.py       # Registry enumeration + directory sizing
│   ├── leftovers.py     # Leftover scanning (FS + registry) + risk scoring, cancellable
│   ├── uninstaller.py   # Official uninstaller runner: 60 s timeout + heartbeat + controlled force-kill
│   ├── cleaner.py       # Deletion + hard-coded safety checks
│   ├── backup.py        # Registry export + System Restore point
│   ├── reporter.py      # Cleanup report
│   └── gui.py           # tkinter UI: batched log flush / uninstall wizard / cancellable scans
├── docs/                # Release notes
└── tools/selftest.py    # Read-only self test (deletes nothing)
```

## Self test

```bat
python tools/selftest.py
```

Read-only verification (matcher unit tests, safety-check cases, live enumeration & scan, registry export, protected-process list) — **deletes nothing**.

## Known limitations

- Microsoft Store (UWP) apps do not use the registry Uninstall mechanism and are not listed.
- Silent uninstall relies on `QuietUninstallString` or the Inno/NSIS families; otherwise the official UI is shown.
- Recycle-bin deletion may fail for very long paths (>260 chars); failures are recorded in the report.
- UI text is Chinese-first with some English; no language switcher yet.

## License

[MIT](LICENSE)
