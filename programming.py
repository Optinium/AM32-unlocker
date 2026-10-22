"""OpenOCD programming workflow, independent of Tk and of attached hardware."""

from dataclasses import dataclass
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time


PROBES = {"ST Link": "stlink", "JLink": "jlink", "CMSIS-DAP": "cmsis-dap"}
CAN_ERASE_LAST_SECTOR = {"F415": 15, "G431": 8, "L431": 8}


def tcl_string(value):
    """Quote one literal Tcl argument, including Windows paths and $/[ characters."""
    value = str(value).replace("\\", "/")
    for char in ('$', '[', ']', '"'):
        value = value.replace(char, "\\" + char)
    return '"' + value + '"'


@dataclass
class AttemptResult:
    returncode: int
    timed_out: bool = False


class AttemptLog:
    """Interpret each attempt separately; never accept evidence from an earlier ESC."""

    def __init__(self):
        self.markers = set()
        self.found = False
        self.error = ""
        self.fatal = ""
        self.retry_reason = ""
        self.mismatch = False

    def feed(self, line):
        text = line.strip()
        lower = text.lower()
        if text.startswith("AM32_"):
            self.markers.add(text)
        if "cortex-m" in lower:
            self.found = True
        if re.search(r"\berror\s*:", lower):
            self.error = text

        # These cannot be fixed by repeatedly touching the SWD pads.
        if "libusb_error_not_supported" in lower or "libusb_error_access" in lower:
            self.fatal = "USB driver/access problem. Check the probe driver and close other programming tools."
        elif any(s in lower for s in ("can't find", "couldn't open", "could not open file",
                                      "invalid command name", "unknown command", "no such file")):
            self.fatal = "OpenOCD configuration or file error. Check the selected files and diagnostic log."
        elif "no device found" in lower or "unable to find a matching" in lower or "open failed" in lower:
            self.retry_reason = "Probe not found. Check its USB connection."
        elif any(s in lower for s in ("contents differ", "verify failed", "verification failed",
                                      "checksum mismatch", "verify mismatch")):
            self.mismatch = True
            self.retry_reason = "Verification did not match the selected bootloader."
        elif "cannot identify target" in lower or "auto_probe failed" in lower:
            self.retry_reason = "MCU identification failed. Reseat the SWD pins and check the selected MCU type."
        elif "unable to connect to the target" in lower or "examination failed" in lower:
            self.retry_reason = "Waiting for ESC contact. Check power and hold the SWD pins against the pads."
        elif any(s in lower for s in ("communication failure", "read_memory", "failed to read memory",
                                      "polling target", "timed out", "timeout", "target not halted",
                                      "failed to write memory", "error writing", "failed to erase",
                                      "flash write failed", "target was not halted")):
            self.retry_reason = "Contact was lost. Hold the SWD pins steady."

    @property
    def write_complete(self):
        return "AM32_WRITE_COMPLETE" in self.markers

    @property
    def verified(self):
        return {"AM32_VERIFY_COMPLETE", "AM32_READBACK_COMPLETE"} <= self.markers


def run_attempt(command, stop, on_line, timeout=60):
    """Drain a merged output pipe live; cancellation and timeouts always reap the child."""
    options = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", **options)
    lines = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while True:
            if stop.is_set() or time.monotonic() >= deadline:
                timed_out = not stop.is_set()
                break
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                # A child may close its pipe before exiting; keep Stop responsive.
                while process.poll() is None:
                    if stop.wait(0.05) or time.monotonic() >= deadline:
                        timed_out = not stop.is_set()
                        break
                break
            on_line(line)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait()
        reader.join(timeout=2)
        process.stdout.close()
    # Include output written just before termination, for diagnostics and write markers.
    while not lines.empty():
        line = lines.get_nowait()
        if line is not None:
            on_line(line)
    return AttemptResult(process.returncode, timed_out)


class Programmer:
    """One ESC per run. A successful run returns and can never restart itself."""

    def __init__(self, resource_dir, openocd, emit, log, runner=run_attempt, retry_delay=0.5):
        self.resources = Path(resource_dir)
        self.openocd = Path(openocd)
        self.emit = emit
        self.log = log
        self.runner = runner
        self.retry_delay = retry_delay
        self.stop = threading.Event()

    def prepare(self, params, directory):
        base, _, variant = params['mcu_type'].partition('_')
        mode = params['mode'].lower()
        config = self.resources / 'MCU' / base / ('openocd-%s.cfg' % mode)
        verify = self.resources / 'MCU' / base / 'openocd-verify.cfg'
        probe = self.resources / 'probes' / (PROBES[params['probe_type']] + '.cfg')
        for path in (self.openocd, config, probe):
            if not path.is_file():
                raise ValueError("Required file is missing: %s" % path)
        command = [str(self.openocd), '-s', str(self.resources),
                   '-c', 'gdb_port disabled; tcl_port disabled; telnet_port disabled']
        expected = None
        if mode == 'unlock':
            if not verify.is_file():
                raise ValueError("Verification configuration is missing: %s" % verify)
            use_can = params['can'] and base in CAN_ERASE_LAST_SECTOR
            name = 'AM32_%s_BOOTLOADER_%s%s%s_V17.bin' % (
                base, params['pin'], '_CAN' if use_can else '', '_' + variant if variant else '')
            source = Path(params['custom_bootloader']) if params['custom_bootloader'] else self.resources / 'bootloaders' / name
            snapshot = directory / 'bootloader.bin'
            if source.suffix.lower() == '.hex':
                from intelhex import IntelHex
                firmware = IntelHex(str(source))
                if firmware.minaddr() not in (0, 0x08000000):
                    raise ValueError("HEX bootloader must start at flash address 0x08000000 (or offset 0).")
                if firmware.maxaddr() - firmware.minaddr() >= 512 * 1024:
                    raise ValueError("HEX bootloader spans more than 512 KiB; check the selected file.")
                firmware.tobinfile(str(snapshot))
                expected = snapshot.read_bytes()
            else:
                expected = source.read_bytes()
                snapshot.write_bytes(expected)
            if not expected:
                raise ValueError("The selected bootloader is empty.")
            command += ['-c', 'set BOOTLOADER ' + tcl_string(snapshot)]
            if use_can:
                command += ['-c', 'set ERASE_LAST_SECTOR %d' % CAN_ERASE_LAST_SECTOR[base]]
            self.emit('image', name=source.name, size=len(expected))
        command += ['--file', str(probe)]
        return command, config, verify, expected

    def run(self, params):
        outcome = 'stopped'
        message = 'Stopped. This ESC has not been confirmed successful.'
        attempts = 0
        phase = 'lock' if params['mode'] == 'Lock' else 'program'
        last_reason = None

        def retry(reason, delay=None):
            nonlocal last_reason
            self.emit('retry', message=reason, phase=phase, changed=reason != last_reason)
            if reason != last_reason:
                self.log(reason)
            last_reason = reason
            self.stop.wait(self.retry_delay if delay is None else delay)

        with tempfile.TemporaryDirectory(prefix='am32_') as temporary:
            directory = Path(temporary)
            command = None
            while not self.stop.is_set():
                if command is None:
                    try:
                        command, config, verify, expected = self.prepare(params, directory)
                    except Exception as exc:
                        retry('Waiting for setup: %s. Retrying automatically; press Stop to change settings.' % exc,
                              max(1, self.retry_delay))
                        continue
                attempts += 1
                self.emit('attempt', number=attempts, phase=phase)
                self.log('Attempt %d (%s)' % (attempts, phase))
                evidence = AttemptLog()
                # Reuse a single path but remove the previous attempt's file first.
                # Old or partial readbacks must never count as a fresh verification.
                readback = directory / 'readback.bin'
                args = command + ['-c', 'set READBACK ' + tcl_string(readback),
                                  '--file', str(verify if phase == 'verify' else config)]
                self.log('Command: ' + repr(args))
                seen_stages = set()

                def on_line(line):
                    self.log(line.rstrip())
                    self.emit('detail', text=line)
                    evidence.feed(line)
                    stripped = line.strip()
                    stage = None
                    if stripped == 'AM32_VERIFY_START':
                        stage = 'Reading back and verifying. Keep the pins steady.'
                    elif stripped == 'Load bootloader':
                        stage = 'Programming bootloader. Keep the pins steady.'
                    elif stripped.startswith('erase '):
                        stage = 'Erasing flash. Keep the pins steady.'
                    elif evidence.found and 'found' not in seen_stages:
                        seen_stages.add('found')
                        stage = 'ESC detected. Keep the pins steady.'
                    if stage and stage not in seen_stages:
                        seen_stages.add(stage)
                        self.emit('stage', message=stage)

                try:
                    readback.unlink(missing_ok=True)
                    result = self.runner(args, self.stop, on_line)
                    self.log('Exit code: %s; timeout: %s' % (result.returncode, result.timed_out))
                    if self.stop.is_set():
                        break
                    if phase == 'lock':
                        complete = ('AM32_LOCK_COMPLETE' in evidence.markers and not evidence.error)
                    else:
                        complete = evidence.verified
                        if complete:
                            if readback.is_file():
                                actual = readback.read_bytes()
                                complete = actual == expected
                                if len(actual) == len(expected) and not complete:
                                    evidence.mismatch = True
                                if not complete:
                                    evidence.retry_reason = 'Readback did not match. Hold the pins steady.'
                            else:
                                complete = False
                                evidence.retry_reason = 'No readback file was returned. Retrying verification.'
                    if result.returncode == 0 and not result.timed_out and complete and not evidence.fatal:
                        outcome = 'success'
                        message = ('Lock operation completed.' if phase == 'lock' else
                                   'Bootloader programmed and verified: all %d bytes match.' % len(expected))
                        break
                    # After writing, reconnect only to read/verify. Erase/write again
                    # only when a complete read or OpenOCD reports a mismatch.
                    if phase != 'lock':
                        if evidence.mismatch:
                            phase = 'program'
                        elif evidence.write_complete:
                            phase = 'verify'
                    reason = evidence.fatal or evidence.retry_reason
                    if result.timed_out:
                        reason = 'OpenOCD timed out. Reconnecting; keep the pins steady.'
                    if evidence.mismatch:
                        reason = 'Verification mismatch. Retrying programming; keep the pins steady.'
                    if not reason:
                        # A zero exit code, Cortex banner or "Success!" alone is not proof.
                        reason = 'No confirmed success yet. Retrying; check diagnostic details if this persists.'
                    if phase == 'verify':
                        reason += ' Retrying verification only.'
                    retry(reason, max(1, self.retry_delay) if evidence.fatal else None)
                except Exception as exc:
                    if evidence.write_complete and phase != 'lock':
                        phase = 'verify'
                    retry('Attempt could not finish: %s. Retrying automatically.' % exc,
                          max(1, self.retry_delay))
        # Delivered after cleanup, so Next cannot overlap a previous OpenOCD process.
        self.log(message)
        self.emit('finished', outcome=outcome, message=message, attempts=attempts)
