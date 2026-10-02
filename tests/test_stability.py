"""Regression checks for the Windows player's resource and playback stability."""

from __future__ import annotations

import ast
import base64
import json
import math
import os
import queue
import re
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
from collections import OrderedDict
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "lumveil.py"


def _load_target_code():
    """Compile only the small source regions under test; never import lumveil."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    methods = {
        "_rt_worker_valid",
        "_finish_rt_result",
        "_rt_establish_baseline",
        "_toggle_rt_adj",
        "_apply_gpu_settings",
        "_set_interpolate_mpv",
        "_after_current_file",
        "_handle_mpv_file_loaded",
        "_play_list",
        "_apply_shader_opts",
        "_apply_effective_contrast",
        "_apply_shadow_lift",
        "_rt_blend_step",
        "_on_eof_setting",
        "_apply_playback_eof_action",
        "_on_eof_reached",
        "_load_player_settings",
        "_save_player_settings",
        "_on_mpv_eof",
        "_eof_matches_current_file",
        "_drain_mpv_events",
        "_rt_sample_due",
        "_rt_loop",
        "_load_gpu_settings", "_save_adj", "_load_adj", "_fix_scale_click",
        "_replace_playlist", "_write_playlist_file", "_read_playlist_file",
        "_seek_to_time", "_chapter_list", "_play_relative_chapter",
        "_refresh_playback_state", "_on_mpv_start_file", "_on_mpv_end_file",
        "_report_playback_error", "_on_playlist_sort_setting",
        "_on_close",
    }
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "VideoPlayer"
    )
    selected = [node for node in class_node.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in methods]
    selected += [node for node in class_node.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id.endswith("_OPTIONS")
                         for target in node.targets)]
    harness_node = ast.ClassDef(
        name="VideoPlayerHarness",
        bases=[],
        keywords=[],
        body=selected or [ast.Pass()],
        decorator_list=[],
    )
    resource_node = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name == "_resource_dir"),
        None,
    )
    namespace = {
        "__file__": str(SOURCE),
        "os": os,
        "sys": sys,
        "threading": threading,
        "json": json,
        "tempfile": tempfile,
        "math": math, "re": re, "base64": base64, "subprocess": subprocess,
        "analyze_frame": lambda *_args, **_kwargs: {
            "lum_mean": 140.0, "lum_std": 45.0, "chroma": 30.0,
        },
    }
    namespace["EOF_ACTION_OPTIONS"] = ast.literal_eval(next(
        node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "EOF_ACTION_OPTIONS"
                for target in node.targets)))
    body = [harness_node, next(node for node in tree.body
                              if isinstance(node, ast.FunctionDef)
                              and node.name == "_atomic_write_json")]
    body.extend(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"_bounded_number", "_parse_seek_time", "_launch_installer_after_exit"})
    if resource_node is not None:
        body.insert(0, resource_node)
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class _Player:
    def __init__(self):
        self.values = {}
        self.commands = []
        self.writes = []

    def __setitem__(self, key, value):
        self.values[key] = value
        self.writes.append((key, value))

    def command(self, *args):
        self.commands.append(args)


class _Var:
    def __init__(self, value=0):
        self.value = value
        self.writes = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        self.writes.append(value)


class StabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = _load_target_code()
        cls.VideoPlayer = cls.code["VideoPlayerHarness"]

    def test_resource_dir_uses_script_or_executable_location(self):
        resource_dir = self.code.get("_resource_dir")
        self.assertIsNotNone(resource_dir, "_resource_dir must be defined")
        with tempfile.TemporaryDirectory(dir=SOURCE.parent / "tests") as temp:
            script = Path(temp) / "dev"
            executable = Path(temp) / "release" / "Lumveil.exe"
            adjacent = executable.parent / "shaders"
            adjacent.mkdir(parents=True)
            self.assertEqual(
                resource_dir("shaders", script_dir=str(script), frozen=False),
                str(script / "shaders"),
            )
            self.assertEqual(
                resource_dir("shaders", script_dir=str(script),
                             executable=str(executable), frozen=True),
                str(executable.parent / "shaders"),
            )
            fallback = executable.parent / "_internal" / "assets"
            fallback.mkdir(parents=True)
            self.assertEqual(
                resource_dir("assets", script_dir=str(script),
                             executable=str(executable), frozen=True),
                str(fallback),
            )
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        shader_assignment = next(
            (node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "_SHADER_DIR"
                     for target in node.targets)),
            None,
        )
        self.assertIsNotNone(shader_assignment, "_SHADER_DIR must be initialized")
        self.assertIsInstance(shader_assignment.value, ast.Call)
        self.assertIsInstance(shader_assignment.value.func, ast.Name)
        self.assertEqual(shader_assignment.value.func.id, "_resource_dir")
        self.assertEqual(ast.literal_eval(shader_assignment.value.args[0]), "shaders")

    def test_stopped_old_auto_worker_stays_invalid_after_restart(self):
        player = self.VideoPlayer()
        old_stop = threading.Event()
        new_stop = threading.Event()
        old_stop.set()
        player._rt_stop = new_stop
        player._rt_enabled = True
        player._closing = False
        player._rt_generation = 2
        self.assertFalse(player._rt_worker_valid(old_stop, 1))
        self.assertTrue(player._rt_worker_valid(new_stop, 2))

    def test_old_and_late_eof_notifications_do_not_advance_current_video(self):
        player = self.VideoPlayer()
        sentinel = object()
        self.code["_MPV_EVENT_PENDING"] = sentinel
        player._mpv_event_lock = threading.Lock()
        player._closing = False
        player._media_generation = 1
        player._current_path = "a.mp4"
        player._mpv_pending_duration = sentinel
        player._mpv_pending_pause = sentinel
        player._mpv_pending_file_loaded = False
        player.player = type("Native", (), {"eof_reached": False, "path": "b.mp4"})()
        advanced = []
        player._on_eof_reached = lambda: advanced.append("advance")
        player._on_mpv_eof(True)
        self.assertEqual(player._mpv_pending_eof, (1, "a.mp4"))
        player._media_generation = 2
        player._current_path = "b.mp4"
        player._drain_mpv_events()
        self.assertEqual(advanced, [])
        # A late old notification can be tagged with the new generation;
        # native eof/path must agree before it is acted on.
        player._on_mpv_eof(True)
        player._drain_mpv_events()
        self.assertEqual(advanced, [])
        player.player.eof_reached = True
        player.player.path = "a.mp4"
        player._on_mpv_eof(True)
        player._drain_mpv_events()
        self.assertEqual(advanced, [])
        player.player.path = "b.mp4"
        player._on_mpv_eof(True)
        player._drain_mpv_events()
        self.assertEqual(advanced, ["advance"])
        player._on_mpv_eof(True)
        player._on_mpv_eof(False)
        player._drain_mpv_events()
        self.assertEqual(advanced, ["advance"])

    def test_same_path_new_load_rejects_old_eof(self):
        player = self.VideoPlayer()
        player._media_generation = 2
        player._current_path = "a.mp4"
        player.player = type("Native", (), {"eof_reached": True, "path": "a.mp4"})()
        self.assertFalse(player._eof_matches_current_file((1, "a.mp4")))
        self.assertTrue(player._eof_matches_current_file((2, "a.mp4")))

    def test_atomic_json_serialization_failure_preserves_original(self):
        writer = self.code["_atomic_write_json"]
        with tempfile.TemporaryDirectory(dir=SOURCE.parent / "tests") as temp:
            path = Path(temp) / "settings.json"
            original = b'{"previous":true}'
            path.write_bytes(original)
            with self.assertRaises(TypeError):
                writer(path, {"partial": "text", "bad": object()})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])
            writer(path, {"new": "日本語"}, ensure_ascii=False)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": "日本語"})
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_atomic_json_replace_or_flush_failure_preserves_original(self):
        writer = self.code["_atomic_write_json"]
        with tempfile.TemporaryDirectory(dir=SOURCE.parent / "tests") as temp:
            path = Path(temp) / "settings.json"
            original = b'{"previous":true}'
            path.write_bytes(original)
            for operation in ("replace", "fsync"):
                with patch.object(os, operation, side_effect=OSError("injected failure")):
                    with self.assertRaises(OSError):
                        writer(path, {"new": True})
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(Path(temp).glob("*.tmp")), [])

    def test_paused_sampling_waits_but_updates_after_changes_or_intent_decay(self):
        due = self.VideoPlayer._rt_sample_due
        signature = (1, 1000, "standard")
        self.assertFalse(due(True, signature, signature, 0.5))
        self.assertFalse(due(True, signature, signature, 4.99))
        self.assertTrue(due(True, signature, signature, 5.0))
        self.assertTrue(due(True, signature, None, 0))
        self.assertTrue(due(True, (1, 2000, "standard"), signature, 0.5))
        self.assertTrue(due(True, (1, 1000, "extreme"), signature, 0.5))
        self.assertTrue(due(True, signature, signature, 0.5, force=True))
        self.assertTrue(due(False, signature, signature, 0.5))

    def test_paused_extreme_darkness_completes_existing_three_second_decay(self):
        clock = type("Clock", (), {"now": 0.0})()
        clock.time = lambda: clock.now
        clock.monotonic = lambda: clock.now

        class Stop:
            def is_set(self):
                return clock.now >= 4.0

            def wait(self, interval):
                clock.now += interval

        player = self.VideoPlayer()
        player._closing = False
        player._rt_enabled = True
        player._rt_generation = 1
        player._media_generation = 1
        player._current_path = "clip.mp4"
        player._rt_analysis_revision = 0
        player._cached_pause = True
        player._rt_mode = "標準"
        player._dark_thresh = 1.0
        player._rt_base_adj = {key: 0.0 for key in
                               ("brightness", "contrast", "gamma", "saturation")}
        player._rt_current = dict(player._rt_base_adj, shadow_lift=0.0)
        player._auto_adj_status = _Var()
        player.player = object()
        player._get_duration_ms = lambda: 12000
        player._get_time_ms = lambda: 1000
        player._rt_establish_baseline = lambda *args: {
            "lum_mean": 120.0, "lum_std": 40.0, "chroma": 10.0}
        player._post_ui = lambda callback, *args: callback(*args)
        samples = []

        def frame(*args, **kwargs):
            samples.append(clock.now)
            return {"lum_mean": 1.0, "lum_std": 1.0, "chroma": 0.0, "dark_ratio": 1.0}

        with patch.dict(self.code, {"time": clock, "RT_MODES": {"標準": (0.6, 0.2)},
                                   "analyze_current_frame": frame}):
            player._rt_loop(Stop(), 1)
        self.assertIn(3.0, samples)
        self.assertEqual(samples[-1], 3.0)
        self.assertAlmostEqual(player._rt_targets["shadow_lift"], (1 - 1 / 120) * 0.2 * 1.2 * 0.6)

    def test_repeat_setting_updates_native_loop_and_disables_it_for_other_modes(self):
        player = self.VideoPlayer()
        player.player = _Player()
        player._current_path = None
        saved = []
        player._save_player_settings = lambda: saved.append(player._playback_eof_action)
        player._set_settings_error = lambda *args: self.fail(str(args))
        for label, action, native in (("リピート", "repeat", "inf"),
                                      ("停止", "stop", "no"),
                                      ("次の動画を再生", "next", "no")):
            player._on_eof_setting(label)
            self.assertEqual(player._playback_eof_action, action)
            self.assertEqual(player.player.values["loop-file"], native)
        self.assertEqual(saved, ["repeat", "stop", "next"])

    def test_repeat_eof_never_advances_or_wraps_playlist(self):
        player = self.VideoPlayer()
        player._playlist = ["a.mp4", "b.mp4"]
        player._folder_end_action = "loop"
        advances = []
        player._play_next = lambda: advances.append("next")
        player._open_path = lambda *args, **kwargs: advances.append("wrap")
        for index in (0, 1):
            player._playlist_idx = index
            for action in ("repeat", "stop"):
                player._playback_eof_action = action
                player._on_eof_reached()
        self.assertEqual(advances, [])
        player._playback_eof_action = "next"
        player._playlist_idx = 0
        player._on_eof_reached()
        self.assertEqual(advances, ["next"])
        player._playlist_idx = 1
        player._on_eof_reached()
        self.assertEqual(advances, ["next", "wrap"])

    def test_repeat_settings_round_trip_and_unknown_value_fallback(self):
        player = self.VideoPlayer()
        player.player = _Player()
        player.vol_var = _Var(80)
        player._shot_dir = "shots"
        player._toolbar_default_visible = set()
        player._toolbar_visible = set()
        player._toolbar_order = []
        player._toolbar_items = {}
        player._toolbar_item_definitions = lambda: {}
        player._set_settings_error = lambda *args: self.fail(str(args))
        with tempfile.TemporaryDirectory(dir=SOURCE.parent / "tests") as temp:
            settings = Path(temp) / "settings.json"
            self.code["PLAYER_SETTINGS"] = str(settings)
            settings.write_text(json.dumps({"playback_eof_action": "repeat",
                                            "ui_layout_version": 2}), encoding="utf-8")
            player._load_player_settings()
            self.assertEqual(player._playback_eof_action, "repeat")
            player._save_player_settings()
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))[
                "playback_eof_action"], "repeat")
            settings.write_text(json.dumps({"playback_eof_action": "unknown"}), encoding="utf-8")
            player._load_player_settings()
            self.assertEqual(player._playback_eof_action, "next")

    def test_old_worker_result_cannot_overwrite_new_media_state(self):
        player = self.VideoPlayer()
        old_stop = threading.Event()
        old_stop.set()
        player._rt_stop = threading.Event()
        player._rt_generation = 3
        player._media_generation = 12
        player._rt_enabled = True
        player._closing = False
        player._rt_baseline = {"lum_mean": 90.0}
        player._rt_targets = {"contrast": 7.0}
        status = _Var("current status")
        player._auto_adj_status = status
        stale_baseline = {"lum_mean": 220.0}
        stale_targets = {"contrast": 99.0}
        player._finish_rt_result(
            old_stop, 2, 12, stale_baseline, stale_targets, "stale worker",
        )
        player._finish_rt_result(
            player._rt_stop, 3, 11,
            stale_baseline, stale_targets, "old media",
        )
        self.assertEqual(player._rt_baseline, {"lum_mean": 90.0})
        self.assertEqual(player._rt_targets, {"contrast": 7.0})
        self.assertEqual(status.get(), "current status")

    def test_auto_restart_uses_new_stop_event_and_generation(self):
        player = self.VideoPlayer()
        player._rt_enabled = False
        player._current_path = "clip.mp4"
        player._rt_stop = threading.Event()
        player._rt_stop.set()
        old_stop = player._rt_stop
        player._rt_generation = 10
        player._rt_applied_values = {}
        player._rt_threads = []
        player._rt_baseline = None
        player._manual_status_stats = None
        player._rt_base_adj = {key: 0.0 for key in
                               ("brightness", "contrast", "gamma", "saturation")}
        player._rt_current = dict(player._rt_base_adj)
        player._rt_targets = dict(player._rt_base_adj)
        player._rt_targets["shadow_lift"] = 0.0
        player._rt_current["shadow_lift"] = 0.0
        player._adj_vars = {key: (_Var(),) for key in player._rt_base_adj}
        player._pre_rt_adj = None
        player._manual_shadow_lift = _Var(0.0)
        player._apply_glsl_shaders = lambda: None
        player._set_button_selected = lambda *_args, **_kwargs: None
        player._sync_rt_mode_buttons = lambda: None
        player._rt_btn = type("Button", (), {"config": lambda *_args, **_kwargs: None})()
        player._auto_adj_status = _Var()
        started = []

        class FakeThread:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                started.append(self.kwargs)

        class ThreadModule:
            Event = threading.Event
            Thread = FakeThread

        previous_threading = self.code["threading"]
        previous_ffmpeg = self.code.get("FFMPEG")
        self.code["threading"] = ThreadModule
        self.code["FFMPEG"] = "ffmpeg.exe"
        player._rt_loop = lambda *_args: None
        try:
            player._toggle_rt_adj()
        finally:
            self.code["threading"] = previous_threading
            self.code["FFMPEG"] = previous_ffmpeg

        self.assertIsNot(player._rt_stop, old_stop)
        self.assertFalse(player._rt_stop.is_set())
        self.assertEqual(player._rt_generation, 11)
        self.assertEqual(len(started), 1)

    def test_baseline_cache_keys_file_revision_and_caches_none(self):
        player = self.VideoPlayer()
        player._closing = False
        player._rt_enabled = True
        player._rt_generation = 1
        player._media_generation = 1
        player._rt_baseline_cache = OrderedDict()
        player._rt_baseline_cache_lock = threading.Lock()
        stop = threading.Event()
        with tempfile.TemporaryDirectory(dir=SOURCE.parent / "tests") as temp:
            video = Path(temp) / "clip.mp4"
            video.write_bytes(b"a")
            calls = []

            def analyze(path, position):
                calls.append((path, position))
                return {"lum_mean": 140.0, "lum_std": 45.0, "chroma": 30.0}

            self.code["analyze_frame"] = analyze
            first = player._rt_establish_baseline(
                str(video), 10.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            count_after_first = len(calls)
            second = player._rt_establish_baseline(
                str(video), 10.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            self.assertEqual(first, second)
            self.assertEqual(count_after_first, 32)
            self.assertEqual(len(calls), count_after_first)

            video.write_bytes(b"changed-size")
            player._rt_establish_baseline(
                str(video), 10.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            self.assertEqual(len(calls), count_after_first * 2)

            none_calls = []

            def dark_sample(*_args):
                none_calls.append(1)
                return {"lum_mean": 10.0, "lum_std": 5.0, "chroma": 3.0}

            self.code["analyze_frame"] = dark_sample
            player._rt_establish_baseline(
                str(video), 20.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            player._rt_establish_baseline(
                str(video), 20.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            self.assertEqual(len(none_calls), 32)

            # Total extraction failures are transient and must be retried.
            failed_calls = []
            self.code["analyze_frame"] = lambda *_args: failed_calls.append(1) or None
            player._rt_establish_baseline(
                str(video), 30.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            player._rt_establish_baseline(
                str(video), 30.0, stop_event=stop, generation=1,
                media_generation=1,
            )
            self.assertEqual(len(failed_calls), 64)

            self.code["analyze_frame"] = analyze
            for index in range(9):
                candidate = Path(temp) / f"clip-{index}.mp4"
                candidate.write_bytes(str(index).encode("ascii"))
                player._rt_establish_baseline(
                    str(candidate), 40.0, stop_event=stop, generation=1,
                    media_generation=1,
                )
            self.assertLessEqual(len(player._rt_baseline_cache), 8)

    def test_baseline_analysis_honors_cancellation(self):
        player = self.VideoPlayer()
        player._closing = False
        player._rt_baseline_cache = OrderedDict()
        player._rt_baseline_cache_lock = threading.Lock()
        stop = threading.Event()
        count = []

        def analyze(*_args):
            count.append(1)
            stop.set()
            return {"lum_mean": 140.0, "lum_std": 45.0, "chroma": 30.0}

        self.code["analyze_frame"] = analyze
        with tempfile.NamedTemporaryFile(dir=SOURCE.parent / "tests") as video:
            result = player._rt_establish_baseline(
                video.name, 10.0, stop_event=stop,
            )
        self.assertIsNone(result)
        self.assertEqual(len(count), 1)

    def test_gpu_settings_applies_interpolation_off_and_on(self):
        player = self.VideoPlayer()
        player.player = _Player()
        player._gpu_scale = "lanczos"
        player._gpu_cscale = "spline36"
        player._gpu_deband = False
        player._gpu_antiring = 0.0
        player._gpu_sigmoid = False
        player._gpu_correct_ds = False
        player._gpu_hwdec = "no"
        player._gpu_dither = "fruit"
        player._gpu_tonemapping = "auto"
        player._gpu_deinterlace = False
        player._apply_glsl_shaders = lambda: None
        player._apply_vf_chain = lambda: None

        for enabled in (False, True):
            player.player.values.clear()
            player._gpu_interpolate = enabled
            player._apply_gpu_settings()
            self.assertEqual(player.player.values.get("interpolation"), enabled)
            self.assertEqual(player.player.values.get("video-sync"),
                             "display-resample" if enabled else "audio")
            if enabled:
                self.assertEqual(player.player.values.get("tscale"), "oversample")

    def test_delayed_callback_is_discarded_for_old_media_generation(self):
        player = self.VideoPlayer()
        player._current_path = "same.mp4"
        player._media_generation = 4
        player._closing = False
        scheduled = []

        class Root:
            def after(self, _delay, callback):
                scheduled.append(callback)
                return "timer"

        player.root = Root()
        called = []
        player._after_current_file(200, lambda: called.append("current"))
        scheduled[-1]()
        self.assertEqual(called, ["current"])
        player._after_current_file(200, lambda: called.append("ran"))
        player._media_generation += 1
        player._current_path = "same.mp4"  # same path, distinct load
        scheduled[-1]()
        player._after_current_file(200, lambda: called.append("closing"))
        player._closing = True
        scheduled[-1]()
        self.assertEqual(called, ["current"])

    def test_file_loaded_routes_delays_through_media_guard(self):
        player = self.VideoPlayer()
        player._closing = False
        player._denoise = False
        player._gpu_amf_frc = False
        player._resume_positions = {}
        player._resume_enabled = False
        player._apply_all_adj = lambda: None
        scheduled = []
        player._after_current_file = lambda delay, callback, *args: scheduled.append(
            (delay, callback, args)
        )
        player._handle_mpv_file_loaded()
        self.assertTrue(scheduled)
        self.assertTrue(all(callable(item[1]) for item in scheduled))

    def test_playlist_replacement_invalidates_pending_folder_scan(self):
        player = self.VideoPlayer()
        player._playlist_scan_token = 8
        opened = []
        player._open_path = lambda path, _from_playlist=False: opened.append(path)
        player._play_list(["one.mp4", "two.mp4"], 1)
        self.assertGreater(player._playlist_scan_token, 8)
        self.assertEqual(opened, ["two.mp4"])

    def test_shader_writes_are_cached_and_blend_batches_options(self):
        player = self.VideoPlayer()
        player.player = _Player()
        player._shader_opts = {"auto_contrast": 0.0, "shadow_lift": 0.0}
        player._last_shader_opts = None
        player._set_settings_error = lambda *_args: self.fail("unexpected shader error")
        player._apply_shader_opts()
        player._apply_shader_opts()
        self.assertEqual(len(player.player.commands), 1)

        player.player.commands.clear()
        player._rt_enabled = True
        player._rt_applied_values = {}
        player._rt_current = {key: 0.0 for key in
                              ("brightness", "contrast", "gamma", "saturation",
                               "shadow_lift")}
        player._rt_targets = dict(player._rt_current)
        player._rt_targets["contrast"] = 20.0
        player._rt_targets["shadow_lift"] = 0.5
        player._adj_vars = {key: (_Var(),) for key in
                            ("brightness", "contrast", "gamma", "saturation")}
        player._apply_effective_contrast = self.VideoPlayer._apply_effective_contrast.__get__(
            player, self.VideoPlayer
        )
        player._apply_shadow_lift = self.VideoPlayer._apply_shadow_lift.__get__(
            player, self.VideoPlayer
        )
        player._apply_shader_opts = self.VideoPlayer._apply_shader_opts.__get__(
            player, self.VideoPlayer
        )
        player._rt_blend_step()
        shader_commands = [command for command in player.player.commands
                          if command[:2] == ("set", "glsl-shader-opts")]
        self.assertEqual(len(shader_commands), 1)

        # Values that keep the same rounded mpv output on adjacent ticks must
        # not be written to libmpv again.
        player.player.writes.clear()
        player._rt_applied_values = {}
        player._rt_current = {key: 0.0 for key in
                              ("brightness", "contrast", "gamma", "saturation",
                               "shadow_lift")}
        player._rt_targets = {key: 1.0 for key in
                              ("brightness", "contrast", "gamma", "saturation")}
        player._rt_targets["shadow_lift"] = 0.0
        player._rt_blend_step()
        first_tick_writes = len(player.player.writes)
        player._rt_blend_step()
        self.assertEqual(len(player.player.writes), first_tick_writes)


if __name__ == "__main__":
    unittest.main()
