"""Exercise retries and completion without opening a debug probe."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter
import unittest

from programming import AttemptLog, AttemptResult, Programmer, run_attempt, tcl_string

ROOT = Path(__file__).resolve().parents[1]
GOOD = 'AM32_WRITE_COMPLETE\nAM32_VERIFY_START\nAM32_READBACK_COMPLETE\nAM32_VERIFY_COMPLETE\nSuccess!\n'
VERIFY_GOOD = GOOD.replace('AM32_WRITE_COMPLETE\n', '')
NO_CONTACT = 'Info : Target voltage: 3.258863\nError: init mode failed (unable to connect to the target)\n'
LOST_CONTACT = ('Info : [stm32f0x.cpu] Cortex-M0 r0p0 processor detected\n'
                'Error: jtag status contains invalid mode value - communication failure\n'
                'Error: read_memory: read at 0x40015804 with width=32 and count=1 failed\n')


def command_value(command, name):
    for argument in command:
        if argument.startswith('set %s ' % name):
            return Path(tkinter.Tcl().splitlist(argument)[2])
    raise AssertionError('Missing Tcl variable ' + name)


class WorkflowTests(unittest.TestCase):
    def run_steps(self, steps, mode='Unlock', **overrides):
        events, commands = [], []
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'custom bootloader.bin'
            image.write_bytes(bytes(range(256)) * 16)
            expected = image.read_bytes()

            def runner(command, stop, on_line):
                commands.append(command)
                self.assertTrue(steps, 'Unexpected extra programming attempt')
                step = steps.pop(0)
                if 'readback' in step:
                    data = expected if step['readback'] == 'match' else step['readback']
                    command_value(command, 'READBACK').write_bytes(data)
                for line in step.get('log', '').splitlines(keepends=True):
                    on_line(line)
                if step.get('stop'):
                    stop.set()
                return AttemptResult(step.get('code', 1), step.get('timeout', False))

            def emit(kind, **data):
                events.append((kind, data))
                if kind == 'retry' and not steps:
                    programmer.stop.set()  # Test harness cannot retry forever on regressions.

            programmer = Programmer(ROOT, Path(sys.executable), emit, lambda text: None,
                                    runner=runner, retry_delay=0)
            params = dict(mcu_type='F031', mode=mode, probe_type='ST Link', pin='PA2',
                          can=False, custom_bootloader=str(image))
            params.update(overrides)
            programmer.run(params)
        return events, commands

    def test_user_log_retries_without_success_or_repeated_summary(self):
        events, commands = self.run_steps(
            [{'log': NO_CONTACT}] * 30 + [{'log': LOST_CONTACT},
             {'log': 'Warn : target stm32f0x.cpu examination failed\n'},
             {'log': 'unlocking\nWarn : Cannot identify target as a STM32 family.\nError: auto_probe failed\n'},
             {'log': GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual(len(commands), 34)
        retries = [data for kind, data in events if kind == 'retry']
        self.assertEqual(sum(data['changed'] for data in retries[:30]), 1)
        finishes = [data for kind, data in events if kind == 'finished']
        self.assertEqual(len(finishes), 1)
        self.assertEqual(finishes[0]['outcome'], 'success')

    def test_zero_exit_and_legacy_success_are_not_proof(self):
        events, commands = self.run_steps([
            {'log': 'Cortex-M0 processor detected\nSuccess!\n', 'code': 0},
            {'log': GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual(len(commands), 2)
        self.assertEqual(events[-1][1]['outcome'], 'success')

    def test_read_failure_retries_only_verification(self):
        events, commands = self.run_steps([
            {'log': 'AM32_WRITE_COMPLETE\n' + LOST_CONTACT},
            {'log': NO_CONTACT},
            {'log': VERIFY_GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual([Path(command[-1]).name for command in commands],
                         ['openocd-unlock.cfg', 'openocd-verify.cfg', 'openocd-verify.cfg'])
        self.assertEqual(events[-1][1]['outcome'], 'success')

    def test_byte_mismatch_reprograms(self):
        events, commands = self.run_steps([
            {'log': GOOD, 'readback': bytes(4096), 'code': 0},
            {'log': GOOD, 'readback': 'match', 'code': 0}])
        self.assertTrue(all(Path(command[-1]).name == 'openocd-unlock.cfg' for command in commands))
        self.assertIn('mismatch', next(d['message'] for k, d in events if k == 'retry'))
        self.assertEqual(events[-1][1]['outcome'], 'success')

    def test_openocd_mismatch_returns_from_verify_to_program(self):
        events, commands = self.run_steps([
            {'log': 'AM32_WRITE_COMPLETE\n' + LOST_CONTACT},
            {'log': 'Error: contents differ\n'},
            {'log': GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual([Path(c[-1]).name for c in commands],
                         ['openocd-unlock.cfg', 'openocd-verify.cfg', 'openocd-unlock.cfg'])

    def test_missing_or_partial_readback_never_succeeds(self):
        events, commands = self.run_steps([
            {'log': GOOD, 'readback': b'short', 'code': 0},
            {'log': VERIFY_GOOD, 'code': 0},  # Previous partial file is removed.
            {'log': VERIFY_GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual(len(commands), 3)
        self.assertEqual(events[-1][1]['outcome'], 'success')

    def test_nonzero_exit_cannot_report_success(self):
        events, commands = self.run_steps([
            {'log': GOOD, 'readback': 'match', 'code': 1},
            {'log': VERIFY_GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual(len(commands), 2)
        self.assertEqual(Path(commands[1][-1]).name, 'openocd-verify.cfg')

    def test_timeout_retries_and_stop_overrides_success(self):
        events, commands = self.run_steps([
            {'log': '', 'timeout': True},
            {'log': GOOD, 'readback': 'match', 'code': 0, 'stop': True}])
        self.assertEqual(len(commands), 2)
        self.assertEqual(events[-1][1]['outcome'], 'stopped')

    def test_driver_and_unknown_errors_still_retry(self):
        events, commands = self.run_steps([
            {'log': 'Error: libusb_open() failed with LIBUSB_ERROR_NOT_SUPPORTED\n'},
            {'log': 'Error: new unknown failure\n'},
            {'log': GOOD, 'readback': 'match', 'code': 0}])
        self.assertEqual(len(commands), 3)
        self.assertEqual(events[-1][1]['outcome'], 'success')

    def test_lock_is_not_claimed_as_bootloader_verified(self):
        events, commands = self.run_steps([
            {'log': 'AM32_LOCK_COMPLETE\n', 'code': 0}], mode='Lock',
            custom_bootloader='does-not-exist.hex')
        self.assertEqual(events[-1][1]['outcome'], 'success')
        self.assertEqual(events[-1][1]['message'], 'Lock operation completed.')

    def test_setup_problem_remains_retrying_and_can_be_stopped(self):
        events, commands = self.run_steps([], custom_bootloader='does-not-exist.bin')
        self.assertEqual(commands, [])
        self.assertTrue(any(k == 'retry' and 'Waiting for setup' in d['message'] for k, d in events))
        self.assertEqual(events[-1][1]['outcome'], 'stopped')


class ProcessTests(unittest.TestCase):
    def test_stdout_stderr_are_drained_without_deadlock(self):
        lines = []
        script = "import sys; sys.stderr.write('diagnostic\\n' * 20000); print('AM32_VERIFY_COMPLETE')"
        result = run_attempt([sys.executable, '-c', script], threading.Event(), lines.append, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(len(lines), 20001)

    def test_hung_child_times_out_and_is_reaped(self):
        lines = []
        start = time.monotonic()
        result = run_attempt([sys.executable, '-u', '-c', "import time; print('ready'); time.sleep(30)"],
                             threading.Event(), lines.append, timeout=0.4)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - start, 4)
        self.assertIn('ready\n', lines)

    def test_stop_terminates_blocked_child(self):
        stop = threading.Event()
        timer = threading.Timer(0.3, stop.set)
        timer.start()
        try:
            result = run_attempt([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 stop, lambda line: None, timeout=10)
        finally:
            timer.cancel()
        self.assertFalse(result.timed_out)
        self.assertNotEqual(result.returncode, 0)

    def test_path_characters_are_literal_tcl(self):
        path = 'C:/custom folder/$boot[loader]"name.bin'
        interpreter = tkinter.Tcl()
        interpreter.eval('set BOOTLOADER ' + tcl_string(path))
        self.assertEqual(interpreter.getvar('BOOTLOADER'), path)


if __name__ == '__main__':
    unittest.main()
