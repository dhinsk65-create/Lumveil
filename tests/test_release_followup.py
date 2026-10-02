"""Malformed preferences and uninstall ownership checks without a real registry."""
import copy
import io
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from test_stability import _load_target_code, _Var
from test_distribution import _load_association_module, INSTALLER_SOURCE


class SettingsShutdownTests(unittest.TestCase):
    def setUp(self):
        self.code = _load_target_code()
        self.app = self.code['VideoPlayerHarness']()
        self.app.vol_var = _Var(80)
        self.app.player = SimpleNamespace(volume=80)
        self.app._resume_positions = {}
        self.app._toolbar_default_visible = {'speed'}
        self.app._toolbar_visible = {'speed'}
        self.app._toolbar_order = ['speed', 'playlist']
        self.app._toolbar_items = {}
        self.app._toolbar_item_definitions = lambda: {'speed': None, 'playlist': None}
        self.code['PLAYER_SETTINGS'] = 'unused-test-preferences.json'

    def load(self, data):
        with patch('builtins.open', return_value=io.StringIO(json.dumps(data))):
            self.app._load_player_settings()

    def test_wrong_setting_types_fall_back_without_interrupting_load(self):
        self.load({'volume': 'bad', 'resume_positions': [], 'recent_files': None,
                   'restore_manual_settings': 'false', 'resume_enabled': [],
                   'always_on_top': {}, 'auto_update_checks': 'yes',
                   'last_update_check': 'NaN', 'ui_layout_version': None,
                   'folder_end_action': [], 'playlist_sort': {},
                   'playback_eof_action': [], 'screenshot_dir': ['bad']})
        self.assertEqual(self.app._resume_positions, {})
        self.assertEqual(self.app._recent_files, [])
        self.assertEqual(self.app.vol_var.get(), 80)
        self.assertTrue(self.app._resume_enabled)
        self.assertTrue(self.app._restore_manual_settings)
        self.assertFalse(self.app._always_on_top)
        self.assertFalse(self.app._auto_update_checks)
        self.assertEqual(self.app._last_update_check, 0)
        self.assertEqual(self.app._folder_end_action, 'stop')
        self.assertEqual(self.app._playlist_sort, 'name')
        self.assertEqual(self.app._playback_eof_action, 'next')

    def test_invalid_entries_are_removed_and_valid_entries_preserved(self):
        self.load({'resume_positions': {'valid': 15.5, 'zero': 0, 'negative': -2,
                    'bool': True, 'nan': float('nan'), 'inf': float('inf'),
                    'text': '20', 'huge': 10 ** 400, '': 5},
                   'recent_files': ['a', 'a', None, {}, '', 'b'],
                   'ui_layout_version': 2,
                   'toolbar_visible': ['speed', {}, None, 'unknown'],
                   'toolbar_order': ['playlist', 'playlist', {}, 'unknown']})
        self.assertEqual(self.app._resume_positions, {'valid': 15.5, 'zero': 0.0})
        self.assertEqual(self.app._recent_files, ['a', 'b'])
        self.assertEqual(self.app._toolbar_visible, {'speed'})
        self.assertEqual(self.app._toolbar_order, ['playlist', 'speed'])

    def test_settings_save_failures_still_destroy_window_and_start_native_cleanup(self):
        app = self.app
        app._closing = False
        app._playlist_scan_token = 1
        app._rt_generation = 1
        app._rt_stop = threading.Event()
        app._rt_threads = []
        app._gpu_save_after_id = None
        observer = SimpleNamespace(unobserve_mpv_properties=Mock())
        app._mpv_observer_fns = [observer]
        app._update_resume_position = Mock(side_effect=TypeError('bad position type'))
        app._save_window_settings = Mock(side_effect=PermissionError('read-only profile'))
        app._save_adj = Mock(side_effect=OSError('disk unavailable'))
        app._stop_preview_worker = Mock()
        app._terminate_player_after_close = Mock()
        app.root = SimpleNamespace(destroy=Mock())
        native = app.player
        with patch.object(self.code['threading'], 'Thread') as start:
            app._on_close()
            app._on_close()
            start.assert_called_once_with(target=app._terminate_player_after_close,
                args=(native, []), name='LumveilMpvShutdown', daemon=True)
            start.return_value.start.assert_called_once()
        self.assertIsNone(app.player)
        self.assertTrue(app._rt_stop.is_set())
        observer.unobserve_mpv_properties.assert_called_once()
        app.root.destroy.assert_called_once()
        app._save_adj.assert_called_once_with(quiet=True)


class UninstallAssociationTests(unittest.TestCase):
    def setUp(self):
        self.assoc, self.reg = _load_association_module()
        self.exe = r'C:\Program Files\Lumveil\Lumveil.exe'
        self.assoc._exe_path = lambda: self.exe

    def test_headless_uninstall_restores_prior_default_without_gui(self):
        ext = r'Software\Classes\.mp4'
        self.reg.put(ext, '', 'VLC.mp4')
        self.assoc._associate('.mp4', self.exe)
        with patch.object(self.assoc, '_notify_shell') as notify, \
             patch.object(self.assoc.tk, 'Tk', side_effect=AssertionError('no GUI')):
            self.assertEqual(self.assoc.main(['--unassociate-all']), 0)
            notify.assert_called_once()
        self.assertEqual(self.reg.get(ext, ''), 'VLC.mp4')
        self.assertNotIn(self.reg._norm(r'Software\Classes\Lumveilmp4'), self.reg.keys)

    def test_foreign_default_is_preserved_while_owned_command_is_removed(self):
        ext = r'Software\Classes\.mkv'
        prog = r'Software\Classes\Lumveilmkv'
        self.assoc._associate('.mkv', self.exe)
        self.reg.put(ext, '', 'Other.mkv')
        self.reg.put(prog, 'CustomValue', 'keep')
        self.assertTrue(self.assoc._unassociate('.mkv', expected_exe=self.exe))
        self.assertEqual(self.reg.get(ext, ''), 'Other.mkv')
        self.assertEqual(self.reg.get(prog, 'CustomValue'), 'keep')
        self.assertNotIn(self.reg._norm(prog + r'\shell\open\command'), self.reg.keys)

    def test_uninstall_of_old_directory_preserves_another_install(self):
        self.assoc._associate('.avi', r'D:\NewLumveil\Lumveil.exe')
        before = copy.deepcopy(self.reg.keys)
        self.assertTrue(self.assoc._unassociate('.avi', expected_exe=self.exe))
        self.assertEqual(self.reg.keys, before)

    def test_failed_command_or_icon_delete_reports_failure_and_is_retryable(self):
        for suffix in (r'\shell\open\command', r'\DefaultIcon'):
            with self.subTest(suffix=suffix):
                self.assoc._associate('.mov', self.exe)
                prog = r'Software\Classes\Lumveilmov'
                self.reg.fail_delete = (self.reg._norm(prog + suffix), '')
                self.assertFalse(self.assoc._unassociate('.mov', expected_exe=self.exe))
                self.assertEqual(self.reg.get(prog + r'\shell\open\command', ''),
                                 f'"{self.exe}" "%1"')
                self.reg.fail_delete = None
                self.assertTrue(self.assoc._unassociate('.mov', expected_exe=self.exe))
                self.assertNotIn(self.reg._norm(prog), self.reg.keys)

    def test_cli_reports_failure_but_attempts_every_extension(self):
        results = [False] + [True] * (len(self.assoc.EXTENSIONS) - 1)
        with patch.object(self.assoc, '_unassociate', side_effect=results) as remove, \
             patch.object(self.assoc, '_notify_shell'):
            self.assertEqual(self.assoc.main(['--unassociate-all']), 1)
        self.assertEqual(remove.call_count, len(self.assoc.EXTENSIONS))
        self.assertEqual(remove.call_args_list[0].kwargs, {'expected_exe': self.exe})

    def test_unknown_cli_does_not_touch_registry_or_gui(self):
        before = copy.deepcopy(self.reg.keys)
        with patch.object(self.assoc.tk, 'Tk', side_effect=AssertionError('no GUI')):
            self.assertEqual(self.assoc.main(['--invalid']), 2)
        self.assertEqual(self.reg.keys, before)

    def test_uninstaller_requires_success_before_deleting_files(self):
        source = INSTALLER_SOURCE.read_text(encoding='utf-8')
        self.assertIn('Function un.onInit', source)
        section = source.split('Section "Uninstall"', 1)[1]
        self.assertIn('ExecWait', section)
        self.assertIn('--unassociate-all', section)
        self.assertIn('IfErrors unassociate_failed', section)
        self.assertIn('StrCmp $0 "0" unassociate_done', section)
        self.assertLess(section.index('Abort'), section.index('DeleteRegKey'))
        self.assertLess(section.index('ExecWait'), section.index('RMDir /r "$INSTDIR"'))


if __name__ == '__main__':
    unittest.main()
