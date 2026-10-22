# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AM32 ESC Unlocker - a Python/Tkinter GUI tool for unlocking flash protection on ARM Cortex-M0 ESCs and flashing AM32 bootloaders. Eliminates the need to solder SWD cables by providing a simple GUI-driven workflow using OpenOCD and debug adapters (ST-Link v2, CMSIS-DAP, JLink).

## Build Commands

**Install dependencies:**
```
pip install pyinstaller sounddevice setuptools intelhex numpy
```

**Build standalone executable (via PyInstaller):**
```
python package/build.py
```
`package/build.py` is the canonical build: it discovers every `MCU/*` directory and bundles `bootloaders/`, `probes/`, and only the *current* platform's `tools/{platform}` via `--add-data`, then copies the result to `esc_unlocker_{linux,windows,macos}`. The `esc_unlocker.spec` is a generated artifact (git-ignored) — don't hand-edit it; regenerate via `build.py`.

**Run directly:**
```
python esc_unlocker.py
```

CI builds for Linux/Windows/macOS are in `.github/workflows/`.
Run `python -m unittest discover -s tests -v` for simulated backend, Tcl-script,
and Tk GUI tests. Tests never use a debug probe; GUI tests require a display.

## Architecture

`esc_unlocker.py` contains `UnlockerApp`, the Tk UI, CLI options, optional audio,
and rotating diagnostic logging. `programming.py` contains `Programmer`, the
retry state machine, per-attempt log interpretation and subprocess management.
Worker events go through a queue; only the main thread touches Tk.

One `Programmer.run()` handles one ESC. It retries all failures until verified
success or explicit Stop. After `AM32_WRITE_COMPLETE`, reconnect attempts use
`openocd-verify.cfg` and do not erase/write unless verification reports a mismatch.
Success requires an exit code of zero, explicit verification/readback markers and
an exact byte comparison of a fresh readback against a snapshot of the chosen
image. A zero exit code or legacy `Success!` message alone is insufficient.

A successful run returns before the popup opens. Success automatically starts
a three-second countdown with one beep per second before starting the next ESC
with the same selections. Stop/Quit, Escape or closing the popup cancel the
countdown. There is no Space confirmation. Tk `after` keeps the UI responsive.
Each attempt has a 60-second timeout, and Stop/Quit terminate and reap the child.
Setup and unknown errors keep retrying with readable status messages.

**OpenOCD config files per MCU** (`MCU/{type}/`):
- `openocd-unlock.cfg` — disables flash protection, erases sectors, programs bootloader
- `openocd-lock.cfg` — re-enables flash protection
- These configs contain the actual flash manipulation logic (register writes, option byte changes)

**Supported MCUs** (`MCU_LIST` in `esc_unlocker.py`): F031, F051, G071, G071_64K, G431, L431, E230, F415, F421 — each with different unlock procedures (STM32 vs Arterytek AT32 vs Geehy). The selected MCU string is split on `_`: the part before `_` is the directory/config base (`G071`), the suffix becomes a `k_tag` (`_64K`) appended to the bootloader filename. So a flash-size or feature variant is modeled as a `MCU_LIST` entry sharing one `MCU/<base>/` config dir, distinguished only by the bootloader file it picks.

**Bootloaders** (`bootloaders/`): Pre-compiled `.bin` files named `AM32_{base}_BOOTLOADER_{PIN}{k_tag}_V17.bin` (e.g. `AM32_G071_BOOTLOADER_PA6_64K_V17.bin`). Auto-selected from MCU base + signal pin + tag. A **custom bootloader** chosen via the Browse field overrides auto-selection; if it ends in `.hex` it is converted to a temp `.bin` using `intelhex.hex2bin` before flashing. The included bins are dev builds — the GUI points users at https://am32.ca/downloads for stable bootloaders via the custom field.

**Adding a new MCU**: add `MCU/<base>/openocd-{unlock,lock,verify}.cfg`,
matching bootloader files, and the name in `MCU_LIST`. After writing, unlock
scripts must emit `AM32_WRITE_COMPLETE` and source the common verification
script. Lock scripts must emit `AM32_LOCK_COMPLETE` only after completion.
`build.py` auto-discovers MCU directories, including `common`.

**Platform-specific OpenOCD** (`tools/{linux,macos,windows}/openocd/`): Bundled
custom OpenOCD builds that include Arterytek AT32 support.

## Key Design Details

- Resources resolve relative to the source file or PyInstaller's `_MEIPASS`.
- Tcl arguments use `tcl_string()`; filesystem paths themselves are not escaped.
- BIN/HEX images are copied/converted into a closed temporary binary snapshot.
  HEX bootloaders must begin at 0x08000000 or offset 0. Lock mode needs no image.
- Raw stdout/stderr are merged and drained live, avoiding a two-pipe deadlock.
- The main view keeps 100 concise messages; diagnostic view keeps 500 raw lines.
- `esc_unlocker.log` rotates at 5 MB with three backups; its path is shown in the UI.
- Audio uses a bounded queue and a separate thread and is optional.
- The build uses the active Python environment, with no shell invocation.
