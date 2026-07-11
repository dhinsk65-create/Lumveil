"""
Video Player MPV版  ―  YouTube スタイル UI
依存: python-mpv, pillow, tkinterdnd2  +  ffmpeg
      Windows: mpv-2.dll を Python.exe と同じフォルダか PATH に置くこと
        → https://mpv.io/installation/ の "Windows" から入手
  pip install python-mpv pillow tkinterdnd2
"""
import os, sys, shutil, subprocess, threading, time, math, json

# libmpv-2.dll をスクリプトと同じフォルダから確実に読み込む
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]
import tkinter as tk
from tkinter import filedialog, ttk

import mpv
from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD

# ── 定数 ─────────────────────────────────────────────────────────────────
SEEK_SEC       = 5
PREV_W, PREV_H = 192, 108
CACHE_MAX      = 30
SNAP_STEP      = 2
AMF_FRC_MAX_FPS = 50.0  # 元動画がこれを超えるfpsならAMD AMFフレーム補間を自動バイパス
FFMPEG         = shutil.which("ffmpeg")

_SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
_RT_CONTRAST_SHADER_PATH = os.path.join(_SCRIPT_DIR, "shaders", "lumveil_auto_contrast.glsl")
_SHADER_DIR     = os.path.join(_SCRIPT_DIR, "shaders")

# Anime4K公式プリセット（bloc97/Anime4K のGLSL_Instructions_Advanced.mdに準拠）
# サイズはM（速度と画質のバランス型）を採用。S=速いが荒い、VL=遅いが高画質。
ANIME4K_PRESETS = {
    "なし": [],
    "モードA": [
        "Anime4K_Restore_CNN_M.glsl",
        "Anime4K_Upscale_CNN_x2_M.glsl",
    ],
    "モードB": [
        "Anime4K_Restore_CNN_Soft_M.glsl",
        "Anime4K_Upscale_CNN_x2_M.glsl",
    ],
    "モードC": [
        "Anime4K_Upscale_Denoise_CNN_x2_M.glsl",
        "Anime4K_Upscale_CNN_x2_M.glsl",
    ],
    "軽量": [
        "Anime4K_Upscale_CNN_x2_S.glsl",
    ],
}

ANIME4K_DESCRIPTIONS = {
    "なし":     "Anime4K系シェーダーを使わない",
    "モードA":  "迷ったらコレ（一般的なアニメ向け）",
    "モードB":  "元からぼやけている映像向け",
    "モードC":  "劣化が少ないきれいな映像・イラスト向け",
    "軽量":     "PCが重いとき用（クリーンアップは省略）",
}

# 設定は %APPDATA%\Lumveil\ に保存（Program Files は書き込み不可のため）
_BASE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lumveil")
os.makedirs(_BASE_DIR, exist_ok=True)

ADJ_SETTINGS    = os.path.join(_BASE_DIR, "adj_settings_mpv.json")
GPU_SETTINGS    = os.path.join(_BASE_DIR, "gpu_settings_mpv.json")
PLAYER_SETTINGS = os.path.join(_BASE_DIR, "player_settings.json")
WINDOW_SETTINGS = os.path.join(_BASE_DIR, "window_settings.json")
BOOKMARKS_FILE  = os.path.join(_BASE_DIR, "bookmarks.json")

# MPV 画像調整パラメータ（整数 -100〜100、デフォルト 0）
ADJ_PARAMS = [
    ("brightness", "輝度",          -100, 100, 0),
    ("contrast",   "実効コントラスト", -100, 300, 0),
    ("gamma",      "ガンマ",         -100, 100, 0),
    ("saturation", "彩度",           -100, 100, 0),
    ("hue",        "色相",           -100, 100, 0),
]

# 映像モード（TVの「シネマ」「ダイナミック」等に相当するプリセット）
# 値は (brightness, contrast, gamma, saturation)
PICTURE_MODES = {
    "標準":     (0, 0, 0, 0),
    "シネマ":   (0, -8, 6, -12),
    "ダイナミック": (2, 15, -4, 18),
    "鮮やか":   (0, 5, 0, 25),
}

# フォルダ内連続再生・複数ファイルD&D時の対象拡張子（open_fileのフィルタと同一）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".m2ts", ".vob", ".ogv", ".3gp", ".rmvb", ".rm", ".hevc", ".h264",
}


BG_VIDEO = "#000000"
BG_CTRL  = "#0f0f0f"
BG_BTN   = "#0f0f0f"
BG_BTN_H = "#2a2a2a"
BG_RED   = "#c0392b"
BG_ADJ   = "#111111"
COL_TXT  = "#ffffff"
COL_DIM  = "#aaaaaa"
COL_BLU  = "#4fc3f7"
COL_YEL  = "#ffcc02"
COL_GRN  = "#00b050"
SETTING_LABEL_W = 18


# ── ツールチップ ─────────────────────────────────────────────────────────
class _ToolTip:
    def __init__(self, widget, text):
        self._tip  = None
        self._text = text
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event):
        w = event.widget
        x = w.winfo_rootx() + 20
        y = w.winfo_rooty() + w.winfo_height() + 4
        self._tip = tk.Toplevel()
        self._tip.overrideredirect(True)
        self._tip.attributes("-topmost", True)
        self._tip.geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self._text, bg="#2a2a2a", fg="#e0e0e0",
                 font=("Segoe UI", 8), padx=8, pady=4,
                 justify=tk.LEFT, relief=tk.SOLID, bd=1).pack()

    def _hide(self, _event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ── サムネイルキャッシュ ──────────────────────────────────────────────────
class ThumbnailCache:
    def __init__(self, maxsize=CACHE_MAX):
        self._data  = {}
        self._order = []
        self._lock  = threading.Lock()
        self._max   = maxsize

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def put(self, key, img):
        with self._lock:
            if key in self._data:
                self._order.remove(key)
            elif len(self._data) >= self._max:
                del self._data[self._order.pop(0)]
            self._data[key] = img
            self._order.append(key)

    def clear(self):
        with self._lock:
            self._data.clear()
            self._order.clear()


# ── ffmpeg ────────────────────────────────────────────────────────────────
def _ffmpeg_pipe(path, pos_sec, w, h, timeout=4.0):
    if not FFMPEG:
        return None
    try:
        import io
        cmd = [
            FFMPEG, "-y", "-ss", f"{pos_sec:.3f}", "-i", path,
            "-vframes", "1",
            "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"),
            "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0)
        if r.returncode == 0 and r.stdout:
            return Image.open(io.BytesIO(r.stdout))
    except Exception:
        pass
    return None


def ffmpeg_thumbnail(path, pos_sec):
    return _ffmpeg_pipe(path, pos_sec, PREV_W, PREV_H)


def _frame_stats(img):
    from PIL import ImageStat
    gray = img.convert("L")
    rgb  = img.convert("RGB")
    gs   = ImageStat.Stat(gray)
    cs   = ImageStat.Stat(rgb)
    rm, gm, bm = cs.mean
    return {
        "lum_mean": gs.mean[0],
        "lum_std":  gs.stddev[0],
        "chroma":   max(rm, gm, bm) - min(rm, gm, bm),
    }


def analyze_frame(path, pos_sec):
    """ベースライン確立用: 任意の時刻のフレームをffmpegで抜き出して解析する
    （再生位置を動かさずにサンプリングする必要があるため、こちらは維持）。"""
    img = _ffmpeg_pipe(path, pos_sec, 64, 36, timeout=2.0)
    if img is None:
        return None
    return _frame_stats(img)


def analyze_current_frame(player):
    """リアルタイム補正用: 現在表示中のフレームをmpvから直接取得して解析する。
    ffmpegのプロセス起動・ファイル再オープンが不要になり、AUTO稼働中の
    CPU/ディスク負荷を大幅に減らせる（0.5秒毎にffmpegを起動していたのを廃止）。"""
    try:
        img = player.screenshot_raw(includes="video")
    except Exception:
        return None
    img = img.resize((64, 36))
    return _frame_stats(img)



# ── メインクラス ──────────────────────────────────────────────────────────
class VideoPlayer:
    def __init__(self, root: TkinterDnD.Tk):
        self.root = root
        self.root.title("Lumveil")
        self.root.configure(bg=BG_VIDEO)
        self.root.minsize(720, 460)

        # RT 自動調整（MPV整数空間で計算: 0=中立）
        self._rt_enabled  = False
        self._rt_stop     = threading.Event()
        self._rt_targets  = {k: 0.0 for k in ("brightness", "contrast", "gamma", "saturation")}
        self._rt_current  = {k: 0.0 for k in ("brightness", "contrast", "gamma", "saturation")}
        # AUTO開始時の手動調整値。暗所補正はこの値を打ち消さず、補正分だけを加算する。
        self._rt_base_adj = {k: 0.0 for k in ("brightness", "contrast", "gamma", "saturation")}
        self._rt_baseline = None
        self._rt_thread   = None
        self._dark_thresh = 1.0
        self._pre_rt_adj  = None

        self._denoise = False

        # GPU設定（ファイルから復元、なければデフォルト）
        self._gpu_scale       = "lanczos"
        self._gpu_cscale      = "spline36"
        self._gpu_deband      = False
        self._gpu_antiring    = 0.0
        self._gpu_sigmoid     = False
        self._gpu_correct_ds  = False
        self._gpu_interpolate = True
        self._gpu_hwdec       = "no"  # 初回起動時は安全側のオフを既定にする
        self._gpu_dither      = "fruit"
        self._gpu_tonemapping = "auto"
        self._gpu_deinterlace = False
        self._gpu_amf_frc     = False  # AMD AMF専用のGPUハードウェアフレーム補間
        self._gpu_glsl        = []   # list of absolute shader paths
        self._load_gpu_settings()

        self._thumb_cache   = ThumbnailCache()
        self._prev_after_id = None
        self._prev_cancel   = threading.Event()
        self._prev_img_ref  = None

        self._current_path = None
        self._bookmarks    = self._load_bookmarks()
        self._shot_dir     = os.path.join(_BASE_DIR, "Screenshots")
        self._native_dialog_open = False
        self._fs_bar_visible  = False
        self._fs_hide_after_id = None
        self._picture_mode = "標準"
        self._mode_btns    = {}
        self._a4k_btns     = {}
        self._recent_files = []
        self._resume_positions = {}
        self._always_on_top = False
        self._playlist      = []
        self._playlist_idx  = -1
        # 再生設定（初期値は従来の挙動を維持）
        self._playback_eof_action = "next"   # next / stop
        self._resume_enabled      = True
        self._folder_end_action   = "stop"   # stop / loop
        self._playlist_sort       = "name"   # name / modified
        self._ab_state      = 0   # 0=未設定 1=A地点設定済み 2=ループ中
        self.fps           = 30.0
        self.is_seeking    = False
        self._adj_vars     = {}
        self._speed        = 1.0
        self._muted        = False
        self._lbtn_prev    = False
        # 右側の操作バーは、よく使う項目だけを常時表示できる。
        # どの項目も「…」メニューから実行できるため、非表示にしても機能は失われない。
        self._toolbar_default_visible = {
            "speed", "subtitles", "audio", "screenshot", "bookmark",
            "auto_adjust", "fullscreen",
        }
        self._toolbar_visible = set(self._toolbar_default_visible)
        self._toolbar_order = [
            "fullscreen", "auto_adjust", "speed", "subtitles", "audio",
            "screenshot", "bookmark", "pin", "recent", "ab_repeat", "gpu", "about",
        ]
        self._toolbar_items = {}
        self._toolbar_auto_hidden = set()
        self._toolbar_resize_after = None

        self._build_ui()
        self.root.update()  # canvas を確実に実体化してから winfo_id を取得

        # MPV プレイヤー（wid でキャンバスに埋め込み）
        mpv_kwargs = dict(
            wid=str(self.video_canvas.winfo_id()),
            keep_open="yes",
            keep_open_pause=False,
            loglevel="error",
            vo="gpu",
            hwdec="no",
        )
        if sys.platform == "win32":
            mpv_kwargs["gpu_api"] = "d3d11"
        self.player = mpv.MPV(**mpv_kwargs)
        self.player.volume = 80

        # duration/pauseは変化頻度が低いため、毎ティックの問い合わせをやめて
        # mpv側からのプロパティ変化通知をキャッシュする方式に変更（負荷軽減）。
        # time-posは再生中ほぼ毎フレーム変化し通知が来すぎるため、従来通り
        # 定期ポーリング（_get_time_ms）のままにしている。
        self._cached_duration_ms = 0.0
        self._cached_pause       = True
        self._mpv_observer_fns   = []

        @self.player.property_observer("duration")
        def _obs_duration(_name, value):
            self.root.after(0, self._on_duration_prop, value)
        self._mpv_observer_fns.append(_obs_duration)

        @self.player.property_observer("pause")
        def _obs_pause(_name, value):
            self.root.after(0, self._on_pause_prop, value)
        self._mpv_observer_fns.append(_obs_pause)

        # 連続再生: ファイル終端に達したらプレイリストの次のファイルへ自動移行
        @self.player.property_observer("eof-reached")
        def _obs_eof(_name, value):
            if value:
                self.root.after(0, self._on_eof_reached)
        self._mpv_observer_fns.append(_obs_eof)

        self._load_player_settings()

        # ファイルロード後に調整値・GPU設定を再適用
        self.player.event_callback("file-loaded")(self._on_mpv_file_loaded)
        self._apply_gpu_settings()

        self._build_adj_win()
        self._build_gpu_win()
        self._bind_keys()
        self._setup_dnd()
        self._setup_video_click()
        self._update_loop()
        self._blend_loop()
        self._manual_status_loop()
        self._autosave_resume_loop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self._update_resume_position()
        self._save_window_settings()
        self._rt_enabled = False
        self._rt_stop.set()
        # AUTO自動補正のスレッドはplayer.screenshot_raw()でmpvに直接アクセスするため、
        # 完全に停止してからterminate()しないとメインスレッドと競合してフリーズする。
        if self._rt_thread and self._rt_thread.is_alive():
            self._rt_thread.join(timeout=2.0)
        # property_observerを解除せずにterminate()すると、mpvのイベントスレッドと
        # デッドロックしてアプリが終了不能になることを確認済み。必ず先に解除する。
        for fn in self._mpv_observer_fns:
            try:
                fn.unobserve_mpv_properties()
            except Exception:
                pass
        try:
            self.player.terminate()
        except Exception:
            pass
        self.root.destroy()

    def _save_window_settings(self):
        try:
            geo = self.root.geometry()  # "WxH+X+Y"
            with open(WINDOW_SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"geometry": geo}, f)
        except Exception:
            pass

    def _load_player_settings(self):
        try:
            with open(PLAYER_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            vol = max(0, min(100, int(data.get("volume", 80))))
            self.vol_var.set(vol)
            self.player.volume = float(vol)
            shot_dir = data.get("screenshot_dir")
            if shot_dir:
                self._shot_dir = shot_dir
            self._recent_files = data.get("recent_files", [])
            self._resume_positions = data.get("resume_positions", {})
            self._always_on_top = bool(data.get("always_on_top", False))
            self._playback_eof_action = data.get("playback_eof_action", "next")
            self._resume_enabled = bool(data.get("resume_enabled", True))
            self._folder_end_action = data.get("folder_end_action", "stop")
            self._playlist_sort = data.get("playlist_sort", "name")
            saved_toolbar = data.get("toolbar_visible")
            if isinstance(saved_toolbar, list):
                valid = set(self._toolbar_item_definitions())
                self._toolbar_visible = set(saved_toolbar) & valid
            saved_order = data.get("toolbar_order")
            if isinstance(saved_order, list):
                valid = set(self._toolbar_order)
                ordered = [key for key in saved_order if key in valid]
                self._toolbar_order = ordered + [key for key in self._toolbar_order
                                                 if key not in ordered]
            if self._always_on_top:
                self.root.attributes("-topmost", True)
                self._pin_btn.config(fg=COL_GRN)
            if self._toolbar_items:
                self._refresh_toolbar()
        except Exception:
            pass

    def _save_player_settings(self):
        try:
            with open(PLAYER_SETTINGS, "w", encoding="utf-8") as f:
                json.dump({
                    "volume": self.vol_var.get(),
                    "screenshot_dir": self._shot_dir,
                    "recent_files": self._recent_files,
                    "resume_positions": self._resume_positions,
                    "always_on_top": self._always_on_top,
                    "toolbar_visible": sorted(self._toolbar_visible),
                    "toolbar_order": self._toolbar_order,
                    "playback_eof_action": self._playback_eof_action,
                    "resume_enabled": self._resume_enabled,
                    "folder_end_action": self._folder_end_action,
                    "playlist_sort": self._playlist_sort,
                }, f, ensure_ascii=False)
        except Exception:
            pass

    def _toggle_always_on_top(self):
        self._always_on_top = not self._always_on_top
        self.root.attributes("-topmost", self._always_on_top)
        self._pin_btn.config(fg=COL_GRN if self._always_on_top else COL_TXT)
        self._save_player_settings()

    # ── 続きから再生 ──────────────────────────────────────────────────────
    # 動画の先頭・末尾付近は「続きから」の意味がないため保存対象から除外する。
    _RESUME_MARGIN_SEC = 5.0

    def _update_resume_position(self):
        path = self._current_path
        if not path:
            return
        if not self._resume_enabled:
            self._resume_positions.pop(path, None)
            self._save_player_settings()
            return
        pos_ms = self._get_time_ms()
        dur_ms = self._get_duration_ms()
        pos_sec = pos_ms / 1000.0
        dur_sec = dur_ms / 1000.0
        if dur_sec > 0 and self._RESUME_MARGIN_SEC < pos_sec < dur_sec - self._RESUME_MARGIN_SEC:
            self._resume_positions[path] = pos_sec
        else:
            self._resume_positions.pop(path, None)
        self._save_player_settings()

    def _autosave_resume_loop(self):
        self._update_resume_position()
        self.root.after(10000, self._autosave_resume_loop)

    def _add_recent_file(self, path):
        path = os.path.abspath(path)
        self._recent_files = [p for p in self._recent_files if p != path]
        self._recent_files.insert(0, path)
        self._recent_files = self._recent_files[:10]
        self._save_player_settings()

    def _on_mpv_file_loaded(self, _event):
        """ファイルロード後に画像調整値・ノイズ設定・AMFフレーム補間を再適用し、続きの位置へシーク"""
        self.root.after(200, self._apply_all_adj)
        if self._denoise or self._gpu_amf_frc:
            self.root.after(300, self._apply_vf_chain)
        resume_sec = self._resume_positions.get(self._current_path) if self._resume_enabled else None
        if resume_sec:
            self.root.after(200, lambda s=resume_sec: self._resume_seek(s))

    def _resume_seek(self, pos_sec):
        try:
            self.player.seek(pos_sec, reference="absolute", precision="exact")
        except Exception:
            pass

    def _apply_all_adj(self):
        for key, *_ in ADJ_PARAMS:
            self._on_adjust(key)

    # ── ボタンヘルパー ────────────────────────────────────────────────────

    def _btn(self, parent, text, cmd, fg=COL_TXT, bg=None,
             font=("Segoe UI", 11), pad=(8, 4), tooltip=None):
        bg = bg or BG_BTN
        b  = tk.Button(parent, text=text, command=cmd,
                       bg=bg, fg=fg, relief=tk.FLAT, bd=0,
                       font=font, padx=pad[0], pady=pad[1],
                       cursor="hand2",
                       activebackground=BG_BTN_H, activeforeground=COL_TXT)
        b.bind("<Enter>", lambda e, b=b, abg=BG_BTN_H: b.config(bg=abg))
        b.bind("<Leave>", lambda e, b=b, nbg=bg: b.config(bg=nbg))
        if tooltip:
            self._add_tooltip(b, tooltip)
        return b

    def _fixed_btn(self, parent, text, cmd, w=34, h=28,
                   fg=COL_TXT, bg=None, font=("Segoe UI", 11), tooltip=None):
        bg = bg or BG_CTRL
        f  = tk.Frame(parent, bg=BG_CTRL, width=w, height=h)
        f.pack_propagate(False)
        b  = tk.Button(f, text=text, command=cmd,
                       bg=bg, fg=fg, relief=tk.FLAT, bd=0,
                       font=font, cursor="hand2",
                       activebackground=BG_BTN_H, activeforeground=COL_TXT)
        b.bind("<Enter>", lambda e: b.config(bg=BG_BTN_H))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        b.pack(fill=tk.BOTH, expand=True)
        if tooltip:
            self._add_tooltip(b, tooltip)
        return f, b

    def _add_tooltip(self, widget, text):
        state = {"win": None, "after_id": None}

        def show():
            state["after_id"] = None
            win = tk.Toplevel(self.root)
            state["win"] = win
            win.overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(win, text=text, bg="#222222", fg=COL_TXT,
                     font=("Segoe UI", 8), padx=6, pady=2).pack()
            x = widget.winfo_rootx() + widget.winfo_width() // 2 - 10
            y = widget.winfo_rooty() - 24
            win.geometry(f"+{max(x,0)}+{max(y,0)}")

        def on_enter(_e):
            state["after_id"] = self.root.after(400, show)

        def on_leave(_e):
            if state["after_id"]:
                self.root.after_cancel(state["after_id"])
                state["after_id"] = None
            if state["win"]:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", on_enter, add="+")
        widget.bind("<Leave>", on_leave, add="+")
        widget.bind("<Button-1>", on_leave, add="+")

    def _sep(self, parent):
        tk.Frame(parent, bg="#333333", width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=6)

    # ── UI構築 ────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.video_canvas = tk.Canvas(self.root, bg=BG_VIDEO,
                                      highlightthickness=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)

        self.ctrl_bar = tk.Frame(self.root, bg=BG_CTRL)
        self.ctrl_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(self.ctrl_bar, bg="#282828", height=1).pack(fill=tk.X)

        seek_row = tk.Frame(self.ctrl_bar, bg=BG_CTRL)
        seek_row.pack(fill=tk.X, padx=12, pady=(6, 2))

        self.time_var = tk.StringVar(value="0:00:00")
        tk.Label(seek_row, textvariable=self.time_var,
                 bg=BG_CTRL, fg=COL_DIM,
                 font=("Consolas", 9), width=7).pack(side=tk.LEFT)

        self.seek_var = tk.DoubleVar()
        self.seekbar  = ttk.Scale(seek_row, from_=0, to=1000,
                                  orient=tk.HORIZONTAL, variable=self.seek_var,
                                  command=self._on_seek_drag)
        self.seekbar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.seekbar.bind("<ButtonPress-1>",   self._on_seekbar_press)
        self.seekbar.bind("<ButtonRelease-1>", self._on_seek_release)
        self.seekbar.bind("<Motion>",          self._on_seekbar_motion)
        self.seekbar.bind("<Leave>",           self._hide_preview)

        self.dur_var = tk.StringVar(value="0:00:00")
        tk.Label(seek_row, textvariable=self.dur_var,
                 bg=BG_CTRL, fg=COL_DIM,
                 font=("Consolas", 9), width=7).pack(side=tk.LEFT)

        btn_row = tk.Frame(self.ctrl_bar, bg=BG_CTRL)
        btn_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._btn_row = btn_row

        f, _ = self._fixed_btn(btn_row, "⊕", self.open_file, w=30,
                               font=("Segoe UI", 10), tooltip="ファイルを開く")
        f.pack(side=tk.LEFT, padx=1)
        self._sep(btn_row)

        f, _ = self._fixed_btn(btn_row, "⏮", self.seek_backward, w=32,
                               font=("Segoe UI", 13), tooltip="5秒戻る")
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(btn_row, "⏴", self.frame_backward, w=30,
                               font=("Segoe UI", 11), tooltip="1コマ戻る")
        f.pack(side=tk.LEFT, padx=1)

        _pf, self.play_btn = self._fixed_btn(btn_row, "▶", self.toggle_play,
                                             w=42, font=("Segoe UI", 16),
                                             tooltip="再生 / 一時停止")
        _pf.pack(side=tk.LEFT, padx=2)

        f, _ = self._fixed_btn(btn_row, "⏵", self.frame_forward, w=30,
                               font=("Segoe UI", 11), tooltip="1コマ進む")
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(btn_row, "⏭", self.seek_forward, w=32,
                               font=("Segoe UI", 13), tooltip="5秒進む")
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(btn_row, "⏹", self.stop, w=30,
                               font=("Segoe UI", 11), tooltip="停止")
        f.pack(side=tk.LEFT, padx=1)

        self._sep(btn_row)

        _mf, self._mute_btn = self._fixed_btn(btn_row, "🔊", self._toggle_volume_popup,
                                              w=32, font=("Segoe UI", 12),
                                              tooltip="音量（右クリックでミュート）")
        _mf.pack(side=tk.LEFT)
        self._mute_btn.bind("<Button-3>", lambda _e: self.toggle_mute())
        self._volume_popup = None

        self.vol_var = tk.IntVar(value=80)
        self._vol_pending = False

        self._time_btn_var = tk.StringVar(value="0:00:00 / 0:00:00")
        tk.Label(btn_row, textvariable=self._time_btn_var,
                 bg=BG_CTRL, fg=COL_DIM,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)

        self._toolbar_right = tk.Frame(btn_row, bg=BG_CTRL)
        self._toolbar_right.pack(side=tk.RIGHT)
        btn_row.bind("<Configure>", self._on_toolbar_resize)
        right = self._toolbar_right

        f, self._fs_btn = self._fixed_btn(right, "⛶", self.toggle_fullscreen,
                                          w=30, font=("Segoe UI", 12), tooltip="全画面表示")
        self._toolbar_items["fullscreen"] = f
        f, self._pin_btn = self._fixed_btn(right, "📌", self._toggle_always_on_top,
                                           w=30, font=("Segoe UI", 11), tooltip="常に手前に表示 (T)")
        self._toolbar_items["pin"] = f
        f, _ = self._fixed_btn(right, "ⓘ", self._show_about, w=24,
                               font=("Segoe UI", 9), tooltip="Lumveilについて")
        self._toolbar_items["about"] = f
        f, self._auto_btn = self._fixed_btn(right, "⚡ AUTO", self._toggle_rt_adj,
                                            w=66, font=("Segoe UI", 9), tooltip="暗闇補正(自動)のON/OFF")
        self._toolbar_items["auto_adjust"] = f
        self._settings_btn, self._settings_button = self._fixed_btn(
            right, "⚙ 設定", self._toggle_adj_win,
            w=58, font=("Segoe UI", 9), tooltip="設定を開く")
        f, _ = self._fixed_btn(right, "⚙ GPU", self._toggle_gpu_win, w=54,
                               font=("Segoe UI", 9), tooltip="GPU/シェーダー設定")
        self._toolbar_items["gpu"] = f
        f, _ = self._fixed_btn(right, "🕘", self._show_recent_menu, w=30,
                               font=("Segoe UI", 11), tooltip="最近開いたファイル")
        self._toolbar_items["recent"] = f
        f, _ = self._fixed_btn(right, "🎵", lambda: self._show_track_menu("audio"), w=30,
                               font=("Segoe UI", 11), tooltip="音声トラック")
        self._toolbar_items["audio"] = f
        f, _ = self._fixed_btn(right, "💬", lambda: self._show_track_menu("sub"), w=30,
                               font=("Segoe UI", 11), tooltip="字幕トラック")
        self._toolbar_items["subtitles"] = f
        f, self._ab_btn = self._fixed_btn(right, "A-B", self._toggle_ab_loop, w=34,
                                          font=("Segoe UI", 9), tooltip="A-Bリピート")
        self._toolbar_items["ab_repeat"] = f
        f, self._shot_btn = self._fixed_btn(right, "SS", self._take_screenshot, w=30,
                                            font=("Consolas", 9, "bold"), tooltip="スクリーンショット")
        self._shot_btn.bind("<Button-3>", self._show_shot_menu)
        self._toolbar_items["screenshot"] = f
        f, _ = self._fixed_btn(right, "🔖", self._show_bookmark_menu, w=30,
                               font=("Segoe UI", 11), tooltip="ブックマーク")
        self._toolbar_items["bookmark"] = f

        # 速度は数値ボタンに集約し、クリックで一覧から選ぶ。
        spd = tk.Frame(right, bg=BG_CTRL, width=50, height=28)
        spd.pack_propagate(False)
        self._speed_var = tk.StringVar(value="1.00×")
        self._speed_btn = tk.Button(spd, textvariable=self._speed_var,
                                    command=self._show_speed_menu,
                                    bg=BG_CTRL, fg=COL_BLU, relief=tk.FLAT, bd=0,
                                    font=("Consolas", 9), cursor="hand2",
                                    activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._speed_btn.pack(fill=tk.BOTH, expand=True)
        self._add_tooltip(self._speed_btn, "再生速度を選択")
        self._toolbar_items["speed"] = spd

        self._more_btn = self._btn(right, "⋯", self._show_toolbar_menu,
                                   tooltip="その他の操作",
                                   font=("Segoe UI", 14), pad=(7, 1))
        self._refresh_toolbar()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Horizontal.TScale",
                        background=BG_CTRL, troughcolor="#333333",
                        sliderlength=14, sliderrelief=tk.FLAT)
        style.configure("Adj.Horizontal.TScale",
                        background=BG_ADJ, troughcolor="#333333",
                        sliderlength=12, sliderrelief=tk.FLAT)
        style.configure("Lumveil.TNotebook", background=BG_ADJ, borderwidth=0)
        style.configure("Lumveil.TNotebook.Tab", background=BG_BTN, foreground=COL_TXT,
                        padding=(11, 6), font=("Segoe UI", 10))
        style.map("Lumveil.TNotebook.Tab",
                  background=[("selected", BG_BTN_H)], foreground=[("selected", COL_BLU)])

        self.prev_popup = tk.Toplevel(self.root)
        self.prev_popup.overrideredirect(True)
        self.prev_popup.withdraw()
        self.prev_popup.configure(bg="#000000")
        self.prev_img_label = tk.Label(self.prev_popup, bg="black",
                                       bd=1, relief=tk.SOLID)
        self.prev_img_label.pack()
        self.prev_time_label = tk.Label(self.prev_popup, bg="black", fg="white",
                                        font=("Consolas", 8), pady=2)
        self.prev_time_label.pack()

    # ── 操作バーの表示設定 ────────────────────────────────────────────────

    def _toolbar_item_definitions(self):
        """ID: （メニュー表示名、実行関数）。すべて「…」から利用できる。"""
        return {
            "fullscreen":  ("全画面表示", self.toggle_fullscreen),
            "pin":         ("常に手前に表示", self._toggle_always_on_top),
            "about":       ("Lumveilについて", self._show_about),
            "auto_adjust": ("暗闇補正（AUTO）", self._toggle_rt_adj),
            "settings":    ("設定", self._toggle_adj_win),
            "gpu":         ("GPU / シェーダー設定", self._toggle_gpu_win),
            "recent":      ("最近開いたファイル", self._show_recent_menu),
            "audio":       ("音声トラック", lambda: self._show_track_menu("audio")),
            "subtitles":   ("字幕トラック", lambda: self._show_track_menu("sub")),
            "ab_repeat":   ("A-Bリピート", self._toggle_ab_loop),
            "screenshot":  ("スクリーンショットを保存", self._take_screenshot),
            "bookmark":    ("ブックマーク", self._show_bookmark_menu),
            "speed":       ("再生速度を選択", self._show_speed_menu),
        }

    def _refresh_toolbar(self):
        if not self._toolbar_items:
            return
        for frame in self._toolbar_items.values():
            frame.pack_forget()
        self._more_btn.pack_forget()
        # side=RIGHT のため、表示順の逆から詰めて左→右の順序を保つ。
        self._settings_btn.pack_forget()
        self._more_btn.pack(side=tk.RIGHT, padx=(4, 1))
        self._settings_btn.pack(side=tk.RIGHT, padx=1)
        for key in reversed(self._toolbar_order):
            if key in self._toolbar_visible and key not in self._toolbar_auto_hidden:
                self._toolbar_items[key].pack(side=tk.RIGHT, padx=1)
        self.root.after_idle(self._update_toolbar_overflow)

    def _on_toolbar_resize(self, _event=None):
        """連続するリサイズイベントをまとめて、操作項目の退避を再計算する。"""
        if self._toolbar_resize_after:
            self.root.after_cancel(self._toolbar_resize_after)
        self._toolbar_resize_after = self.root.after(40, self._update_toolbar_overflow)

    def _update_toolbar_overflow(self):
        self._toolbar_resize_after = None
        if not hasattr(self, "_btn_row") or not hasattr(self, "_settings_btn"):
            return
        self._btn_row.update_idletasks()
        left_width = sum(
            child.winfo_width() for child in self._btn_row.winfo_children()
            if child is not self._toolbar_right
        )
        available = max(0, self._btn_row.winfo_width() - left_width - 8)
        fixed_width = self._settings_btn.winfo_reqwidth() + self._more_btn.winfo_reqwidth() + 8
        visible_keys = [key for key in self._toolbar_order if key in self._toolbar_visible]
        remaining = fixed_width + sum(self._toolbar_items[key].winfo_reqwidth() + 2
                                      for key in visible_keys)
        hidden = set()
        # 設定された表示順の右側から、必要な分だけメニューへ一時退避する。
        for key in reversed(self._toolbar_order):
            if key in self._toolbar_visible and remaining > available:
                hidden.add(key)
                remaining -= self._toolbar_items[key].winfo_reqwidth() + 2
        if hidden != self._toolbar_auto_hidden:
            self._toolbar_auto_hidden = hidden
            self._refresh_toolbar()

    def _set_toolbar_item_visible(self, key, var):
        if var.get():
            self._toolbar_visible.add(key)
        else:
            self._toolbar_visible.discard(key)
        self._refresh_toolbar()
        self._refresh_toolbar_settings()
        self._save_player_settings()

    def _show_toolbar_settings(self):
        self._settings_tabs.select(self._toolbar_tab)
        if not self._settings_win.winfo_viewable():
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._settings_win.geometry(f"+{x}+{y}")
            self._settings_win.deiconify()
        self._settings_win.lift()

    def _refresh_toolbar_settings(self):
        if not hasattr(self, "_toolbar_preview"):
            return
        for child in self._toolbar_preview.winfo_children():
            child.destroy()
        self._toolbar_preview_items = {}
        for key in self._toolbar_order:
            if key in self._toolbar_visible:
                icon = self._toolbar_icons[key]
                item = tk.Label(self._toolbar_preview, text=icon, bg=BG_BTN, fg=COL_TXT,
                                font=("Segoe UI", 11), padx=7, pady=4, cursor="fleur")
                item.pack(side=tk.LEFT, padx=1)
                item.bind("<ButtonPress-1>", lambda e, k=key: self._toolbar_preview_drag_start(e, k))
                item.bind("<B1-Motion>", self._toolbar_preview_drag_motion)
                item.bind("<ButtonRelease-1>", self._toolbar_drag_end)
                self._toolbar_preview_items[key] = item
        # side=RIGHT は先にpackしたものが最右端になる。
        tk.Label(self._toolbar_preview, text="⋯", bg="#2a2a2a", fg=COL_TXT,
                 font=("Segoe UI", 13), padx=7, pady=2).pack(side=tk.RIGHT, padx=1)
        tk.Label(self._toolbar_preview, text="⚙ 設定", bg="#2a2a2a", fg=COL_TXT,
                 font=("Segoe UI", 9), padx=7, pady=5).pack(side=tk.RIGHT, padx=1)

        for key, row in getattr(self, "_toolbar_rows", {}).items():
            active = key == getattr(self, "_toolbar_drag_target", None)
            row.config(bg="#2a4a6a" if active else BG_ADJ)
            for child in row.winfo_children():
                if isinstance(child, tk.Label):
                    child.config(bg="#2a4a6a" if active else BG_ADJ)

    def _toolbar_drag_start(self, event, key):
        self._toolbar_drag_key = key
        self._toolbar_drag_target = key
        self._refresh_toolbar_settings()

    def _toolbar_preview_drag_start(self, _event, key):
        self._toolbar_drag_key = key
        self._toolbar_drag_target = key

    def _toolbar_preview_drag_motion(self, event):
        if not getattr(self, "_toolbar_drag_key", None):
            return
        visible = [key for key in self._toolbar_order if key in self._toolbar_preview_items]
        target = None  # 最後の項目より右なら末尾へ追加する。
        for key in visible:
            item = self._toolbar_preview_items[key]
            if event.x_root < item.winfo_rootx() + item.winfo_width() // 2:
                target = key
                break
        self._toolbar_drag_target = target

    def _toolbar_drag_motion(self, event):
        if not getattr(self, "_toolbar_drag_key", None):
            return
        y = event.y_root
        target = self._toolbar_drag_key
        for key in self._toolbar_order:
            row = self._toolbar_rows[key]
            if y < row.winfo_rooty() + row.winfo_height() // 2:
                target = key
                break
        self._toolbar_drag_target = target
        self._refresh_toolbar_settings()

    def _toolbar_drag_end(self, _event):
        key = getattr(self, "_toolbar_drag_key", None)
        target = getattr(self, "_toolbar_drag_target", None)
        self._toolbar_drag_key = None
        self._toolbar_drag_target = None
        if key and target is None:
            self._toolbar_order.remove(key)
            self._toolbar_order.append(key)
            self._refresh_toolbar()
            self._save_player_settings()
            self._build_toolbar_settings_tab()
            self._settings_tabs.select(self._toolbar_tab)
        elif key and target and key != target:
            self._toolbar_order.remove(key)
            self._toolbar_order.insert(self._toolbar_order.index(target), key)
            self._refresh_toolbar()
            self._save_player_settings()
            self._build_toolbar_settings_tab()
            self._settings_tabs.select(self._toolbar_tab)
        else:
            self._refresh_toolbar_settings()

    def _show_toolbar_menu(self):
        menu = tk.Menu(self.root, tearoff=False, bg=BG_ADJ, fg=COL_TXT,
                       activebackground=BG_BTN_H, activeforeground=COL_TXT,
                       font=("Segoe UI", 9))
        definitions = self._toolbar_item_definitions()
        for key in ("fullscreen", "pin", "auto_adjust", "settings", "gpu", "recent",
                    "subtitles", "audio", "ab_repeat", "screenshot", "bookmark", "about"):
            label, command = definitions[key]
            menu.add_command(label=label, command=command)
        menu.add_separator()
        menu.add_command(label="再生速度を選択…", command=self._show_speed_menu)
        menu.add_command(label="スクリーンショットの保存先…", command=self._show_shot_menu)
        menu.add_separator()

        menu.add_command(label="操作バーを設定…", command=self._show_toolbar_settings)
        try:
            menu.tk_popup(self._more_btn.winfo_rootx(),
                          self._more_btn.winfo_rooty() + self._more_btn.winfo_height())
        finally:
            menu.grab_release()

    # ── 最近開いたファイル ──────────────────────────────────────────────

    def _show_recent_menu(self):
        win = tk.Toplevel(self.root)
        self._menu_popup = win
        win.overrideredirect(True)
        win.configure(bg=BG_ADJ)
        tk.Label(win, text="最近開いたファイル", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 9, "bold"), pady=4).pack(fill=tk.X)

        existing = [p for p in self._recent_files if os.path.exists(p)]
        if existing != self._recent_files:
            self._recent_files = existing
            self._save_player_settings()

        if not self._recent_files:
            tk.Label(win, text="（履歴なし）", bg=BG_ADJ, fg=COL_DIM,
                     font=("Segoe UI", 9), pady=6, padx=14).pack()

        def pick(p):
            win.destroy()
            self._open_path(p)

        for p in self._recent_files:
            fg = COL_GRN if p == self._current_path else COL_TXT
            b = self._btn(win, os.path.basename(p), lambda p=p: pick(p),
                         fg=fg, bg=BG_ADJ, pad=(14, 4))
            b.pack(fill=tk.X, padx=4, pady=1)

        win.update_idletasks()
        x = self.root.winfo_pointerx() - win.winfo_reqwidth() // 2
        y = self.root.winfo_rooty() + self.root.winfo_height() - 120 - win.winfo_reqheight()
        win.geometry(f"+{max(x,0)}+{max(y,0)}")
        win.bind("<FocusOut>", lambda e: win.destroy())
        win.focus_force()

    # ── ブックマーク ─────────────────────────────────────────────────────

    def _load_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._bookmarks, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _fmt_ms(self, pos_ms):
        h, rem = divmod(max(0, pos_ms) // 1000, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _show_bookmark_menu(self):
        if not self._current_path:
            return
        key = self._current_path
        marks = sorted(self._bookmarks.get(key, []), key=lambda m: m["pos_ms"])

        win = tk.Toplevel(self.root)
        self._menu_popup = win
        win.overrideredirect(True)
        win.configure(bg=BG_ADJ)
        tk.Label(win, text="ブックマーク", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 9, "bold"), pady=4).pack(fill=tk.X)

        def add_current():
            pos_ms = max(0, self._get_time_ms())
            lst = self._bookmarks.setdefault(key, [])
            lst.append({"pos_ms": pos_ms, "label": self._fmt_ms(pos_ms)})
            self._save_bookmarks()
            win.destroy()
            self._show_bookmark_menu()

        self._btn(win, "＋ 現在位置を追加", add_current, bg=BG_ADJ,
                  pad=(14, 4)).pack(fill=tk.X, padx=4, pady=(1, 4))

        if not marks:
            tk.Label(win, text="（ブックマークなし）", bg=BG_ADJ, fg=COL_DIM,
                     font=("Segoe UI", 9), pady=6, padx=14).pack()

        def jump(pos_ms):
            self.player.seek(pos_ms / 1000.0, reference="absolute", precision="exact")
            win.destroy()

        def remove(pos_ms):
            self._bookmarks[key] = [m for m in self._bookmarks.get(key, [])
                                    if m["pos_ms"] != pos_ms]
            self._save_bookmarks()
            win.destroy()
            self._show_bookmark_menu()

        for m in marks:
            row = tk.Frame(win, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=4, pady=1)
            self._btn(row, f"{m['label']}", lambda p=m["pos_ms"]: jump(p),
                      bg=BG_ADJ, pad=(14, 4)).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._btn(row, "✕", lambda p=m["pos_ms"]: remove(p),
                      bg=BG_RED, pad=(6, 4)).pack(side=tk.LEFT, padx=(2, 0))

        win.update_idletasks()
        x = self.root.winfo_pointerx() - win.winfo_reqwidth() // 2
        y = self.root.winfo_rooty() + self.root.winfo_height() - 120 - win.winfo_reqheight()
        win.geometry(f"+{max(x,0)}+{max(y,0)}")
        win.bind("<FocusOut>", lambda e: win.destroy())
        win.focus_force()

    # ── スクリーンショット ───────────────────────────────────────────────

    def _take_screenshot(self):
        if not self._current_path:
            return
        try:
            os.makedirs(self._shot_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(self._current_path))[0]
            # _get_time_ms()はfloatを返すため、intにしないと":02d"整形でValueErrorになる
            pos_ms = int(max(0, self._get_time_ms()))
            ts = time.strftime("%Y%m%d_%H%M%S")
            h, rem = divmod(pos_ms // 1000, 3600)
            m, s = divmod(rem, 60)
            fname = f"{base}_{h:02d}{m:02d}{s:02d}_{ts}.png"
            path = os.path.join(self._shot_dir, fname)
            # デフォルト(字幕・GLSLシェーダー等の表示状態込み)でキャプチャ
            self.player.screenshot_to_file(path)
            self._flash_shot_btn("✓")
        except Exception as e:
            self._flash_shot_btn("✗")
            self._show_error_popup(f"スクリーンショット保存に失敗しました:\n{e}")

    def _show_error_popup(self, message):
        win = tk.Toplevel(self.root)
        self._menu_popup = win
        win.overrideredirect(True)
        win.configure(bg=BG_ADJ)
        tk.Label(win, text=message, bg=BG_ADJ, fg="#ff6b6b",
                 font=("Segoe UI", 9), justify="left",
                 wraplength=320, padx=14, pady=10).pack()
        self._btn(win, "閉じる", win.destroy,
                  bg=BG_ADJ, pad=(14, 4)).pack(pady=(0, 10))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(x,0)}+{max(y,0)}")
        win.focus_force()

    def _flash_shot_btn(self, text):
        orig = "📷"
        self._shot_btn.config(text=text)
        self.root.after(700, lambda: self._shot_btn.config(text=orig))

    def _open_shot_folder(self):
        os.makedirs(self._shot_dir, exist_ok=True)
        try:
            os.startfile(self._shot_dir)
        except Exception:
            pass

    def _change_shot_folder(self):
        d = self._ask_file(filedialog.askdirectory,
                           title="スクリーンショット保存先を選択",
                           initialdir=self._shot_dir)
        if d:
            self._shot_dir = d
            self._save_player_settings()
            if hasattr(self, "_toolbar_tab"):
                self._build_toolbar_settings_tab()
                if self._settings_win.winfo_viewable():
                    self._settings_tabs.select(self._toolbar_tab)

    def _show_shot_menu(self, event=None):
        win = tk.Toplevel(self.root)
        self._menu_popup = win
        win.overrideredirect(True)
        win.configure(bg=BG_ADJ)
        self._btn(win, "📂 保存先フォルダを開く", self._chain(win.destroy, self._open_shot_folder),
                  bg=BG_ADJ, pad=(14, 4)).pack(fill=tk.X, padx=4, pady=1)
        self._btn(win, "✏ 保存先フォルダを変更...", self._chain(win.destroy, self._change_shot_folder),
                  bg=BG_ADJ, pad=(14, 4)).pack(fill=tk.X, padx=4, pady=1)
        win.update_idletasks()
        x = self.root.winfo_pointerx() - win.winfo_reqwidth() // 2
        y = self.root.winfo_rooty() + self.root.winfo_height() - 120 - win.winfo_reqheight()
        win.geometry(f"+{max(x,0)}+{max(y,0)}")
        win.bind("<FocusOut>", lambda e: win.destroy())
        win.focus_force()

    def _chain(self, *fns):
        def run():
            for fn in fns:
                fn()
        return run

    # ── 字幕・音声トラック選択 ────────────────────────────────────────────

    def _show_track_menu(self, kind):
        """kind: 'sub' or 'audio'（mpvのtrack-listのtype、mpv側は'sub'/'audio'）"""
        try:
            tracks = [t for t in (self.player.track_list or []) if t.get("type") == kind]
        except Exception:
            tracks = []

        win = tk.Toplevel(self.root)
        self._menu_popup = win
        win.overrideredirect(True)
        win.configure(bg=BG_ADJ)
        title = "字幕" if kind == "sub" else "音声"
        tk.Label(win, text=title, bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 9, "bold"), pady=4).pack(fill=tk.X)

        cur = self.player.sid if kind == "sub" else self.player.aid

        def pick(track_id):
            try:
                if kind == "sub":
                    self.player.sid = track_id
                else:
                    self.player.aid = track_id
            except Exception:
                pass
            win.destroy()

        if kind == "sub":
            off_fg = COL_GRN if not cur else COL_TXT
            self._btn(win, "オフ", lambda: pick(False), bg=BG_ADJ,
                      pad=(14, 4)).pack(fill=tk.X, padx=4, pady=1)

        if not tracks:
            tk.Label(win, text="（トラックなし）", bg=BG_ADJ, fg=COL_DIM,
                     font=("Segoe UI", 9), pady=6, padx=14).pack()
        for t in tracks:
            lang  = t.get("lang") or t.get("metadata", {}).get("language", "")
            label = t.get("title") or lang or f"トラック {t['id']}"
            if lang and t.get("title"):
                label += f" ({lang})"
            fg = COL_GRN if t.get("selected") else COL_TXT
            b = self._btn(win, label, lambda i=t["id"]: pick(i), bg=BG_ADJ,
                         pad=(14, 4))
            b.pack(fill=tk.X, padx=4, pady=1)

        win.update_idletasks()
        # トリガーボタンの少し上に開く
        x = self.root.winfo_pointerx() - win.winfo_reqwidth() // 2
        y = self.root.winfo_rooty() + self.root.winfo_height() - 120 - win.winfo_reqheight()
        win.geometry(f"+{max(x,0)}+{max(y,0)}")
        win.bind("<FocusOut>", lambda e: win.destroy())
        win.focus_force()

    # ── About ─────────────────────────────────────────────────────────────

    def _show_about(self):
        win = tk.Toplevel(self.root)
        self._about_win = win
        win.title("About")
        win.configure(bg=BG_ADJ)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        # ウィンドウを親の中央に配置
        win.update_idletasks()
        pw, ph = self.root.winfo_width(), self.root.winfo_height()
        px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
        w, h = 300, 180
        win.geometry(f"{w}x{h}+{px + (pw - w)//2}+{py + (ph - h)//2}")

        tk.Label(win, text="Lumveil",
                 bg=BG_ADJ, fg=COL_BLU,
                 font=("Segoe UI", 20, "bold")).pack(pady=(24, 4))
        tk.Label(win, text="ver. 1.6",
                 bg=BG_ADJ, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack()
        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=30, pady=14)
        tk.Label(win, text="ふぁん",
                 bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 9)).pack()
        self._btn(win, "閉じる", win.destroy,
                  pad=(20, 5)).pack(pady=(16, 0))

    # ── 画像調整ウィンドウ ─────────────────────────────────────────────────

    def _add_settings_tab_intro(self, parent, title, description):
        """全設定タブで共通の見出しと短い説明を表示する。"""
        head = tk.Frame(parent, bg=BG_ADJ)
        head.pack(fill=tk.X, padx=16, pady=(12, 6))
        tk.Label(head, text=title, bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(head, text=description, bg=BG_ADJ, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        tk.Frame(parent, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(0, 4))

    def _build_adj_win(self):
        self._settings_win = tk.Toplevel(self.root)
        self._settings_win.title("設定")
        self._settings_win.configure(bg=BG_ADJ)
        self._settings_win.resizable(True, True)
        self._settings_win.minsize(520, 420)
        self._settings_win.withdraw()
        self._settings_win.protocol("WM_DELETE_WINDOW", self._settings_win.withdraw)
        self._adj_win = self._settings_win
        self._settings_tabs = ttk.Notebook(self._settings_win, style="Lumveil.TNotebook")
        self._settings_tabs.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        win = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._picture_tab = win
        self._settings_tabs.add(win, text="画質")
        self._add_settings_tab_intro(win, "画質", "明るさ・色味・字幕と音声の見た目を調整します。")

        mode_row = tk.Frame(win, bg=BG_ADJ)
        mode_row.pack(fill=tk.X, padx=16, pady=(8, 4))
        tk.Label(mode_row, text="映像モード:", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(0, 6))
        for mname in PICTURE_MODES:
            b = self._btn(mode_row, mname, lambda m=mname: self._apply_picture_mode(m),
                         bg=BG_ADJ, pad=(8, 3))
            b.pack(side=tk.LEFT, padx=2)
            self._mode_btns[mname] = b
        self._mode_btns[self._picture_mode].config(fg=COL_GRN)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        for key, label, lo, hi, default in ADJ_PARAMS:
            row = tk.Frame(win, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=16, pady=4)
            tk.Label(row, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            var = tk.DoubleVar(value=default)
            self._adj_vars[key] = (var, default)
            sc = ttk.Scale(row, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var,
                           length=200, style="Adj.Horizontal.TScale",
                           command=lambda _v, k=key: self._on_adjust(k))
            sc.pack(side=tk.LEFT, padx=6)
            self._fix_scale_click(sc, var, lo, hi)
            disp = tk.StringVar(value=f"{default:+d}")
            tk.Label(row, textvariable=disp, width=5,
                     bg=BG_ADJ, fg=COL_BLU, font=("Consolas", 9)).pack(side=tk.LEFT)
            var.trace_add("write",
                lambda *_, v=var, d=disp: d.set(f"{int(round(v.get())):+d}"))
            self._btn(row, "↺", lambda k=key: self._reset_adj(k),
                      bg=BG_ADJ, pad=(5, 3)).pack(side=tk.LEFT, padx=4)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(6, 2))

        tr = tk.Frame(win, bg=BG_ADJ)
        tr.pack(fill=tk.X, padx=16, pady=4)
        tk.Label(tr, text="暗闇補正の閾値:", width=SETTING_LABEL_W, anchor="w",
                 bg=BG_ADJ, fg=COL_YEL, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._thresh_var = tk.DoubleVar(value=self._dark_thresh)
        ts = ttk.Scale(tr, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                       variable=self._thresh_var, length=200,
                       style="Adj.Horizontal.TScale",
                       command=lambda _: setattr(self, "_dark_thresh",
                                                 round(self._thresh_var.get(), 2)))
        ts.pack(side=tk.LEFT, padx=6)
        self._fix_scale_click(ts, self._thresh_var, 0.0, 0.9)
        td = tk.StringVar(value=f"{self._dark_thresh:.2f}")
        tk.Label(tr, textvariable=td, width=5,
                 bg=BG_ADJ, fg=COL_YEL, font=("Consolas", 9)).pack(side=tk.LEFT)
        self._thresh_var.trace_add("write",
            lambda *_: td.set(f"{self._thresh_var.get():.2f}"))
        tk.Label(tr, text="← 鈍感   敏感 →",
                 bg=BG_ADJ, fg=COL_DIM, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        self._build_sync_controls(win)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        br = tk.Frame(win, bg=BG_ADJ, pady=8)
        br.pack()
        self._rt_btn = self._btn(br, "⚡ リアルタイム自動調整: OFF",
                                 self._toggle_rt_adj, bg=BG_ADJ, pad=(10, 5))
        self._rt_btn.pack(side=tk.LEFT, padx=5)
        self._btn(br, "↺ すべてリセット", self._reset_all_adj,
                  bg=BG_RED, pad=(10, 5)).pack(side=tk.LEFT, padx=5)
        self._denoise_btn = self._btn(br, "🔇 ノイズ軽減: OFF",
                                      self._toggle_denoise, bg=BG_ADJ, pad=(10, 5))
        self._denoise_btn.pack(side=tk.LEFT, padx=5)

        br2 = tk.Frame(win, bg=BG_ADJ, pady=4)
        br2.pack()
        self._btn(br2, "💾 設定を保存", self._save_adj,
                  bg=BG_ADJ, pad=(10, 5)).pack(side=tk.LEFT, padx=5)
        self._btn(br2, "📂 設定を読み込む", self._load_adj,
                  bg=BG_ADJ, pad=(10, 5)).pack(side=tk.LEFT, padx=5)

        self._auto_adj_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._auto_adj_status,
                 bg=BG_ADJ, fg=COL_GRN,
                 font=("Segoe UI", 8), pady=6).pack()

    def _toggle_adj_win(self):
        self._settings_tabs.select(self._picture_tab)
        if self._settings_win.winfo_viewable():
            self._settings_win.withdraw()
        else:
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._settings_win.geometry(f"+{x}+{y}")
            self._settings_win.deiconify()
            self._settings_win.lift()

    # ── 字幕/音声の同期・見た目調整 ────────────────────────────────────────

    def _build_sync_controls(self, win):
        def _sync_row(label, lo, hi, default, unit, mpv_prop, fmt="{:+.1f}"):
            row = tk.Frame(win, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=16, pady=4)
            tk.Label(row, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            var = tk.DoubleVar(value=default)

            def _apply(_=None, var=var, prop=mpv_prop):
                try:
                    self.player[prop] = var.get()
                except Exception:
                    pass

            sc = ttk.Scale(row, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var,
                           length=200, style="Adj.Horizontal.TScale", command=_apply)
            sc.pack(side=tk.LEFT, padx=6)
            self._fix_scale_click(sc, var, lo, hi)
            disp = tk.StringVar(value=fmt.format(default))
            tk.Label(row, textvariable=disp, width=6,
                     bg=BG_ADJ, fg=COL_BLU, font=("Consolas", 9)).pack(side=tk.LEFT)
            var.trace_add("write", lambda *_, v=var, d=disp: d.set(fmt.format(v.get())))
            tk.Label(row, text=unit, bg=BG_ADJ, fg=COL_DIM,
                     font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=4)

            def _reset(var=var, default=default):
                var.set(default)
                _apply()
            self._btn(row, "↺", _reset, bg=BG_ADJ, pad=(5, 3)).pack(side=tk.LEFT, padx=4)
            return var

        self._sub_delay_var = _sync_row("字幕遅延", -5.0, 5.0, 0.0, "秒", "sub-delay")
        self._sub_scale_var = _sync_row("字幕サイズ", 0.5, 2.0, 1.0, "倍", "sub-scale",
                                        fmt="{:.2f}")
        self._audio_delay_var = _sync_row("音声遅延", -5.0, 5.0, 0.0, "秒", "audio-delay")

    # ── GPU設定ウィンドウ ─────────────────────────────────────────────────

    _SCALE_OPTIONS = [
        ("bilinear",    "bilinear  （高速・標準）"),
        ("lanczos",     "Lanczos   （高品質）"),
        ("spline36",    "Spline36  （高品質）"),
        ("ewa_lanczos", "EWA Lanczos（最高品質・重い）"),
    ]
    _CSCALE_OPTIONS = [
        ("bilinear", "bilinear  （デフォルト）"),
        ("spline36", "Spline36  （推奨）"),
        ("lanczos",  "Lanczos   （高品質）"),
    ]
    _HWDEC_OPTIONS = [
        ("auto-safe", "オン（推奨）"),
        ("auto",      "オン（強制・実験的）"),
        ("no",        "オフ（CPUで処理）"),
    ]
    _DITHER_OPTIONS = [
        ("fruit",   "fruit  （高品質・デフォルト）"),
        ("ordered", "ordered（軽量）"),
        ("no",      "無効"),
    ]
    _TONEMAP_OPTIONS = [
        ("auto",     "自動（デフォルト）"),
        ("bt.2390",  "BT.2390（放送規格・推奨）"),
        ("hable",    "Hable"),
        ("mobius",   "Mobius"),
        ("reinhard", "Reinhard"),
        ("clip",     "クリップ（トーンマッピングなし）"),
    ]

    def _build_gpu_win(self):
        # 画質タブと同じ設定ウィンドウ内に、用途別の詳細タブを追加する。
        self._smooth_tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._decode_tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._shader_tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._output_tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._settings_tabs.add(self._smooth_tab, text="なめらかさ")
        self._settings_tabs.add(self._decode_tab, text="GPU再生支援")
        self._settings_tabs.add(self._shader_tab, text="シェーダー")
        self._settings_tabs.add(self._output_tab, text="映像出力")
        self._add_settings_tab_intro(self._smooth_tab, "なめらかさ", "再生の滑らかさとフレーム補間を設定します。")
        self._add_settings_tab_intro(self._decode_tab, "GPU再生支援", "GPUを使って動画を再生するための設定です。")
        self._add_settings_tab_intro(self._shader_tab, "シェーダー", "映像の拡大・補正・外部シェーダーを設定します。")
        self._add_settings_tab_intro(self._output_tab, "映像出力", "HDR変換や表示時の画質処理を設定します。")
        self._gpu_win = self._settings_win
        win = self._shader_tab

        _TIPS = {
            "スケール":
                "映像の拡大縮小アルゴリズム。\n"
                "bilinear: 最速・標準品質\n"
                "Lanczos: 高品質・シャープ\n"
                "Spline36: 高品質・なめらか\n"
                "EWA Lanczos: 最高品質（GPU負荷大）",
            "クロマスケール":
                "色差成分（クロマ）の拡大アルゴリズム。\n"
                "Spline36 推奨: 肌色・グラデーションが自然に滑らか。",
            "デバンディング":
                "グラデーション部分に現れる縞模様（バンディング）を除去します。\n"
                "アニメや暗部のグラデーションに効果的です。",
            "アンチリンギング":
                "スケーリング時に輪郭周辺に発生するにじみ（リンギング）を抑制します。\n"
                "0 = 無効、1.0 = 最強（輪郭のシャープさと引き換えになる場合あり）",
            "シグモイド拡大":
                "映像を拡大する際にシグモイド曲線を適用。\n"
                "コントラストの過剰な強調やリンギングを軽減し、自然な印象を保ちます。",
            "縮小補正":
                "縮小時に線形光量で計算し、暗部の潰れを防ぎます。\n"
                "高解像度動画を小さいウィンドウで見る際に有効です。",
            "フレーム補間":
                "フレーム間に中間フレームを生成し、再生をなめらかにします。\n"
                "速度変更時（0.75x・1.25x等）のカクカク感を軽減します。\n"
                "video-sync=display-resample + interpolation=yes を適用。",
            "AMD AMFフレーム補間":
                "AMD GPU内蔵の専用ハードウェアで、動きを解析して本物の中間\n"
                "フレームを生成します（フレームレートを約2倍に）。上の「フレーム\n"
                "補間」より高品質ですが、AMD GPU + GPU再生支援（オン）が必要です。\n"
                "非対応環境では自動的に無効のまま動作します（fallback指定）。\n"
                f"※ 元動画が{AMF_FRC_MAX_FPS:.0f}fpsを超える場合は、GPU使用率が\n"
                "跳ね上がりドロップの原因になるため自動的にスキップされます。\n"
                "※「フレーム補間」「ノイズ軽減」とは同時使用できないため、\n"
                "どちらかをONにすると自動的にもう片方はOFFになります。",
            "GPU再生支援":
                "GPUでデコードしCPU負荷を軽減します（従来のハードウェアデコード）。\n"
                "オン（推奨）: 実績あるデコーダのみ使用\n"
                "オン（強制・実験的）: 対応していれば全て試行（不安定な場合あり）\n"
                "オフ: ソフトウェアデコード（互換性最高・初期状態）\n"
                "※ AMD AMFフレーム補間を使うにはオンが必須です。\n"
                "※ 変更は次のファイルから有効",
            "GLSLシェーダー":
                "外部シェーダーファイル（.glsl）を適用します。\n"
                "Anime4K: アニメ向け超解像・ノイズ除去\n"
                "FSRCNNX: ニューラルネット超解像（GPU負荷大）\n"
                "複数追加可能。上から順に適用されます。",
            "ディザリング":
                "表示時の色深度変換で新たに発生するバンディングを目立たなくします。\n"
                "デバンディングとは別物（あちらは元映像側の縞模様を除去）。\n"
                "fruit 推奨: 高品質な誤差拡散法。",
            "トーンマッピング":
                "HDR動画をSDRディスプレイ向けに変換するアルゴリズム。\n"
                "本物のHDR素材（BD/配信の一部）でのみ意味があり、\n"
                "通常のSDR動画には効果がありません（無理に使うと逆効果）。",
            "デインターレース":
                "インターレース素材（古いTV放送由来の映像等）の\n"
                "横縞ノイズを除去します。プログレッシブ素材ではOFFのままでOK。",
        }

        def _row(label):
            r = tk.Frame(win, bg=BG_ADJ)
            r.pack(fill=tk.X, padx=16, pady=5)
            lbl = tk.Label(r, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                           bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10))
            lbl.pack(side=tk.LEFT)
            if label in _TIPS:
                _ToolTip(lbl, _TIPS[label])
            return r

        # スケールアルゴリズム
        r = _row("スケール")
        self._gpu_scale_var = tk.StringVar(value=self._gpu_scale)
        scale_labels = [lbl for _, lbl in self._SCALE_OPTIONS]
        scale_vals   = [val for val, _ in self._SCALE_OPTIONS]
        cur_lbl = next(lbl for val, lbl in self._SCALE_OPTIONS
                       if val == self._gpu_scale)
        self._gpu_scale_var.set(cur_lbl)
        om = tk.OptionMenu(r, self._gpu_scale_var, *scale_labels,
                           command=self._on_gpu_scale)
        om.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                  activeforeground=COL_TXT, highlightthickness=0,
                  relief=tk.FLAT, font=("Segoe UI", 10), width=22)
        om["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                          activebackground=BG_BTN_H, activeforeground=COL_TXT)
        om.pack(side=tk.LEFT)

        # クロマスケール
        r = _row("クロマスケール")
        self._gpu_cscale_var = tk.StringVar()
        cur_cs_lbl = next(lbl for val, lbl in self._CSCALE_OPTIONS
                          if val == self._gpu_cscale)
        self._gpu_cscale_var.set(cur_cs_lbl)
        cs_labels = [lbl for _, lbl in self._CSCALE_OPTIONS]
        com = tk.OptionMenu(r, self._gpu_cscale_var, *cs_labels,
                            command=self._on_gpu_cscale)
        com.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                   activeforeground=COL_TXT, highlightthickness=0,
                   relief=tk.FLAT, font=("Segoe UI", 10), width=22)
        com["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                           activebackground=BG_BTN_H, activeforeground=COL_TXT)
        com.pack(side=tk.LEFT)

        # デバンディング
        r = _row("デバンディング")
        self._gpu_deband_var = tk.BooleanVar(value=self._gpu_deband)
        self._deband_btn = tk.Button(
            r, text="ON" if self._gpu_deband else "OFF", command=self._on_gpu_deband,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_deband else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=8, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._deband_btn.pack(side=tk.LEFT)

        # アンチリンギング
        r = _row("アンチリンギング")
        self._gpu_antiring_var = tk.DoubleVar(value=self._gpu_antiring)
        ar_sc = ttk.Scale(r, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                          variable=self._gpu_antiring_var, length=180,
                          style="Adj.Horizontal.TScale",
                          command=self._on_gpu_antiring)
        ar_sc.pack(side=tk.LEFT, padx=6)
        self._fix_scale_click(ar_sc, self._gpu_antiring_var, 0.0, 1.0)
        ar_disp = tk.StringVar(value=f"{self._gpu_antiring:.2f}")
        tk.Label(r, textvariable=ar_disp, width=5,
                 bg=BG_ADJ, fg=COL_BLU, font=("Consolas", 9)).pack(side=tk.LEFT)
        self._gpu_antiring_var.trace_add(
            "write", lambda *_: ar_disp.set(f"{self._gpu_antiring_var.get():.2f}"))

        # シグモイド拡大
        r = _row("シグモイド拡大")
        self._sigmoid_btn = tk.Button(
            r, text="ON" if self._gpu_sigmoid else "OFF",
            command=self._on_gpu_sigmoid,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_sigmoid else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=8, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._sigmoid_btn.pack(side=tk.LEFT)

        # 縮小補正
        r = _row("縮小補正")
        self._correct_ds_btn = tk.Button(
            r, text="ON" if self._gpu_correct_ds else "OFF",
            command=self._on_gpu_correct_ds,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_correct_ds else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=8, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._correct_ds_btn.pack(side=tk.LEFT)

        # なめらかさ
        win = self._smooth_tab

        # フレーム補間
        r = _row("フレーム補間")
        self._interpolate_btn = tk.Button(
            r, text="ON" if self._gpu_interpolate else "OFF",
            command=self._on_gpu_interpolate,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_interpolate else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=8, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._interpolate_btn.pack(side=tk.LEFT)

        # AMD AMFフレーム補間（GPUハードウェアによる動き補償型の実補間）
        r = _row("AMD AMF補間")
        self._amf_frc_btn = tk.Button(
            r, text="ON" if self._gpu_amf_frc else "OFF",
            command=self._on_gpu_amf_frc,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_amf_frc else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=8, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._amf_frc_btn.pack(side=tk.LEFT)

        # シェーダー
        win = self._shader_tab
        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(6, 2))

        # Anime4Kプリセット
        ar = _row("Anime4Kプリセット")
        for pname in ANIME4K_PRESETS:
            b = self._btn(ar, pname, lambda p=pname: self._apply_anime4k_preset(p),
                         bg=BG_ADJ, pad=(6, 3), font=("Segoe UI", 8),
                         tooltip=ANIME4K_DESCRIPTIONS[pname])
            b.pack(side=tk.LEFT, padx=2)
            self._a4k_btns[pname] = b
        self._a4k_btns["なし"].config(fg=COL_GRN)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        # GLSLシェーダー
        r = _row("GLSLシェーダー")
        self._btn(r, "＋ 追加", self._on_gpu_glsl_add,
                  bg=BG_ADJ, pad=(8, 3)).pack(side=tk.LEFT)
        self._btn(r, "選択削除", self._on_gpu_glsl_remove,
                  bg=BG_ADJ, pad=(8, 3)).pack(side=tk.LEFT, padx=4)
        self._btn(r, "全クリア", self._on_gpu_glsl_clear,
                  bg=BG_ADJ, pad=(8, 3)).pack(side=tk.LEFT)

        glsl_frame = tk.Frame(win, bg=BG_ADJ)
        glsl_frame.pack(fill=tk.X, padx=16, pady=(2, 6))
        sb = tk.Scrollbar(glsl_frame, orient=tk.VERTICAL)
        self._glsl_listbox = tk.Listbox(
            glsl_frame, height=4, yscrollcommand=sb.set,
            bg="#1a1a1a", fg=COL_BLU, selectbackground="#2a4a6a",
            font=("Consolas", 8), relief=tk.FLAT, bd=0,
            activestyle="none")
        sb.config(command=self._glsl_listbox.yview)
        self._glsl_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        for p in self._gpu_glsl:
            self._glsl_listbox.insert(tk.END, os.path.basename(p))

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(2, 2))

        # GPU再生支援（従来のハードウェアデコード）
        win = self._decode_tab
        r = _row("GPU再生支援")
        self._gpu_hwdec_var = tk.StringVar()
        cur_hw_lbl = next(lbl for val, lbl in self._HWDEC_OPTIONS
                          if val == self._gpu_hwdec)
        self._gpu_hwdec_var.set(cur_hw_lbl)
        hw_labels = [lbl for _, lbl in self._HWDEC_OPTIONS]
        hom = tk.OptionMenu(r, self._gpu_hwdec_var, *hw_labels,
                            command=self._on_gpu_hwdec)
        hom.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                   activeforeground=COL_TXT, highlightthickness=0,
                   relief=tk.FLAT, font=("Segoe UI", 10), width=22)
        hom["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                           activebackground=BG_BTN_H, activeforeground=COL_TXT)
        hom.pack(side=tk.LEFT)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        # 映像出力
        win = self._output_tab

        # ディザリング
        r = _row("ディザリング")
        self._gpu_dither_var = tk.StringVar()
        cur_dt_lbl = next(lbl for val, lbl in self._DITHER_OPTIONS
                          if val == self._gpu_dither)
        self._gpu_dither_var.set(cur_dt_lbl)
        dt_labels = [lbl for _, lbl in self._DITHER_OPTIONS]
        dtom = tk.OptionMenu(r, self._gpu_dither_var, *dt_labels,
                             command=self._on_gpu_dither)
        dtom.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                    activeforeground=COL_TXT, highlightthickness=0,
                    relief=tk.FLAT, font=("Segoe UI", 10), width=22)
        dtom["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        dtom.pack(side=tk.LEFT)

        # トーンマッピング
        r = _row("トーンマッピング")
        self._gpu_tonemap_var = tk.StringVar()
        cur_tm_lbl = next(lbl for val, lbl in self._TONEMAP_OPTIONS
                          if val == self._gpu_tonemapping)
        self._gpu_tonemap_var.set(cur_tm_lbl)
        tm_labels = [lbl for _, lbl in self._TONEMAP_OPTIONS]
        tmom = tk.OptionMenu(r, self._gpu_tonemap_var, *tm_labels,
                             command=self._on_gpu_tonemap)
        tmom.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                    activeforeground=COL_TXT, highlightthickness=0,
                    relief=tk.FLAT, font=("Segoe UI", 10), width=22)
        tmom["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        tmom.pack(side=tk.LEFT)

        # デインターレース
        r = _row("デインターレース")
        self._deinterlace_btn = tk.Button(
            r, text="ON" if self._gpu_deinterlace else "OFF",
            command=self._on_gpu_deinterlace,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_deinterlace else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=8, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._deinterlace_btn.pack(side=tk.LEFT)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        br = tk.Frame(win, bg=BG_ADJ, pady=8)
        br.pack()
        self._btn(br, "↺ リセット", self._reset_gpu,
                  bg=BG_RED, pad=(10, 5)).pack(side=tk.LEFT, padx=5)

        self._gpu_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._gpu_status,
                 bg=BG_ADJ, fg=COL_GRN,
                 font=("Segoe UI", 8), pady=6).pack()
        self._build_playback_settings_tab()
        self._build_toolbar_settings_tab()

    def _build_playback_settings_tab(self):
        tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._playback_tab = tab
        self._settings_tabs.add(tab, text="再生")
        self._add_settings_tab_intro(tab, "再生", "動画の終了時・再開位置・フォルダ再生の動作を設定します。")

        def option_row(label, variable, options, command):
            row = tk.Frame(tab, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=16, pady=6)
            tk.Label(row, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            menu = tk.OptionMenu(row, variable, *options, command=command)
            menu.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                        activeforeground=COL_TXT, highlightthickness=0,
                        relief=tk.FLAT, font=("Segoe UI", 10), width=22)
            menu["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                                activebackground=BG_BTN_H, activeforeground=COL_TXT)
            menu.pack(side=tk.LEFT)

        self._eof_var = tk.StringVar(value="次の動画を再生" if self._playback_eof_action == "next" else "停止")
        option_row("再生終了時", self._eof_var, ("停止", "次の動画を再生"), self._on_eof_setting)

        row = tk.Frame(tab, bg=BG_ADJ)
        row.pack(fill=tk.X, padx=16, pady=6)
        tk.Label(row, text="前回位置から再開:", width=SETTING_LABEL_W, anchor="w",
                 bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._resume_btn = tk.Button(row, text="ON" if self._resume_enabled else "OFF",
                                     command=self._toggle_resume_enabled,
                                     bg=BG_ADJ, fg=COL_GRN if self._resume_enabled else COL_TXT,
                                     relief=tk.FLAT, bd=0, font=("Segoe UI", 10), width=8,
                                     padx=6, pady=3, cursor="hand2",
                                     activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._resume_btn.pack(side=tk.LEFT)

        self._end_var = tk.StringVar(value="先頭へ戻る" if self._folder_end_action == "loop" else "停止")
        option_row("フォルダ末尾", self._end_var, ("停止", "先頭へ戻る"), self._on_folder_end_setting)

        self._sort_var = tk.StringVar(value="更新日時順" if self._playlist_sort == "modified" else "ファイル名順")
        option_row("次の動画の並び", self._sort_var, ("ファイル名順", "更新日時順"), self._on_playlist_sort_setting)

    def _on_eof_setting(self, value):
        self._playback_eof_action = "next" if value == "次の動画を再生" else "stop"
        self._save_player_settings()

    def _toggle_resume_enabled(self):
        self._resume_enabled = not self._resume_enabled
        self._resume_btn.config(text="ON" if self._resume_enabled else "OFF",
                                fg=COL_GRN if self._resume_enabled else COL_TXT)
        if not self._resume_enabled:
            self._resume_positions.clear()
        self._save_player_settings()

    def _on_folder_end_setting(self, value):
        self._folder_end_action = "loop" if value == "先頭へ戻る" else "stop"
        self._save_player_settings()

    def _on_playlist_sort_setting(self, value):
        self._playlist_sort = "modified" if value == "更新日時順" else "name"
        if self._current_path:
            self._build_folder_playlist(self._current_path)
        self._save_player_settings()

    def _build_toolbar_settings_tab(self):
        """常時表示の切替と並び順を、実物に近いプレビューで設定する。"""
        if hasattr(self, "_toolbar_tab"):
            self._settings_tabs.forget(self._toolbar_tab)
            self._toolbar_tab.destroy()
        tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._toolbar_tab = tab
        self._settings_tabs.add(tab, text="操作バー")
        self._toolbar_icons = {
            "fullscreen": "⛶", "pin": "📌", "about": "ⓘ", "auto_adjust": "⚡",
            "gpu": "⚙ GPU", "recent": "🕘", "audio": "🎵", "subtitles": "💬",
            "ab_repeat": "A-B", "screenshot": "📷", "bookmark": "🔖", "speed": "1.00×",
        }
        tk.Label(tab, text="操作バー", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(tab, text="チェックで常時表示を切り替え、⠿ をドラッグして並び順を変えます。",
                 bg=BG_ADJ, fg=COL_DIM, font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        tk.Label(tab, text="プレビュー", bg=BG_ADJ, fg=COL_BLU,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 3))
        self._toolbar_preview = tk.Frame(tab, bg="#080808", bd=1, relief=tk.SOLID)
        self._toolbar_preview.pack(fill=tk.X, padx=16)

        tk.Label(tab, text="常時表示する項目", bg=BG_ADJ, fg=COL_BLU,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        rows = tk.Frame(tab, bg=BG_ADJ)
        rows.pack(fill=tk.X, padx=16, pady=(0, 12))
        self._toolbar_rows = {}
        definitions = self._toolbar_item_definitions()
        for key in self._toolbar_order:
            row = tk.Frame(rows, bg=BG_ADJ)
            row.pack(fill=tk.X, pady=1)
            self._toolbar_rows[key] = row
            handle = tk.Label(row, text="⠿", bg=BG_ADJ, fg=COL_DIM,
                              font=("Segoe UI", 12), cursor="fleur")
            handle.pack(side=tk.LEFT, padx=(4, 8))
            handle.bind("<ButtonPress-1>", lambda e, k=key: self._toolbar_drag_start(e, k))
            handle.bind("<B1-Motion>", self._toolbar_drag_motion)
            handle.bind("<ButtonRelease-1>", self._toolbar_drag_end)
            tk.Label(row, text=self._toolbar_icons[key], width=7, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            tk.Label(row, text=definitions[key][0], width=24, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            var = tk.BooleanVar(value=key in self._toolbar_visible)
            tk.Checkbutton(row, text="常時表示", variable=var,
                           command=lambda k=key, v=var: self._set_toolbar_item_visible(k, v),
                           bg=BG_ADJ, fg=COL_TXT, selectcolor=BG_BTN,
                           activebackground=BG_ADJ, activeforeground=COL_TXT,
                           font=("Segoe UI", 10), bd=0, highlightthickness=0).pack(side=tk.RIGHT)

        tk.Frame(tab, bg="#333333", height=1).pack(fill=tk.X, padx=16, pady=(2, 8))
        shot_box = tk.Frame(tab, bg=BG_ADJ)
        shot_box.pack(fill=tk.X, padx=16, pady=(0, 12))
        tk.Label(shot_box, text="スクリーンショットの保存先", anchor="w",
                 bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(fill=tk.X)
        shot_row = tk.Frame(shot_box, bg=BG_ADJ)
        shot_row.pack(fill=tk.X, pady=(3, 0))
        tk.Label(shot_row, text=self._shot_dir, anchor="w", justify=tk.LEFT,
                 wraplength=340, bg=BG_ADJ, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self._btn(shot_row, "変更…", self._change_shot_folder,
                  bg=BG_ADJ, font=("Segoe UI", 10), pad=(10, 4)).pack(side=tk.RIGHT)
        self._refresh_toolbar_settings()

    def _toggle_gpu_win(self):
        self._settings_tabs.select(self._decode_tab)
        if not self._settings_win.winfo_viewable():
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._settings_win.geometry(f"+{x}+{y}")
            self._settings_win.deiconify()
        self._settings_win.lift()

    def _on_gpu_scale(self, lbl):
        val = next(v for v, l in self._SCALE_OPTIONS if l == lbl)
        self._gpu_scale = val
        try:
            self.player["scale"] = val
            self._gpu_status.set(f"✓ スケール: {val}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_cscale(self, lbl):
        val = next(v for v, l in self._CSCALE_OPTIONS if l == lbl)
        self._gpu_cscale = val
        try:
            self.player["cscale"] = val
            self._gpu_status.set(f"✓ クロマスケール: {val}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_deband(self):
        self._gpu_deband = not self._gpu_deband
        self._deband_btn.config(
            text="ON" if self._gpu_deband else "OFF",
            fg=COL_GRN if self._gpu_deband else COL_TXT)
        try:
            self.player["deband"] = self._gpu_deband
            self._gpu_status.set(
                f"✓ デバンディング: {'ON' if self._gpu_deband else 'OFF'}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_antiring(self, _=None):
        val = round(self._gpu_antiring_var.get(), 2)
        self._gpu_antiring = val
        try:
            self.player["scale-antiring"] = val
        except Exception:
            pass
        self._save_gpu_settings()

    def _on_gpu_sigmoid(self):
        self._gpu_sigmoid = not self._gpu_sigmoid
        on = self._gpu_sigmoid
        self._sigmoid_btn.config(text="ON" if on else "OFF",
                                 fg=COL_GRN if on else COL_TXT)
        try:
            self.player["sigmoid-upscaling"] = on
            self._gpu_status.set(f"✓ シグモイド拡大: {'ON' if on else 'OFF'}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_correct_ds(self):
        self._gpu_correct_ds = not self._gpu_correct_ds
        on = self._gpu_correct_ds
        self._correct_ds_btn.config(text="ON" if on else "OFF",
                                    fg=COL_GRN if on else COL_TXT)
        try:
            self.player["correct-downscaling"] = on
            self._gpu_status.set(f"✓ 縮小補正: {'ON' if on else 'OFF'}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _set_interpolate_mpv(self, on):
        """フレーム補間(oversample)のmpvプロパティのみを適用する（ボタン表示や
        設定保存は呼び出し側の責任）。AMF FRCとの排他制御から共通で使うため分離。"""
        try:
            if on:
                self.player["video-sync"]    = "display-resample"
                self.player["interpolation"] = True
                self.player["tscale"]        = "oversample"
            else:
                self.player["video-sync"]    = "audio"
                self.player["interpolation"] = False
        except Exception:
            pass

    def _on_gpu_interpolate(self):
        self._gpu_interpolate = not self._gpu_interpolate
        on = self._gpu_interpolate
        if on and self._gpu_amf_frc:
            # フレーム補間とAMD AMFフレーム補間は目的が重複し二重負荷になるため排他化
            self._gpu_amf_frc = False
            self._amf_frc_btn.config(text="OFF", fg=COL_TXT)
            self._apply_vf_chain()
        self._interpolate_btn.config(text="ON" if on else "OFF",
                                     fg=COL_GRN if on else COL_TXT)
        self._set_interpolate_mpv(on)
        self._gpu_status.set(f"✓ フレーム補間: {'ON' if on else 'OFF'}")
        self._save_gpu_settings()

    def _on_gpu_amf_frc(self):
        self._gpu_amf_frc = not self._gpu_amf_frc
        on = self._gpu_amf_frc
        if on and self._denoise:
            # hqdn3d(ソフトウェア)とamf_frc(GPUサーフェス直結)は同時使用不可のため排他化
            self._denoise = False
            self._denoise_btn.config(text="🔇 ノイズ軽減: OFF", fg=COL_TXT)
        if on and self._gpu_interpolate:
            # フレーム補間とAMD AMFフレーム補間は目的が重複し二重負荷になるため排他化
            self._gpu_interpolate = False
            self._interpolate_btn.config(text="OFF", fg=COL_TXT)
            self._set_interpolate_mpv(False)
        self._amf_frc_btn.config(text="ON" if on else "OFF",
                                 fg=COL_GRN if on else COL_TXT)
        self._apply_vf_chain()
        if on and self.fps > AMF_FRC_MAX_FPS:
            self._gpu_status.set(
                f"⚠ AMD AMFフレーム補間: ON（高フレームレート素材のため自動スキップ中）")
        else:
            self._gpu_status.set(
                f"✓ AMD AMFフレーム補間: {'ON' if on else 'OFF'}"
                + ("（非AMD環境では自動的に無効のままです）" if on else ""))
        self._save_gpu_settings()

    def _apply_anime4k_preset(self, name):
        files = ANIME4K_PRESETS.get(name)
        if files is None:
            return
        # 既存のAnime4K_*シェーダーだけを外し、ユーザーが手動追加した
        # 他のシェーダー（自作のもの等）はそのまま残す
        self._gpu_glsl = [p for p in self._gpu_glsl
                          if not os.path.basename(p).startswith("Anime4K_")]
        for fname in files:
            self._gpu_glsl.append(os.path.join(_SHADER_DIR, fname))
        self._glsl_listbox.delete(0, tk.END)
        for p in self._gpu_glsl:
            self._glsl_listbox.insert(tk.END, os.path.basename(p))
        self._apply_glsl_shaders()
        self._save_gpu_settings()
        self._gpu_status.set(f"✓ Anime4Kプリセット適用: {name}")
        for mname, btn in self._a4k_btns.items():
            btn.config(fg=COL_GRN if mname == name else COL_TXT)

    def _on_gpu_glsl_add(self):
        paths = self._ask_file(filedialog.askopenfilenames,
            title="GLSLシェーダーを選択",
            filetypes=[("GLSLシェーダー", "*.glsl *.frag *.vert"),
                       ("すべてのファイル", "*.*")])
        for p in paths:
            p = os.path.abspath(p)
            if p not in self._gpu_glsl:
                self._gpu_glsl.append(p)
                self._glsl_listbox.insert(tk.END, os.path.basename(p))
        if paths:
            self._apply_glsl_shaders()
            self._save_gpu_settings()
            self._gpu_status.set(f"✓ シェーダー追加: {len(self._gpu_glsl)}件")

    def _on_gpu_glsl_remove(self):
        sel = self._glsl_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self._glsl_listbox.delete(idx)
        self._gpu_glsl.pop(idx)
        self._apply_glsl_shaders()
        self._save_gpu_settings()
        self._gpu_status.set(f"✓ シェーダー削除 ({len(self._gpu_glsl)}件残)")

    def _on_gpu_glsl_clear(self):
        self._gpu_glsl.clear()
        self._glsl_listbox.delete(0, tk.END)
        self._apply_glsl_shaders()
        self._save_gpu_settings()
        self._gpu_status.set("✓ シェーダー全クリア")

    def _apply_glsl_shaders(self):
        try:
            self.player.command("change-list", "glsl-shaders", "clr", "")
            for p in self._gpu_glsl:
                self.player.command("change-list", "glsl-shaders", "append", p)
            # 追加コントラストは手動でも使えるため、常時読込する（強度0なら無処理）。
            self.player.command("change-list", "glsl-shaders", "append", _RT_CONTRAST_SHADER_PATH)
            if "contrast" in self._adj_vars:
                self._apply_effective_contrast(self._adj_vars["contrast"][0].get())
        except Exception:
            pass

    def _apply_effective_contrast(self, value):
        """-100〜+300の実効値を、MPV(+100まで)と拡張シェーダーへ連続して配分する。"""
        value = max(-100.0, min(300.0, float(value)))
        mpv_value = min(100.0, value)
        strength = max(0.0, (value - 100.0) / 100.0)
        try:
            self.player["contrast"] = int(round(mpv_value))
            self.player.command("set", "glsl-shader-opts",
                                f"auto_contrast={strength:.3f}")
        except Exception:
            pass

    def _on_gpu_hwdec(self, lbl):
        val = next(v for v, l in self._HWDEC_OPTIONS if l == lbl)
        self._gpu_hwdec = val
        try:
            self.player["hwdec"] = val
            self._gpu_status.set(f"✓ GPU再生支援: {lbl}（次ファイルから有効）")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_dither(self, lbl):
        val = next(v for v, l in self._DITHER_OPTIONS if l == lbl)
        self._gpu_dither = val
        try:
            self.player["dither"] = val
            self._gpu_status.set(f"✓ ディザリング: {val}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_tonemap(self, lbl):
        val = next(v for v, l in self._TONEMAP_OPTIONS if l == lbl)
        self._gpu_tonemapping = val
        try:
            self.player["tone-mapping"] = val
            self._gpu_status.set(f"✓ トーンマッピング: {val}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_deinterlace(self):
        self._gpu_deinterlace = not self._gpu_deinterlace
        self._deinterlace_btn.config(
            text="ON" if self._gpu_deinterlace else "OFF",
            fg=COL_GRN if self._gpu_deinterlace else COL_TXT)
        try:
            self.player["deinterlace"] = self._gpu_deinterlace
            self._gpu_status.set(
                f"✓ デインターレース: {'ON' if self._gpu_deinterlace else 'OFF'}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _reset_gpu(self):
        self._gpu_scale      = "lanczos"
        self._gpu_cscale     = "spline36"
        self._gpu_deband     = False
        self._gpu_antiring   = 0.0
        self._gpu_sigmoid     = False
        self._gpu_correct_ds  = False
        self._gpu_interpolate = False
        self._gpu_hwdec       = "no"
        self._gpu_dither      = "fruit"
        self._gpu_tonemapping = "auto"
        self._gpu_deinterlace = False
        self._gpu_amf_frc     = False
        self._gpu_glsl        = []
        # UI更新
        cur_lbl = next(lbl for val, lbl in self._SCALE_OPTIONS
                       if val == self._gpu_scale)
        self._gpu_scale_var.set(cur_lbl)
        cur_cs_lbl = next(lbl for val, lbl in self._CSCALE_OPTIONS
                          if val == self._gpu_cscale)
        self._gpu_cscale_var.set(cur_cs_lbl)
        self._deband_btn.config(text="OFF", fg=COL_TXT)
        self._gpu_antiring_var.set(0.0)
        self._sigmoid_btn.config(text="OFF", fg=COL_TXT)
        self._correct_ds_btn.config(text="OFF", fg=COL_TXT)
        self._interpolate_btn.config(text="OFF", fg=COL_TXT)
        self._amf_frc_btn.config(text="OFF", fg=COL_TXT)
        self._glsl_listbox.delete(0, tk.END)
        cur_hw_lbl = next(lbl for val, lbl in self._HWDEC_OPTIONS
                          if val == self._gpu_hwdec)
        self._gpu_hwdec_var.set(cur_hw_lbl)
        cur_dt_lbl = next(lbl for val, lbl in self._DITHER_OPTIONS
                          if val == self._gpu_dither)
        self._gpu_dither_var.set(cur_dt_lbl)
        cur_tm_lbl = next(lbl for val, lbl in self._TONEMAP_OPTIONS
                          if val == self._gpu_tonemapping)
        self._gpu_tonemap_var.set(cur_tm_lbl)
        self._deinterlace_btn.config(text="OFF", fg=COL_TXT)
        # MPVに適用
        try:
            self.player["scale"]               = self._gpu_scale
            self.player["cscale"]              = self._gpu_cscale
            self.player["deband"]              = False
            self.player["scale-antiring"]      = 0.0
            self.player["sigmoid-upscaling"]   = False
            self.player["correct-downscaling"] = False
            self.player["video-sync"]          = "audio"
            self.player["interpolation"]       = False
            self.player["hwdec"]               = self._gpu_hwdec
            self.player["dither"]              = self._gpu_dither
            self.player["tone-mapping"]        = self._gpu_tonemapping
            self.player["deinterlace"]         = False
            self._apply_glsl_shaders()
            self._apply_vf_chain()
            self._gpu_status.set("↺ リセット完了")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _save_gpu_settings(self):
        data = {
            "scale":       self._gpu_scale,
            "cscale":      self._gpu_cscale,
            "deband":      self._gpu_deband,
            "antiring":    self._gpu_antiring,
            "sigmoid":     self._gpu_sigmoid,
            "correct_ds":  self._gpu_correct_ds,
            "interpolate": self._gpu_interpolate,
            "hwdec":       self._gpu_hwdec,
            "dither":      self._gpu_dither,
            "tonemapping": self._gpu_tonemapping,
            "deinterlace": self._gpu_deinterlace,
            "amf_frc":     self._gpu_amf_frc,
            "glsl":        self._gpu_glsl,
        }
        try:
            with open(GPU_SETTINGS, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_gpu_settings(self):
        if not os.path.exists(GPU_SETTINGS):
            return
        try:
            with open(GPU_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            self._gpu_scale       = data.get("scale",       self._gpu_scale)
            self._gpu_cscale      = data.get("cscale",      self._gpu_cscale)
            self._gpu_deband      = data.get("deband",      self._gpu_deband)
            self._gpu_antiring    = data.get("antiring",    self._gpu_antiring)
            self._gpu_sigmoid     = data.get("sigmoid",     self._gpu_sigmoid)
            self._gpu_correct_ds  = data.get("correct_ds",  self._gpu_correct_ds)
            self._gpu_interpolate = data.get("interpolate", self._gpu_interpolate)
            self._gpu_hwdec       = data.get("hwdec",       self._gpu_hwdec)
            self._gpu_dither      = data.get("dither",      self._gpu_dither)
            self._gpu_tonemapping = data.get("tonemapping", self._gpu_tonemapping)
            self._gpu_deinterlace = data.get("deinterlace", self._gpu_deinterlace)
            self._gpu_amf_frc     = data.get("amf_frc",     self._gpu_amf_frc)
            self._gpu_glsl        = [p for p in data.get("glsl", [])
                                     if os.path.exists(p)]
        except Exception:
            pass

    def _apply_gpu_settings(self):
        try:
            self.player["scale"]               = self._gpu_scale
            self.player["cscale"]              = self._gpu_cscale
            self.player["deband"]              = self._gpu_deband
            self.player["scale-antiring"]      = self._gpu_antiring
            self.player["sigmoid-upscaling"]   = self._gpu_sigmoid
            self.player["correct-downscaling"] = self._gpu_correct_ds
            self.player["hwdec"]               = self._gpu_hwdec
            self.player["dither"]              = self._gpu_dither
            self.player["tone-mapping"]        = self._gpu_tonemapping
            self.player["deinterlace"]         = self._gpu_deinterlace
            if self._gpu_interpolate:
                self.player["video-sync"]    = "display-resample"
                self.player["interpolation"] = True
                self.player["tscale"]        = "oversample"
        except Exception:
            pass
        self._apply_glsl_shaders()
        self._apply_vf_chain()

    # ── スライダークリック修正 ─────────────────────────────────────────────

    @staticmethod
    def _fix_scale_click(scale, var, lo, hi):
        def _jump(e):
            ratio = max(0.0, min(1.0, e.x / max(scale.winfo_width(), 1)))
            scale.after(1, lambda: var.set(lo + ratio * (hi - lo)))
        scale.bind("<Button-1>", _jump, add=True)

    # ── 速度 ──────────────────────────────────────────────────────────────

    _SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]

    def _show_speed_menu(self):
        menu = tk.Menu(self.root, tearoff=False, bg=BG_ADJ, fg=COL_TXT,
                       activebackground=BG_BTN_H, activeforeground=COL_TXT,
                       font=("Segoe UI", 10))
        for rate in self._SPEEDS:
            label = f"{'✓  ' if abs(rate - self._speed) < 0.01 else '    '}{rate:.2f}×"
            menu.add_command(label=label, command=lambda r=rate: self._set_speed(r))
        try:
            menu.update_idletasks()
            if self._speed_btn.winfo_ismapped():
                x = self._speed_btn.winfo_rootx()
                y = self._speed_btn.winfo_rooty() - menu.winfo_reqheight()
            else:
                x = self.root.winfo_pointerx()
                y = self.root.winfo_pointery()
            menu.tk_popup(max(0, x), max(0, y))
        finally:
            menu.grab_release()

    def _set_speed(self, rate):
        self._speed = rate
        try:
            self.player.speed = rate
        except Exception:
            pass
        self._speed_var.set(f"{rate:.2f}×")

    def _speed_up(self):
        nxt = [s for s in self._SPEEDS if s > self._speed + 0.01]
        if nxt: self._set_speed(nxt[0])

    def _speed_down(self):
        prv = [s for s in self._SPEEDS if s < self._speed - 0.01]
        if prv: self._set_speed(prv[-1])

    # ── 音量 ──────────────────────────────────────────────────────────────

    def _toggle_volume_popup(self):
        if self._volume_popup and self._volume_popup.winfo_exists():
            self._volume_popup.destroy()
            self._volume_popup = None
            return
        pop = tk.Toplevel(self.root)
        self._volume_popup = pop
        pop.overrideredirect(True)
        pop.configure(bg=BG_ADJ)
        pop.attributes("-topmost", True)
        pop.bind("<FocusOut>", lambda _e: self._close_volume_popup())
        value = tk.StringVar(value=f"{self.vol_var.get()}%")
        self._volume_value_label = value
        self._volume_trace_id = self.vol_var.trace_add("write", self._sync_volume_label)
        tk.Label(pop, text="音量", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 8)).pack(padx=10, pady=(8, 0))
        tk.Label(pop, textvariable=value, bg=BG_ADJ, fg=COL_BLU,
                 font=("Consolas", 9)).pack(padx=10)
        scale = tk.Scale(pop, from_=100, to=0, orient=tk.VERTICAL,
                         variable=self.vol_var, command=self._on_volume,
                         length=130, showvalue=False, width=12,
                         bg=BG_ADJ, fg=COL_TXT, troughcolor="#333333",
                         activebackground=COL_BLU, highlightthickness=0,
                         bd=0, sliderlength=14, sliderrelief=tk.FLAT)
        scale.pack(padx=12, pady=(2, 8))
        scale.bind("<MouseWheel>", self._on_volume_wheel)
        pop.bind("<MouseWheel>", self._on_volume_wheel)
        pop.update_idletasks()
        x = self._mute_btn.winfo_rootx() + self._mute_btn.winfo_width() // 2 - pop.winfo_reqwidth() // 2
        y = self._mute_btn.winfo_rooty() - pop.winfo_reqheight() - 4
        pop.geometry(f"+{max(0, x)}+{max(0, y)}")
        pop.focus_force()

    def _close_volume_popup(self):
        if hasattr(self, "_volume_trace_id"):
            try:
                self.vol_var.trace_remove("write", self._volume_trace_id)
            except Exception:
                pass
            del self._volume_trace_id
        if self._volume_popup and self._volume_popup.winfo_exists():
            self._volume_popup.destroy()
        self._volume_popup = None

    def _sync_volume_label(self, *_args):
        if hasattr(self, "_volume_value_label"):
            self._volume_value_label.set(f"{self.vol_var.get()}%")

    def _on_volume_wheel(self, event):
        """ポップアップ上ではホイール量を明示的に音量へ反映する。"""
        step = 2 if event.delta > 0 else -2
        value = max(0, min(100, self.vol_var.get() + step))
        self.vol_var.set(value)
        self._on_volume(value)
        return "break"

    def toggle_mute(self):
        self._muted = not self._muted
        try:
            self.player.mute = self._muted
        except Exception:
            pass
        self._mute_btn.config(text="🔇" if self._muted else "🔊")

    def _on_volume(self, val):
        v = int(float(val))
        if hasattr(self, "_volume_value_label"):
            self._volume_value_label.set(f"{v}%")
        if self._muted and v > 0:
            self._muted = False
            self._mute_btn.config(text="🔊")
            try:
                self.player.mute = False
            except Exception:
                pass
        if not self._vol_pending:
            self._vol_pending = True
            self.root.after(50, self._apply_volume)

    def _apply_volume(self):
        self._vol_pending = False
        v = self.vol_var.get()
        try:
            if self._muted:
                self.player.mute = True
            else:
                self.player.mute = False
                self.player.volume = float(v)
        except Exception:
            pass

    def _vol_step(self, delta):
        v = max(0, min(100, self.vol_var.get() + delta))
        self.vol_var.set(v)
        self._apply_volume()

    # ── フルスクリーン ────────────────────────────────────────────────────

    def toggle_fullscreen(self):
        is_fs = not self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", is_fs)
        if is_fs:
            self._enter_fullscreen_ui()
        else:
            self._exit_fullscreen_ui()

    def _exit_fullscreen(self):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
            self._exit_fullscreen_ui()

    def _enter_fullscreen_ui(self):
        # 通常配置(pack)から外し、映像を画面いっぱいにする。
        # 操作バーはマウス操作/右クリック時だけ上に重ねて(place)一時表示する。
        self.ctrl_bar.pack_forget()
        self._fs_bar_visible = False
        self._fs_hide_after_id = None
        self.root.bind("<Motion>", self._on_fullscreen_motion, add="+")
        self.root.bind("<Button-3>", self._on_fullscreen_rclick, add="+")

    def _exit_fullscreen_ui(self):
        self.root.unbind("<Motion>")
        self.root.unbind("<Button-3>")
        if self._fs_hide_after_id:
            self.root.after_cancel(self._fs_hide_after_id)
            self._fs_hide_after_id = None
        self.ctrl_bar.place_forget()
        self.ctrl_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _on_fullscreen_motion(self, event):
        if not self.root.attributes("-fullscreen"):
            return
        near_bottom = event.y_root >= self.root.winfo_screenheight() - 80
        if near_bottom:
            self._show_fullscreen_bar()
        elif self._fs_bar_visible and self._fs_hide_after_id is None:
            self._fs_hide_after_id = self.root.after(1500, self._hide_fullscreen_bar)

    def _on_fullscreen_rclick(self, event):
        self._show_fullscreen_bar()

    def _show_fullscreen_bar(self):
        if not self._fs_bar_visible:
            self.ctrl_bar.place(relx=0, rely=1.0, anchor="sw", relwidth=1.0)
            self.ctrl_bar.lift()
            self._fs_bar_visible = True
        if self._fs_hide_after_id:
            self.root.after_cancel(self._fs_hide_after_id)
        self._fs_hide_after_id = self.root.after(3000, self._hide_fullscreen_bar)

    def _hide_fullscreen_bar(self):
        self._fs_hide_after_id = None
        if self.root.attributes("-fullscreen"):
            self.ctrl_bar.place_forget()
            self._fs_bar_visible = False

    # ── 画像調整 ──────────────────────────────────────────────────────────

    def _on_adjust(self, key):
        val = int(round(self._adj_vars[key][0].get()))
        if key == "contrast":
            self._apply_effective_contrast(val)
            return
        try:
            self.player[key] = val
        except Exception:
            pass

    def _reset_adj(self, key):
        var, default = self._adj_vars[key]
        var.set(default)
        self._on_adjust(key)

    def _reset_all_adj(self):
        for key in self._adj_vars:
            self._reset_adj(key)
        if self._rt_enabled:
            self._toggle_rt_adj()

    def _apply_picture_mode(self, name):
        vals = PICTURE_MODES.get(name)
        if not vals:
            return
        # 暗闇補正(RT自動調整)は0を基準に動くため、プリセットの非ゼロ値と
        # 競合してしまう。すべてリセットと同様、モード切替時はRTを止める。
        if self._rt_enabled:
            self._toggle_rt_adj()
        for key, val in zip(("brightness", "contrast", "gamma", "saturation"), vals):
            var, _ = self._adj_vars[key]
            var.set(val)
            self._on_adjust(key)
        self._picture_mode = name
        for mname, btn in self._mode_btns.items():
            btn.config(fg=COL_GRN if mname == name else COL_TXT)

    def _save_adj(self):
        data = {k: int(round(v.get())) for k, (v, _) in self._adj_vars.items()}
        try:
            with open(ADJ_SETTINGS, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._auto_adj_status.set("💾 設定を保存しました")
        except Exception as e:
            self._auto_adj_status.set(f"⚠ 保存失敗: {e}")

    def _load_adj(self):
        if not os.path.exists(ADJ_SETTINGS):
            self._auto_adj_status.set("⚠ 保存された設定がありません")
            return
        try:
            with open(ADJ_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if k in self._adj_vars:
                    self._adj_vars[k][0].set(int(float(v)))
                    self._on_adjust(k)
            # 旧形式の「追加コントラスト」は実効コントラストへ移行する。
            if "extra_contrast" in data:
                base = float(data.get("contrast", 0))
                self._adj_vars["contrast"][0].set(min(300.0,
                    base + float(data["extra_contrast"])))
                self._on_adjust("contrast")
            self._auto_adj_status.set("📂 設定を読み込みました")
        except Exception as e:
            self._auto_adj_status.set(f"⚠ 読み込み失敗: {e}")

    # ── ノイズ軽減 ────────────────────────────────────────────────────────

    def _amf_frc_effective(self):
        """AMF FRCがONでも、元動画が高フレームレート（AMF_FRC_MAX_FPS超）の場合は
        GPU使用率100%＋大量のフレームドロップにつながるため自動的に適用しない
        （実測確認済み）。ON設定自体は保持し、動画のfpsに応じて都度判定する。"""
        return self._gpu_amf_frc and self.fps <= AMF_FRC_MAX_FPS

    def _apply_vf_chain(self):
        """ノイズ軽減(hqdn3d)はソフトウェア形式のフレームを要求し、AMD AMF
        フレーム補間(amf_frc)はGPU上のd3d11サーフェスのままである必要がある
        ため、両者は同時に組み合わせるとフィルタグラフの初期化に失敗する
        （実測確認済み）。そのため排他的に扱い、常にどちらか一方だけを適用する。
        AMF FRCを優先する（後からONにした側が勝つよう、両方のトグル側で
        もう一方を強制OFFにしている）。
        また、amf_frcが絡む切り替えはAMDのGPUハードウェアコンテキストの
        再初期化を伴うため、一度チェーンを空にしてから間を置いて次のフィルタを
        設定することで、切り替え直後の一時的な不安定化を避ける。"""
        if self._amf_frc_effective():
            vf_str = "amf_frc=fallback=yes"
        elif self._denoise:
            vf_str = "hqdn3d"
        else:
            vf_str = ""
        try:
            self.player.command("vf", "set", "")
        except Exception:
            pass

        def _set_target(target=vf_str):
            try:
                self.player.command("vf", "set", target)
            except Exception:
                pass
        self.root.after(50, _set_target)

    def _toggle_denoise(self):
        self._denoise = not self._denoise
        on = self._denoise
        if on and self._gpu_amf_frc:
            self._gpu_amf_frc = False
            self._amf_frc_btn.config(text="OFF", fg=COL_TXT)
            self._save_gpu_settings()
        self._apply_vf_chain()
        self._denoise_btn.config(
            text=f"🔇 ノイズ軽減: {'ON' if on else 'OFF'}",
            fg=COL_GRN if on else COL_TXT)

    # ── DnD ──────────────────────────────────────────────────────────────

    def _setup_dnd(self):
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            raw = event.data.strip()
            if raw.startswith("{") and "}" in raw:
                raw = raw[1:raw.index("}")]
            paths = [raw.strip()]
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            return
        if len(files) > 1:
            self._play_list(files, 0)
        else:
            self._open_path(files[0])

    # ── ファイルを開く ────────────────────────────────────────────────────

    def open_file(self):
        path = self._ask_file(filedialog.askopenfilename,
            title="動画ファイルを選択",
            filetypes=[
                ("動画ファイル",
                 "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v "
                 "*.ts *.m2ts *.vob *.ogv *.3gp *.rmvb *.rm *.hevc *.h264"),
                ("すべてのファイル", "*.*"),
            ])
        if path:
            self._open_path(path)

    def _open_path(self, path, _from_playlist=False):
        self._update_resume_position()  # 切り替え前のファイルの位置を保存
        path = os.path.abspath(path)
        self._current_path = path
        self._thumb_cache.clear()
        try:
            self.player.play(path)
            self.player["ab-loop-a"] = "no"
            self.player["ab-loop-b"] = "no"
        except Exception:
            pass
        self._ab_state = 0
        self._ab_btn.config(text="A-B", fg=COL_TXT)
        self.root.title(f"Lumveil — {os.path.basename(path)}")
        self.root.after(600, self._fetch_fps)
        self._add_recent_file(path)
        if not _from_playlist:
            self._build_folder_playlist(path)

    # ── プレイリスト・連続再生 ────────────────────────────────────────────

    def _build_folder_playlist(self, path):
        """単体でファイルを開いた際、同じフォルダ内の動画を連続再生の対象にする。"""
        folder = os.path.dirname(path)
        try:
            entries = sorted(os.listdir(folder))
        except Exception:
            self._playlist, self._playlist_idx = [path], 0
            return
        files = [os.path.join(folder, f) for f in entries
                 if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
        if self._playlist_sort == "modified":
            files.sort(key=lambda p: os.path.getmtime(p))
        else:
            files.sort(key=lambda p: os.path.basename(p).lower())
        if path not in files:
            files = [path]
        self._playlist     = files
        self._playlist_idx = files.index(path)

    def _play_list(self, files, start_idx):
        self._playlist     = files
        self._playlist_idx = start_idx
        self._open_path(files[start_idx], _from_playlist=True)

    def _play_relative(self, delta):
        if not self._playlist or self._playlist_idx < 0:
            return
        nxt = self._playlist_idx + delta
        if 0 <= nxt < len(self._playlist):
            self._playlist_idx = nxt
            self._open_path(self._playlist[nxt], _from_playlist=True)

    def _play_next(self):
        self._play_relative(1)

    def _play_prev(self):
        self._play_relative(-1)

    def _on_eof_reached(self):
        if self._playback_eof_action != "next" or not self._playlist:
            return
        if 0 <= self._playlist_idx < len(self._playlist) - 1:
            self._play_next()
        elif self._folder_end_action == "loop":
            self._playlist_idx = 0
            self._open_path(self._playlist[0], _from_playlist=True)

    # ── A-Bリピート ───────────────────────────────────────────────────────

    def _toggle_ab_loop(self):
        try:
            pos = self.player.time_pos
        except Exception:
            pos = None
        if self._ab_state == 0:
            if pos is None:
                return
            try:
                self.player["ab-loop-a"] = pos
            except Exception:
                pass
            self._ab_state = 1
            self._ab_btn.config(text="A-B: A", fg=COL_YEL)
        elif self._ab_state == 1:
            if pos is None:
                return
            try:
                self.player["ab-loop-b"] = pos
            except Exception:
                pass
            self._ab_state = 2
            self._ab_btn.config(text="A-B: ▶", fg=COL_GRN)
        else:
            try:
                self.player["ab-loop-a"] = "no"
                self.player["ab-loop-b"] = "no"
            except Exception:
                pass
            self._ab_state = 0
            self._ab_btn.config(text="A-B", fg=COL_TXT)

    def _fetch_fps(self):
        try:
            fps = self.player.container_fps
            if fps and fps > 0:
                self.fps = fps
        except Exception:
            pass
        if self._gpu_amf_frc:
            # 高フレームレート素材かどうかがこの時点で初めて確定するため、
            # AMF FRCのバイパス判定を動画ごとに再適用する。
            self._apply_vf_chain()
            if self.fps > AMF_FRC_MAX_FPS:
                self._gpu_status.set(
                    "⚠ AMD AMFフレーム補間: ON（高フレームレート素材のため自動スキップ中）")
            else:
                self._gpu_status.set("✓ AMD AMFフレーム補間: ON")

    # ── 再生制御 ──────────────────────────────────────────────────────────

    def toggle_play(self):
        try:
            self.player.pause = not self.player.pause
        except Exception:
            pass

    def stop(self):
        try:
            self.player.seek(0, reference="absolute", precision="exact")
            self.player.pause = True
        except Exception:
            pass

    def seek_forward(self):
        try:
            self.player.seek(SEEK_SEC, reference="relative")
        except Exception:
            pass

    def seek_backward(self):
        try:
            self.player.seek(-SEEK_SEC, reference="relative")
        except Exception:
            pass

    def frame_forward(self):
        try:
            self.player.frame_step()
        except Exception:
            pass

    def frame_backward(self):
        try:
            self.player.frame_back_step()
        except Exception:
            pass

    # ── シークバー ────────────────────────────────────────────────────────

    def _on_duration_prop(self, value):
        self._cached_duration_ms = (value or 0.0) * 1000

    def _on_pause_prop(self, value):
        self._cached_pause = bool(value)

    def _get_duration_ms(self):
        return self._cached_duration_ms

    def _get_time_ms(self):
        try:
            t = self.player.time_pos
            return (t or 0.0) * 1000
        except Exception:
            return 0.0

    def _on_seekbar_press(self, event):
        self.is_seeking = True
        dur_ms = self._get_duration_ms()
        if dur_ms > 0:
            ratio = max(0.0, min(1.0, event.x / max(self.seekbar.winfo_width(), 1)))
            try:
                self.player.seek(ratio * dur_ms / 1000,
                                 reference="absolute", precision="exact")
            except Exception:
                pass
            self.seek_var.set(ratio * 1000)

    def _on_seek_drag(self, val):
        if self.is_seeking:
            dur_ms = self._get_duration_ms()
            if dur_ms > 0:
                # ドラッグ中はキーフレーム単位の軽いシークに留め、カクつきを防ぐ。
                # 正確な位置への着地は指を離した瞬間（_on_seek_release）で行う。
                try:
                    self.player.seek(float(val) / 1000 * dur_ms / 1000,
                                     reference="absolute", precision="keyframes")
                except Exception:
                    pass

    def _on_seek_release(self, _event):
        self.is_seeking = False
        dur_ms = self._get_duration_ms()
        if dur_ms > 0:
            try:
                self.player.seek(self.seek_var.get() / 1000 * dur_ms / 1000,
                                 reference="absolute", precision="exact")
            except Exception:
                pass

    def _on_seekbar_motion(self, event):
        if not self._current_path or not FFMPEG:
            return
        if self._prev_after_id:
            self.root.after_cancel(self._prev_after_id)
        self._prev_after_id = self.root.after(
            120, lambda x=event.x: self._schedule_preview(x))

    def _schedule_preview(self, hover_x):
        dur_ms = self._get_duration_ms()
        if dur_ms <= 0:
            return
        w       = self.seekbar.winfo_width()
        ratio   = max(0.0, min(1.0, hover_x / max(w, 1)))
        pos_ms  = int(ratio * dur_ms)
        pos_sec = pos_ms / 1000.0
        key     = (self._current_path, int(pos_sec / SNAP_STEP))

        self.prev_time_label.config(text=self._fmt(pos_ms))
        rx = self.seekbar.winfo_rootx() + hover_x - PREV_W // 2
        ry = self.seekbar.winfo_rooty() - PREV_H - 30
        self.prev_popup.geometry(f"{PREV_W}x{PREV_H + 22}+{rx}+{ry}")
        self.prev_popup.deiconify()
        self.prev_popup.lift()

        cached = self._thumb_cache.get(key)
        if cached:
            self._apply_preview_img(cached)
            return

        self._prev_cancel.set()
        cancel = threading.Event()
        self._prev_cancel = cancel
        threading.Thread(target=self._gen_preview_bg,
                         args=(self._current_path, pos_sec, key, cancel),
                         daemon=True).start()

    def _gen_preview_bg(self, path, pos_sec, key, cancel):
        img = ffmpeg_thumbnail(path, pos_sec)
        if cancel.is_set() or img is None:
            return
        self.root.after(0, lambda i=img, k=key: self._finalize_preview(i, k))

    def _finalize_preview(self, img_pil, key):
        photo = ImageTk.PhotoImage(img_pil)
        self._thumb_cache.put(key, photo)
        self._apply_preview_img(photo)

    def _apply_preview_img(self, photo):
        self._prev_img_ref = photo
        self.prev_img_label.config(image=photo)
        self.prev_popup.deiconify()

    def _hide_preview(self, _event=None):
        if self._prev_after_id:
            self.root.after_cancel(self._prev_after_id)
            self._prev_after_id = None
        self.prev_popup.withdraw()

    # ── 動画クリック（ポーリング）─────────────────────────────────────────

    def _setup_video_click(self):
        self._lbtn_prev      = False
        self._our_pid        = os.getpid()
        self._click_time     = 0.0
        self._poll_video_click()

    def _ask_file(self, fn, *args, **kwargs):
        """filedialogのラッパー。開いている間だけ_native_dialog_openを立てて
        クリック・ホイール操作の貫通判定に使う（ネイティブダイアログはTk側から
        矩形を取得できないため）。"""
        self._native_dialog_open = True
        try:
            return fn(*args, **kwargs)
        finally:
            self._native_dialog_open = False

    def _pos_blocked_by_subwindow(self, px, py):
        """指定した画面座標が、自アプリの浮動サブウィンドウ（About/GPU設定/
        画像調整/プレビュー/各種ポップアップメニュー）の上にあるかどうか。
        winfo_id()とGetForegroundWindow()の直接比較はTk側のウィンドウ構造の
        都合で一致しないことがあり、通常のクリック・ホイール操作まで巻き込んで
        壊れてしまったため、元の矩形判定方式に戻し、列挙対象を追加している。
        ネイティブのファイルダイアログはTk側から矩形を取得できないため、
        こちらは_native_dialog_openフラグ（開いている間だけ立てる）で判定する。
        """
        if self._native_dialog_open:
            return True
        # 全画面時は操作バーが映像の上にオーバーレイ表示される（place）ため、
        # 表示中はタイトルバーなしのウィンドウ内子要素として矩形判定する。
        try:
            bar = self.ctrl_bar
            if bar.winfo_viewable():
                ox = bar.winfo_rootx()
                oy = bar.winfo_rooty()
                ow = bar.winfo_width()
                oh = bar.winfo_height()
                if ox <= px <= ox + ow and oy <= py <= oy + oh:
                    return True
        except Exception:
            pass
        TITLE_H = 35
        for win in (getattr(self, "_adj_win", None),
                    getattr(self, "_gpu_win", None),
                    getattr(self, "prev_popup", None),
                    getattr(self, "_about_win", None),
                    getattr(self, "_menu_popup", None)):
            try:
                if win and win.winfo_viewable():
                    ox = win.winfo_rootx()
                    oy = win.winfo_rooty() - TITLE_H
                    ow = win.winfo_width()
                    oh = win.winfo_height() + TITLE_H
                    if ox <= px <= ox + ow and oy <= py <= oy + oh:
                        return True
            except Exception:
                pass
        return False

    def _poll_video_click(self):
        try:
            if sys.platform == "win32":
                import ctypes
                state   = ctypes.windll.user32.GetAsyncKeyState(0x01)
                is_down = bool(state & 0x8000)
                if is_down and not self._lbtn_prev:
                    fg     = ctypes.windll.user32.GetForegroundWindow()
                    fg_pid = ctypes.c_ulong(0)
                    ctypes.windll.user32.GetWindowThreadProcessId(
                        fg, ctypes.byref(fg_pid))
                    if fg_pid.value != self._our_pid:
                        self._lbtn_prev = is_down
                        self.root.after(50, self._poll_video_click)
                        return
                    px = self.root.winfo_pointerx()
                    py = self.root.winfo_pointery()
                    if self._pos_blocked_by_subwindow(px, py):
                        self._lbtn_prev = is_down
                        self.root.after(50, self._poll_video_click)
                        return
                    cx = self.video_canvas.winfo_rootx()
                    cy = self.video_canvas.winfo_rooty()
                    cw = self.video_canvas.winfo_width()
                    ch = self.video_canvas.winfo_height()
                    if cx <= px <= cx + cw and cy <= py <= cy + ch:
                        # シングルクリックでの一時停止は廃止（全画面移行のダブルクリック
                        # 判定と競合し、切替時にpauseが挟まって滑らかさを損なうため）。
                        # 動画エリアはダブルクリックでの全画面切替のみを担当する。
                        # 一時停止はスペースキーまたは操作バーの▶ボタンで行う。
                        now = time.time()
                        if now - self._click_time < 0.35:
                            self._click_time = 0.0
                            self.toggle_fullscreen()
                        else:
                            self._click_time = now
                self._lbtn_prev = is_down
        except Exception:
            pass
        self.root.after(50, self._poll_video_click)

    # ── キーバインド ──────────────────────────────────────────────────────

    def _bind_keys(self):
        self.root.bind("<space>",   lambda e: self.toggle_play())
        self.root.bind("<Left>",    lambda e: self.seek_backward())
        self.root.bind("<Right>",   lambda e: self.seek_forward())
        self.root.bind(",",         lambda e: self.frame_backward())
        self.root.bind(".",         lambda e: self.frame_forward())
        self.root.bind("<F11>",     lambda e: self.toggle_fullscreen())
        self.root.bind("<Escape>",  lambda e: self._exit_fullscreen())
        self.root.bind("<Up>",      lambda e: self._vol_step(5))
        self.root.bind("<Down>",    lambda e: self._vol_step(-5))
        self.root.bind("m",         lambda e: self.toggle_mute())
        self.root.bind("t",         lambda e: self._toggle_always_on_top())
        self.root.bind("<Control-Right>", lambda e: self._play_next())
        self.root.bind("<Control-Left>",  lambda e: self._play_prev())
        self.root.bind("i",         lambda e: self.player.command("script-binding", "stats/display-stats-toggle"))
        self.root.bind("I",         lambda e: self.player.command("script-binding", "stats/display-stats-toggle"))
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        # bind_allは画面座標が動画キャンバスと重なっていれば発火するため、
        # サブウィンドウ（設定画面等、動画キャンバスに重ねて開く）が前面にある
        # 状態でホイール操作すると音量が変わってしまうクリック貫通と同種のバグを防ぐ。
        if self._pos_blocked_by_subwindow(event.x_root, event.y_root):
            return
        cx = self.video_canvas.winfo_rootx()
        cy = self.video_canvas.winfo_rooty()
        cw = self.video_canvas.winfo_width()
        ch = self.video_canvas.winfo_height()
        if cx <= event.x_root <= cx + cw and cy <= event.y_root <= cy + ch:
            self._vol_step(5 if event.delta > 0 else -5)

    # ── リアルタイム自動調整 ──────────────────────────────────────────────

    def _toggle_rt_adj(self):
        if self._rt_enabled:
            self._rt_enabled = False
            self._rt_stop.set()
            self._rt_btn.config(text="⚡ リアルタイム自動調整: OFF", fg=COL_TXT)
            self._auto_btn.config(text="⚡ AUTO", fg=COL_TXT)
            self._auto_adj_status.set("")
            if self._pre_rt_adj:
                for k, v in self._pre_rt_adj.items():
                    self._adj_vars[k][0].set(v)
                    try:
                        self.player[k] = int(v)
                    except Exception:
                        pass
                self._pre_rt_adj = None
            self._apply_glsl_shaders()
        else:
            if not self._current_path or not FFMPEG:
                self._auto_adj_status.set("⚠ 動画を開いてください（ffmpeg必須）")
                return
            # 現在のスライダー値（MPV整数）を保存して RT 初期値にセット
            self._pre_rt_adj = {k: int(round(self._adj_vars[k][0].get()))
                                for k in ("brightness", "contrast", "gamma", "saturation")}
            for k in ("brightness", "contrast", "gamma", "saturation"):
                v = float(self._pre_rt_adj[k])
                self._rt_base_adj[k] = v
                self._rt_current[k] = v
                self._rt_targets[k] = v
            self._rt_enabled  = True
            self._rt_baseline = None
            self._rt_stop.clear()
            self._apply_glsl_shaders()
            self._rt_btn.config(text="⚡ リアルタイム自動調整: ON", fg=COL_GRN)
            self._auto_btn.config(text="⚡ AUTO", fg=COL_GRN)
            self._auto_adj_status.set("ベースライン解析中...")
            self._rt_thread = threading.Thread(target=self._rt_loop, daemon=True)
            self._rt_thread.start()

    def _rt_establish_baseline(self, path, duration_sec):
        # 32点サンプル（均等分布 + 前後端）で正常フレームをより多く確保
        ratios = [i / 31 for i in range(1, 31)] + [0.02, 0.98]
        samples = []
        for r in ratios:
            if self._rt_stop.is_set():
                return None
            stats = analyze_frame(path, r * duration_sec)
            if stats:
                samples.append(stats)

        if not samples:
            return None

        good = [s for s in samples
                if s["lum_mean"] >= 100.0 and s["lum_std"] >= 30.0]
        if len(good) < 3:
            good = [s for s in samples
                    if s["lum_mean"] >= 70.0 and s["lum_std"] >= 20.0]
        if len(good) < 3:
            good = [s for s in samples if s["lum_mean"] >= 50.0]
        if not good:
            return None

        # 上位15件平均でベースラインを安定化
        top = sorted(good, key=lambda s: s["lum_mean"] * s["lum_std"], reverse=True)[:15]
        return {
            "lum_mean": sum(s["lum_mean"] for s in top) / len(top),
            "lum_std":  sum(s["lum_std"]  for s in top) / len(top),
            "chroma":   sum(s["chroma"]   for s in top) / len(top),
        }

    def _rt_loop(self):
        # MPV整数空間（0=中立）で暗闇補正を計算する。
        EXTREME_RATIO = 0.05
        INTENT_SECS   = 3.0

        path     = self._current_path
        duration = self._get_duration_ms() / 1000.0
        if path and duration > 0:
            bl = self._rt_establish_baseline(path, duration)
            if bl and not self._rt_stop.is_set():
                self._rt_baseline = bl
                self.root.after(0, lambda b=bl: self._auto_adj_status.set(
                    f"ベースライン確立  輝度:{b['lum_mean']:.0f}  "
                    f"コントラスト指標:{b['lum_std']:.0f}"))
            elif not self._rt_stop.is_set():
                self.root.after(0, lambda: self._auto_adj_status.set(
                    "⚠ 明るいフレームが見つかりません"))

        dark_start_time = None

        while self._rt_enabled and not self._rt_stop.is_set():
            path   = self._current_path
            pos_ms = self._get_time_ms()
            bl     = self._rt_baseline
            if path and pos_ms >= 0 and bl:
                stats = analyze_current_frame(self.player)
                if stats and not self._rt_stop.is_set():
                    cur_mean = stats["lum_mean"]
                    cur_std  = stats["lum_std"]
                    norm     = cur_mean / 255.0
                    norm_tgt = bl["lum_mean"] / 255.0
                    ratio_mean = cur_mean / max(bl["lum_mean"], 1.0)
                    ratio_std  = cur_std  / max(bl["lum_std"],  1.0)

                    raw_ratio   = min(ratio_mean, ratio_std)
                    dark_factor = max(0.0, min(1.0,
                        (self._dark_thresh - raw_ratio)
                        / max(self._dark_thresh, 0.01)))

                    if dark_factor < 0.02:
                        dark_start_time = None
                        self._rt_targets.update(self._rt_base_adj)
                        self.root.after(0, lambda rm=ratio_mean, rs=ratio_std:
                            self._auto_adj_status.set(
                                f"✓ 正常  輝度比:{rm:.2f}  コントラスト比:{rs:.2f}"))
                    else:
                        is_extreme = raw_ratio < EXTREME_RATIO
                        if is_extreme:
                            if dark_start_time is None:
                                dark_start_time = time.time()
                            elapsed = time.time() - dark_start_time
                            intent_factor = max(0.6,
                                1.0 - 0.4 * min(1.0, elapsed / INTENT_SECS))
                        else:
                            dark_start_time = None
                            intent_factor   = 1.0

                        scale = dark_factor * intent_factor

                        # ── MPV整数空間で直接計算（提案アルゴリズム）────────
                        lum_ratio = norm / max(norm_tgt, 0.001)
                        # コントラスト: MPV側は+100まで、超過分はGLSLで追加する。
                        # 実効コントラストを最大+300まで拡張する。+100まではMPV、
                        # それを超える分は同じ明るさ方向を引き継ぐGLSLで補う。
                        std_ratio    = bl["lum_std"] / max(cur_std, 1.0)
                        contrast_adj = min(300.0, max(0.0,
                            dark_factor * 180.0 * intent_factor
                            + (std_ratio - 1.0) * 120.0 * scale))

                        # 輝度: 全体を白側へ寄せやすいので、必要な時だけごく少量に留める。
                        brightness_adj = min(3.0, max(0.0, 3.0 * scale))

                        # 彩度: 暗部を持ち上げた時の眠い見え方を補う。
                        # 元映像より色差が落ちた場合だけ追加分を少し増やす。
                        chroma_ratio = bl["chroma"] / max(stats["chroma"], 1.0)
                        saturation_adj = min(60.0, max(0.0,
                            dark_factor * 20.0 * intent_factor
                            + max(0.0, chroma_ratio - 1.0) * 32.0 * scale))

                        def with_base(key, adjustment):
                            return max(-100.0, min(100.0,
                                self._rt_base_adj[key] + adjustment))

                        brightness_tgt = with_base("brightness", brightness_adj)
                        contrast_total = max(-100.0, min(300.0,
                            self._rt_base_adj["contrast"] + contrast_adj))
                        contrast_tgt = contrast_total
                        # ガンマの自動連動は撤去。実効コントラストのシェーダーを
                        # 中間点(pivot)基準の計算に修正した結果、以前のように
                        # コントラストを上げるほど白側だけが伸びる挙動ではなく
                        # なったため、白側を締めるための補正ガンマは不要かつ
                        # コントラストの効きを打ち消してしまう（実測確認済み）。
                        gamma_tgt = with_base("gamma", 0.0)
                        saturation_tgt = with_base("saturation", saturation_adj)

                        self._rt_targets.update({
                            "gamma":      gamma_tgt,
                            "brightness": brightness_tgt,
                            "contrast":   contrast_tgt,
                            "saturation": saturation_tgt,
                        })

                        mode = "🌑 意図的暗所" if is_extreme else "🔄 補正中"
                        self.root.after(0,
                            lambda m=mode, df=dark_factor,
                                   rm=ratio_mean,
                                   b=brightness_tgt, g=gamma_tgt,
                                   c=contrast_tgt, s=saturation_tgt:
                            self._auto_adj_status.set(
                                f"{m}  比率:{rm:.2f}  補正:{df:.2f}"
                                f"  B:{b:.0f}  γ:{g:.0f}  C:{c:.0f}  S:{s:.0f}"))
            self._rt_stop.wait(0.5)

    def _rt_blend_step(self):
        """MPV整数空間でEMAブレンドして直接適用"""
        ALPHA = 0.12
        if self._rt_enabled:
            for key in ("brightness", "contrast", "gamma", "saturation"):
                cur = self._rt_current.get(key, 0.0)
                tgt = self._rt_targets.get(key, 0.0)
                if abs(cur - tgt) > 0.05:
                    new_val = cur + (tgt - cur) * ALPHA
                    self._rt_current[key] = new_val
                else:
                    new_val = cur
                mpv_val = int(round(new_val))
                self._adj_vars[key][0].set(mpv_val)
                if key == "contrast":
                    self._apply_effective_contrast(mpv_val)
                else:
                    try:
                        self.player[key] = mpv_val
                    except Exception:
                        pass

    def _blend_loop(self):
        if self._rt_enabled:
            self._rt_blend_step()
        self.root.after(50, self._blend_loop)

    def _manual_status_loop(self):
        """AUTO停止中も、画質タブのステータス欄で手動調整の実効値を確認できるようにする。"""
        if not self._rt_enabled and hasattr(self, "_auto_adj_status"):
            values = {k: int(round(self._adj_vars[k][0].get()))
                      for k in ("brightness", "gamma", "contrast", "saturation")}
            ratio = "--"
            correction = "手動"
            if self._rt_baseline:
                stats = analyze_current_frame(self.player)
                if stats:
                    ratio = f"{stats['lum_mean'] / max(self._rt_baseline['lum_mean'], 1.0):.2f}"
            self._auto_adj_status.set(
                f"手動  比率:{ratio}  補正:{correction}"
                f"  B:{values['brightness']:+d}  γ:{values['gamma']:+d}"
                f"  C:{values['contrast']:+d}  S:{values['saturation']:+d}")
        self.root.after(1000, self._manual_status_loop)

    # ── 定期更新ループ ────────────────────────────────────────────────────

    def _update_loop(self):
        if not self.is_seeking:
            dur_ms = self._get_duration_ms()
            pos_ms = self._get_time_ms()
            if dur_ms > 0:
                self.seek_var.set(pos_ms / dur_ms * 1000)
            tc = self._fmt(int(pos_ms))
            td = self._fmt(int(dur_ms))
            self.time_var.set(tc)
            self.dur_var.set(td)
            self._time_btn_var.set(f"{tc} / {td}")

        is_playing = (self._cached_pause is False)
        new_icon = "⏸" if is_playing else "▶"
        if self.play_btn.cget("text") != new_icon:
            self.play_btn.config(text=new_icon)

        self.root.after(200, self._update_loop)

    @staticmethod
    def _fmt(ms):
        if ms < 0: ms = 0
        s = ms // 1000
        return f"{s // 3600}:{(s % 3600) // 60:02}:{s % 60:02}"


def _restore_window_geometry(root):
    """保存済みジオメトリを復元。画面外の場合はデフォルトに戻す。"""
    DEFAULT = "960x580"
    try:
        with open(WINDOW_SETTINGS, encoding="utf-8") as f:
            geo = json.load(f).get("geometry", DEFAULT)
        # "WxH+X+Y" をパース
        import re
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo)
        if not m:
            root.geometry(DEFAULT)
            return
        w, h, x, y = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        # ウィンドウが完全に画面外／操作不能なサイズの場合はデフォルトへ
        margin = 50  # タイトルバーが最低限この幅は画面内に収まるか
        if (x + w < margin or x > sw - margin or
                y + h < margin or y > sh - margin or
                w < 100 or h < 100):
            root.geometry(DEFAULT)
        else:
            root.geometry(geo)
    except Exception:
        root.geometry(DEFAULT)


def main():
    root = TkinterDnD.Tk()
    _restore_window_geometry(root)
    app = VideoPlayer(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(500, lambda: app._open_path(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
