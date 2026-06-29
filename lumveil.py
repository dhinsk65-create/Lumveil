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
FFMPEG         = shutil.which("ffmpeg")

# 設定は %APPDATA%\Lumveil\ に保存（Program Files は書き込み不可のため）
_BASE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Lumveil")
os.makedirs(_BASE_DIR, exist_ok=True)

ADJ_SETTINGS    = os.path.join(_BASE_DIR, "adj_settings_mpv.json")
GPU_SETTINGS    = os.path.join(_BASE_DIR, "gpu_settings_mpv.json")
PLAYER_SETTINGS = os.path.join(_BASE_DIR, "player_settings.json")
WINDOW_SETTINGS = os.path.join(_BASE_DIR, "window_settings.json")

# MPV 画像調整パラメータ（整数 -100〜100、デフォルト 0）
ADJ_PARAMS = [
    ("brightness", "輝度",          -100, 100, 0),
    ("contrast",   "コントラスト",   -100, 100, 0),
    ("gamma",      "ガンマ",         -100, 100, 0),
    ("saturation", "彩度",           -100, 100, 0),
    ("hue",        "色相",           -100, 100, 0),
]


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


def analyze_frame(path, pos_sec):
    img = _ffmpeg_pipe(path, pos_sec, 64, 36, timeout=2.0)
    if img is None:
        return None
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
        self._rt_baseline = None
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
        self._gpu_hwdec       = "auto-safe"
        self._gpu_glsl        = []   # list of absolute shader paths
        self._load_gpu_settings()

        self._thumb_cache   = ThumbnailCache()
        self._prev_after_id = None
        self._prev_cancel   = threading.Event()
        self._prev_img_ref  = None

        self._current_path = None
        self.fps           = 30.0
        self.is_seeking    = False
        self._adj_vars     = {}
        self._speed        = 1.0
        self._muted        = False
        self._lbtn_prev    = False

        self._build_ui()
        self.root.update()  # canvas を確実に実体化してから winfo_id を取得

        # MPV プレイヤー（wid でキャンバスに埋め込み）
        mpv_kwargs = dict(
            wid=str(self.video_canvas.winfo_id()),
            keep_open="yes",
            keep_open_pause=False,
            loglevel="error",
            vo="gpu",
            hwdec="auto-safe",
        )
        if sys.platform == "win32":
            mpv_kwargs["gpu_api"] = "d3d11"
        self.player = mpv.MPV(**mpv_kwargs)
        self.player.volume = 80

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
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self._save_player_settings()
        self._save_window_settings()
        self._rt_stop.set()
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
        except Exception:
            pass

    def _save_player_settings(self):
        try:
            with open(PLAYER_SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"volume": self.vol_var.get()}, f)
        except Exception:
            pass

    def _on_mpv_file_loaded(self, _event):
        """ファイルロード後に画像調整値とノイズ設定を再適用"""
        self.root.after(200, self._apply_all_adj)
        if self._denoise:
            self.root.after(300, lambda: self.player.command("vf", "set", "hqdn3d"))

    def _apply_all_adj(self):
        for key, *_ in ADJ_PARAMS:
            self._on_adjust(key)

    # ── ボタンヘルパー ────────────────────────────────────────────────────

    def _btn(self, parent, text, cmd, fg=COL_TXT, bg=None,
             font=("Segoe UI", 11), pad=(8, 4)):
        bg = bg or BG_BTN
        b  = tk.Button(parent, text=text, command=cmd,
                       bg=bg, fg=fg, relief=tk.FLAT, bd=0,
                       font=font, padx=pad[0], pady=pad[1],
                       cursor="hand2",
                       activebackground=BG_BTN_H, activeforeground=COL_TXT)
        b.bind("<Enter>", lambda e, b=b, abg=BG_BTN_H: b.config(bg=abg))
        b.bind("<Leave>", lambda e, b=b, nbg=bg: b.config(bg=nbg))
        return b

    def _fixed_btn(self, parent, text, cmd, w=34, h=28,
                   fg=COL_TXT, bg=None, font=("Segoe UI", 11)):
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
        return f, b

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

        f, _ = self._fixed_btn(btn_row, "⊕", self.open_file, w=30,
                               font=("Segoe UI", 10))
        f.pack(side=tk.LEFT, padx=1)
        self._sep(btn_row)

        f, _ = self._fixed_btn(btn_row, "⏮", self.seek_backward, w=32,
                               font=("Segoe UI", 13))
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(btn_row, "⏴", self.frame_backward, w=30,
                               font=("Segoe UI", 11))
        f.pack(side=tk.LEFT, padx=1)

        _pf, self.play_btn = self._fixed_btn(btn_row, "▶", self.toggle_play,
                                             w=42, font=("Segoe UI", 16))
        _pf.pack(side=tk.LEFT, padx=2)

        f, _ = self._fixed_btn(btn_row, "⏵", self.frame_forward, w=30,
                               font=("Segoe UI", 11))
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(btn_row, "⏭", self.seek_forward, w=32,
                               font=("Segoe UI", 13))
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(btn_row, "⏹", self.stop, w=30,
                               font=("Segoe UI", 11))
        f.pack(side=tk.LEFT, padx=1)

        self._sep(btn_row)

        _mf, self._mute_btn = self._fixed_btn(btn_row, "🔊", self.toggle_mute,
                                              w=32, font=("Segoe UI", 12))
        _mf.pack(side=tk.LEFT)

        self.vol_var = tk.IntVar(value=80)
        vol_sc = tk.Scale(btn_row, from_=0, to=100, orient=tk.HORIZONTAL,
                          variable=self.vol_var, command=self._on_volume,
                          length=90, showvalue=False,
                          bg="#ffffff", troughcolor="#555555",
                          activebackground="#dddddd",
                          highlightthickness=1, highlightbackground="#ffffff",
                          bd=0, sliderlength=14, sliderrelief=tk.RAISED)
        vol_sc.pack(side=tk.LEFT, padx=(2, 6))
        self._vol_pending = False

        self._time_btn_var = tk.StringVar(value="0:00:00 / 0:00:00")
        tk.Label(btn_row, textvariable=self._time_btn_var,
                 bg=BG_CTRL, fg=COL_DIM,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)

        right = tk.Frame(btn_row, bg=BG_CTRL)
        right.pack(side=tk.RIGHT)

        f, self._fs_btn = self._fixed_btn(right, "⛶", self.toggle_fullscreen,
                                          w=30, font=("Segoe UI", 12))
        f.pack(side=tk.RIGHT, padx=1)

        f, _ = self._fixed_btn(right, "ⓘ", self._show_about,
                               w=24, font=("Segoe UI", 9))
        f.pack(side=tk.RIGHT, padx=(4, 1))

        f, self._auto_btn = self._fixed_btn(right, "⚡ AUTO", self._toggle_rt_adj,
                                            w=66, font=("Segoe UI", 9))
        f.pack(side=tk.RIGHT, padx=1)

        f, _ = self._fixed_btn(right, "🎛", self._toggle_adj_win,
                               w=30, font=("Segoe UI", 11))
        f.pack(side=tk.RIGHT, padx=1)

        f, _ = self._fixed_btn(right, "⚙ GPU", self._toggle_gpu_win,
                               w=54, font=("Segoe UI", 9))
        f.pack(side=tk.RIGHT, padx=1)

        self._sep(right)

        spd = tk.Frame(right, bg=BG_CTRL)
        spd.pack(side=tk.RIGHT)
        self._btn(spd, "＋", self._speed_up,
                  font=("Segoe UI", 10), pad=(5, 3)).pack(side=tk.RIGHT, padx=1)
        self._speed_var = tk.StringVar(value="1.00×")
        tk.Label(spd, textvariable=self._speed_var,
                 bg=BG_CTRL, fg=COL_BLU,
                 font=("Consolas", 9), width=5).pack(side=tk.RIGHT)
        self._btn(spd, "－", self._speed_down,
                  font=("Segoe UI", 10), pad=(5, 3)).pack(side=tk.RIGHT, padx=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Horizontal.TScale",
                        background=BG_CTRL, troughcolor="#333333",
                        sliderlength=14, sliderrelief=tk.FLAT)
        style.configure("Adj.Horizontal.TScale",
                        background=BG_ADJ, troughcolor="#333333",
                        sliderlength=12, sliderrelief=tk.FLAT)

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

    # ── About ─────────────────────────────────────────────────────────────

    def _show_about(self):
        win = tk.Toplevel(self.root)
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
        tk.Label(win, text="ver. 1.2",
                 bg=BG_ADJ, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack()
        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=30, pady=14)
        tk.Label(win, text="ふぁん × Claude Code",
                 bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 9)).pack()
        self._btn(win, "閉じる", win.destroy,
                  pad=(20, 5)).pack(pady=(16, 0))

    # ── 画像調整ウィンドウ ─────────────────────────────────────────────────

    def _build_adj_win(self):
        win = tk.Toplevel(self.root)
        win.title("画像調整")
        win.configure(bg=BG_ADJ)
        win.resizable(False, False)
        win.withdraw()
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._adj_win = win

        tk.Label(win, text="画像調整 (MPV)", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 11, "bold"), pady=10).pack()
        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12)

        for key, label, lo, hi, default in ADJ_PARAMS:
            row = tk.Frame(win, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=16, pady=4)
            tk.Label(row, text=f"{label}:", width=11, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 9)).pack(side=tk.LEFT)
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
        tk.Label(tr, text="補正閾値:", width=11, anchor="w",
                 bg=BG_ADJ, fg=COL_YEL, font=("Segoe UI", 9)).pack(side=tk.LEFT)
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
        tk.Label(tr, text="← 敏感   鈍感 →",
                 bg=BG_ADJ, fg=COL_DIM, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)

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
        if self._adj_win.winfo_viewable():
            self._adj_win.withdraw()
        else:
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._adj_win.geometry(f"+{x}+{y}")
            self._adj_win.deiconify()
            self._adj_win.lift()

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
        ("auto-safe", "自動（安全）"),
        ("auto",      "自動（全て試行）"),
        ("no",        "無効（ソフトウェア）"),
    ]

    def _build_gpu_win(self):
        win = tk.Toplevel(self.root)
        win.title("GPU / シェーダー設定")
        win.configure(bg=BG_ADJ)
        win.resizable(False, False)
        win.withdraw()
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._gpu_win = win

        tk.Label(win, text="GPU / シェーダー設定", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 11, "bold"), pady=10).pack()
        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12)

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
            "ハードウェアデコード":
                "GPUでデコードし CPU 負荷を軽減します。\n"
                "自動（安全）: 実績あるデコーダのみ使用（推奨）\n"
                "自動（全て）: 対応していれば全て試行\n"
                "無効: ソフトウェアデコード（互換性最高）\n"
                "※ 変更は次のファイルから有効",
            "GLSLシェーダー":
                "外部シェーダーファイル（.glsl）を適用します。\n"
                "Anime4K: アニメ向け超解像・ノイズ除去\n"
                "FSRCNNX: ニューラルネット超解像（GPU負荷大）\n"
                "複数追加可能。上から順に適用されます。",
        }

        def _row(label):
            r = tk.Frame(win, bg=BG_ADJ)
            r.pack(fill=tk.X, padx=16, pady=5)
            lbl = tk.Label(r, text=f"{label}:", width=16, anchor="w",
                           bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 9))
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
                  relief=tk.FLAT, font=("Segoe UI", 9), width=22)
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
                   relief=tk.FLAT, font=("Segoe UI", 9), width=22)
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
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
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
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._sigmoid_btn.pack(side=tk.LEFT)

        # 縮小補正
        r = _row("縮小補正")
        self._correct_ds_btn = tk.Button(
            r, text="ON" if self._gpu_correct_ds else "OFF",
            command=self._on_gpu_correct_ds,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_correct_ds else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._correct_ds_btn.pack(side=tk.LEFT)

        # フレーム補間
        r = _row("フレーム補間")
        self._interpolate_btn = tk.Button(
            r, text="ON" if self._gpu_interpolate else "OFF",
            command=self._on_gpu_interpolate,
            bg=BG_ADJ, fg=COL_GRN if self._gpu_interpolate else COL_TXT,
            relief=tk.FLAT, bd=0,
            font=("Segoe UI", 9), padx=10, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._interpolate_btn.pack(side=tk.LEFT)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(6, 2))

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

        # ハードウェアデコード
        r = _row("ハードウェアデコード")
        self._gpu_hwdec_var = tk.StringVar()
        cur_hw_lbl = next(lbl for val, lbl in self._HWDEC_OPTIONS
                          if val == self._gpu_hwdec)
        self._gpu_hwdec_var.set(cur_hw_lbl)
        hw_labels = [lbl for _, lbl in self._HWDEC_OPTIONS]
        hom = tk.OptionMenu(r, self._gpu_hwdec_var, *hw_labels,
                            command=self._on_gpu_hwdec)
        hom.config(bg=BG_ADJ, fg=COL_TXT, activebackground=BG_BTN_H,
                   activeforeground=COL_TXT, highlightthickness=0,
                   relief=tk.FLAT, font=("Segoe UI", 9), width=22)
        hom["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                           activebackground=BG_BTN_H, activeforeground=COL_TXT)
        hom.pack(side=tk.LEFT)

        tk.Frame(win, bg="#333333", height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        br = tk.Frame(win, bg=BG_ADJ, pady=8)
        br.pack()
        self._btn(br, "↺ リセット", self._reset_gpu,
                  bg=BG_RED, pad=(10, 5)).pack(side=tk.LEFT, padx=5)

        self._gpu_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._gpu_status,
                 bg=BG_ADJ, fg=COL_GRN,
                 font=("Segoe UI", 8), pady=6).pack()

    def _toggle_gpu_win(self):
        if self._gpu_win.winfo_viewable():
            self._gpu_win.withdraw()
        else:
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._gpu_win.geometry(f"+{x}+{y}")
            self._gpu_win.deiconify()
            self._gpu_win.lift()

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

    def _on_gpu_interpolate(self):
        self._gpu_interpolate = not self._gpu_interpolate
        on = self._gpu_interpolate
        self._interpolate_btn.config(text="ON" if on else "OFF",
                                     fg=COL_GRN if on else COL_TXT)
        try:
            if on:
                self.player["video-sync"]    = "display-resample"
                self.player["interpolation"] = True
                self.player["tscale"]        = "oversample"
            else:
                self.player["video-sync"]    = "audio"
                self.player["interpolation"] = False
            self._gpu_status.set(f"✓ フレーム補間: {'ON' if on else 'OFF'}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_glsl_add(self):
        paths = filedialog.askopenfilenames(
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
        except Exception:
            pass

    def _on_gpu_hwdec(self, lbl):
        val = next(v for v, l in self._HWDEC_OPTIONS if l == lbl)
        self._gpu_hwdec = val
        try:
            self.player["hwdec"] = val
            self._gpu_status.set(f"✓ hwdec: {val}（次ファイルから有効）")
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
        self._gpu_hwdec       = "auto-safe"
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
        self._glsl_listbox.delete(0, tk.END)
        cur_hw_lbl = next(lbl for val, lbl in self._HWDEC_OPTIONS
                          if val == self._gpu_hwdec)
        self._gpu_hwdec_var.set(cur_hw_lbl)
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
            self._apply_glsl_shaders()
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
            if self._gpu_interpolate:
                self.player["video-sync"]    = "display-resample"
                self.player["interpolation"] = True
                self.player["tscale"]        = "oversample"
        except Exception:
            pass
        self._apply_glsl_shaders()

    # ── スライダークリック修正 ─────────────────────────────────────────────

    @staticmethod
    def _fix_scale_click(scale, var, lo, hi):
        def _jump(e):
            ratio = max(0.0, min(1.0, e.x / max(scale.winfo_width(), 1)))
            scale.after(1, lambda: var.set(lo + ratio * (hi - lo)))
        scale.bind("<Button-1>", _jump, add=True)

    # ── 速度 ──────────────────────────────────────────────────────────────

    _SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]

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

    def toggle_mute(self):
        self._muted = not self._muted
        try:
            self.player.mute = self._muted
        except Exception:
            pass
        self._mute_btn.config(text="🔇" if self._muted else "🔊")

    def _on_volume(self, val):
        v = int(float(val))
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
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def _exit_fullscreen(self):
        self.root.attributes("-fullscreen", False)

    # ── 画像調整 ──────────────────────────────────────────────────────────

    def _on_adjust(self, key):
        val = int(round(self._adj_vars[key][0].get()))
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
            self._auto_adj_status.set("📂 設定を読み込みました")
        except Exception as e:
            self._auto_adj_status.set(f"⚠ 読み込み失敗: {e}")

    # ── ノイズ軽減 ────────────────────────────────────────────────────────

    def _toggle_denoise(self):
        self._denoise = not self._denoise
        on = self._denoise
        try:
            self.player.command("vf", "set", "hqdn3d" if on else "")
        except Exception:
            pass
        self._denoise_btn.config(
            text=f"🔇 ノイズ軽減: {'ON' if on else 'OFF'}",
            fg=COL_GRN if on else COL_TXT)

    # ── DnD ──────────────────────────────────────────────────────────────

    def _setup_dnd(self):
        self.root.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event):
        raw = event.data.strip()
        if raw.startswith("{") and "}" in raw:
            raw = raw[1:raw.index("}")]
        path = raw.strip()
        if os.path.isfile(path):
            self._open_path(path)

    # ── ファイルを開く ────────────────────────────────────────────────────

    def open_file(self):
        path = filedialog.askopenfilename(
            title="動画ファイルを選択",
            filetypes=[
                ("動画ファイル",
                 "*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v "
                 "*.ts *.m2ts *.vob *.ogv *.3gp *.rmvb *.rm *.hevc *.h264"),
                ("すべてのファイル", "*.*"),
            ])
        if path:
            self._open_path(path)

    def _open_path(self, path):
        self._current_path = path
        self._thumb_cache.clear()
        try:
            self.player.play(path)
        except Exception:
            pass
        self.root.title(f"Lumveil — {os.path.basename(path)}")
        self.root.after(600, self._fetch_fps)

    def _fetch_fps(self):
        try:
            fps = self.player.container_fps
            if fps and fps > 0:
                self.fps = fps
        except Exception:
            pass

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

    def _get_duration_ms(self):
        try:
            d = self.player.duration
            return (d or 0.0) * 1000
        except Exception:
            return 0.0

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
                try:
                    self.player.seek(float(val) / 1000 * dur_ms / 1000,
                                     reference="absolute", precision="exact")
                except Exception:
                    pass

    def _on_seek_release(self, _event):
        self.is_seeking = False

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
        self._click_after_id = None
        self._poll_video_click()

    def _fire_single_click(self):
        self._click_after_id = None
        self.toggle_play()

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
                    # 自アプリの浮動ウィンドウ上のクリックは無視
                    # winfo_rooty は クライアント上端なので -35px でタイトルバーを含める
                    TITLE_H = 35
                    blocked = False
                    for overlay in (getattr(self, "_adj_win", None),
                                    getattr(self, "_gpu_win", None),
                                    getattr(self, "prev_popup", None)):
                        if overlay and overlay.winfo_viewable():
                            ox = overlay.winfo_rootx()
                            oy = overlay.winfo_rooty() - TITLE_H
                            ow = overlay.winfo_width()
                            oh = overlay.winfo_height() + TITLE_H
                            if ox <= px <= ox + ow and oy <= py <= oy + oh:
                                blocked = True
                                break
                    if not blocked:
                        cx = self.video_canvas.winfo_rootx()
                        cy = self.video_canvas.winfo_rooty()
                        cw = self.video_canvas.winfo_width()
                        ch = self.video_canvas.winfo_height()
                        if cx <= px <= cx + cw and cy <= py <= cy + ch:
                            now = time.time()
                            if now - self._click_time < 0.35:
                                if self._click_after_id:
                                    self.root.after_cancel(self._click_after_id)
                                    self._click_after_id = None
                                self._click_time = 0.0
                                self.toggle_fullscreen()
                            else:
                                self._click_time = now
                                if self._click_after_id:
                                    self.root.after_cancel(self._click_after_id)
                                self._click_after_id = self.root.after(
                                    350, self._fire_single_click)
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
        self.root.bind("i",         lambda e: self.player.command("script-binding", "stats/display-stats-toggle"))
        self.root.bind("I",         lambda e: self.player.command("script-binding", "stats/display-stats-toggle"))
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
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
        else:
            if not self._current_path or not FFMPEG:
                self._auto_adj_status.set("⚠ 動画を開いてください（ffmpeg必須）")
                return
            # 現在のスライダー値（MPV整数）を保存して RT 初期値にセット
            self._pre_rt_adj = {k: int(round(self._adj_vars[k][0].get()))
                                for k in ("brightness", "contrast", "gamma", "saturation")}
            for k in ("brightness", "contrast", "gamma", "saturation"):
                v = float(self._pre_rt_adj[k])
                self._rt_current[k] = v
                self._rt_targets[k] = v
            self._rt_enabled  = True
            self._rt_baseline = None
            self._rt_stop.clear()
            self._rt_btn.config(text="⚡ リアルタイム自動調整: ON", fg=COL_GRN)
            self._auto_btn.config(text="⚡ AUTO", fg=COL_GRN)
            self._auto_adj_status.set("ベースライン解析中...")
            threading.Thread(target=self._rt_loop, daemon=True).start()

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
        # MPV整数空間（0=中立）で直接計算
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
                stats = analyze_frame(path, max(0.0, pos_ms / 1000.0))
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
                        self._rt_targets.update({
                            "brightness": 0.0, "contrast": 0.0,
                            "gamma": 0.0,      "saturation": 0.0})
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

                        # ガンマ: 対数スケールで白飛び防止（MPV 0〜+65）
                        # log(1/ratio)/log(20) で ratio=0.07→≈58, ratio=0.3→≈26
                        log_need  = math.log(1.0 / max(lum_ratio, 0.01)) / math.log(20)
                        gamma_tgt = min(65.0, max(0.0, log_need * 65.0 * scale))

                        # コントラスト: 動的レンジ不足を補正（MPV 0〜+70）
                        std_ratio    = bl["lum_std"] / max(cur_std, 1.0)
                        contrast_tgt = min(70.0, max(0.0,
                            (std_ratio - 1.0) * 20.0 * scale))

                        # 輝度: 廃止（ガンマのみで持ち上げ、白飛び防止）
                        brightness_tgt = 0.0

                        # 彩度: 上限を抑制（MPV 0〜+30）
                        saturation_tgt = min(30.0, max(0.0,
                            dark_factor * 32.0 * scale))

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
                                   g=gamma_tgt, c=contrast_tgt:
                            self._auto_adj_status.set(
                                f"{m}  比率:{rm:.2f}  補正:{df:.2f}"
                                f"  γ:{g:.0f}  C:{c:.0f}"))
            self._rt_stop.wait(0.5)

    def _rt_blend_step(self):
        """MPV整数空間でEMAブレンドして直接適用"""
        ALPHA = 0.12
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
            try:
                self.player[key] = mpv_val
            except Exception:
                pass

    def _blend_loop(self):
        if self._rt_enabled:
            self._rt_blend_step()
        self.root.after(50, self._blend_loop)

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

        try:
            paused = self.player.pause
            is_playing = (paused is False)
        except Exception:
            is_playing = False
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
