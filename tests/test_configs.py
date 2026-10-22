"""Run MCU scripts in Tcl with hardware commands replaced by recording stubs."""
from pathlib import Path
import tempfile
import tkinter
import unittest

from programming import Programmer

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def execute_config(self, config, fail_verify=False):
        interpreter = tkinter.Tcl()
        messages, flash_calls, targets = [], [], []
        interpreter.createcommand('record_echo', messages.append)
        interpreter.createcommand('record_flash', lambda *args: flash_calls.append(args))
        interpreter.createcommand('record_target', targets.append)
        interpreter.eval('''
            proc echo {message} { record_echo $message }
            proc find {path} { return $path }
            rename source actual_source
            proc source {path} {
                if {[string match "target/*" $path]} { record_target $path; return }
                uplevel 1 [list actual_source [file join $::RESOURCE_ROOT $path]]
            }
            foreach name {init reset halt wait_halt sleep mww mwh stm32f0x stm32f1x
                          stm32l4x at32f415xx.cpu at32f421xx.cpu exit} {
                proc $name {args} { return 0 }
            }
            proc mrw {args} { return 0 }
            proc flash {args} {
                record_flash {*}$args
                if {$::FAIL_VERIFY && [lindex $args 0] eq "verify_bank"} {
                    error "contents differ"
                }
            }
        ''')
        with tempfile.TemporaryDirectory() as temporary:
            bootloader = Path(temporary) / 'bootloader.bin'
            bootloader.write_bytes(b'1234567890')
            interpreter.setvar('RESOURCE_ROOT', ROOT.as_posix())
            interpreter.setvar('BOOTLOADER', bootloader.as_posix())
            interpreter.setvar('READBACK', (Path(temporary) / 'readback.bin').as_posix())
            interpreter.setvar('FAIL_VERIFY', int(fail_verify))
            error = None
            try:
                interpreter.eval(config.read_text())
            except tkinter.TclError as exc:
                error = str(exc)
        return messages, flash_calls, targets, error

    def test_every_unlock_marks_success_only_after_verify_and_readback(self):
        for config in (ROOT / 'MCU').glob('*/openocd-unlock.cfg'):
            with self.subTest(mcu=config.parent.name):
                messages, calls, targets, error = self.execute_config(config)
                self.assertIsNone(error)
                operations = [call[0] for call in calls]
                self.assertLess(operations.index('write_bank'), operations.index('verify_bank'))
                self.assertLess(operations.index('verify_bank'), operations.index('read_bank'))
                self.assertEqual(calls[-1][0], 'read_bank')
                self.assertEqual(calls[-1][-2:], ('0', '10'))
                self.assertLess(messages.index('AM32_WRITE_COMPLETE'), messages.index('AM32_VERIFY_START'))
                self.assertLess(messages.index('AM32_READBACK_COMPLETE'), messages.index('AM32_VERIFY_COMPLETE'))
                self.assertIn('Success!', messages)

    def test_failed_verify_never_prints_completion(self):
        for config in (ROOT / 'MCU').glob('*/openocd-unlock.cfg'):
            with self.subTest(mcu=config.parent.name):
                messages, calls, targets, error = self.execute_config(config, fail_verify=True)
                self.assertEqual(error, 'contents differ')
                self.assertNotIn('AM32_VERIFY_COMPLETE', messages)
                self.assertNotIn('Success!', messages)
                self.assertNotIn('read_bank', [call[0] for call in calls])

    def test_every_verify_config_only_reads_and_uses_same_target(self):
        for config in (ROOT / 'MCU').glob('*/openocd-verify.cfg'):
            with self.subTest(mcu=config.parent.name):
                messages, calls, targets, error = self.execute_config(config)
                self.assertIsNone(error)
                self.assertEqual([call[0] for call in calls], ['verify_bank', 'read_bank'])
                _, _, unlock_targets, _ = self.execute_config(config.with_name('openocd-unlock.cfg'))
                self.assertEqual(targets, unlock_targets)


class ImageTests(unittest.TestCase):
    def test_hex_conversion_is_closed_and_uses_an_immutable_snapshot(self):
        import sys
        from intelhex import IntelHex
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = IntelHex()
            image.frombytes(b'\x00\x20\x00\x20\x11\x00\x00\x08', offset=0x08000000)
            source = directory / 'bootloader.hex'
            image.write_hex_file(str(source))
            programmer = Programmer(ROOT, sys.executable, lambda *args, **kwargs: None, lambda text: None)
            command, config, verify, expected = programmer.prepare(dict(
                mcu_type='F031', mode='Unlock', probe_type='ST Link', pin='PA2',
                can=False, custom_bootloader=str(source)), directory)
            source.write_text('changed after starting')
            self.assertEqual(expected, b'\x00\x20\x00\x20\x11\x00\x00\x08')
            self.assertEqual((directory / 'bootloader.bin').read_bytes(), expected)

    def test_hex_for_main_firmware_is_rejected_before_openocd(self):
        import sys
        from intelhex import IntelHex
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = IntelHex()
            image.frombytes(b'wrong offset', offset=0x08001000)
            source = directory / 'application.hex'
            image.write_hex_file(str(source))
            programmer = Programmer(ROOT, sys.executable, lambda *args, **kwargs: None, lambda text: None)
            with self.assertRaisesRegex(ValueError, 'must start at flash address'):
                programmer.prepare(dict(mcu_type='F031', mode='Unlock', probe_type='ST Link',
                    pin='PA2', can=False, custom_bootloader=str(source)), directory)


if __name__ == '__main__':
    unittest.main()
