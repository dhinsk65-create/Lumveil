"""Real Tk/libmpv checks for review fixes, isolated from the user's settings."""
import argparse
import importlib.util
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import types
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--software-only', action='store_true',
                        help='Disable GPU decoding and video output in every test process.')
    parser.add_argument('--verify-restore', choices=['on', 'off'])
    args = parser.parse_args()
    work = args.work_dir.resolve(); work.mkdir(parents=True, exist_ok=True)
    os.environ['APPDATA'] = str(work/'profile')
    profile = work/'profile'/'Lumveil'; profile.mkdir(parents=True, exist_ok=True)
    if not args.verify_restore:
        (profile/'gpu_settings_mpv.json').write_text(json.dumps({'scale':'invalid', 'cscale':None,
                    'antiring':'NaN','glsl':None}), encoding='utf-8')
        (profile/'adj_settings_mpv.json').write_text(json.dumps({'brightness':25,'contrast':140,
                    'gamma':4,'hue':8,'shadow_lift':30,'rt_mode':'控えめ','dark_thresh':.75}), encoding='utf-8')
        (profile/'player_settings.json').write_text(json.dumps({'restore_manual_settings':True,
                    'sub_delay':.5,'sub_scale':1.2,'audio_delay':.25}), encoding='utf-8')
    source = Path(__file__).resolve().parents[1]/'lumveil.py'
    if args.bundle:
        from PyInstaller.archive.readers import CArchiveReader
        bundle=args.bundle.resolve()
        module=types.ModuleType('full_ui_player'); module.__file__=str(bundle.parent/'_internal'/'lumveil.py')
        previous=sys.executable; sys.executable=str(bundle); sys.frozen=True
        try: exec(marshal.loads(CArchiveReader(str(bundle)).extract('lumveil')),module.__dict__)
        finally: sys.executable=previous; del sys.frozen
    else:
        spec=importlib.util.spec_from_file_location('full_ui_player',source)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    forbidden_gpu_requests=[]
    if args.software_only:
        original_mpv=module.mpv.MPV
        class SoftwareOnlyMPV(original_mpv):
            def __init__(self, **options):
                options.pop('gpu_api', None)
                options.update(vo='null', ao='null', hwdec='no', config='no',
                               vd_lavc_threads=1, vd_lavc_dr='no')
                super().__init__(**options)
            def __setitem__(self, name, value):
                if (name=='hwdec' and value!='no') or (name=='vo' and value!='null'):
                    forbidden_gpu_requests.append((name,value))
                    raise AssertionError(f'GPU request blocked: {name}={value}')
                return super().__setitem__(name,value)
        module.mpv.MPV=SoftwareOnlyMPV
    root=module.TkinterDnD.Tk(); root.withdraw(); errors=[]
    root.report_callback_exception=lambda *ex:errors.append(''.join(traceback.format_exception(*ex)))
    app=module.VideoPlayer(root); root.withdraw(); native=app.player
    results={'target':'bundle' if args.bundle else 'source'}
    if args.software_only:
        results['test_backend']={'vo':'null','hwdec':'no','decoder_threads':1,
                                'gpu_rendering_tested':False,'bootloader_tested':False}
    def pump(sec=.2, until=None):
        deadline=time.monotonic()+sec
        while time.monotonic()<deadline:
            root.update()
            if until and until():return True
            time.sleep(.01)
        return bool(until()) if until else True
    def walk(widget):
        for child in widget.winfo_children():
            yield child; yield from walk(child)
    try:
        if args.verify_restore:
            expected=27 if args.verify_restore=='on' else 0
            assert app._adj_vars['brightness'][0].get()==expected
            assert native.brightness==expected
            assert abs(native.audio_delay-.25)<.001 and abs(native.sub_scale-1.2)<.001
            # Do not save the intentionally disabled startup defaults over the
            # saved manual preset in this probe.
            app._save_adj=lambda **kw:None
            print('restore_'+args.verify_restore+' PASS')
            return
        assert native.brightness==25 and native.contrast==100
        assert abs(app._shader_opts['auto_contrast']-.4)<.001
        assert abs(native.audio_delay-.25)<.001 and abs(native.sub_delay-.5)<.001 and abs(native.sub_scale-1.2)<.001, (native.audio_delay,native.sub_delay,native.sub_scale,app._sync_preferences)
        results['initial_manual_and_sync_restore']='PASS'
        app._ensure_gpu_settings()
        assert app._gpu_scale=='lanczos' and app._gpu_antiring==0
        results['invalid_gpu_settings_fallback']='PASS'

        a=work/'a.mp4'; b=work/'chapters.mkv'
        subprocess.run([module.FFMPEG,'-hide_banner','-loglevel','error','-y','-f','lavfi',
                        '-i','testsrc2=size=320x180:rate=24','-t','12','-c:v','libx264',
                        '-threads','1','-preset','ultrafast',str(a)],check=True,timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        meta=work/'chapters.ffmeta'
        meta.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=4000\ntitle=First\n'
                        '[CHAPTER]\nTIMEBASE=1/1000\nSTART=4000\nEND=12000\ntitle=Second\n',encoding='utf-8')
        subprocess.run([module.FFMPEG,'-hide_banner','-loglevel','error','-y','-i',str(a),
                        '-f','ffmetadata','-i',str(meta),'-map_metadata','1','-map_chapters','1',
                        '-c','copy',str(b)],check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        app._open_path(str(b)); assert pump(4,lambda:app._cached_duration_ms>0)
        native.pause=True; pump()
        win=app._settings_win; win.geometry('760x520'); win.deiconify()
        app._settings_tabs.select(app._picture_tab); pump()
        canvas=next(w for w in walk(app._picture_tab) if isinstance(w,module.tk.Canvas))
        canvas.yview_moveto(1); pump()
        save=next(w for w in walk(app._picture_tab) if isinstance(w,module.tk.Button) and w.cget('text')=='設定を保存')
        assert save.winfo_ismapped()
        assert canvas.winfo_rooty() <= save.winfo_rooty() < canvas.winfo_rooty()+canvas.winfo_height()
        for tab in (app._quick_tab,app._picture_tab,app._playback_tab,app._about_tab):
            assert any(isinstance(w,module.tk.Canvas) for w in walk(tab))
        results['settings_scroll_at_minimum_size']='PASS'
        canvas.yview_moveto(0); pump()
        scales=[w for w in walk(app._picture_tab) if isinstance(w,module.ttk.Scale)]
        bright=scales[0]
        bright.event_generate('<ButtonPress-1>',x=bright.winfo_width()-2,y=bright.winfo_height()//2)
        pump(.05); bright.event_generate('<ButtonRelease-1>',x=bright.winfo_width()-2,y=bright.winfo_height()//2); pump()
        assert int(round(app._adj_vars['brightness'][0].get()))==native.brightness
        assert native.brightness>90
        threshold=scales[5]
        threshold.event_generate('<ButtonPress-1>',x=threshold.winfo_width()-2,y=threshold.winfo_height()//2)
        pump(.05); threshold.event_generate('<ButtonRelease-1>',x=threshold.winfo_width()-2,y=threshold.winfo_height()//2); pump()
        assert abs(app._thresh_var.get()-app._dark_thresh)<.011
        results['slider_click_native_sync']='PASS'
        app._settings_tabs.select(app._playback_tab); pump()
        syncs=[w for w in walk(app._playback_tab) if isinstance(w,module.ttk.Scale)]
        syncs[0].event_generate('<ButtonPress-1>',x=syncs[0].winfo_width()-2,y=syncs[0].winfo_height()//2)
        pump(.05); syncs[0].event_generate('<ButtonRelease-1>',x=syncs[0].winfo_width()-2,y=syncs[0].winfo_height()//2); pump()
        assert abs(app._sub_delay_var.get()-native.sub_delay)<.001
        app._sub_delay_var.set(.5); native.sub_delay=.5
        results['subtitle_slider_native_sync']='PASS'
        win.withdraw()

        root.geometry('960x580+20+20'); root.deiconify(); pump()
        app._toggle_quality_quick_panel(); pump(); q=app._quality_popup; q.geometry('+80+100'); pump()
        x,y=q.winfo_rootx()+40,q.winfo_rooty()+60
        assert app._pos_blocked_by_subwindow(x,y)
        before=app.vol_var.get()
        app._on_mousewheel(types.SimpleNamespace(widget=q,delta=120,x_root=x,y_root=y))
        assert app.vol_var.get()==before
        app._close_quality_quick_panel()
        calls=[]
        attributes=root.attributes
        with patch.object(root,'attributes',side_effect=lambda key,*a:True if key=='-fullscreen' else attributes(key,*a)), \
             patch.object(app,'_show_fullscreen_bar'),patch.object(app,'_show_context_menu',side_effect=lambda e:calls.append(e)):
            app._on_right_click(types.SimpleNamespace(x_root=app.video_canvas.winfo_rootx()+30,y_root=app.video_canvas.winfo_rooty()+30))
        assert len(calls)==1
        results['popup_input_and_fullscreen_menu']='PASS'

        app._seek_to_time('0:03'); assert pump(2,lambda:abs(native.time_pos-3)<.1)
        assert len(app._chapter_list())==2
        app._play_relative_chapter(1); assert pump(2,lambda:abs(native.time_pos-4)<.1)
        app._play_relative_chapter(-1); assert pump(2,lambda:native.time_pos<.1)
        app._show_chapters(); app._show_time_jump(); app._show_shortcuts(); app._show_playback_info(); pump()
        for popup in root.winfo_children():
            if isinstance(popup,module.tk.Toplevel) and popup.title() in ('チャプター','指定時刻へ移動','ショートカット一覧','再生情報 / GPU使用状態'):popup.destroy()
        assert 'CPU' in app._playback_info_text()
        app._on_eof_setting('リピート'); app.toggle_mute(); app._refresh_playback_state()
        assert 'リピート' in app._playback_state_var.get() and 'ミュート' in app._playback_state_var.get()
        app.toggle_mute(); app._on_eof_setting('停止')
        results['time_chapters_info_states_help']='PASS'

        if not args.software_only:
            app._on_gpu_hwdec('オン（推奨）')
        app._open_path(str(b))
        assert pump(4,lambda:app._cached_duration_ms>0 and bool(native.video_params))
        native.pause=True; pump()
        actual_hwdec=native.hwdec_current
        expected_decoder=f'GPU: {actual_hwdec}' if actual_hwdec and actual_hwdec!='no' else 'CPU'
        assert expected_decoder in app._playback_info_text()
        results['actual_hwdec_observation']={'requested':'no' if args.software_only else 'auto-safe',
                                           'actual':actual_hwdec}
        if args.software_only:
            assert native.current_vo=='null' and actual_hwdec in (None,'no',False)
            assert not forbidden_gpu_requests,forbidden_gpu_requests

        app._replace_playlist([str(a),str(b)])
        app._show_playlist(); pump()
        listing=app._playlist_listbox; listing.selection_clear(0,module.tk.END); listing.selection_set(1)
        app._playlist_move_selected(-1)
        assert app._playlist==[str(b),str(a)] and app._playlist_idx==0
        listing.selection_clear(0,module.tk.END); listing.selection_set(1); app._playlist_remove_selected()
        assert app._playlist==[str(b)]
        with patch.object(module.filedialog,'askopenfilenames',return_value=[str(a),str(a)]):app._playlist_add_files()
        assert app._playlist==[str(b),str(a)]
        saved=work/'日本語.lumveil.json'
        with patch.object(module.filedialog,'asksaveasfilename',return_value=str(saved)):app._playlist_save()
        with patch.object(module.filedialog,'askopenfilename',return_value=str(saved)):app._playlist_load()
        assert app._playlist==[str(b),str(a)]
        app._close_playlist_popup()
        results['editable_persistent_playlist']='PASS'

        app._bookmarks[str(b)]=[{'pos_ms':i*1000,'label':str(i)} for i in range(80)]
        app._show_bookmark_menu(); popup=app._menu_popup; popup.update_idletasks()
        assert popup.winfo_height()<=520
        assert any(isinstance(w,module.tk.Scrollbar) for w in walk(popup))
        scroll=next(w for w in walk(popup) if isinstance(w,module.tk.Canvas)); scroll.yview_moveto(1)
        assert scroll.yview()[1]>.99
        popup.destroy()
        results['large_bookmarks_scroll']='PASS'
        bad=work/'corrupt.mp4'; bad.write_bytes(b'not a media container'); notices=[]
        with patch.object(app,'_show_error_popup',side_effect=notices.append):
            app._open_path(str(bad)); assert pump(4,lambda:bool(notices))
        assert 'corrupt.mp4' in notices[0]
        results['corrupt_video_visible_error']='PASS'
        app._adj_vars['brightness'][0].set(27); app._on_adjust('brightness')
        app._toggle_restore_manual_settings(); assert not app._restore_manual_settings
        app._toggle_restore_manual_settings(); assert app._restore_manual_settings
        assert not errors,errors
        results['ui_callbacks']='PASS'
        app._on_close(); time.sleep(.3)
        command=[sys.executable,'-B',str(Path(__file__).resolve()),'--work-dir',str(work)]
        if args.bundle:command+=['--bundle',str(args.bundle.resolve())]
        if args.software_only:command+=['--software-only']
        for setting in ('on','off'):
            path=profile/'player_settings.json'; data=json.loads(path.read_text(encoding='utf-8'))
            data['restore_manual_settings']=setting=='on'; module._atomic_write_json(str(path),data)
            child=subprocess.run(command+['--verify-restore',setting],capture_output=True,text=True,
                                 timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
            assert child.returncode==0,child.stdout+child.stderr
        results['manual_restore_on_off_fresh_process']='PASS'
    except Exception:
        results['FAIL']=traceback.format_exc(); raise
    finally:
        if not app._closing:app._on_close()
        time.sleep(.3)
        (work/('restore-'+args.verify_restore+'.json' if args.verify_restore else 'results.json')).write_text(
            json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        if not args.verify_restore:print(json.dumps(results,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
