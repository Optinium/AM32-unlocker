AM32 ESC Unlocker
-----------------

This is a tool for fast and easy unlocking of ESCs for the AM32
project.

The key benefits of using this tool over traditional debug tools are:

 - avoids the need to solder SWD cables
 - speed and simplicity
 - the same tool works with all AM32 MCU targets
 - bootloaders are included
 - works with stlink-v2, cmsis-dap and jlink adapters
 - allows for use of a custom bootloader

[![Unlocking a 4-in-1](https://i.ytimg.com/vi/8_A27GfAbSg/maxresdefault.jpg)](https://www.youtube.com/watch?v=8_A27GfAbSg "Unlocking a 4-in-1")

Background
----------

A lot of ESCs come with flash locked preventing updates. This is most
common with BLHeli32 ESCs but may apply to others as well. In order to
move these ESCs to the AM32 firmware you need to unlock them and flash
a bootloader.

Note that the best option is to buy ESCs from vendors that ship with
AM32, in which case you don't need this tool at all. This tool is for
people who have 32 bit ESCs already that shipped with firmware other
than AM32 and want to migrate to AM32.

Usage
-----

You will need to download the linux or windows binaries for
"esc_unlocker" from the releases section of this repository. Two
packaged binaries are provided, one for windows and one for linux.

 https://github.com/am32-firmware/AM32-unlocker/releases

When you run the tool you will get an interface like this:

![Unlocker GUI](docs/screen_shot.jpg "Unlocker GUI")

You then need to select your MCU type. If you don't know the MCU type
then look at the MCU on your ESC under a magnifying glass.

You also need to select your signal pin. This is the pin on the MCU
that communicates with the flight controller (ie. the DShot signal
pin). This is usually PA2 or PB4.

Finding the SWD pads
--------------------

You need to find the SWD pads on your ESC. Some ESCs don't have
exposed SWD pads, in which case you may not be able to unlock at all.

Here is an example showing the 4 sets of SWD pads on a Foxeer 4-in-1
F421 ESC:

![Foxeer 4-in-1 SWD](docs/foxeer_SWD.jpg "Foxeer 4-in-1 SWD")

Note that you don't really need to identify which is SWDIO and which
is SWDCLK as the process of using this tool makes it very easy to swap
around when unlocking.


Connecting SWD
--------------

You will need a debug adapter. The tool supports 3 types of adapter

 - stlink-v2 (or clone)
 - cmsis-dap
 - jlink

The simplest is to get one of the cheap stlink-v2 clones. These are available cheaply
from places like ebay and aliexpress.

![STLink-v2](docs/stlink-v2.jpg "STLink-v2")

You will need to connect the ground pin of the debug probe to ground on
your ESC.

Then you should get 2 male to female dupont leads. The two female ends
will connect to the SWDIO and SWDCLK pins on your debug adapter.

The two male ends should be bent slightly so that when held together
they are separated by the distance between

![Bent dupont pins](docs/bent_pins.jpg "Bent dupont pins")

Unlocking Process
-----------------

Choose the MCU, signal pin and probe, then press Start. The status panel
shows what is happening and counts attempts. It automatically retries
until success, including when contact is intermittent. Press Stop to
cancel or change settings. Repeated errors update the status without
filling the main window with identical logs.

Now power on your ESC (preferably with no motor attached!) and then
press the tips of the two dupont pins against the SWD pads. Listen for
a change in sound as the tool finds the MCU. The tone will rise in
pitch and the "LED" in the display will go orange if it finds your
MCU. If it can't find the MCU then try the pins the other way around.

After programming, OpenOCD verifies the bootloader and reads the entire
programmed image back to a file. The application compares every byte with
the selected bootloader. Success requires that comparison, the explicit
verification completion markers and a successful OpenOCD exit.

If contact is lost after writing completes, retries only reconnect and
verify; they do not erase or rewrite the ESC unless a mismatch is reported.
Keep the probe on the same ESC until success. Setup problems and unknown
errors also keep retrying, with an explanation on screen; use Stop to
change the MCU, probe or file selection.

On verified success the LED turns green, a completion tone plays, and a
popup says **Success - press Space for next ESC**. All programming stops
while this popup is open. Press **Space** or click **Next ESC (Space)** to
begin a three-second countdown: **3, 2, 1**, with one beep per second when
Sound is enabled. Move the probe to the next ESC during the countdown;
programming then starts with the same settings. **Stop** cancels the
countdown. **Done** closes the popup without restarting; the main **Next
ESC** button also starts the countdown. The completed count is for this session.

This checks that the selected **bootloader** was stored correctly. It does
not check that the selected MCU/pin image suits your board, or install or
validate the main ESC firmware. Use the normal AM32 configuration tools
to load that firmware afterwards. Lock mode reports completion of the
lock script, without claiming a bootloader readback verification.

Troubleshooting
---------------

You may need to install WinUSB via zadig if you get this error:

 - Error: libusb_open() failed with LIBUSB_ERROR_NOT_SUPPORTED

Install from https://zadig.akeo.ie/

Diagnostics
-----------

The main window shows concise status changes. Enable **Show diagnostic
details** to see the last 500 lines of raw OpenOCD output. Full diagnostic
output goes to `esc_unlocker.log` at the path shown in the window, rotating
at 5 MB with three backups. If the working directory is read-only, logs go
to `AM32-unlocker-logs` in your home directory.

Running and building this version
---------------------------------

Run `python esc_unlocker.py` with Python 3 and `intelhex` installed.
Audio is optional (`numpy` and `sounddevice`). Resources are resolved
relative to the application, so it can be launched from another folder.

To build a Windows executable in an isolated environment:

```
python -m venv .venv
.venv\Scripts\python -m pip install pyinstaller sounddevice setuptools intelhex numpy
.venv\Scripts\python package/build.py
```

The output is `esc_unlocker_windows.exe`. On Linux/macOS, use the
corresponding environment's Python executable with `package/build.py`.

Run the simulated workflow and GUI checks with
`python -m unittest discover -s tests -v`. The GUI checks require Tk and a
display; they use a fake programmer and never open a debug probe.

CAUTION
-------

It is possible that this tool could permanently brick your ESC. It
isn't likely, but there are locking options in the MCUs that are not
reversible. If the wrong options are set then you will not be able to
recover your ESC, even with a soldered SWD connection. Use at own
risk.
