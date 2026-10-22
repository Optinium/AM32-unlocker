"""Tk integration tests with a fake programmer: never touches an ESC."""
import threading
import tkinter as tk
import unittest
import logging
from unittest.mock import patch

from esc_unlocker import UnlockerApp


class FakeProgrammer:
    instances = []

    def __init__(self, resources, openocd, emit, log):
        self.stop = threading.Event()
        self.emit = emit
        self.runs = 0
        self.params = None
        self.__class__.instances.append(self)

    def run(self, params):
        self.runs += 1
        self.params = params
        # The test controls completion explicitly, leaving the UI in running state.
        self.emit('attempt', number=1, phase='program')


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        FakeProgrammer.instances = []
        self.audio = patch('esc_unlocker.Audio')
        self.audio.start().return_value.available = False
        # Simulated success must not appear in the operator's diagnostic log.
        logger = logging.Logger('gui-tests')
        logger.addHandler(logging.NullHandler())
        self.logging = patch('esc_unlocker.make_logger', return_value=logger)
        self.logging.start()
        self.app = UnlockerApp(self.root, programmer_factory=FakeProgrammer)
        self.app.sound.set(False)
        # Keep the actual popup hidden during tests.
        original = tk.Toplevel

        def hidden_popup(*args, **kwargs):
            popup = original(*args, **kwargs)
            popup.withdraw()
            return popup

        self.popup = patch('esc_unlocker.tk.Toplevel', side_effect=hidden_popup)
        self.popup.start()

    def tearDown(self):
        self.app.closing = True
        self.root.destroy()
        self.popup.stop()
        self.audio.stop()
        self.logging.stop()

    def drain(self):
        if self.app.worker:
            self.app.worker.join(timeout=2)
        self.app.process_events()

    def advance_countdown(self):
        # Run the real scheduled Tk callback without waiting a wall-clock second.
        timer = self.app.countdown_after_id
        command, _ = self.root.tk.call('after', 'info', timer)
        self.root.tk.call('after', 'cancel', timer)
        self.root.tk.eval(command)

    def complete_first_esc(self):
        self.app.start()
        self.drain()
        FakeProgrammer.instances[0].emit('finished', outcome='success',
                                        message='All bytes match.', attempts=1)
        self.drain()

    def test_double_start_and_settings_locked_while_running(self):
        self.app.start()
        self.app.start()
        self.drain()
        self.assertEqual(len(FakeProgrammer.instances), 1)
        self.assertTrue(self.app.running)
        self.assertTrue(self.app.start_button.instate(['disabled']))
        self.assertTrue(all(widget.instate(['disabled']) for widget in self.app.settings_widgets))

    def test_success_automatically_counts_down_and_preserves_selections(self):
        self.app.mcu.set('F051')
        self.app.pin.set('PB4')
        self.app.start()
        self.drain()
        first = FakeProgrammer.instances[0]
        self.app.sound.set(True)
        self.app.audio.play.reset_mock()
        with patch.object(self.root, 'after', wraps=self.root.after) as schedule:
            first.emit('finished', outcome='success', message='All bytes match.', attempts=1)
            self.drain()
            self.assertFalse(self.app.running)
            self.assertEqual(self.app.completed, 1)
            self.assertIsNotNone(self.app.dialog)
            self.assertIn('automatically', self.app.dialog.title())
            self.assertTrue(self.app.countdown_active)
            self.assertEqual(len(FakeProgrammer.instances), 1)
            self.assertIn('3...', self.app.status.get())
            self.assertIn('3...', self.app.countdown_text.get())
            self.assertTrue(all(widget.instate(['disabled']) for widget in self.app.settings_widgets))
            self.app.begin_countdown()  # Repeated activation must not add timers/beeps.
            self.app.start()  # Cannot bypass the countdown with Start.
            self.advance_countdown()
            self.assertIn('2...', self.app.status.get())
            self.assertEqual(len(FakeProgrammer.instances), 1)
            self.advance_countdown()
            self.assertIn('1...', self.app.status.get())
            self.assertEqual(len(FakeProgrammer.instances), 1)
            self.assertEqual(self.app.audio.play.call_count, 3)
            self.advance_countdown()
            self.assertEqual([call.args[0] for call in schedule.call_args_list
                              if call.args[1] == self.app.countdown_tick], [1000, 1000, 1000])
        self.drain()
        self.assertIsNone(self.app.dialog)
        self.assertFalse(self.app.countdown_active)
        self.assertEqual(len(FakeProgrammer.instances), 2)
        self.assertEqual(FakeProgrammer.instances[1].params, first.params)
        self.assertTrue(self.app.running)

    def test_stop_cancels_countdown_and_next_can_restart_it(self):
        self.complete_first_esc()
        timer = self.app.countdown_after_id
        self.app.stop()
        self.assertFalse(self.app.countdown_active)
        self.assertIsNone(self.app.countdown_after_id)
        self.assertEqual(len(FakeProgrammer.instances), 1)
        self.assertEqual(self.app.completed, 1)
        self.assertFalse(self.app.running)
        self.assertTrue(self.app.start_button.instate(['!disabled']))
        with self.assertRaises(tk.TclError):
            self.root.tk.call('after', 'info', timer)
        self.app.start_button.invoke()
        self.assertTrue(self.app.countdown_active)
        self.assertIn('3...', self.app.status.get())

    def test_popup_stop_cancels_and_next_button_restarts(self):
        self.complete_first_esc()
        self.app.dialog.winfo_children()[-1].invoke()
        self.assertIsNone(self.app.dialog)
        self.assertFalse(self.app.countdown_active)
        self.assertEqual(len(FakeProgrammer.instances), 1)
        self.app.start_button.invoke()
        self.assertTrue(self.app.countdown_active)

    def test_closing_success_popup_cancels_countdown(self):
        self.complete_first_esc()
        self.root.tk.eval(self.app.dialog.protocol('WM_DELETE_WINDOW'))
        self.assertIsNone(self.app.dialog)
        self.assertFalse(self.app.countdown_active)
        self.assertIsNone(self.app.countdown_after_id)
        self.assertEqual(len(FakeProgrammer.instances), 1)

    def test_escape_cancels_countdown(self):
        self.complete_first_esc()
        binding = self.app.dialog.bind('<Escape>')
        callback = binding.split('[', 1)[1].split()[0]
        self.root.tk.call(callback, *(['0'] * 19))
        self.assertIsNone(self.app.dialog)
        self.assertFalse(self.app.countdown_active)
        self.assertEqual(len(FakeProgrammer.instances), 1)

    def test_muted_countdown_still_starts_and_quit_cancels(self):
        self.complete_first_esc()
        self.app.audio.play.reset_mock()
        self.advance_countdown()
        self.assertEqual(self.app.audio.play.call_count, 0)
        timer = self.app.countdown_after_id
        with patch.object(self.app, 'wait_for_exit'):
            self.app.quit()
        self.assertFalse(self.app.countdown_active)
        self.assertEqual(len(FakeProgrammer.instances), 1)
        with self.assertRaises(tk.TclError):
            self.root.tk.call('after', 'info', timer)

    def test_stop_never_creates_success_dialog(self):
        self.app.start()
        self.drain()
        self.app.stop()
        first = FakeProgrammer.instances[0]
        first.emit('stage', message='ESC detected')
        self.drain()
        self.assertTrue(self.app.status.get().startswith('Stopping'))
        first.emit('finished', outcome='stopped', message='Stopped.', attempts=1)
        self.drain()
        self.assertFalse(self.app.running)
        self.assertIsNone(self.app.dialog)
        self.assertEqual(self.app.completed, 0)

    def test_history_and_diagnostics_remain_bounded(self):
        for number in range(200):
            self.app.summary('Status %d' % number)
        for number in range(1000):
            self.app.handle_event('detail', dict(text='Diagnostic %d\n' % number))
        self.assertEqual(len(self.app.history), 100)
        self.assertEqual(len(self.app.details), 500)
        self.app.show_details.set(True)
        self.app.render_output()
        self.assertIn('Diagnostic 999', self.app.output.get('1.0', 'end'))


if __name__ == '__main__':
    unittest.main()
