"""Real Tk/libmpv shutdown with corrupt isolated preferences; CPU output only."""
import argparse
import json
import marshal
import os
from pathlib import Path
import sys
import time
import types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ['APPDATA'] = str(work / 'profile')
    profile = work / 'profile' / 'Lumveil'
    profile.mkdir(parents=True, exist_ok=True)
    (profile / 'player_settings.json').write_text(json.dumps({
        'resume_positions': [], 'recent_files': None, 'volume': 'invalid',
        'ui_layout_version': 2, 'toolbar_visible': [{}, 'speed'],
        'toolbar_order': [{}, 'speed', 'speed'],
        'resume_enabled': 'bad', 'restore_manual_settings': 'bad',
    }), encoding='utf-8')
    (profile / 'gpu_settings_mpv.json').write_text(json.dumps({'hwdec': False}), encoding='utf-8')
    from PyInstaller.archive.readers import CArchiveReader
    bundle = args.bundle.resolve()
    module = types.ModuleType('settings_shutdown_player')
    module.__file__ = str(bundle.parent / '_internal' / 'lumveil.py')
    previous = sys.executable
    sys.executable = str(bundle)
    sys.frozen = True
    try:
        exec(marshal.loads(CArchiveReader(str(bundle)).extract('lumveil')), module.__dict__)
    finally:
        sys.executable = previous
        del sys.frozen
    original = module.mpv.MPV
    class SoftwareOnlyMPV(original):
        def __init__(self, **options):
            options.pop('gpu_api', None)
            options.update(vo='null', ao='null', hwdec='no', config='no',
                           vd_lavc_threads=1, vd_lavc_dr='no')
            super().__init__(**options)
    module.mpv.MPV = SoftwareOnlyMPV
    root = module.TkinterDnD.Tk()
    root.withdraw()
    callback_errors = []
    root.report_callback_exception = lambda *error: callback_errors.append(str(error))
    app = module.VideoPlayer(root)
    native = app.player
    results = {'target': str(bundle), 'video_output': 'null', 'hardware_decode': 'no',
               'bootloader_tested': False}
    try:
        assert isinstance(app._resume_positions, dict) and app._recent_files == []
        assert app.vol_var.get() == 80 and app._restore_manual_settings
        results['corrupt_settings_normalized'] = 'PASS'
        app._open_path(str(args.video.resolve()))
        end = time.monotonic() + 6
        while time.monotonic() < end and app._cached_duration_ms <= 0:
            root.update()
            time.sleep(.02)
        assert app._cached_duration_ms > 0, 'native file loading failed'
        assert native.hwdec_current == 'no' and native.current_vo == 'null'
        results['playback_after_corrupt_settings'] = 'PASS'
        saves = []
        def failing_save(name):
            def save(*_args, **_kwargs):
                saves.append(name)
                raise OSError('injected preference storage failure')
            return save
        app._update_resume_position = failing_save('resume')
        app._save_window_settings = failing_save('window')
        app._save_adj = failing_save('adjustment')
        app._on_close()
        app._on_close()
        assert saves == ['resume', 'window', 'adjustment']
        assert app.player is None and app._rt_stop.is_set()
        native._event_thread.join(timeout=8)
        assert not native._event_thread.is_alive(), 'mpv did not terminate'
        assert not callback_errors, callback_errors
        results['save_failure_window_and_native_shutdown'] = 'PASS'
    finally:
        if not app._closing:
            app._on_close()
    (work / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results))


if __name__ == '__main__':
    main()
