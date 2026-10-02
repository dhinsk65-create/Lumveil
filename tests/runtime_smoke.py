"""Exercise the real Tk/libmpv player with synthetic media and isolated settings."""
import argparse
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import traceback
import types
from unittest.mock import patch
from PIL import ImageChops, ImageStat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, help="Read the built EXE's embedded player code")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    source = Path(__file__).resolve().parents[1] / "lumveil.py"
    if args.bundle:
        from PyInstaller.archive.readers import CArchiveReader
        bundle = args.bundle.resolve()
        code = marshal.loads(CArchiveReader(str(bundle)).extract("lumveil"))
        module = types.ModuleType("lumveil_smoke")
        module.__file__ = str(bundle.parent / "_internal" / "lumveil.py")
        original_executable = sys.executable
        original_frozen = getattr(sys, "frozen", None)
        try:
            sys.executable = str(bundle)
            sys.frozen = True
            exec(code, module.__dict__)
        finally:
            sys.executable = original_executable
            if original_frozen is None:
                del sys.frozen
            else:
                sys.frozen = original_frozen
        assert Path(module._SHADER_DIR) == bundle.parent / "_internal" / "shaders"
    else:
        spec = importlib.util.spec_from_file_location("lumveil_smoke", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    video_a = work / "a.mp4"
    video_b = work / "b.mp4"
    subprocess.run(
        [module.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-t", "12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video_a)],
        check=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    shutil.copyfile(video_a, video_b)
    root = module.TkinterDnD.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))
    app = None
    results = {"target": "embedded_exe_code" if args.bundle else "source"}
    try:
        app = module.VideoPlayer(root)
        native = app.player
        log_errors = []
        native._log_handler = lambda level, prefix, text: log_errors.append((level, prefix, text))
        root.withdraw()

        def pump(seconds=0.25, until=None):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                if until is not None and until():
                    return True
                time.sleep(0.01)
            return bool(until()) if until is not None else True

        app._open_path(str(video_a))
        assert pump(5, lambda: app._cached_duration_ms > 0 and bool(native.video_params))
        native.pause = True
        pump()
        app._apply_quality_preset("軽快")
        assert native.interpolation is False and native.video_sync == "audio"
        app._apply_quality_preset("標準")
        assert native.interpolation is True and native.video_sync == "display-resample"
        results["interpolation_on_off"] = "PASS"

        root.deiconify()
        pump(.3)
        before_gpu = native.screenshot_raw(includes="window").convert("RGB")
        assert native.current_vo == "gpu"
        if os.name == "nt":
            gpu_api = native["gpu-api"]
            # mpv's object-settings option is a list of named/enabled records.
            if isinstance(gpu_api, list) and gpu_api and isinstance(gpu_api[0], dict):
                selected_api = [entry['name'] for entry in gpu_api if entry.get('enabled', True)]
            else:
                selected_api = gpu_api if isinstance(gpu_api, list) else [gpu_api]
            assert selected_api == ['d3d11'], repr(gpu_api)
        assert sum(ImageStat.Stat(before_gpu).stddev) > 10, 'Blank GPU output'
        app._manual_shadow_lift.set(70)
        app._on_manual_shadow_lift()
        app._apply_effective_contrast(140)
        pump()
        assert native.vo_configured
        assert module.analyze_current_frame(native) is not None
        after_gpu = native.screenshot_raw(includes="window").convert("RGB")
        assert before_gpu.size == after_gpu.size
        gpu_difference = sum(ImageStat.Stat(ImageChops.difference(before_gpu, after_gpu)).mean)
        assert gpu_difference > 1, 'GPU adjustment did not change rendered pixels'
        after_gpu.save(work / 'gpu-rendered-frame.png')
        results["gpu_rendered_output"] = {"PASS": True, "vo": native.current_vo,
            "gpu_api": native["gpu-api"], "size": list(after_gpu.size),
            "pixel_difference": round(gpu_difference, 3)}
        assert all(Path(path).is_file() for path in native.glsl_shaders)
        assert not log_errors, log_errors
        results["gpu_shader_and_frame"] = "PASS"
        app._manual_shadow_lift.set(0)
        app._on_manual_shadow_lift()
        app._apply_effective_contrast(0)

        count = 0
        original_analyze = module.analyze_frame

        def counted(*args):
            nonlocal count
            count += 1
            return original_analyze(*args)

        module.analyze_frame = counted
        started = time.perf_counter()
        baseline = app._rt_establish_baseline(str(video_a), 12.0)
        elapsed = time.perf_counter() - started
        assert baseline is not None and count == 32
        before = count
        started = time.perf_counter()
        assert app._rt_establish_baseline(str(video_a), 12.0) == baseline
        cached_elapsed = time.perf_counter() - started
        assert count == before
        results["baseline_cache"] = {"PASS": True, "first_samples": 32,
                                     "cached_samples": 0, "first_seconds": round(elapsed, 3),
                                     "cached_seconds": round(cached_elapsed, 6)}

        app._toggle_rt_adj()
        old_stop = app._rt_stop
        old_thread = app._rt_thread
        app._toggle_rt_adj()
        app._toggle_rt_adj()
        assert old_stop.is_set() and app._rt_stop is not old_stop
        assert pump(5, lambda: bool(app._rt_baseline) and not old_thread.is_alive())
        assert app._rt_thread.is_alive()
        results["auto_restart"] = "PASS"

        original_current_frame = module.analyze_current_frame
        frame_calls = []

        def count_current_frame(*args, **kwargs):
            frame_calls.append(time.perf_counter())
            return original_current_frame(*args, **kwargs)

        module.analyze_current_frame = count_current_frame
        pump(1.0)
        frame_calls.clear()
        pump(2.0)
        paused_calls = len(frame_calls)
        assert paused_calls <= 1, paused_calls
        before = len(frame_calls)
        native.seek(3, reference="absolute", precision="exact")
        assert pump(1.5, lambda: len(frame_calls) > before)
        before = len(frame_calls)
        app._adj_vars["hue"][0].set(15)
        app._on_adjust("hue")
        assert pump(1.5, lambda: len(frame_calls) > before)
        app._adj_vars["hue"][0].set(0)
        app._on_adjust("hue")
        native.pause = False
        frame_calls.clear()
        pump(1.2)
        playing_calls = len(frame_calls)
        assert playing_calls >= 2, playing_calls
        native.pause = True
        results["paused_auto_sampling"] = {"PASS": True, "stable_calls_in_2s": paused_calls,
                                           "playing_calls_in_1_2s": playing_calls,
                                           "seek_and_adjustment_refresh": "PASS"}
        module.analyze_current_frame = original_current_frame
        app._toggle_rt_adj()
        pump(0.6)
        assert all(not thread.is_alive() for thread in app._rt_threads)

        app._resume_positions[str(video_a)] = 7.0
        app._handle_mpv_file_loaded()
        seek_calls = []
        original_seek = type(native).seek

        def capture_seek(player, position, *args, **kwargs):
            seek_calls.append((player.path, position))
            return original_seek(player, position, *args, **kwargs)

        with patch.object(type(native), "seek", capture_seek):
            app._open_path(str(video_b))
            pump(1)
        assert not any(path == str(video_b) and position == 7.0 for path, position in seek_calls)
        results["stale_resume"] = "PASS"
        old_token = app._playlist_scan_token
        app._play_list([str(video_b), str(video_a)], 0)
        app._finish_folder_playlist(old_token, str(video_b), [str(video_b)], 0)
        assert app._playlist == [str(video_b), str(video_a)]
        results["stale_playlist"] = "PASS"
        pump(0.7)
        with app._mpv_event_lock:
            app._mpv_pending_eof = (app._media_generation - 1, str(video_a))
        app._drain_mpv_events()
        assert app._playlist_idx == 0
        # Simulate an old true notification delivered after the new file opened.
        app._on_mpv_eof(True)
        app._drain_mpv_events()
        assert app._playlist_idx == 0 and native.path == str(video_b)
        results["stale_and_late_eof"] = "PASS"

        def descendants(widget):
            yield widget
            for child in widget.winfo_children():
                yield from descendants(child)

        repeat_menu = None
        for widget in descendants(app._playback_tab):
            if isinstance(widget, module.tk.OptionMenu):
                menu = widget["menu"]
                labels = [menu.entrycget(index, "label")
                          for index in range(menu.index("end") + 1)]
                if "リピート" in labels:
                    repeat_menu = menu
                    repeat_menu.invoke(labels.index("リピート"))
                    break
        assert repeat_menu is not None
        assert app._eof_var.get() == "リピート" and native.loop_file == "inf"
        for _ in range(2):
            native.seek(11.5, reference="absolute", precision="exact")
            native.pause = False
            assert pump(0.3, lambda: (native.time_pos or 0) > 10)
            assert pump(2, lambda: (native.time_pos or 0) < 5)
            assert native.path == str(video_b) and app._playlist_idx == 0
        assert json.loads(Path(module.PLAYER_SETTINGS).read_text(encoding="utf-8"))[
            "playback_eof_action"] == "repeat"
        results["repeat_current_video_twice_and_save"] = "PASS"

        app._on_eof_setting("停止")
        assert native.loop_file in (False, "no")
        native.seek(11.5, reference="absolute", precision="exact")
        assert pump(2, lambda: bool(native.eof_reached))
        assert native.path == str(video_b) and app._playlist_idx == 0
        app._on_eof_setting("リピート")
        assert pump(2, lambda: not native.eof_reached and (native.time_pos or 0) < 5)
        app._on_eof_setting("次の動画を再生")
        assert native.loop_file in (False, "no")
        native.seek(11.5, reference="absolute", precision="exact")
        native.pause = False
        assert pump(2, lambda: native.path == str(video_a) and app._playlist_idx == 1)
        results["repeat_to_stop_next_and_restart_at_eof"] = "PASS"
        assert not errors, errors
        assert not log_errors, log_errors
        results["ui_callbacks"] = "PASS"
        assert not list(Path(module._BASE_DIR).glob("*.tmp"))
        for path in Path(module._BASE_DIR).glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
        results["atomic_settings_files"] = "PASS"
    finally:
        if app is not None and not app._closing:
            started = time.monotonic()
            app._on_close()
            results["close_callback_seconds"] = round(time.monotonic() - started, 4)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                pending = [thread for thread in threading.enumerate()
                           if thread.name.startswith("Lumveil")]
                if not pending:
                    break
                time.sleep(0.05)
            assert not pending, [thread.name for thread in pending]
            results["worker_shutdown"] = "PASS"
        else:
            root.destroy()
    (work / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
