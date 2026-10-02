"""Failure and interaction regressions for the full Windows review."""
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from test_stability import _load_target_code, _Var


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.code = _load_target_code()
        self.player = self.code['VideoPlayerHarness']()

    def test_time_formats_and_rejected_typos(self):
        parse = self.code['_parse_seek_time']
        for value, expected in [('90.5', 90.5), ('1:30.5', 90.5), ('1:02:03', 3723)]:
            self.assertEqual(parse(value), expected)
        for value in ('-1', '', '1:60', '1:99:00', 'NaN', '1::2', '1:2:3:4'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(value)

    def test_seek_checks_video_and_duration(self):
        p = self.player
        p._current_path = 'video.mp4'
        p._get_duration_ms = lambda: 120000
        p._fmt = lambda n: str(n)
        calls = []
        p.player = SimpleNamespace(seek=lambda *a, **k: calls.append((a, k)))
        p._seek_to_time('1:30')
        self.assertEqual(calls[0][0], (90.,))
        with self.assertRaises(ValueError): p._seek_to_time('121')
        p._current_path = None
        with self.assertRaises(ValueError): p._seek_to_time('0')

    def test_slider_click_calls_scale_command(self):
        applied = []
        scale = SimpleNamespace(winfo_width=lambda: 200,
                                after=lambda ms, fn: fn(),
                                set=lambda value: applied.append(value),
                                bind=lambda event, callback, **kw: setattr(self, 'click', callback))
        self.player._fix_scale_click(scale, _Var(), -100, 100)
        self.click(SimpleNamespace(x=198))
        self.assertEqual(applied, [98])

    def test_gpu_settings_validate_enums_ranges_types(self):
        p = self.player
        defaults = {'scale':'lanczos','cscale':'spline36','hwdec':'no','dither':'fruit',
                    'tonemapping':'auto','interpolate':True,'amf_frc':False,'deband':False}
        for key, value in defaults.items(): setattr(p, '_gpu_' + key, value)
        self.code['QUALITY_PRESETS'] = {'標準': {}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'gpu.json'
            path.write_text(json.dumps({'scale':'unknown','hwdec':['no'],'interpolate':'false',
                                        'antiring':float('nan'),'glsl':None}), encoding='utf-8')
            self.code['GPU_SETTINGS'] = str(path)
            p._load_gpu_settings()
        self.assertEqual(p._gpu_scale, 'lanczos')
        self.assertEqual(p._gpu_hwdec, 'no')
        self.assertTrue(p._gpu_interpolate)
        self.assertEqual(p._gpu_antiring, 0)
        self.assertEqual(p._gpu_glsl, [])

    def test_playlist_roundtrip_relative_paths_missing_and_duplicates(self):
        p = self.player
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); (folder/'日本語.mp4').touch(); (folder/'b.mkv').touch()
            p._playlist = [str(folder/'日本語.mp4'), str(folder/'b.mkv')]
            path = folder/'list.lumveil.json'
            p._write_playlist_file(str(path))
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(data['files'], ['日本語.mp4','b.mkv'])
            data['files'] += ['日本語.mp4','missing.mp4']
            path.write_text(json.dumps(data), encoding='utf-8')
            files, missing = p._read_playlist_file(str(path))
            self.assertEqual(files, p._playlist)
            self.assertEqual(missing, 1)

    def test_invalid_playlist_does_not_change_current_list(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'bad.json'
            path.write_text(json.dumps({'version':1,'files':[None]}), encoding='utf-8')
            with self.assertRaises(ValueError): self.player._read_playlist_file(str(path))

    def test_playlist_edit_preserves_current_and_invalidates_folder_scan(self):
        p = self.player
        p._current_path = 'b.mp4'; p._playlist_scan_token = 4
        p._refresh_playlist_popup = lambda: None
        p._replace_playlist(['b.mp4','a.mp4'])
        self.assertEqual(p._playlist_idx, 0)
        self.assertEqual(p._playlist_scan_token, 5)
        self.assertEqual(p._playlist_source, 'manual')
        calls = []; p._open_path = lambda *a, **k: calls.append((a, k))
        p._replace_playlist(['a.mp4'], start_index=9)
        self.assertEqual(calls[0][0], ('a.mp4',))

    def test_manual_playlist_order_setting_does_not_replace_it(self):
        p = self.player
        p._current_path='a.mp4'; p._playlist_source='manual'
        p._build_folder_playlist=lambda *_: self.fail('manual playlist was discarded')
        p._save_player_settings=lambda: None
        p._on_playlist_sort_setting('更新日時順')
        self.assertEqual(p._playlist_sort,'modified')

    def test_auto_save_preserves_manual_base_not_live_correction(self):
        p = self.player
        p._adj_vars = {k:(_Var(v),0) for k,v in {'brightness':80,'contrast':40,'gamma':20,'saturation':10,'hue':5}.items()}
        p._rt_enabled=True; p._rt_base_adj={'brightness':12,'contrast':3,'gamma':0,'saturation':2}
        p._rt_mode='標準'; p._dark_thresh=.7; p._manual_shadow_lift=_Var(30)
        p._set_settings_error=lambda *a: self.fail(str(a))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'adj.json'; self.code['ADJ_SETTINGS']=str(path)
            p._save_adj(quiet=True)
            saved=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['brightness'],12)
        self.assertEqual(saved['hue'],5)
        self.assertEqual(saved['shadow_lift'],30)

    def test_late_failure_does_not_show_error_for_new_video(self):
        p=self.player; p._closing=False; p._media_generation=2; p._current_path='b.mp4'
        p._set_settings_error=lambda *a: self.fail('late settings error')
        p._show_error_popup=lambda *a: self.fail('late popup')
        p._report_playback_error(1,'a.mp4','failed')

    def test_native_failure_is_queued_and_current_error_is_shown(self):
        p=self.player; p._mpv_event_lock=threading.Lock(); p._media_generation=3
        p._current_path='a.mp4'; p._closing=False
        p._on_mpv_start_file(SimpleNamespace(data=SimpleNamespace(playlist_entry_id=7)))
        calls=[]; p._post_ui=lambda callback,*a: calls.append((callback,a))
        self.code['mpv']=SimpleNamespace(strict_decoder=lambda s:s)
        p._on_mpv_end_file(SimpleNamespace(data=SimpleNamespace(reason=4,error=-17,playlist_entry_id=7),
                                         as_dict=lambda **k:{'file_error':'unrecognized format'}))
        self.assertEqual(calls[0][1],(3,'a.mp4','unrecognized format'))
        notices=[]; p._set_settings_error=lambda *a: None; p._show_error_popup=notices.append
        calls[0][0](*calls[0][1])
        self.assertIn('a.mp4',notices[0])

    def test_state_indicators(self):
        p=self.player; p._playback_eof_action='repeat'; p._muted=True
        p._rt_enabled=True; p._rt_baseline=None; p._playback_state_var=_Var()
        p._refresh_playback_state()
        self.assertEqual(p._playback_state_var.get(),'リピート / ミュート / AUTO解析中')

    def test_installer_helper_waits_and_revalidates_hash(self):
        with patch.object(self.code['subprocess'],'Popen') as launch:
            self.code['_launch_installer_after_exit']("D:/test/a'b.exe",'sha256:'+'a'*64)
            args=launch.call_args.args[0]
        script=base64.b64decode(args[-1]).decode('utf-16le')
        self.assertLess(script.index('Wait-Process'),script.index('Start-Process'))
        self.assertIn('Get-FileHash -LiteralPath',script)
        self.assertIn("a''b.exe",script)
        with self.assertRaises(ValueError): self.code['_launch_installer_after_exit']('x.exe','sha256:invalid')


if __name__=='__main__': unittest.main()
