#!/usr/bin/env python3
"""Human-readable AM32 ESC programming UI."""

import argparse
from collections import deque
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog

from programming import Programmer, CAN_ERASE_LAST_SECTOR

PROBE_LIST = ["ST Link", "JLink", "CMSIS-DAP"]
MCU_LIST = ["F031", "F051", "G071", "G071_64K", "G431", "L431", "E230", "F415", "F421"]
PIN_LIST = ["PA0", "PA2", "PA6", "PB4", "PA15"]


def get_resource_path(relative_path=''):
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / relative_path


def get_openocd():
    system = platform.system()
    folder = {'Windows': 'windows', 'Darwin': 'macos'}.get(system, 'linux')
    return get_resource_path('tools/%s/openocd/bin/openocd%s' % (
        folder, '.exe' if system == 'Windows' else ''))


def make_logger():
    logger = logging.getLogger('esc_unlocker')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        # Keep diagnostics even when launched from a read-only application folder.
        for folder in (Path.cwd(), Path.home() / 'AM32-unlocker-logs'):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(folder / 'esc_unlocker.log', maxBytes=5_000_000,
                                              backupCount=3, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
                logger.addHandler(handler)
                break
            except OSError:
                continue
        else:
            logger.addHandler(logging.NullHandler())
    return logger


class Audio:
    def __init__(self, log):
        self.log = log
        self.pending = queue.Queue(maxsize=4)
        self.available = False
        try:
            import numpy
            import sounddevice
            self.np = numpy
            self.sd = sounddevice
            sounddevice.query_devices(kind='output')
            self.available = True
        except Exception as exc:
            log('Audio unavailable: %s' % exc)
        if self.available:
            threading.Thread(target=self.worker, daemon=True).start()

    def play(self, frequencies):
        if self.available:
            try:
                self.pending.put_nowait(frequencies)
            except queue.Full:
                pass

    def worker(self):
        while True:
            frequencies = self.pending.get()
            try:
                samples = self.np.linspace(0, 0.1, 4410, False)
                wave = self.np.concatenate([self.np.sin(f * samples * 2 * self.np.pi) for f in frequencies])
                self.sd.play((wave * 6553).astype(self.np.int16), 44100)
                self.sd.wait()
            except Exception as exc:
                self.log('Audio error: %s' % exc)


class UnlockerApp:
    def __init__(self, root, programmer_factory=Programmer):
        self.root = root
        self.programmer_factory = programmer_factory
        self.logger = make_logger()
        self.audio = Audio(self.logger.info)
        self.events = queue.Queue()
        self.worker = None
        self.programmer = None
        self.running = False
        self.closing = False
        self.countdown_active = False
        self.countdown_after_id = None
        self.completed = 0
        self.attempts = 0
        self.generation = 0
        self.details = deque(maxlen=500)
        self.history = deque(maxlen=100)
        self.details_dirty = False
        self.dialog = None
        root.title('AM32 ESC Unlocker - verified programming')
        root.minsize(760, 600)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)
        root.protocol('WM_DELETE_WINDOW', self.quit)

        settings = ttk.LabelFrame(root, text='ESC and bootloader', padding=10)
        settings.grid(row=0, column=0, sticky='ew', padx=12, pady=10)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)
        self.mcu = tk.StringVar(value=MCU_LIST[0])
        self.probe = tk.StringVar(value=PROBE_LIST[0])
        self.pin = tk.StringVar(value=PIN_LIST[0])
        self.mode = tk.StringVar(value='Unlock')
        self.bootloader = tk.StringVar()
        self.can = tk.BooleanVar()
        self.sound = tk.BooleanVar(value=self.audio.available)
        self.show_details = tk.BooleanVar()
        self.settings_widgets = []
        for row, column, label, variable, choices in (
            (0, 0, 'MCU', self.mcu, MCU_LIST), (0, 2, 'Probe', self.probe, PROBE_LIST),
            (1, 0, 'Signal pin', self.pin, PIN_LIST), (1, 2, 'Mode', self.mode, ['Unlock', 'Lock'])):
            ttk.Label(settings, text=label).grid(row=row, column=column, padx=5, pady=5, sticky='w')
            field = ttk.Combobox(settings, textvariable=variable, values=choices, state='readonly')
            field.grid(row=row, column=column + 1, padx=5, pady=5, sticky='ew')
            self.settings_widgets.append(field)
        self.can_check = ttk.Checkbutton(settings, text='CAN bootloader', variable=self.can)
        self.can_check.grid(row=2, column=1, sticky='w', padx=5)
        self.mcu.trace_add('write', self.update_can)
        self.update_can()
        ttk.Label(settings, text='Custom bootloader').grid(row=3, column=0, padx=5, pady=5)
        entry = ttk.Entry(settings, textvariable=self.bootloader)
        entry.grid(row=3, column=1, columnspan=2, sticky='ew', padx=5)
        browse = ttk.Button(settings, text='Browse...', command=self.browse)
        browse.grid(row=3, column=3, padx=5, sticky='e')
        self.settings_widgets.extend([entry, browse])
        ttk.Label(settings, text='Included bootloaders are development builds. Stable versions: https://am32.ca/downloads',
                  wraplength=700).grid(row=4, column=0, columnspan=4, sticky='w', padx=5, pady=(8, 0))

        status = ttk.LabelFrame(root, text='Current ESC', padding=12)
        status.grid(row=1, column=0, sticky='ew', padx=12)
        status.columnconfigure(1, weight=1)
        self.canvas = tk.Canvas(status, width=28, height=28, highlightthickness=0)
        self.canvas.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.led = self.canvas.create_oval(3, 3, 25, 25, fill='gray', outline='')
        self.status = tk.StringVar(value='Ready. Select the MCU, signal pin and probe, then press Start.')
        ttk.Label(status, textvariable=self.status, font=('Segoe UI', 11, 'bold'),
                  wraplength=650).grid(row=0, column=1, sticky='w')
        self.counts = tk.StringVar()
        ttk.Label(status, textvariable=self.counts).grid(row=1, column=1, sticky='w', pady=(8, 0))
        self.update_counts()

        controls = ttk.Frame(root, padding=10)
        controls.grid(row=2, column=0, sticky='ew')
        self.start_button = ttk.Button(controls, text='Start', command=self.start)
        self.start_button.pack(side='left', padx=5)
        self.stop_button = ttk.Button(controls, text='Stop', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=5)
        ttk.Checkbutton(controls, text='Sound', variable=self.sound).pack(side='left', padx=10)
        ttk.Button(controls, text='Quit', command=self.quit).pack(side='right', padx=5)
        ttk.Checkbutton(controls, text='Show diagnostic details (last 500 lines)', variable=self.show_details,
                        command=self.render_output).pack(side='right', padx=5)
        self.log_path = next((h.baseFilename for h in self.logger.handlers if hasattr(h, 'baseFilename')), None)
        ttk.Label(root, text=('Full diagnostic log: ' + self.log_path if self.log_path else
                             'Diagnostic file unavailable; details are still shown here.'),
                  wraplength=730).grid(row=3, column=0, sticky='w', padx=15)
        self.output = scrolledtext.ScrolledText(root, wrap=tk.WORD, height=12, state='disabled')
        self.output.grid(row=4, column=0, sticky='nsew', padx=12, pady=10)
        self.summary('Ready. Retries continue until success or Stop. After success, press Space for the next ESC.')
        root.after(50, self.process_events)

    def update_can(self, *args):
        allowed = self.mcu.get().split('_')[0] in CAN_ERASE_LAST_SECTOR
        if not allowed:
            self.can.set(False)
        self.can_check.configure(state='normal' if allowed and not self.running and not self.countdown_active else 'disabled')

    def browse(self):
        path = filedialog.askopenfilename(parent=self.root, title='Custom bootloader',
                                         filetypes=[('Bootloader', '*.bin *.hex'), ('All files', '*.*')])
        if path:
            self.bootloader.set(path)

    def update_counts(self):
        self.counts.set('Attempt: %d    |    Completed this session: %d' % (self.attempts, self.completed))

    def set_status(self, message, color):
        self.status.set(message)
        self.canvas.itemconfigure(self.led, fill=color)

    def summary(self, message):
        self.history.append('%s  %s\n' % (datetime.now().strftime('%H:%M:%S'), message))
        self.logger.info(message)
        if not self.show_details.get():
            self.render_output()

    def render_output(self):
        self.output.configure(state='normal')
        self.output.delete('1.0', tk.END)
        self.output.insert(tk.END, ''.join(self.details if self.show_details.get() else self.history))
        self.output.see(tk.END)
        self.output.configure(state='disabled')
        self.details_dirty = False

    def set_controls(self, active):
        self.start_button.configure(state='disabled' if active else 'normal')
        self.stop_button.configure(state='normal' if active else 'disabled')
        for widget in self.settings_widgets:
            widget.configure(state='disabled' if active else ('readonly' if isinstance(widget, ttk.Combobox) else 'normal'))
        self.update_can()

    def start(self):
        if self.running or self.countdown_active or self.closing or self.dialog is not None:
            return
        self.running = True  # Set before starting the thread: double-click cannot launch twice.
        self.generation += 1
        generation = self.generation
        self.attempts = 0
        self.details.clear()
        self.history.clear()
        self.update_counts()
        self.set_controls(True)
        self.start_button.configure(text='Start', command=self.start)
        self.set_status('Connecting to ESC. Hold the SWD pins against the pads.', 'red')
        params = {'mcu_type': self.mcu.get(), 'probe_type': self.probe.get(), 'pin': self.pin.get(),
                  'mode': self.mode.get(), 'custom_bootloader': self.bootloader.get().strip(), 'can': self.can.get()}
        self.active_mode = params['mode']
        self.summary('Starting ESC %d: %s, %s, %s.' % (self.completed + 1, params['mode'], params['mcu_type'], params['pin']))
        self.programmer = self.programmer_factory(get_resource_path(), get_openocd(),
            lambda kind, **data: self.events.put((generation, kind, data)), self.logger.info)
        self.worker = threading.Thread(target=self.programmer.run, args=(params,), daemon=True)
        self.worker.start()

    def begin_countdown(self):
        if self.running or self.countdown_active or self.closing or self.dialog is not None:
            return
        self.countdown_active = True
        self.set_controls(True)
        self.summary('Next ESC starts after a three-second countdown. Press Stop to cancel.')
        self.countdown_tick(3)

    def countdown_tick(self, remaining):
        self.countdown_after_id = None
        if not self.countdown_active or self.closing:
            return
        if remaining == 0:
            self.countdown_active = False
            self.start()
            return
        self.set_status('Next ESC starts in %d... Move the probe now. Stop cancels.' % remaining, 'green')
        if self.sound.get():
            self.audio.play([880 if remaining > 1 else 1100])
        self.countdown_after_id = self.root.after(1000, self.countdown_tick, remaining - 1)

    def cancel_countdown(self):
        self.countdown_active = False
        if self.countdown_after_id is not None:
            self.root.after_cancel(self.countdown_after_id)
            self.countdown_after_id = None

    def stop(self):
        if self.countdown_active:
            self.cancel_countdown()
            self.set_controls(False)
            self.set_status('Countdown cancelled. The completed ESC remains verified.', 'green')
            self.summary('Countdown cancelled. Press Next ESC when ready to try again.')
            return
        if self.running:
            self.programmer.stop.set()
            self.stop_button.configure(state='disabled')
            self.set_status('Stopping. Waiting for the current attempt to close...', 'gray')

    def handle_event(self, kind, data):
        if kind == 'detail':
            self.details.extend(data['text'].splitlines(keepends=True))
            self.details_dirty = True
        elif kind == 'image':
            self.summary('Selected bootloader: %s (%d bytes).' % (data['name'], data['size']))
        elif kind == 'attempt':
            self.attempts = data['number']
            self.update_counts()
            # Keep the previous retry explanation visible during reconnection.
        elif kind == 'stage':
            self.set_status(data['message'], 'orange')
            if self.sound.get() and data['message'].startswith('ESC detected'):
                self.audio.play([880])
        elif kind == 'retry':
            self.set_status(data['message'], 'red')
            if data['changed']:
                self.summary(data['message'])
            if self.sound.get():
                self.audio.play([300])
        elif kind == 'finished':
            self.running = False
            self.set_controls(False)
            self.summary(data['message'])
            outcome = data['outcome']
            self.set_status(data['message'], 'green' if outcome == 'success' else 'red' if outcome == 'error' else 'gray')
            if outcome == 'success':
                self.completed += 1
                self.update_counts()
                self.start_button.configure(text='Next ESC', command=self.begin_countdown)
                if self.sound.get():
                    self.audio.play([600, 800, 1000])
                self.show_success(data['message'])

    def process_events(self):
        if self.closing:
            return
        for _ in range(1000):
            try:
                generation, kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if generation == self.generation:
                # Once Stop is requested, queued progress must not overwrite it.
                if self.programmer.stop.is_set() and kind not in ('finished', 'detail'):
                    continue
                self.handle_event(kind, data)
        if self.show_details.get() and self.details_dirty:
            self.render_output()
        self.root.after(50, self.process_events)

    def show_success(self, message):
        dialog = tk.Toplevel(self.root)
        self.dialog = dialog
        dialog.title('Success - press Space for next ESC')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        ttk.Label(dialog, text='ESC %d complete' % self.completed,
                  font=('Segoe UI', 14, 'bold')).pack(padx=25, pady=(20, 10))
        ttk.Label(dialog, text=message + '\n\nPress Space for next ESC. A three-beep countdown gives you\nthree seconds to move the probe before programming starts.',
                  justify='center', wraplength=480).pack(padx=25, pady=10)
        buttons = ttk.Frame(dialog)
        buttons.pack(pady=(10, 20))

        def close(next_esc=False):
            if self.dialog is not dialog:
                return 'break'
            dialog.grab_release()
            dialog.destroy()
            self.dialog = None
            if next_esc:
                self.begin_countdown()
            return 'break'

        ttk.Button(buttons, text='Done', command=close).pack(side='left', padx=8)
        ttk.Button(buttons, text='Next ESC (Space)', command=lambda: close(True)).pack(side='left', padx=8)
        dialog.protocol('WM_DELETE_WINDOW', close)
        dialog.grab_set()
        # Handle Space before a focused button's default keyboard binding, so it
        # always means Next and cannot accidentally activate Done or start twice.
        def bind_space(widget):
            widget.bind('<KeyPress-space>', lambda event: 'break')
            widget.bind('<KeyRelease-space>', lambda event: close(True))
            for child in widget.winfo_children():
                bind_space(child)

        bind_space(dialog)
        dialog.after_idle(dialog.focus_set)

    def quit(self):
        if self.closing:
            return
        self.closing = True
        self.cancel_countdown()
        if self.programmer is not None:
            self.programmer.stop.set()
        self.root.withdraw()
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None
        self.wait_for_exit()

    def wait_for_exit(self):
        if self.worker is not None and self.worker.is_alive():
            self.root.after(50, self.wait_for_exit)
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='AM32 ESC Unlocker')
    parser.add_argument('--mcu', choices=MCU_LIST)
    parser.add_argument('--port', '--pin', dest='pin', choices=PIN_LIST)
    parser.add_argument('--probe', choices=PROBE_LIST)
    parser.add_argument('--mode', choices=['Unlock', 'Lock'])
    parser.add_argument('--bootloader')
    parser.add_argument('--can', action='store_true')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--test-audio', action='store_true')
    parser.add_argument('--exit-after', type=float, default=0)
    args, _ = parser.parse_known_args()
    root = tk.Tk()
    app = UnlockerApp(root)
    for option, variable in ((args.mcu, app.mcu), (args.pin, app.pin), (args.probe, app.probe),
                             (args.mode, app.mode), (args.bootloader, app.bootloader)):
        if option:
            variable.set(option)
    if args.can:
        app.can.set(True)
        app.update_can()
    if args.test_audio:
        app.audio.play([880, 660])
    if args.start:
        root.after(500, app.start)
    if args.exit_after > 0:
        root.after(int(args.exit_after * 1000), app.quit)
    root.mainloop()


if __name__ == '__main__':
    main()
