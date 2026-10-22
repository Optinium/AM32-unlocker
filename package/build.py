#!/usr/bin/env python3
import os
import subprocess
import sys
import platform
import shutil
import shlex
from pathlib import Path

# Use this checkout and this Python environment, including on UNC shares.
os.chdir(Path(__file__).resolve().parents[1])

is_windows = platform.system() == "Windows"
is_macos = platform.system() == "Darwin"

# Define the path to the MCU directory
MCUPath = "MCU"

# Initialize the options for PyInstaller
options = "--noconfirm --onefile --windowed --collect-all sounddevice --add-data bootloaders:bootloaders --add-data probes:probes"

if is_windows:
    options += " --add-data=tools/windows:tools/windows"
elif is_macos:
    options += " --add-data=tools/macos:tools/macos"
else:
    options += " --add-data=tools/linux:tools/linux"

# Get the list of subdirectories in the MCU directory
if os.path.exists(MCUPath):
    mcus = [name for name in os.listdir(MCUPath) if os.path.isdir(os.path.join(MCUPath, name))]

    # Loop through each MCU directory and add it to the PyInstaller options
    for m in mcus:
        mcu_dir = os.path.join(MCUPath, m)
        if os.path.isdir(mcu_dir):
            print(f"Adding MCU {m}")
            options += f" --add-data \"{Path(mcu_dir).as_posix()}:{Path(mcu_dir).as_posix()}\""
else:
    print(f"Error: The directory '{MCUPath}' does not exist.")
    sys.exit(1)

# Run PyInstaller with the accumulated options
try:
    subprocess.run([sys.executable, "-m", "PyInstaller", *shlex.split(options), "esc_unlocker.py"], check=True)
except subprocess.CalledProcessError as e:
    print(f"Error: PyInstaller failed with exit code {e.returncode}")
    sys.exit(1)

if is_windows:
    src_file = "dist/esc_unlocker.exe"
    release_file = "esc_unlocker_windows.exe"
elif is_macos:
    src_file = "dist/esc_unlocker"
    release_file = "esc_unlocker_macos"
else:
    src_file = "dist/esc_unlocker"
    release_file = "esc_unlocker_linux"

shutil.copy(src_file, release_file)
print(f"Created {release_file}")
