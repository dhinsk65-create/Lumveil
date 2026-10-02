"""
Video Player MPV版  ―  YouTube スタイル UI
依存: python-mpv, pillow, tkinterdnd2  +  ffmpeg
      Windows: mpv-2.dll を Python.exe と同じフォルダか PATH に置くこと
        → https://mpv.io/installation/ の "Windows" から入手
  pip install python-mpv pillow tkinterdnd2
"""
import os, sys, shutil, subprocess, threading, time, math, json, ctypes, queue
import base64, hashlib, re, tempfile, urllib.error, urllib.request, webbrowser
from ctypes import wintypes

# libmpv-2.dll をスクリプトと同じフォルダから確実に読み込む
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ["PATH"]
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import mpv
from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD

# ── 定数 ─────────────────────────────────────────────────────────────────
SEEK_SEC       = 5
PREV_W, PREV_H = 192, 108
CACHE_MAX      = 30
SNAP_STEP      = 2
APP_VERSION     = "2.1.0"
GITHUB_REPO     = "dhinsk65-create/Lumveil"
GITHUB_URL      = f"https://github.com/{GITHUB_REPO}"
UPDATE_CHECK_INTERVAL = 6 * 60 * 60
AMF_FRC_MAX_FPS = 50.0  # 元動画がこれを超えるfpsならAMD AMFフレーム補間を自動バイパス
FFMPEG         = shutil.which("ffmpeg")

_SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))


def _resource_dir(name, script_dir=None, executable=None, frozen=None):
    """Resolve external release assets as well as bundled/development assets."""
    script_dir = script_dir if script_dir is not None else _SCRIPT_DIR
    executable = executable if executable is not None else sys.executable
    frozen = frozen if frozen is not None else getattr(sys, "frozen", False)
    candidates = []
    if frozen:
        candidates.append(os.path.join(os.path.dirname(executable), name))
    candidates.append(os.path.join(script_dir, name))
    if frozen:
        candidates.append(os.path.join(os.path.dirname(executable), "_internal", name))
    return next((path for path in candidates if os.path.isdir(path)), candidates[0])


_SHADER_DIR = _resource_dir("shaders")
_RT_CONTRAST_SHADER_PATH = os.path.join(_SHADER_DIR, "lumveil_auto_contrast.glsl")
_RT_SHADOW_SHADER_PATH = os.path.join(_SHADER_DIR, "lumveil_shadow_lift.glsl")

if not FFMPEG:
    for _ffmpeg_candidate in (
        os.path.join(_SCRIPT_DIR, "ffmpeg.exe"),
        os.path.join(os.path.dirname(_SCRIPT_DIR), "ffmpeg.exe"),
    ):
        if os.path.isfile(_ffmpeg_candidate):
            FFMPEG = _ffmpeg_candidate
            break

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


def _atomic_write_json(path, data, **kwargs):
    """Keep the previous settings intact until a complete new file is durable."""
    path = os.path.abspath(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.",
                                     suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, **kwargs)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _bounded_number(value, default, lo, hi):
    try:
        number = float(value)
        return max(lo, min(hi, number)) if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _parse_seek_time(value):
    """Accept seconds, mm:ss, or hh:mm:ss, without silently accepting typos."""
    parts = str(value).strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ValueError("秒、分:秒、時:分:秒で入力してください。")
    numbers = [float(p) for p in parts]
    if any(not math.isfinite(n) for n in numbers) or any(n >= 60 for n in numbers[1:]):
        raise ValueError("分・秒は60未満で入力してください。")
    return sum(number * 60 ** i for i, number in enumerate(reversed(numbers)))


def _launch_installer_after_exit(path, digest):
    """A separate system process waits for this executable to release its files."""
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest or ""):
        raise ValueError("インストーラーの検証情報がありません。")
    quoted_path = os.path.abspath(path).replace("'", "''")
    script = (
        f"$target = '{quoted_path}'; "
        f"Wait-Process -Id {os.getpid()} -Timeout 30 -ErrorAction SilentlyContinue; "
        f"if (Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue) {{ exit 1 }}; "
        f"if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne '{digest[7:]}') {{ exit 2 }}; "
        "Start-Process -FilePath $target"
    )
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return subprocess.Popen([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            creationflags=subprocess.CREATE_NO_WINDOW)

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

# AUTO（リアルタイム暗所自動調整）の強度モード
# 値は (強度倍率, 意図的暗所での残存補正下限)
RT_MODES = {
    "控えめ":       (0.4, 0.10),
    "標準":         (0.6, 0.20),
    "見やすさ優先": (0.9, 0.35),
    "極暗":         (0.9, 0.35),
}

# フォルダ内連続再生・複数ファイルD&D時の対象拡張子（open_fileのフィルタと同一）
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".m2ts", ".vob", ".ogv", ".3gp", ".rmvb", ".rm", ".hevc", ".h264",
}
EOF_ACTION_OPTIONS = {"停止": "stop", "次の動画を再生": "next", "リピート": "repeat"}

# The desktop build uses a per-session mutex and a local named pipe so opening
# a second file reuses the already visible player instead of creating another
# mpv/Tk process.  The pipe carries only UTF-8 JSON file paths and never
# exposes a network port.
_SINGLE_INSTANCE_MUTEX = r"Local\Lumveil.SingleInstance.v1"
_SINGLE_INSTANCE_PIPE  = r"\\.\pipe\Lumveil.SingleInstance.v1"
_SINGLE_INSTANCE_MAX_MESSAGE = 64 * 1024
_MPV_EVENT_PENDING = object()


class _SingleInstance:
    """Keep one Lumveil process per Windows desktop session.

    The primary process owns a named mutex and a short-lived named-pipe
    listener.  A later process forwards its file arguments and exits.  The
    implementation is deliberately best-effort on non-Windows platforms so
    source development remains possible there without adding a dependency.
    """

    _ERROR_ALREADY_EXISTS = 183
    _ERROR_PIPE_BUSY = 231
    _ERROR_PIPE_CONNECTED = 535
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _PIPE_ACCESS_INBOUND = 0x00000001
    _PIPE_TYPE_MESSAGE = 0x00000004
    _PIPE_READMODE_MESSAGE = 0x00000002
    _PIPE_WAIT = 0x00000000

    def __init__(self, primary, kernel32=None, mutex_handle=None):
        self.primary = primary
        self._kernel32 = kernel32
        self._mutex_handle = mutex_handle
        self._stop_event = threading.Event()
        self._server_thread = None

    @staticmethod
    def _valid_handle(handle):
        value = getattr(handle, "value", handle)
        invalid = ctypes.c_void_p(-1).value
        return value not in (None, 0, -1, invalid)

    @classmethod
    def acquire(cls):
        if sys.platform != "win32":
            return cls(True)
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.GetLastError.restype = wintypes.DWORD
            kernel32.CreateNamedPipeW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                wintypes.DWORD, wintypes.LPVOID]
            kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
            kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
            kernel32.ConnectNamedPipe.restype = wintypes.BOOL
            kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
            kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
            kernel32.ReadFile.argtypes = [
                wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            kernel32.ReadFile.restype = wintypes.BOOL
            kernel32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                wintypes.HANDLE]
            kernel32.CreateFileW.restype = wintypes.HANDLE
            kernel32.WriteFile.argtypes = [
                wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            kernel32.WriteFile.restype = wintypes.BOOL
            kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
            kernel32.WaitNamedPipeW.restype = wintypes.BOOL

            mutex = kernel32.CreateMutexW(
                None, False, _SINGLE_INSTANCE_MUTEX)
            if not cls._valid_handle(mutex):
                # Failing open keeps unsupported/locked-down Windows builds
                # usable; normal Windows builds always take the mutex path.
                return cls(True, kernel32)
            if ctypes.get_last_error() == cls._ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(mutex)
                return cls(False, kernel32)
            return cls(True, kernel32, mutex)
        except Exception:
            # The app has historically started without any single-instance
            # dependency.  Preserve that fallback if Win32 setup is blocked.
            return cls(True)

    def start_server(self, message_queue):
        if not (self.primary and self._mutex_handle and self._kernel32):
            return
        self._server_thread = threading.Thread(
            target=self._server_loop,
            args=(message_queue,),
            name="LumveilSingleInstance",
            daemon=True,
        )
        self._server_thread.start()

    def _server_loop(self, message_queue):
        kernel32 = self._kernel32
        while not self._stop_event.is_set():
            pipe = kernel32.CreateNamedPipeW(
                _SINGLE_INSTANCE_PIPE,
                self._PIPE_ACCESS_INBOUND,
                self._PIPE_TYPE_MESSAGE | self._PIPE_READMODE_MESSAGE | self._PIPE_WAIT,
                1,
                _SINGLE_INSTANCE_MAX_MESSAGE,
                _SINGLE_INSTANCE_MAX_MESSAGE,
                1000,
                None,
            )
            if not self._valid_handle(pipe):
                time.sleep(0.05)
                continue
            try:
                connected = bool(kernel32.ConnectNamedPipe(pipe, None))
                if not connected and ctypes.get_last_error() != self._ERROR_PIPE_CONNECTED:
                    continue
                buffer = ctypes.create_string_buffer(_SINGLE_INSTANCE_MAX_MESSAGE)
                read = wintypes.DWORD()
                if not kernel32.ReadFile(
                        pipe, buffer, _SINGLE_INSTANCE_MAX_MESSAGE,
                        ctypes.byref(read), None):
                    continue
                try:
                    payload = json.loads(buffer.raw[:read.value].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if payload.get("shutdown"):
                    self._stop_event.set()
                    continue
                paths = payload.get("paths")
                if payload.get("bring_to_front") and isinstance(paths, list):
                    message_queue.put([
                        os.path.abspath(path) for path in paths
                        if isinstance(path, str)
                    ])
            finally:
                try:
                    kernel32.DisconnectNamedPipe(pipe)
                except Exception:
                    pass
                kernel32.CloseHandle(pipe)

    def _send_payload(self, payload, timeout=3.0):
        if not self._kernel32:
            return False
        try:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if len(raw) > _SINGLE_INSTANCE_MAX_MESSAGE:
            return False
        deadline = time.monotonic() + timeout
        kernel32 = self._kernel32
        while time.monotonic() < deadline:
            pipe = kernel32.CreateFileW(
                _SINGLE_INSTANCE_PIPE,
                self._GENERIC_WRITE,
                0,
                None,
                self._OPEN_EXISTING,
                self._FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if self._valid_handle(pipe):
                try:
                    written = wintypes.DWORD()
                    buffer = ctypes.create_string_buffer(raw)
                    return bool(kernel32.WriteFile(
                        pipe, buffer, len(raw), ctypes.byref(written), None)
                        and written.value == len(raw))
                finally:
                    kernel32.CloseHandle(pipe)
            if ctypes.get_last_error() == self._ERROR_PIPE_BUSY:
                kernel32.WaitNamedPipeW(_SINGLE_INSTANCE_PIPE, 100)
            else:
                time.sleep(0.05)
        return False

    def forward_paths(self, paths):
        return self._send_payload({
            "bring_to_front": True,
            "paths": list(paths),
        })

    def close(self):
        if not self.primary:
            return
        self._stop_event.set()
        # The listener is a daemon thread and Windows releases its named pipe
        # handles when this process exits.  Do not send a synchronous wake-up
        # message or join here: shutdown must never wait on the IPC path.
        if self._valid_handle(self._mutex_handle):
            try:
                self._kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
            self._mutex_handle = None


# Lumveil v2.0 palette. The video surface remains near-black so the content
# stays visually dominant while the controls use a restrained blue/cyan glass.
BG_VIDEO   = "#05080D"
BG_APP     = "#0A0F15"
BG_CTRL    = "#0D151D"
BG_BTN     = "#101B24"
BG_BTN_H   = "#1A2A35"
BG_BORDER  = "#283844"
BG_PRESSED = "#253947"
BG_SELECTED = "#123947"
BG_SUCCESS = "#12372D"
BG_WARNING = "#3B3219"
BG_DANGER  = "#3D2025"
BG_RED     = "#FF6B6B"
BG_ADJ     = "#0B0D10"
COL_TXT    = "#E8EDF2"
COL_DIM    = "#97A1AD"
COL_BLU    = "#2ED7FF"
COL_YEL    = "#F2C14E"
COL_GRN    = "#4FD18B"
COL_RED    = "#FF6B6B"
COL_PUR    = "#58C8F8"
SETTING_LABEL_W = 18


def _apply_dark_titlebar(window):
    """Use the supported DWM dark-caption attribute when available.

    Windows 10 builds used attribute 19 before attribute 20 became stable.
    Both calls are deliberately best-effort so older Windows/Tk builds retain
    the normal system title bar instead of failing application startup.
    """
    if sys.platform != "win32":
        return False
    try:
        window.update_idletasks()
        child_hwnd = int(window.winfo_id())
        parent_hwnd = ctypes.windll.user32.GetParent(child_hwnd)
        hwnd = parent_hwnd or child_hwnd
        enabled = ctypes.c_int(1)
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attribute in (20, 19):
            if dwm(hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0:
                return True
    except (AttributeError, OSError, tk.TclError, ValueError):
        pass
    return False

# AUTO強度モードの表示色(操作バーの⚡AUTOボタンはテキスト固定・色でモードを示す)
RT_MODE_COLORS = {
    "OFF":          COL_TXT,
    "控えめ":       COL_BLU,
    "標準":         COL_GRN,
    "見やすさ優先": COL_YEL,
    "極暗":         COL_PUR,
}

# 操作バーは狭幅でもA-Bなど隣接操作を圧迫しない短縮名を使う。
# 設定値・メニュー・保存データでは従来の正式名を維持する。
RT_MODE_TOOLBAR_LABELS = {
    "OFF": "OFF",
    "控えめ": "控えめ",
    "標準": "標準",
    "見やすさ優先": "見やすさ",
    "極暗": "極暗",
}

# 用途別画質プリセット。詳細設定を知らなくても、画質と負荷の優先度を選べる。
# 外部GLSLは利用者の設定資産のため、ここでは変更しない。
QUALITY_PRESETS = {
    "軽快":       {"scale": "bilinear", "cscale": "bilinear", "deband": False,
                   "interpolate": False, "anime": "なし", "rt_mode": None},
    "標準":       {"scale": "lanczos", "cscale": "spline36", "deband": False,
                   "interpolate": True,  "anime": "なし", "rt_mode": None},
    "アニメ高画質": {"scale": "lanczos", "cscale": "spline36", "deband": True,
                   "interpolate": True,  "anime": "モードA", "rt_mode": None},
    "暗所優先":   {"scale": "lanczos", "cscale": "spline36", "deband": False,
                   "interpolate": True,  "anime": "なし", "rt_mode": "標準"},
}


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
        tk.Label(self._tip, text=self._text, bg=BG_BTN_H, fg=COL_TXT,
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


def _frame_stats(img, dark_thresh=60, crush_thresh=25):
    from PIL import ImageStat
    gray = img.convert("L")
    rgb  = img.convert("RGB")
    gs   = ImageStat.Stat(gray)
    cs   = ImageStat.Stat(rgb)
    rm, gm, bm = cs.mean
    # 暗部率: 輝度<dark_threshの画素の割合。histogram()はCで計算されるためピクセルループより高速。
    hist = gray.histogram()
    total = sum(hist)
    dark_thresh = max(0, min(256, int(dark_thresh)))
    dark_ratio = (sum(hist[:dark_thresh]) / total) if total else 0.0
    # 黒潰れ率: 輝度<crush_threshの画素の割合。通常の暗所(輝度30〜90に分布)と
    # 真の黒潰れ場面を識別するための第3条件。
    crush_thresh = max(0, min(256, int(crush_thresh)))
    crush_ratio = (sum(hist[:crush_thresh]) / total) if total else 0.0
    return {
        "lum_mean": gs.mean[0],
        "lum_std":  gs.stddev[0],
        "chroma":   max(rm, gm, bm) - min(rm, gm, bm),
        "dark_ratio": dark_ratio,
        "crush_ratio": crush_ratio,
    }


def analyze_frame(path, pos_sec):
    """ベースライン確立用: 任意の時刻のフレームをffmpegで抜き出して解析する
    （再生位置を動かさずにサンプリングする必要があるため、こちらは維持）。"""
    img = _ffmpeg_pipe(path, pos_sec, 64, 36, timeout=2.0)
    if img is None:
        return None
    return _frame_stats(img)


def analyze_current_frame(player, dark_thresh=60, crush_thresh=25):
    """リアルタイム補正用: 現在表示中のフレームをmpvから直接取得して解析する。
    ffmpegのプロセス起動・ファイル再オープンが不要になり、AUTO稼働中の
    CPU/ディスク負荷を大幅に減らせる（0.5秒毎にffmpegを起動していたのを廃止）。"""
    try:
        img = player.screenshot_raw(includes="video")
    except Exception:
        return None
    img = img.resize((64, 36))
    return _frame_stats(img, dark_thresh=dark_thresh, crush_thresh=crush_thresh)



# ── メインクラス ──────────────────────────────────────────────────────────
class VideoPlayer:
    def __init__(self, root: TkinterDnD.Tk):
        self.root = root
        self._closing = False
        self._scroll_wheel_handlers = []
        self._ui_dispatch_queue = queue.Queue()
        self.root.title("Lumveil")
        self.root.configure(bg=BG_APP)
        self.root.minsize(720, 460)
        self._init_ui_icons()

        # RT 自動調整（MPV整数空間で計算: 0=中立）
        self._rt_enabled  = False
        self._rt_stop     = threading.Event()
        self._rt_generation = 0
        self._rt_baseline_cache = {}
        self._rt_baseline_cache_lock = threading.Lock()
        self._rt_applied_values = {}
        self._rt_analysis_revision = 0
        self._rt_targets  = {k: 0.0 for k in ("brightness", "contrast", "gamma", "saturation")}
        self._rt_current  = {k: 0.0 for k in ("brightness", "contrast", "gamma", "saturation")}
        # AUTO開始時の手動調整値。暗所補正はこの値を打ち消さず、補正分だけを加算する。
        self._rt_base_adj = {k: 0.0 for k in ("brightness", "contrast", "gamma", "saturation")}
        # シャドウリフト（0.0〜1.0のGLSL空間、MPV整数4キーとは別管理）
        self._rt_targets["shadow_lift"] = 0.0
        self._rt_current["shadow_lift"] = 0.0
        self._rt_baseline = None
        self._rt_thread   = None
        self._rt_threads = []
        self._manual_status_stats = None
        self._manual_status_pending = False
        self._dark_thresh = 1.0
        self._pre_rt_adj  = None
        self._rt_mode     = "標準"
        # シャドウリフト手動スライダー（0〜100%、AUTO停止中のみ有効）。
        # _adj_vars（mpvプロパティ直結の4キー）とは別管理。
        self._manual_shadow_lift = tk.DoubleVar(value=0.0)

        # GLSLシェーダーへ渡すパラメータの一元管理（個別にsetすると互いに上書きし合うため）
        self._shader_opts = {"auto_contrast": 0.0, "shadow_lift": 0.0}
        self._last_shader_opts = None

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
        self._quality_preset  = "カスタム"
        self._applying_quality_preset = False
        self._load_gpu_settings()
        # AUTO強度を先に復元。手動画質はUI構築後に復元し、AUTOは開始しない。
        self._load_rt_mode()

        self._thumb_cache   = ThumbnailCache()
        self._prev_after_id = None
        self._prev_img_ref  = None
        self._preview_condition = threading.Condition()
        self._preview_job = None
        self._preview_worker = None
        self._preview_worker_stop = threading.Event()

        self._current_path = None
        self._media_generation = 0
        self._bookmarks    = self._load_bookmarks()
        self._shot_dir     = os.path.join(_BASE_DIR, "Screenshots")
        self._native_dialog_open = False
        self._fs_bar_visible  = False
        self._fs_hide_after_id = None
        self._control_hide_after_id = None
        self._controls_visible = True
        self._picture_mode = "標準"
        self._mode_btns    = {}
        self._rt_mode_btns = {}
        self._a4k_btns     = {}
        self._recent_files = []
        self._resume_positions = {}
        self._always_on_top = False
        self._playlist      = []
        self._playlist_idx  = -1
        self._playlist_scan_token = 0
        self._playlist_popup = None
        self._quality_popup = None
        self._auto_update_checks = False
        self._last_update_check = 0.0
        self._update_info = None
        self._update_check_in_progress = False
        self._volume_popup_after_id = None
        self._preview_key = None
        self._preview_pending_key = None
        # 再生設定（初期値は従来の挙動を維持）
        self._playback_eof_action = "next"   # next / stop / repeat (current file)
        self._resume_enabled      = True
        self._restore_manual_settings = True
        self._sync_preferences = {}
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
        # v2.1-style immersive layout: keep only the most common secondary
        # actions on the dock.  Everything else remains available in "...".
        self._toolbar_default_visible = {"speed", "playlist", "fullscreen"}
        self._toolbar_visible = set(self._toolbar_default_visible)
        self._toolbar_order = [
            "fullscreen", "speed", "playlist", "auto_adjust", "quality", "subtitles", "audio",
            "screenshot", "bookmark", "pin", "recent", "ab_repeat", "gpu", "about",
        ]
        self._toolbar_items = {}
        self._toolbar_auto_hidden = set()
        self._toolbar_resize_after = None
        self._gpu_settings_built = False
        self._gpu_save_after_id = None

        self._build_ui()
        self.root.update()  # canvas を確実に実体化してから winfo_id を取得
        _apply_dark_titlebar(self.root)

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
        # mpv creates a native child window.  Raise the Tk overlays after it
        # exists so the v2.0 header remains visible above the video surface.
        self.root.after(250, self._raise_ui_overlays)

        # duration/pauseは変化頻度が低いため、毎ティックの問い合わせをやめて
        # mpv側からのプロパティ変化通知をキャッシュする方式に変更（負荷軽減）。
        # time-posは再生中ほぼ毎フレーム変化し通知が来すぎるため、従来通り
        # 定期ポーリング（_get_time_ms）のままにしている。
        self._cached_duration_ms = 0.0
        self._cached_time_ms     = 0.0
        self._cached_pause       = True
        self._mpv_observer_fns   = []
        # python-mpv invokes property/event callbacks on its own event thread.
        # Never call Tk from that thread: Tcl may synchronously wait for the
        # main loop, which can deadlock against terminate() during shutdown.
        self._mpv_event_lock = threading.Lock()
        self._mpv_pending_duration = _MPV_EVENT_PENDING
        self._mpv_pending_pause = _MPV_EVENT_PENDING
        self._mpv_pending_eof = None
        self._mpv_pending_file_loaded = False
        self._mpv_active_load = None

        @self.player.property_observer("duration")
        def _obs_duration(_name, value):
            with self._mpv_event_lock:
                self._mpv_pending_duration = value
        self._mpv_observer_fns.append(_obs_duration)

        @self.player.property_observer("pause")
        def _obs_pause(_name, value):
            with self._mpv_event_lock:
                self._mpv_pending_pause = value
        self._mpv_observer_fns.append(_obs_pause)

        # 連続再生: ファイル終端に達したらプレイリストの次のファイルへ自動移行
        @self.player.property_observer("eof-reached")
        def _obs_eof(_name, value):
            self._on_mpv_eof(value)
        self._mpv_observer_fns.append(_obs_eof)

        self._load_player_settings()
        self._apply_playback_eof_action()

        # ファイルロード後に調整値・GPU設定を再適用
        self.player.event_callback("file-loaded")(self._on_mpv_file_loaded)
        self.player.event_callback("start-file")(self._on_mpv_start_file)
        self.player.event_callback("end-file")(self._on_mpv_end_file)
        self._apply_gpu_settings()

        self._build_adj_win()
        if self._restore_manual_settings:
            self._load_adj(quiet=True)
        self.root.after(2500, self._maybe_check_updates)
        self._bind_keys()
        self._setup_dnd()
        self._setup_video_click()
        self._update_loop()
        self._blend_loop()
        self._manual_status_loop()
        self._autosave_resume_loop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._playlist_scan_token += 1
        # Persisting settings must never prevent native cleanup/window close.
        for save in (self._update_resume_position, self._save_window_settings,
                     lambda: self._save_adj(quiet=True)):
            try:
                save()
            except Exception:
                pass
        self._rt_enabled = False
        self._rt_generation += 1
        self._rt_stop.set()
        # property_observerを解除せずにterminate()すると、mpvのイベントスレッドと
        # デッドロックしてアプリが終了不能になることを確認済み。必ず先に解除する。
        for fn in self._mpv_observer_fns:
            try:
                fn.unobserve_mpv_properties()
            except Exception:
                pass
        if self._gpu_save_after_id is not None:
            try:
                self.root.after_cancel(self._gpu_save_after_id)
            except tk.TclError:
                pass
            self._gpu_save_after_id = None
            self._save_gpu_settings()
        self._stop_preview_worker()
        # terminate() waits indefinitely for python-mpv's event thread.  Keep
        # that native cleanup off Tk's close callback so a transient mpv/driver
        # shutdown delay can never leave the window unresponsive.  The RT
        # worker is allowed a bounded grace period in the same daemon cleanup
        # thread before terminate() touches the player handle.
        player = self.player
        self.player = None
        rt_threads = list(self._rt_threads)
        if player:
            threading.Thread(
                target=self._terminate_player_after_close,
                args=(player, rt_threads),
                name="LumveilMpvShutdown",
                daemon=True,
            ).start()
        self.root.destroy()

    @staticmethod
    def _terminate_player_after_close(player, rt_threads):
        deadline = time.monotonic() + 5.0
        for thread in rt_threads:
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        try:
            player.terminate()
        except Exception:
            pass

    def _save_window_settings(self):
        try:
            geo = self.root.geometry()  # "WxH+X+Y"
            _atomic_write_json(WINDOW_SETTINGS, {"geometry": geo})
        except Exception:
            pass

    def _load_player_settings(self):
        try:
            with open(PLAYER_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            vol = round(_bounded_number(data.get("volume"), 80, 0, 100))
            self.vol_var.set(vol)
            self.player.volume = float(vol)
            shot_dir = data.get("screenshot_dir")
            if isinstance(shot_dir, str) and shot_dir:
                self._shot_dir = shot_dir
            recent = data.get("recent_files", [])
            self._recent_files = list(dict.fromkeys(
                path for path in recent if isinstance(path, str) and path
            ))[:10] if isinstance(recent, list) else []
            resume = data.get("resume_positions", {})
            self._resume_positions = {
                path: value for path, position in resume.items()
                if isinstance(path, str) and path
                and type(position) in (int, float)
                and position >= 0
                and (value := _bounded_number(position, -1, 0, float("inf"))) >= 0
            } if isinstance(resume, dict) else {}
            for key, default in (("always_on_top", False), ("resume_enabled", True),
                                 ("restore_manual_settings", True), ("auto_update_checks", False)):
                if not isinstance(data.get(key, default), bool):
                    data[key] = default
            self._always_on_top = bool(data.get("always_on_top", False))
            self._playback_eof_action = data.get("playback_eof_action", "next")
            if self._playback_eof_action not in EOF_ACTION_OPTIONS.values():
                self._playback_eof_action = "next"
            self._resume_enabled = bool(data.get("resume_enabled", True))
            self._restore_manual_settings = bool(data.get("restore_manual_settings", True))
            self._sync_preferences = {
                "sub-delay": _bounded_number(data.get("sub_delay"), 0, -5, 5),
                "sub-scale": _bounded_number(data.get("sub_scale"), 1, .5, 2),
                "audio-delay": _bounded_number(data.get("audio_delay"), 0, -5, 5),
            }
            self._folder_end_action = data.get("folder_end_action", "stop")
            if self._folder_end_action not in ("stop", "loop"):
                self._folder_end_action = "stop"
            self._playlist_sort = data.get("playlist_sort", "name")
            if self._playlist_sort not in ("name", "modified"):
                self._playlist_sort = "name"
            self._auto_update_checks = bool(data.get("auto_update_checks", False))
            self._last_update_check = _bounded_number(data.get("last_update_check"), 0, 0, float("inf"))
            ui_layout_version = round(_bounded_number(data.get("ui_layout_version"), 1, 1, 2))
            saved_toolbar = data.get("toolbar_visible")
            if ui_layout_version >= 2 and isinstance(saved_toolbar, list):
                valid = set(self._toolbar_item_definitions())
                self._toolbar_visible = {key for key in saved_toolbar
                                         if isinstance(key, str) and key in valid}
            saved_order = data.get("toolbar_order")
            if ui_layout_version >= 2 and isinstance(saved_order, list):
                valid = set(self._toolbar_order)
                ordered = list(dict.fromkeys(key for key in saved_order
                                            if isinstance(key, str) and key in valid))
                self._toolbar_order = ordered + [key for key in self._toolbar_order
                                                 if key not in ordered]
            elif ui_layout_version < 2:
                # One-time migration from the wide v1.x toolbar to the
                # compact v2 layout.  Customization remains available after
                # the new layout has been saved once.
                self._toolbar_visible = set(self._toolbar_default_visible)
                self._toolbar_order = [
                    "fullscreen", "speed", "playlist", "auto_adjust", "quality",
                    "subtitles", "audio", "screenshot", "bookmark", "pin",
                    "recent", "ab_repeat", "gpu", "about",
                ]
            if self._always_on_top:
                self.root.attributes("-topmost", True)
                self._set_button_selected(self._pin_btn, True, "success")
            if self._toolbar_items:
                self._refresh_toolbar()
        except Exception:
            pass

    def _save_player_settings(self):
        try:
            _atomic_write_json(PLAYER_SETTINGS, {
                    "volume": self.vol_var.get(),
                    "screenshot_dir": self._shot_dir,
                    "recent_files": self._recent_files,
                    "resume_positions": self._resume_positions,
                    "always_on_top": self._always_on_top,
                    "toolbar_visible": sorted(self._toolbar_visible),
                    "toolbar_order": self._toolbar_order,
                    "playback_eof_action": self._playback_eof_action,
                    "resume_enabled": self._resume_enabled,
                    "restore_manual_settings": getattr(self, "_restore_manual_settings", True),
                    "sub_delay": self._sub_delay_var.get() if hasattr(self, "_sub_delay_var") else 0,
                    "sub_scale": self._sub_scale_var.get() if hasattr(self, "_sub_scale_var") else 1,
                    "audio_delay": self._audio_delay_var.get() if hasattr(self, "_audio_delay_var") else 0,
                    "folder_end_action": self._folder_end_action,
                    "playlist_sort": self._playlist_sort,
                    "auto_update_checks": self._auto_update_checks,
                    "last_update_check": self._last_update_check,
                    "ui_layout_version": 2,
                }, ensure_ascii=False)
        except Exception as e:
            self._set_settings_error("プレイヤー設定の保存", e)

    def _toggle_always_on_top(self):
        self._always_on_top = not self._always_on_top
        self.root.attributes("-topmost", self._always_on_top)
        self._set_button_selected(self._pin_btn, self._always_on_top, "success")
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
        # Do not query mpv synchronously while a file is still opening or the
        # player is already shutting down.  The 200 ms UI loop keeps this
        # value current enough for resume playback and makes close reliable.
        pos_ms = self._cached_time_ms
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
        """mpvイベントスレッドからの通知をUIループへ引き渡す。"""
        with self._mpv_event_lock:
            self._mpv_pending_file_loaded = True

    def _on_mpv_start_file(self, event):
        with self._mpv_event_lock:
            self._mpv_active_load = (event.data.playlist_entry_id,
                                     self._media_generation, self._current_path)

    def _on_mpv_end_file(self, event):
        if event.data.reason != 4:  # MPV_END_FILE_REASON_ERROR
            return
        with self._mpv_event_lock:
            load = self._mpv_active_load
        if not load or load[0] != event.data.playlist_entry_id:
            return
        details = event.as_dict(decoder=mpv.strict_decoder)
        reason = details.get("file_error") or details.get("file-error") or f"再生エラー ({event.data.error})"
        self._post_ui(self._report_playback_error, load[1], load[2], reason)

    def _report_playback_error(self, generation, path, reason):
        if self._closing or generation != self._media_generation or path != self._current_path:
            return
        message = f"再生できません: {os.path.basename(path)}\n{reason}"
        self._set_settings_error("動画の再生", reason)
        self._show_error_popup(message)

    def _handle_mpv_file_loaded(self):
        """ファイルロード後に画像調整値・ノイズ設定・AMFフレーム補間を再適用し、続きの位置へシーク"""
        if self._closing:
            return
        self._after_current_file(200, self._apply_all_adj)
        if self._denoise or self._gpu_amf_frc:
            self._after_current_file(300, self._apply_vf_chain)
        resume_sec = self._resume_positions.get(self._current_path) if self._resume_enabled else None
        if resume_sec:
            self._after_current_file(200, self._resume_seek, resume_sec)

    def _after_current_file(self, delay_ms, callback, *args):
        """Discard delayed work when a different load (even of the same file) starts."""
        generation = self._media_generation
        path = self._current_path

        def run():
            if (not self._closing and generation == self._media_generation
                    and path == self._current_path):
                callback(*args)

        return self.root.after(delay_ms, run)

    def _resume_seek(self, pos_sec):
        try:
            self.player.seek(pos_sec, reference="absolute", precision="exact")
        except Exception:
            pass

    def _apply_all_adj(self):
        for key, *_ in ADJ_PARAMS:
            self._on_adjust(key)
        self._rt_applied_values.clear()

    # ── ボタンヘルパー ────────────────────────────────────────────────────

    def _init_ui_icons(self):
        """Select one Windows-native icon family with a Unicode fallback."""
        try:
            families = set(tkfont.families(self.root))
        except tk.TclError:
            families = set()
        icon_family = next((name for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets")
                            if name in families), None)
        if icon_family:
            self._icon_font_name = icon_family
            # Segoe Fluent Icons and Segoe MDL2 Assets share these core glyphs.
            self._icons = {
                "open": "\ue8e5", "back": "\ue72b", "frame_back": "\ue892",
                "play": "\ue768", "pause": "\ue769", "frame_forward": "\ue893",
                "forward": "\ue72a", "stop": "\ue71a", "volume": "\ue767",
                "mute": "\ue74f", "fullscreen": "\ue740", "pin": "\ue718",
                "info": "\ue946", "recent": "\ue81c", "playlist": "\ue8fd",
                "audio": "\ue8d6", "subtitles": "\ue7f0", "screenshot": "\ue722",
                "bookmark": "\ue734", "more": "\ue712",
            }
        else:
            self._icon_font_name = "Segoe UI Symbol"
            self._icons = {
                "open": "+", "back": "«", "frame_back": "‹", "play": "▶",
                "pause": "Ⅱ", "frame_forward": "›", "forward": "»", "stop": "■",
                "volume": "VOL", "mute": "MUTE", "fullscreen": "□", "pin": "PIN",
                "info": "i", "recent": "↶", "playlist": "≡", "audio": "♪",
                "subtitles": "CC", "screenshot": "SS", "bookmark": "◇", "more": "…",
            }
        self._icon_font = (self._icon_font_name, 12)

    @staticmethod
    def _button_is_enabled(button):
        return str(button.cget("state")) != str(tk.DISABLED)

    def _bind_button_states(self, button):
        """Give every helper-created button the same hover/press behavior."""
        def enter(_event):
            if self._button_is_enabled(button):
                button.config(bg=button._lumveil_hover_bg)

        def leave(_event):
            button.config(bg=button._lumveil_bg, fg=button._lumveil_fg)

        def press(_event):
            if self._button_is_enabled(button):
                button.config(bg=BG_PRESSED)

        def release(event):
            if self._button_is_enabled(button):
                inside = 0 <= event.x < button.winfo_width() and 0 <= event.y < button.winfo_height()
                button.config(bg=button._lumveil_hover_bg if inside else button._lumveil_bg)

        button.bind("<Enter>", enter)
        button.bind("<Leave>", leave)
        button.bind("<ButtonPress-1>", press, add="+")
        button.bind("<ButtonRelease-1>", release, add="+")

    def _set_button_visual(self, button, bg=None, fg=None, hover_bg=None):
        """Update a persistent visual state without breaking hover restoration."""
        button._lumveil_bg = bg or getattr(button, "_lumveil_neutral_bg", BG_BTN)
        button._lumveil_fg = fg or getattr(button, "_lumveil_neutral_fg", COL_TXT)
        button._lumveil_hover_bg = hover_bg or BG_BTN_H
        button.config(bg=button._lumveil_bg, fg=button._lumveil_fg,
                      activebackground=BG_PRESSED, activeforeground=COL_TXT)

    def _set_button_selected(self, button, selected, tone="accent"):
        palettes = {
            "accent": (BG_SELECTED, COL_BLU),
            "success": (BG_SUCCESS, COL_GRN),
            "warning": (BG_WARNING, COL_YEL),
            "danger": (BG_DANGER, COL_RED),
        }
        if selected:
            bg, fg = palettes.get(tone, palettes["accent"])
            self._set_button_visual(button, bg, fg)
        else:
            self._set_button_visual(button)

    def _btn(self, parent, text, cmd, fg=COL_TXT, bg=None,
             font=("Segoe UI", 11), pad=(8, 4), tooltip=None):
        bg = bg or BG_BTN
        b  = tk.Button(parent, text=text, command=cmd,
                       bg=bg, fg=fg, relief=tk.FLAT, bd=0,
                       font=font, padx=pad[0], pady=pad[1],
                       cursor="hand2", highlightthickness=0,
                       disabledforeground=COL_DIM,
                       activebackground=BG_PRESSED, activeforeground=COL_TXT)
        b._lumveil_neutral_bg = bg
        b._lumveil_neutral_fg = fg
        self._set_button_visual(b, bg, fg)
        self._bind_button_states(b)
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
                       font=font, cursor="hand2", highlightthickness=0,
                       disabledforeground=COL_DIM,
                       activebackground=BG_PRESSED, activeforeground=COL_TXT)
        b._lumveil_neutral_bg = bg
        b._lumveil_neutral_fg = fg
        self._set_button_visual(b, bg, fg)
        self._bind_button_states(b)
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
            tk.Label(win, text=text, bg=BG_BTN_H, fg=COL_TXT,
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
        tk.Frame(parent, bg=BG_BORDER, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=6)

    # ── UI構築 ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # The video surface owns the whole client area.  All controls are
        # transient overlays; no content header reserves pixels above it.
        self.video_canvas = tk.Canvas(self.root, bg=BG_VIDEO,
                                      highlightthickness=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)

        self.ctrl_bar = tk.Frame(self.root, bg=BG_CTRL,
                                 highlightbackground=BG_BORDER,
                                 highlightthickness=1)
        self.ctrl_bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(self.ctrl_bar, bg=COL_BLU, height=1).pack(fill=tk.X, padx=18)

        seek_row = tk.Frame(self.ctrl_bar, bg=BG_CTRL)
        seek_row.pack(fill=tk.X, padx=18, pady=(6, 1))

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
        self._playback_state_var = tk.StringVar(value="")
        tk.Label(seek_row, textvariable=self._playback_state_var, bg=BG_CTRL,
                 fg=COL_YEL, font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=(8, 0))

        btn_row = tk.Frame(self.ctrl_bar, bg=BG_CTRL)
        btn_row.pack(fill=tk.X, padx=12, pady=(1, 7))
        self._btn_row = btn_row

        # Primary playback controls stay in a single flat row.  Secondary
        # features are kept in the compact right-side menu below.
        playback = tk.Frame(btn_row, bg=BG_CTRL, padx=1, pady=1)
        playback.pack(side=tk.LEFT)
        self._playback_group = playback

        f, _ = self._fixed_btn(playback, self._icons["open"], self.open_file, w=34, h=32,
                               font=self._icon_font, tooltip="ファイルを開く")
        f.pack(side=tk.LEFT, padx=1)
        self._sep(playback)

        f, _ = self._fixed_btn(playback, self._icons["back"], self.seek_backward, w=34, h=32,
                               font=self._icon_font, tooltip="5秒戻る")
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(playback, self._icons["frame_back"], self.frame_backward, w=32, h=32,
                               font=self._icon_font, tooltip="1コマ戻る")
        f.pack(side=tk.LEFT, padx=1)

        _pf, self.play_btn = self._fixed_btn(playback, self._icons["play"], self.toggle_play,
                                             w=32, h=30, fg=COL_BLU, bg=BG_BTN,
                                             font=(self._icon_font_name, 12),
                                             tooltip="再生 / 一時停止")
        _pf.pack(side=tk.LEFT, padx=1)
        self.play_btn._lumveil_neutral_bg = BG_BTN
        self.play_btn._lumveil_neutral_fg = COL_BLU
        self._set_button_visual(self.play_btn, BG_BTN, COL_BLU, hover_bg=BG_BTN_H)

        f, _ = self._fixed_btn(playback, self._icons["frame_forward"], self.frame_forward, w=32, h=32,
                               font=self._icon_font, tooltip="1コマ進む")
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(playback, self._icons["forward"], self.seek_forward, w=34, h=32,
                               font=self._icon_font, tooltip="5秒進む")
        f.pack(side=tk.LEFT, padx=1)
        f, _ = self._fixed_btn(playback, self._icons["stop"], self.stop, w=32, h=32,
                               font=self._icon_font, tooltip="停止")
        f.pack(side=tk.LEFT, padx=1)

        viewing = tk.Frame(btn_row, bg=BG_CTRL, highlightbackground=BG_BORDER,
                           highlightthickness=0, padx=1, pady=1)
        viewing.pack(side=tk.LEFT, padx=(8, 0))
        self._viewing_group = viewing

        _mf, self._mute_btn = self._fixed_btn(viewing, self._icons["volume"], self._toggle_volume_popup,
                                              w=34, h=32, font=self._icon_font,
                                              tooltip="音量（右クリックでミュート）")
        _mf.pack(side=tk.LEFT, padx=1)
        self._mute_btn.bind("<Button-3>", lambda _e: self.toggle_mute())
        self._volume_popup = None

        self.vol_var = tk.IntVar(value=80)
        self._vol_pending = False

        self._toolbar_right = tk.Frame(btn_row, bg=BG_CTRL,
                                       highlightbackground=BG_BORDER, highlightthickness=0,
                                       padx=1, pady=1)
        self._toolbar_right.pack(side=tk.RIGHT)
        btn_row.bind("<Configure>", self._on_toolbar_resize)
        right = self._toolbar_right

        f, self._fs_btn = self._fixed_btn(right, self._icons["fullscreen"], self.toggle_fullscreen,
                                          w=32, h=32, font=self._icon_font, tooltip="全画面表示")
        self._toolbar_items["fullscreen"] = f
        f, self._pin_btn = self._fixed_btn(right, self._icons["pin"], self._toggle_always_on_top,
                                           w=32, h=32, font=self._icon_font, tooltip="常に手前に表示 (T)")
        self._toolbar_items["pin"] = f
        f, _ = self._fixed_btn(right, self._icons["info"], self._show_about, w=32, h=32,
                               font=self._icon_font, tooltip="Lumveilについて")
        self._toolbar_items["about"] = f
        f, self._auto_btn = self._fixed_btn(right, "AUTO · OFF", self._show_auto_menu,
                                            w=96, h=32, font=("Segoe UI", 9, "bold"),
                                            tooltip="暗闇補正(自動)の強度を選択")
        self._auto_frame = f
        self._toolbar_items["auto_adjust"] = f
        f, self._quality_button = self._fixed_btn(
            right, "画質", self._toggle_quality_quick_panel,
            w=44, h=32, font=("Segoe UI", 9, "bold"), tooltip="画質クイックパネル")
        self._quality_frame = f
        self._toolbar_items["quality"] = f
        self._settings_btn, self._settings_button = self._fixed_btn(
            right, "設定", self._toggle_adj_win,
            w=50, h=32, font=("Segoe UI", 9), tooltip="設定を開く")
        f, _ = self._fixed_btn(right, "GPU", self._toggle_gpu_win, w=42, h=32,
                               font=("Segoe UI", 9, "bold"), tooltip="GPU/シェーダー設定")
        self._toolbar_items["gpu"] = f
        f, _ = self._fixed_btn(right, self._icons["recent"], self._show_recent_menu, w=32, h=32,
                               font=self._icon_font, tooltip="最近開いたファイル")
        self._toolbar_items["recent"] = f
        f, _ = self._fixed_btn(right, self._icons["playlist"], self._show_playlist, w=32, h=32,
                               font=self._icon_font, tooltip="再生リスト")
        self._toolbar_items["playlist"] = f
        f, _ = self._fixed_btn(right, self._icons["audio"], lambda: self._show_track_menu("audio"), w=32, h=32,
                               font=self._icon_font, tooltip="音声トラック")
        self._toolbar_items["audio"] = f
        f, _ = self._fixed_btn(right, self._icons["subtitles"], lambda: self._show_track_menu("sub"), w=32, h=32,
                               font=self._icon_font, tooltip="字幕トラック")
        self._toolbar_items["subtitles"] = f
        f, self._ab_btn = self._fixed_btn(right, "A-B", self._toggle_ab_loop, w=50, h=32,
                                          font=("Segoe UI", 9), tooltip="A-Bリピート")
        self._toolbar_items["ab_repeat"] = f
        f, self._shot_btn = self._fixed_btn(right, self._icons["screenshot"], self._take_screenshot, w=32, h=32,
                                            font=self._icon_font, tooltip="スクリーンショット")
        self._shot_btn.bind("<Button-3>", self._show_shot_menu)
        self._toolbar_items["screenshot"] = f
        f, _ = self._fixed_btn(right, self._icons["bookmark"], self._show_bookmark_menu, w=32, h=32,
                               font=self._icon_font, tooltip="ブックマーク")
        self._toolbar_items["bookmark"] = f

        # 速度は数値ボタンに集約し、クリックで一覧から選ぶ。
        spd = tk.Frame(right, bg=BG_CTRL, width=52, height=32)
        spd.pack_propagate(False)
        self._speed_var = tk.StringVar(value="1.00×")
        self._speed_btn = tk.Button(spd, textvariable=self._speed_var,
                                    command=self._show_speed_menu,
                                    bg=BG_CTRL, fg=COL_BLU, relief=tk.FLAT, bd=0,
                                    font=("Consolas", 9), cursor="hand2",
                                    highlightthickness=0, disabledforeground=COL_DIM,
                                    activebackground=BG_PRESSED, activeforeground=COL_TXT)
        self._speed_btn._lumveil_neutral_bg = BG_CTRL
        self._speed_btn._lumveil_neutral_fg = COL_BLU
        self._set_button_visual(self._speed_btn, BG_CTRL, COL_BLU)
        self._bind_button_states(self._speed_btn)
        self._speed_btn.pack(fill=tk.BOTH, expand=True)
        self._add_tooltip(self._speed_btn, "再生速度を選択")
        self._toolbar_items["speed"] = spd

        self._more_btn = self._btn(right, self._icons["more"], self._show_toolbar_menu,
                                   tooltip="その他の操作",
                                   font=self._icon_font, pad=(9, 5))
        self._refresh_toolbar()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Horizontal.TScale",
                        background=BG_CTRL, troughcolor=BG_BORDER,
                        sliderlength=12, sliderrelief=tk.FLAT)
        style.configure("Adj.Horizontal.TScale",
                        background=BG_CTRL, troughcolor=BG_BORDER,
                        sliderlength=12, sliderrelief=tk.FLAT)
        style.configure("Lumveil.TNotebook", background=BG_APP, borderwidth=0)
        style.configure("Lumveil.TNotebook.Tab", background=BG_APP, foreground=COL_DIM,
                        padding=(16, 9), font=("Segoe UI", 9))
        style.map("Lumveil.TNotebook.Tab",
                  background=[("selected", BG_CTRL), ("active", BG_BTN_H)],
                  foreground=[("selected", COL_BLU), ("active", COL_TXT)])
        try:
            # The tab strip is rendered by the custom v2 tab bar below.
            style.layout("Lumveil.TNotebook",
                         [("Notebook.client", {"sticky": "nswe"})])
            style.layout("Lumveil.TNotebook.Tab", [])
        except tk.TclError:
            pass

        self.prev_popup = tk.Toplevel(self.root)
        self.prev_popup.overrideredirect(True)
        self.prev_popup.withdraw()
        # mpv owns a native child window; keep the seek preview above it.
        try:
            self.prev_popup.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.prev_popup.configure(bg="#000000")
        self.prev_img_label = tk.Label(self.prev_popup, bg="black",
                                       bd=1, relief=tk.SOLID)
        self.prev_img_label.pack()
        self.prev_time_label = tk.Label(self.prev_popup, bg="black", fg="white",
                                        font=("Consolas", 8), pady=2)
        self.prev_time_label.pack()

    def _raise_ui_overlays(self):
        """Keep the v2.0 Tk overlays above mpv's native child window."""
        try:
            self.ctrl_bar.lift()
        except tk.TclError:
            pass

    # ── 操作バーの表示設定 ────────────────────────────────────────────────

    def _toolbar_item_definitions(self):
        """ID: （メニュー表示名、実行関数）。すべて「…」から利用できる。"""
        return {
            "fullscreen":  ("全画面表示", self.toggle_fullscreen),
            "pin":         ("常に手前に表示", self._toggle_always_on_top),
            "about":       ("Lumveilについて", self._show_about),
            "auto_adjust": ("暗闇補正（AUTO）", self._toggle_rt_adj),
            "quality":     ("画質クイックパネル", self._toggle_quality_quick_panel),
            "settings":    ("設定", self._toggle_adj_win),
            "gpu":         ("GPU / シェーダー設定", self._toggle_gpu_win),
            "recent":      ("最近開いたファイル", self._show_recent_menu),
            "playlist":    ("再生リスト", self._show_playlist),
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
        # The former three-dot button is intentionally not packed.  Secondary
        # actions are available from the video context menu.
        self._more_btn.pack_forget()
        # side=RIGHT のため、表示順の逆から詰めて左→右の順序を保つ。
        self._settings_btn.pack_forget()
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
        fixed_width = self._settings_btn.winfo_reqwidth() + 8
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
        self._close_quality_quick_panel()
        self._settings_tabs.select(self._advanced_tab)
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
                icon_font = (self._icon_font if key in {
                    "fullscreen", "pin", "about", "recent", "playlist", "audio",
                    "subtitles", "screenshot", "bookmark"
                } else ("Segoe UI", 10))
                item = tk.Label(self._toolbar_preview, text=icon, bg=BG_BTN, fg=COL_TXT,
                                font=icon_font, padx=7, pady=4, cursor="fleur")
                item.pack(side=tk.LEFT, padx=1)
                item.bind("<ButtonPress-1>", lambda e, k=key: self._toolbar_preview_drag_start(e, k))
                item.bind("<B1-Motion>", self._toolbar_preview_drag_motion)
                item.bind("<ButtonRelease-1>", self._toolbar_drag_end)
                self._toolbar_preview_items[key] = item
        # side=RIGHT は先にpackしたものが最右端になる。
        tk.Label(self._toolbar_preview, text=self._icons["more"], bg=BG_BTN_H, fg=COL_TXT,
                 font=self._icon_font, padx=7, pady=4).pack(side=tk.RIGHT, padx=1)
        tk.Label(self._toolbar_preview, text="設定", bg=BG_BTN_H, fg=COL_TXT,
                 font=("Segoe UI", 9), padx=7, pady=5).pack(side=tk.RIGHT, padx=1)

        for key, row in getattr(self, "_toolbar_rows", {}).items():
            active = key == getattr(self, "_toolbar_drag_target", None)
            surface = self._settings_surface(row.master)
            row.config(bg=BG_SELECTED if active else surface)
            for child in row.winfo_children():
                if isinstance(child, tk.Label):
                    child.config(bg=BG_SELECTED if active else surface)

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
            self._settings_tabs.select(self._advanced_tab)
        elif key and target and key != target:
            self._toolbar_order.remove(key)
            self._toolbar_order.insert(self._toolbar_order.index(target), key)
            self._refresh_toolbar()
            self._save_player_settings()
            self._build_toolbar_settings_tab()
            self._settings_tabs.select(self._advanced_tab)
        else:
            self._refresh_toolbar_settings()

    def _on_right_click(self, event):
        """Open the secondary-action list from the video surface."""
        if self.root.attributes("-fullscreen"):
            self._show_fullscreen_bar()
        px, py = event.x_root, event.y_root
        cx = self.video_canvas.winfo_rootx()
        cy = self.video_canvas.winfo_rooty()
        cw = self.video_canvas.winfo_width()
        ch = self.video_canvas.winfo_height()
        if not (cx <= px <= cx + cw and cy <= py <= cy + ch):
            return
        if self._pos_blocked_by_subwindow(px, py):
            return
        return self._show_context_menu(event)

    def _show_context_menu(self, event=None):
        menu = tk.Menu(self.root, tearoff=False, bg=BG_ADJ, fg=COL_TXT,
                       activebackground=BG_BTN_H, activeforeground=COL_TXT,
                       font=("Segoe UI", 9))
        menu.add_command(label="再生 / 一時停止", command=self.toggle_play)
        menu.add_command(label="ミュート", command=self.toggle_mute)
        menu.add_command(label="指定時刻へ移動…", command=self._show_time_jump)
        menu.add_command(label="チャプター…", command=self._show_chapters)
        menu.add_separator()
        secondary = tk.Menu(menu, tearoff=False, bg=BG_ADJ, fg=COL_TXT,
                            activebackground=BG_BTN_H, activeforeground=COL_TXT,
                            font=("Segoe UI", 9))
        self._populate_secondary_menu(secondary)
        menu.add_cascade(label="その他の操作", menu=secondary)
        try:
            x = event.x_root if event else self.root.winfo_pointerx()
            y = event.y_root if event else self.root.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _populate_secondary_menu(self, menu):
        definitions = self._toolbar_item_definitions()
        for key in ("fullscreen", "pin", "auto_adjust", "quality", "settings", "gpu",
                    "recent", "playlist", "subtitles", "audio", "ab_repeat",
                    "screenshot", "bookmark", "about"):
            label, command = definitions[key]
            menu.add_command(label=label, command=command)
        menu.add_separator()
        menu.add_command(label="再生速度", command=self._show_speed_menu)
        menu.add_command(label="スクリーンショットの設定", command=self._show_shot_menu)
        menu.add_command(label="再生情報 / GPU使用状態", command=self._show_playback_info)
        menu.add_command(label="ショートカット一覧", command=self._show_shortcuts)
        menu.add_separator()
        menu.add_command(label="操作バーを設定…", command=self._show_toolbar_settings)

    def _show_toolbar_menu(self, event=None):
        menu = tk.Menu(self.root, tearoff=False, bg=BG_ADJ, fg=COL_TXT,
                       activebackground=BG_BTN_H, activeforeground=COL_TXT,
                       font=("Segoe UI", 9))
        definitions = self._toolbar_item_definitions()
        for key in ("fullscreen", "pin", "auto_adjust", "quality", "settings", "gpu", "recent", "playlist",
                    "subtitles", "audio", "ab_repeat", "screenshot", "bookmark", "about"):
            label, command = definitions[key]
            menu.add_command(label=label, command=command)
        menu.add_separator()
        menu.add_command(label="再生速度を選択…", command=self._show_speed_menu)
        menu.add_command(label="スクリーンショットの保存先…", command=self._show_shot_menu)
        menu.add_separator()

        menu.add_command(label="操作バーを設定…", command=self._show_toolbar_settings)
        try:
            x = event.x_root if event else self.root.winfo_pointerx()
            y = event.y_root if event else self.root.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ── 画質クイックパネル ──────────────────────────────────────────────

    def _close_quality_quick_panel(self):
        if self._quality_popup and self._quality_popup.winfo_exists():
            self._quality_popup.destroy()
        self._quality_popup = None
        if hasattr(self, "_quality_button"):
            self._set_button_selected(self._quality_button, False)

    def _toggle_quality_quick_panel(self):
        if self._quality_popup and self._quality_popup.winfo_exists():
            self._close_quality_quick_panel()
            return

        win = tk.Toplevel(self.root)
        self._quality_popup = win
        win.overrideredirect(True)
        win.configure(bg=BG_APP, highlightbackground=BG_BORDER, highlightthickness=1)
        win.transient(self.root)
        # overrideredirectウィンドウが親の背面へ回るのを防ぐため、表示直後だけ
        # topmostを使って前面化し、整列後すぐ通常のowned windowへ戻す。
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._set_button_selected(self._quality_button, True, "accent")
        self._render_quality_quick_panel(reposition=True)
        win.bind("<Escape>", lambda _e: self._close_quality_quick_panel())
        win.lift(self.root)
        win.after_idle(self._raise_quality_quick_panel)

    def _raise_quality_quick_panel(self):
        win = self._quality_popup
        if not (win and win.winfo_exists()):
            return
        try:
            win.attributes("-topmost", False)
        except tk.TclError:
            pass
        win.lift(self.root)
        win.focus_force()

    def _start_quality_panel_drag(self, event):
        win = self._quality_popup
        if not (win and win.winfo_exists()):
            return
        self._quality_drag_offset = (
            event.x_root - win.winfo_x(), event.y_root - win.winfo_y())
        win.lift(self.root)

    def _drag_quality_panel(self, event):
        win = self._quality_popup
        if not (win and win.winfo_exists()):
            return
        offset_x, offset_y = getattr(self, "_quality_drag_offset", (0, 0))
        x = event.x_root - offset_x
        y = event.y_root - offset_y
        width = max(win.winfo_width(), win.winfo_reqwidth())
        height = max(win.winfo_height(), win.winfo_reqheight())
        x = max(8, min(x, win.winfo_screenwidth() - width - 8))
        y = max(8, min(y, win.winfo_screenheight() - height - 8))
        win.geometry(f"+{x}+{y}")

    def _position_quality_quick_panel(self):
        win = self._quality_popup
        if not (win and win.winfo_exists()):
            return
        win.update_idletasks()
        req_w, req_h = win.winfo_reqwidth(), win.winfo_reqheight()
        anchor = self._quality_button
        if anchor.winfo_ismapped():
            x = anchor.winfo_rootx() + anchor.winfo_width() - req_w
            y = anchor.winfo_rooty() - req_h - 6
            if y < 8:
                y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        else:
            x = self.root.winfo_pointerx() - req_w // 2
            y = self.root.winfo_rooty() + self.root.winfo_height() - req_h - 8
        x = max(8, min(x, win.winfo_screenwidth() - req_w - 8))
        y = max(8, min(y, win.winfo_screenheight() - req_h - 8))
        win.geometry(f"+{x}+{y}")

    def _render_quality_quick_panel(self, *, reposition=False):
        win = self._quality_popup
        if not (win and win.winfo_exists()):
            return
        for child in win.winfo_children():
            child.destroy()

        head = tk.Frame(win, bg=BG_APP, cursor="fleur")
        head.pack(fill=tk.X, padx=12, pady=(10, 5))
        title_label = tk.Label(head, text="画質", bg=BG_APP, fg=COL_TXT,
                               font=("Segoe UI", 12, "bold"), cursor="fleur")
        title_label.pack(side=tk.LEFT)
        for drag_handle in (head, title_label):
            drag_handle.bind("<ButtonPress-1>", self._start_quality_panel_drag)
            drag_handle.bind("<B1-Motion>", self._drag_quality_panel)
        close_btn = self._btn(head, "×", self._close_quality_quick_panel,
                              bg=BG_APP, font=("Segoe UI", 12), pad=(8, 1),
                              tooltip="閉じる")
        close_btn.pack(side=tk.RIGHT)

        card = self._settings_card(win, padx=8, pady=(0, 8))
        surface = self._settings_surface(card)
        self._settings_section_heading(card, "用途別プリセット")
        preset_row = tk.Frame(card, bg=surface)
        preset_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        for name in QUALITY_PRESETS:
            button = self._btn(
                preset_row, name,
                lambda value=name: self._apply_quality_from_panel(value),
                bg=surface, font=("Segoe UI", 9), pad=(7, 4))
            button.pack(side=tk.LEFT, padx=2)
            self._set_button_selected(button, name == self._quality_preset, "accent")

        tk.Frame(card, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=10)
        current_auto = self._rt_mode if self._rt_enabled else "OFF"
        self._settings_section_heading(card, "暗闇補正", f"現在: {current_auto}")
        auto_row = tk.Frame(card, bg=surface)
        auto_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        for name in ["OFF"] + list(RT_MODES.keys()):
            button = self._btn(
                auto_row, name,
                lambda value=name: self._apply_auto_from_panel(value),
                bg=surface, font=("Segoe UI", 9), pad=(7, 4))
            button.pack(side=tk.LEFT, padx=2)
            self._set_button_selected(button, name == current_auto, "accent")

        foot = tk.Frame(card, bg=surface)
        foot.pack(fill=tk.X, padx=10, pady=(2, 10))
        self._btn(foot, "詳細設定を開く", self._open_settings_from_quality_panel,
                  bg=surface, font=("Segoe UI", 9), pad=(10, 5)).pack(side=tk.LEFT)
        self._btn(foot, "閉じる", self._close_quality_quick_panel,
                  bg=surface, font=("Segoe UI", 9), pad=(10, 5)).pack(side=tk.RIGHT)
        if reposition:
            self._position_quality_quick_panel()

    def _apply_quality_from_panel(self, name):
        self._apply_quality_preset(name)
        self._render_quality_quick_panel()

    def _apply_auto_from_panel(self, name):
        self._select_rt_mode(name)
        # 動画未読込などでAUTOを開始できなかった場合も、実状態（OFF）へ即時同期する。
        self._sync_rt_mode_buttons()
        self._render_quality_quick_panel()

    def _open_settings_from_quality_panel(self):
        self._close_quality_quick_panel()
        self._settings_tabs.select(self._quick_tab)
        if not self._settings_win.winfo_viewable():
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._settings_win.geometry(f"+{x}+{y}")
            self._settings_win.deiconify()
        self._settings_win.lift()

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

    # ── 再生リスト ───────────────────────────────────────────────────────

    def _show_playlist(self):
        """現在のフォルダまたはD&Dで作られた再生リストを表示する。"""
        if self._playlist_popup and self._playlist_popup.winfo_exists():
            self._refresh_playlist_popup()
            self._playlist_popup.deiconify()
            self._playlist_popup.lift()
            self._playlist_popup.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._playlist_popup = win
        win.title("再生リスト")
        win.configure(bg=BG_ADJ)
        win.transient(self.root)
        win.geometry("540x420")
        win.minsize(360, 240)
        win.protocol("WM_DELETE_WINDOW", self._close_playlist_popup)
        _apply_dark_titlebar(win)

        head = tk.Frame(win, bg=BG_ADJ)
        head.pack(fill=tk.X, padx=12, pady=(12, 6))
        self._playlist_summary = tk.StringVar()
        tk.Label(head, text="再生リスト", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        tk.Label(head, textvariable=self._playlist_summary, bg=BG_ADJ, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack(side=tk.RIGHT)

        body = tk.Frame(win, bg=BG_ADJ)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        scroll = tk.Scrollbar(body, orient=tk.VERTICAL)
        self._playlist_listbox = tk.Listbox(
            body, bg=BG_CTRL, fg=COL_TXT, selectbackground=BG_SELECTED,
            selectforeground=COL_TXT, activestyle="none", relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), yscrollcommand=scroll.set)
        scroll.config(command=self._playlist_listbox.yview)
        self._playlist_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._playlist_listbox.bind("<Double-Button-1>", self._play_selected_from_popup)
        self._playlist_listbox.bind("<Return>", self._play_selected_from_popup)

        edit = tk.Frame(win, bg=BG_ADJ)
        edit.pack(fill=tk.X, padx=12, pady=(0, 8))
        for label, callback in (
            ("追加…", self._playlist_add_files), ("削除", self._playlist_remove_selected),
            ("↑", lambda: self._playlist_move_selected(-1)),
            ("↓", lambda: self._playlist_move_selected(1)),
            ("保存…", self._playlist_save), ("読込…", self._playlist_load),
        ):
            self._btn(edit, label, callback, bg=BG_ADJ, pad=(7, 4)).pack(side=tk.LEFT, padx=2)

        foot = tk.Frame(win, bg=BG_ADJ)
        foot.pack(fill=tk.X, padx=12, pady=(0, 10))
        self._btn(foot, "前の動画", self._playlist_popup_prev,
                  bg=BG_ADJ, pad=(9, 4)).pack(side=tk.LEFT)
        self._btn(foot, "次の動画", self._playlist_popup_next,
                  bg=BG_ADJ, pad=(9, 4)).pack(side=tk.LEFT, padx=4)
        self._btn(foot, "閉じる", self._close_playlist_popup,
                  bg=BG_ADJ, pad=(9, 4)).pack(side=tk.RIGHT)
        self._refresh_playlist_popup()
        win.lift()
        win.focus_force()

    def _refresh_playlist_popup(self):
        if not (self._playlist_popup and self._playlist_popup.winfo_exists()
                and hasattr(self, "_playlist_listbox")):
            return
        self._playlist_summary.set(f"{len(self._playlist)}件  {self._playlist_idx + 1}/{len(self._playlist)}")
        self._playlist_listbox.delete(0, tk.END)
        for idx, path in enumerate(self._playlist):
            marker = "▶ " if idx == self._playlist_idx else "   "
            self._playlist_listbox.insert(tk.END, f"{marker}{os.path.basename(path)}")
        if 0 <= self._playlist_idx < len(self._playlist):
            self._playlist_listbox.selection_set(self._playlist_idx)
            self._playlist_listbox.see(self._playlist_idx)

    def _close_playlist_popup(self):
        if self._playlist_popup and self._playlist_popup.winfo_exists():
            self._playlist_popup.destroy()
        self._playlist_popup = None

    def _play_selected_from_popup(self, _event=None):
        selection = self._playlist_listbox.curselection()
        if not selection:
            return
        self._playlist_idx = selection[0]
        self._open_path(self._playlist[self._playlist_idx], _from_playlist=True)
        self._close_playlist_popup()

    def _playlist_popup_prev(self):
        self._play_prev()
        self._refresh_playlist_popup()

    def _playlist_popup_next(self):
        self._play_next()
        self._refresh_playlist_popup()

    def _replace_playlist(self, files, *, start_index=None):
        """Invalidate folder scans and preserve the playing item when editing."""
        self._playlist_scan_token += 1
        self._playlist_source = "manual"
        self._playlist = list(files)
        current = os.path.normcase(self._current_path or "")
        self._playlist_idx = next((i for i, p in enumerate(files)
                                   if os.path.normcase(p) == current), -1)
        if start_index is not None and files:
            self._playlist_idx = min(max(0, start_index), len(files) - 1)
            self._open_path(files[self._playlist_idx], _from_playlist=True)
        self._refresh_playlist_popup()

    def _playlist_add_files(self):
        paths = self._ask_file(filedialog.askopenfilenames, title="再生リストに動画を追加",
                              filetypes=[("動画ファイル", " ".join("*" + e for e in sorted(VIDEO_EXTS))),
                                         ("すべてのファイル", "*.*")])
        files = list(self._playlist)
        seen = {os.path.normcase(p) for p in files}
        for path in paths:
            path = os.path.abspath(path)
            if os.path.isfile(path) and os.path.normcase(path) not in seen:
                files.append(path)
                seen.add(os.path.normcase(path))
        self._replace_playlist(files)

    def _playlist_remove_selected(self):
        selection = self._playlist_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        was_current = index == self._playlist_idx
        files = self._playlist[:index] + self._playlist[index + 1:]
        self._replace_playlist(files, start_index=index if was_current else None)
        if was_current and not files:
            self.stop()

    def _playlist_move_selected(self, delta):
        selection = self._playlist_listbox.curselection()
        if not selection:
            return
        index, files = selection[0], list(self._playlist)
        target = index + delta
        if not 0 <= target < len(files):
            return
        files[index], files[target] = files[target], files[index]
        self._replace_playlist(files)
        self._playlist_listbox.selection_clear(0, tk.END)
        self._playlist_listbox.selection_set(target)
        self._playlist_listbox.see(target)

    def _write_playlist_file(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        paths = []
        for item in self._playlist:
            try:
                paths.append(os.path.relpath(item, folder))
            except ValueError:  # different Windows drive
                paths.append(os.path.abspath(item))
        _atomic_write_json(path, {"version": 1, "files": paths}, indent=2, ensure_ascii=False)

    @staticmethod
    def _read_playlist_file(path):
        if os.path.getsize(path) > 4 * 1024 * 1024:
            raise ValueError("再生リストが大きすぎます。")
        with open(path, encoding="utf-8-sig") as source:
            data = json.load(source)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files"), list):
            raise ValueError("Lumveil再生リストの形式が正しくありません。")
        if len(data["files"]) > 10000 or any(not isinstance(p, str) for p in data["files"]):
            raise ValueError("再生リストの項目が正しくありません。")
        folder = os.path.dirname(os.path.abspath(path))
        files, seen, missing = [], set(), 0
        for item in data["files"]:
            full = os.path.abspath(os.path.join(folder, item))
            if not os.path.isfile(full):
                missing += 1
            elif os.path.normcase(full) not in seen:
                files.append(full)
                seen.add(os.path.normcase(full))
        return files, missing

    def _playlist_save(self):
        path = self._ask_file(filedialog.asksaveasfilename, title="再生リストを保存",
                              defaultextension=".lumveil.json",
                              filetypes=[("Lumveil再生リスト", "*.lumveil.json")])
        if path:
            try:
                self._write_playlist_file(path)
            except (OSError, ValueError, TypeError) as exc:
                self._show_error_popup(f"再生リストを保存できませんでした:\n{exc}")

    def _playlist_load(self):
        path = self._ask_file(filedialog.askopenfilename, title="再生リストを読み込む",
                              filetypes=[("Lumveil再生リスト", "*.lumveil.json"), ("JSON", "*.json")])
        if not path:
            return
        try:
            files, missing = self._read_playlist_file(path)
            if not files:
                raise ValueError("再生できるファイルがありません。")
            self._replace_playlist(files, start_index=0)
            if missing:
                self._show_error_popup(f"見つからない動画{missing}件を除いて読み込みました。")
        except (OSError, ValueError, TypeError) as exc:
            self._show_error_popup(f"再生リストを読み込めませんでした:\n{exc}")

    # ── ブックマーク ─────────────────────────────────────────────────────

    def _load_bookmarks(self):
        try:
            with open(BOOKMARKS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_bookmarks(self):
        try:
            _atomic_write_json(BOOKMARKS_FILE, self._bookmarks, ensure_ascii=False, indent=2)
        except Exception as e:
            self._set_settings_error("ブックマークの保存", e)

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

        content = self._make_vertical_scroll_area(win)

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
            row = tk.Frame(content, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=4, pady=1)
            self._btn(row, f"{m['label']}", lambda p=m["pos_ms"]: jump(p),
                      bg=BG_ADJ, pad=(14, 4)).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._btn(row, "✕", lambda p=m["pos_ms"]: remove(p),
                      bg=BG_RED, pad=(6, 4)).pack(side=tk.LEFT, padx=(2, 0))

        win.update_idletasks()
        x = self.root.winfo_pointerx() - win.winfo_reqwidth() // 2
        y = self.root.winfo_rooty() + self.root.winfo_height() - 120 - win.winfo_reqheight()
        width, height = 360, min(520, win.winfo_screenheight() - 100)
        x = min(max(x, 0), win.winfo_screenwidth() - width)
        y = min(max(y, 0), win.winfo_screenheight() - height)
        win.geometry(f"{width}x{height}+{x}+{y}")
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
        tk.Label(win, text=message, bg=BG_ADJ, fg=COL_RED,
                 font=("Segoe UI", 9), justify="left",
                 wraplength=320, padx=14, pady=10).pack()
        self._btn(win, "閉じる", win.destroy,
                  bg=BG_ADJ, pad=(14, 4)).pack(pady=(0, 10))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(x,0)}+{max(y,0)}")
        win.focus_force()

    def _set_settings_error(self, operation, error):
        """設定画面で完結する操作の失敗を、非モーダルに明示する。

        再生中に何度も起こり得る軽微な例外へダイアログを出すと操作を妨げるため、
        GPU/シェーダー/設定保存に限って既存のステータス欄へ理由を表示する。
        """
        message = f"⚠ {operation}に失敗: {error}"
        if hasattr(self, "_settings_status"):
            self._settings_status.set(message)
        if hasattr(self, "_gpu_status"):
            self._gpu_status.set(message)
        if hasattr(self, "_auto_adj_status"):
            self._auto_adj_status.set(message)

    def _flash_shot_btn(self, text):
        # 一時的な完了表示の後は、現在の統一アイコンへ戻す。
        orig = self._icons["screenshot"]
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
                    self._settings_tabs.select(self._advanced_tab)

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
            self._btn(win, "＋ 外部字幕を追加…",
                      self._chain(win.destroy, self._add_external_subtitle),
                      bg=BG_ADJ, pad=(14, 4)).pack(fill=tk.X, padx=4, pady=1)

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

    def _add_external_subtitle(self):
        """現在の動画へ外部字幕を追加し、そのまま選択する。"""
        if not self._current_path:
            self._show_error_popup("外部字幕を追加する動画を開いてください。")
            return
        initialdir = os.path.dirname(self._current_path)
        path = self._ask_file(
            filedialog.askopenfilename,
            title="外部字幕ファイルを選択",
            initialdir=initialdir,
            filetypes=[
                ("字幕ファイル", "*.srt *.ass *.ssa *.vtt *.sub *.smi *.sami *.idx"),
                ("すべてのファイル", "*.*"),
            ])
        if not path:
            return
        path = os.path.abspath(path)
        title = os.path.splitext(os.path.basename(path))[0]
        try:
            # cachedは同じファイルを再選択しても二重に追加せず、直ちに選択する。
            self.player.command("sub-add", path, "cached", title)
            if hasattr(self, "_auto_adj_status"):
                self._auto_adj_status.set(f"✓ 外部字幕を追加: {os.path.basename(path)}")
        except Exception as e:
            self._show_error_popup(f"外部字幕を追加できませんでした:\n{e}")

    # ── About ─────────────────────────────────────────────────────────────

    def _show_about(self):
        self._close_quality_quick_panel()
        if hasattr(self, "_about_tab"):
            self._settings_tabs.select(self._about_tab)
        if not self._settings_win.winfo_viewable():
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._settings_win.geometry(f"+{x}+{y}")
            self._settings_win.deiconify()
        self._settings_win.lift()

    def _open_license_notices(self):
        # In a PyInstaller one-dir build __file__ is under _internal, while
        # release documents are copied beside the executable.
        resource_roots = (
            _SCRIPT_DIR,
            os.path.dirname(_SCRIPT_DIR),
            os.path.dirname(os.path.abspath(sys.executable)),
        )
        path = next(
            (os.path.join(root, "THIRD_PARTY_NOTICES.md")
             for root in resource_roots
             if os.path.isfile(os.path.join(root, "THIRD_PARTY_NOTICES.md"))),
            None,
        )
        if path is None:
            if hasattr(self, "_update_status_var"):
                self._update_status_var.set("第三者ライセンス文書が見つかりません。")
            return

        if (hasattr(self, "_license_win") and self._license_win
                and self._license_win.winfo_exists()):
            self._license_win.deiconify()
            self._license_win.lift()
            self._license_win.focus_force()
            return

        try:
            documents = [("THIRD_PARTY_NOTICES.md", path)]
            license_dir = os.path.join(os.path.dirname(path), "licenses")
            if os.path.isdir(license_dir):
                for filename in sorted(os.listdir(license_dir), key=str.casefold):
                    license_path = os.path.join(license_dir, filename)
                    if os.path.isfile(license_path):
                        documents.append((filename, license_path))

            contents = {}
            for filename, document_path in documents:
                try:
                    with open(document_path, "r", encoding="utf-8", errors="replace") as handle:
                        contents[filename] = handle.read()
                except OSError as exc:
                    contents[filename] = f"{filename}を読み込めません:\n{exc}"

            win = tk.Toplevel(self.root)
            self._license_win = win
            win.title("第三者ライセンス")
            win.configure(bg=BG_ADJ)
            win.resizable(True, True)
            win.minsize(700, 500)
            win.geometry("820x620")
            win.transient(self._settings_win if self._settings_win.winfo_exists() else self.root)
            _apply_dark_titlebar(win)

            head = tk.Frame(win, bg=BG_ADJ)
            head.pack(fill=tk.X, padx=18, pady=(14, 8))
            tk.Label(head, text="第三者ライセンス", bg=BG_ADJ, fg=COL_TXT,
                     font=("Segoe UI", 14, "bold"), anchor="w").pack(fill=tk.X)
            tk.Label(
                head,
                text="同梱ライセンスです。左の一覧から文書を選んでください。",
                bg=BG_ADJ, fg=COL_DIM, font=("Segoe UI", 9), anchor="w",
            ).pack(fill=tk.X, pady=(3, 0))

            body = tk.Frame(win, bg=BG_ADJ)
            body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 10))

            list_frame = tk.Frame(body, bg=BG_CTRL, width=220)
            list_frame.pack(side=tk.LEFT, fill=tk.Y)
            list_frame.pack_propagate(False)
            tk.Label(list_frame, text="文書一覧", bg=BG_CTRL, fg=COL_TXT,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(
                         fill=tk.X, padx=10, pady=(10, 6))
            document_list = tk.Listbox(
                list_frame, bg=BG_CTRL, fg=COL_TXT,
                selectbackground=BG_SELECTED, selectforeground=COL_BLU,
                relief=tk.FLAT, bd=0, highlightthickness=0,
                activestyle="none", font=("Segoe UI", 9),
            )
            document_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 8))

            text_frame = tk.Frame(body, bg=BG_CTRL)
            text_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
            document_text = tk.Text(
                text_frame, bg="#0A1118", fg=COL_TXT,
                insertbackground=COL_TXT, relief=tk.FLAT, bd=0,
                highlightthickness=0, wrap=tk.WORD,
                font=("Consolas", 9), padx=12, pady=10,
            )
            document_scroll = tk.Scrollbar(
                text_frame, orient=tk.VERTICAL, command=document_text.yview,
                bg=BG_CTRL, troughcolor=BG_ADJ, activebackground=BG_BTN_H,
            )
            document_text.configure(yscrollcommand=document_scroll.set)
            document_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            document_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            def show_document(_event=None):
                selection = document_list.curselection()
                if not selection:
                    return
                filename = documents[selection[0]][0]
                document_text.configure(state=tk.NORMAL)
                document_text.delete("1.0", tk.END)
                document_text.insert("1.0", contents[filename])
                document_text.configure(state=tk.DISABLED)
                document_text.yview_moveto(0)

            for filename, _document_path in documents:
                document_list.insert(tk.END, filename)
            document_list.bind("<<ListboxSelect>>", show_document)
            if documents:
                document_list.selection_set(0)
                document_list.activate(0)
                show_document()

            def close_license_window():
                self._license_win = None
                win.destroy()

            foot = tk.Frame(win, bg=BG_ADJ)
            foot.pack(fill=tk.X, padx=18, pady=(0, 14))
            self._btn(foot, "閉じる", close_license_window, bg=BG_ADJ,
                      pad=(12, 5)).pack(side=tk.RIGHT)

            win.protocol("WM_DELETE_WINDOW", close_license_window)
            win.bind("<Escape>", lambda _event: close_license_window())
            win.lift()
            win.focus_force()
        except Exception as exc:
            if hasattr(self, "_update_status_var"):
                self._update_status_var.set(f"ライセンスを開けません: {exc}")

    @staticmethod
    def _version_tuple(value):
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value))
        if not match:
            return (0, 0, 0)
        return tuple(int(part or 0) for part in match.groups())

    def _maybe_check_updates(self):
        if (self._auto_update_checks and
                time.time() - self._last_update_check >= UPDATE_CHECK_INTERVAL):
            self._check_for_updates(manual=False)

    def _toggle_auto_update_checks(self):
        self._auto_update_checks = not self._auto_update_checks
        if hasattr(self, "_auto_update_button"):
            self._auto_update_button.config(text="ON" if self._auto_update_checks else "OFF")
            self._style_toggle_button(self._auto_update_button, self._auto_update_checks)
        self._save_player_settings()
        if self._auto_update_checks:
            self._check_for_updates(manual=False)

    def _check_for_updates(self, manual=True):
        if self._update_check_in_progress:
            return
        self._update_check_in_progress = True
        if hasattr(self, "_update_status_var"):
            self._update_status_var.set("GitHubの更新を確認しています…")
        thread = threading.Thread(target=self._fetch_latest_release,
                                  args=(manual,), daemon=True)
        thread.start()

    def _fetch_latest_release(self, manual):
        error = None
        info = None
        try:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": f"Lumveil/{APP_VERSION}"})
            with urllib.request.urlopen(request, timeout=12) as response:
                release = json.loads(response.read().decode("utf-8"))
            assets = release.get("assets") or []
            installers = [asset for asset in assets
                          if str(asset.get("name", "")).lower().endswith(".exe")
                          and any(word in str(asset.get("name", "")).lower()
                                  for word in ("setup", "installer"))]
            installer = installers[0] if installers else None
            info = {
                "version": str(release.get("tag_name") or release.get("name") or ""),
                "release_url": str(release.get("html_url") or GITHUB_URL + "/releases"),
                "asset_name": str(installer.get("name")) if installer else "",
                "asset_url": str(installer.get("browser_download_url")) if installer else "",
                "digest": str(installer.get("digest") or "") if installer else "",
            }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError) as exc:
            error = str(exc)
        try:
            self._post_ui(self._finish_update_check, info, error, manual)
        except Exception:
            pass

    def _finish_update_check(self, info, error, manual):
        self._update_check_in_progress = False
        self._last_update_check = time.time()
        self._save_player_settings()
        if error:
            if hasattr(self, "_update_status_var"):
                prefix = "更新確認に失敗" if manual else "自動更新確認に失敗"
                self._update_status_var.set(f"{prefix}: {error}")
            return
        if not info or self._version_tuple(info["version"]) <= self._version_tuple(APP_VERSION):
            self._update_info = None
            if hasattr(self, "_update_status_var"):
                self._update_status_var.set(f"Lumveil v{APP_VERSION} は最新です。")
            if hasattr(self, "_update_download_button"):
                self._update_download_button.config(state=tk.DISABLED)
            return
        self._update_info = info
        verified_asset = bool(info["asset_url"] and info["digest"].startswith("sha256:"))
        if not info["asset_name"]:
            installer_note = "インストーラーはまだ公開されていません。"
        elif not verified_asset:
            installer_note = "検証用SHA-256がないため、この画面からはインストールできません。"
        else:
            installer_note = info["asset_name"]
        if hasattr(self, "_update_status_var"):
            self._update_status_var.set(
                f"新しいバージョン {info['version']} が公開されています。\n{installer_note}")
        if hasattr(self, "_update_download_button"):
            self._update_download_button.config(
                state=tk.NORMAL if verified_asset else tk.DISABLED)

    def _download_update(self):
        info = self._update_info
        if not info or not info.get("asset_url"):
            if info:
                webbrowser.open(info.get("release_url", GITHUB_URL + "/releases"))
            return
        if not messagebox.askyesno(
                "Lumveilの更新",
                f"{info['asset_name']} をダウンロードしますか？",
                parent=self._settings_win):
            return
        self._update_download_button.config(state=tk.DISABLED)
        self._update_status_var.set("更新をダウンロードしています…")
        threading.Thread(target=self._download_update_worker,
                         args=(dict(info),), daemon=True).start()

    def _download_update_worker(self, info):
        path = None
        error = None
        try:
            target_dir = os.path.join(tempfile.gettempdir(), "LumveilUpdate")
            os.makedirs(target_dir, exist_ok=True)
            filename = os.path.basename(info["asset_name"])
            path = os.path.join(target_dir, filename)
            request = urllib.request.Request(
                info["asset_url"], headers={"User-Agent": f"Lumveil/{APP_VERSION}"})
            digest = hashlib.sha256()
            with urllib.request.urlopen(request, timeout=30) as response, open(path, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
            expected = info.get("digest", "")
            if not expected.startswith("sha256:"):
                raise ValueError("更新ファイルのSHA-256がありません")
            if digest.hexdigest().lower() != expected[7:].lower():
                raise ValueError("SHA-256の検証に失敗しました")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError) as exc:
            error = str(exc)
        try:
            self._post_ui(self._finish_update_download, path, error)
        except Exception:
            pass

    def _finish_update_download(self, path, error):
        if error:
            self._update_status_var.set(f"ダウンロードに失敗: {error}")
            self._update_download_button.config(state=tk.NORMAL)
            return
        self._update_status_var.set("ダウンロードが完了しました。")
        if not messagebox.askyesno(
                "Lumveilの更新",
                "Lumveilを終了して、インストーラーを起動しますか？",
                parent=self._settings_win):
            self._update_download_button.config(state=tk.NORMAL)
            return
        try:
            _launch_installer_after_exit(path, (self._update_info or {}).get("digest"))
        except (OSError, ValueError) as exc:
            self._update_status_var.set(f"インストーラーを起動できません: {exc}")
            self._update_download_button.config(state=tk.NORMAL)
            return
        self._on_close()

    # ── 画像調整ウィンドウ ─────────────────────────────────────────────────

    def _settings_card(self, parent, *, padx=12, pady=(4, 8)):
        """設定項目を載せる共通カード面。既存レイアウトを包むだけに留める。"""
        card = tk.Frame(parent, bg=BG_CTRL, highlightbackground=BG_BORDER,
                        highlightthickness=1)
        card.pack(fill=tk.X, padx=padx, pady=pady)
        return card

    @staticmethod
    def _settings_surface(widget):
        try:
            return widget.cget("bg")
        except tk.TclError:
            return BG_CTRL

    def _settings_section_heading(self, parent, title, description=None):
        """カード内の小見出しと任意の補足説明を同じ階層で表示する。"""
        bg = self._settings_surface(parent)
        box = tk.Frame(parent, bg=bg)
        box.pack(fill=tk.X, padx=12, pady=(10, 5))
        tk.Label(box, text=title, bg=bg, fg=COL_TXT,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill=tk.X)
        if description:
            tk.Label(box, text=description, bg=bg, fg=COL_DIM,
                     font=("Segoe UI", 8), anchor="w").pack(fill=tk.X, pady=(2, 0))
        return box

    def _style_toggle_button(self, button, enabled, tone="success"):
        """既存のON/OFFボタンを共通の背景付き選択状態へ接続する。"""
        if not hasattr(button, "_lumveil_neutral_bg"):
            bg = self._settings_surface(button.master)
            button._lumveil_neutral_bg = bg
            button._lumveil_neutral_fg = COL_TXT
            self._set_button_visual(button, bg, COL_TXT)
            self._bind_button_states(button)
        self._set_button_selected(button, enabled, tone)

    def _recolor_settings_tree(self, widget, surface=BG_CTRL):
        """既存詳細設定のBG_ADJだけをカード面へ寄せる低コスト移行用。"""
        for child in widget.winfo_children():
            try:
                if child.cget("bg") == BG_ADJ:
                    child.config(bg=surface)
            except (tk.TclError, TypeError):
                pass
            if hasattr(child, "_lumveil_neutral_bg") and child._lumveil_neutral_bg == BG_ADJ:
                child._lumveil_neutral_bg = surface
            if hasattr(child, "_lumveil_bg") and child._lumveil_bg == BG_ADJ:
                child._lumveil_bg = surface
                child.config(bg=surface)
            self._recolor_settings_tree(child, surface)

    def _add_settings_tab_intro(self, parent, title, description):
        """全設定タブで共通の見出しと短い説明を表示する。"""
        head = tk.Frame(parent, bg=BG_APP)
        head.pack(fill=tk.X, padx=16, pady=(12, 7))
        tk.Label(head, text=title, bg=BG_APP, fg=COL_TXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(head, text=description, bg=BG_APP, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))

    def _build_settings_tab_bar(self):
        """Replace ttk's platform-dependent tab chrome with a flat tab strip."""
        labels = [
            (self._quick_tab, "かんたん"),
            (self._picture_tab, "画質"),
            (self._playback_tab, "再生と字幕"),
            (self._advanced_tab, "詳細"),
            (self._about_tab, "アプリ情報"),
        ]
        self._settings_tab_buttons = {}
        self._settings_tab_lines = {}
        for tab, label in labels:
            cell = tk.Frame(self._settings_tab_bar, bg=BG_ADJ)
            cell.pack(side=tk.LEFT, fill=tk.X, expand=True)
            button = tk.Button(
                cell, text=label,
                command=lambda target=tab: self._settings_tabs.select(target),
                bg=BG_ADJ, fg=COL_DIM, activebackground=BG_BTN_H,
                activeforeground=COL_TXT, relief=tk.FLAT, bd=0,
                highlightthickness=0, font=("Segoe UI", 8), cursor="hand2",
                padx=6, pady=7)
            button.pack(fill=tk.X)
            line = tk.Frame(cell, bg=BG_ADJ, height=2)
            line.pack(fill=tk.X)
            self._settings_tab_buttons[tab] = button
            self._settings_tab_lines[tab] = line
        self._settings_tabs.bind(
            "<<NotebookTabChanged>>", self._sync_settings_tab_buttons, add="+")
        self._sync_settings_tab_buttons()

    def _sync_settings_tab_buttons(self, _event=None):
        selected = str(self._settings_tabs.select())
        if (getattr(self, "_advanced_tab", None) is not None and
                selected == str(self._advanced_tab)):
            self._ensure_gpu_settings()
        for tab, button in getattr(self, "_settings_tab_buttons", {}).items():
            active = str(tab) == selected
            button.config(bg=BG_CTRL if active else BG_ADJ,
                          fg=COL_BLU if active else COL_DIM)
            self._settings_tab_lines[tab].config(bg=COL_BLU if active else BG_ADJ)

    def _build_adj_win(self):
        self._settings_win = tk.Toplevel(self.root)
        self._settings_win.title("設定")
        self._settings_win.configure(bg=BG_ADJ)
        self._settings_win.resizable(True, True)
        self._settings_win.minsize(760, 520)
        self._settings_win.geometry("780x560")
        _apply_dark_titlebar(self._settings_win)
        self._settings_win.withdraw()
        self._settings_win.protocol("WM_DELETE_WINDOW", self._settings_win.withdraw)
        self._adj_win = self._settings_win

        settings_head = tk.Frame(self._settings_win, bg=BG_ADJ)
        settings_head.pack(fill=tk.X, padx=22, pady=(16, 8))
        tk.Label(settings_head, text="設定", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        tk.Frame(self._settings_win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=22)
        self._settings_tab_bar = tk.Frame(self._settings_win, bg=BG_ADJ)
        self._settings_tab_bar.pack(fill=tk.X, padx=16, pady=(4, 0))
        self._settings_tabs = ttk.Notebook(self._settings_win, style="Lumveil.TNotebook")
        self._settings_tabs.pack(fill=tk.BOTH, expand=True, padx=16, pady=(8, 8))
        self._settings_status = tk.StringVar(value="")
        tk.Label(self._settings_win, textvariable=self._settings_status,
                 bg=BG_ADJ, fg=COL_YEL, anchor="w",
                 font=("Segoe UI", 8), padx=12, pady=3).pack(fill=tk.X, side=tk.BOTTOM)

        self._quick_tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._settings_tabs.add(self._quick_tab, text="かんたん")
        self._build_quick_settings(self._quick_tab)

        win = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._picture_tab = win
        self._settings_tabs.add(win, text="画質を調整")
        self._add_settings_tab_intro(win, "画質を調整", "映像の見た目を細かく調整したいときに使います。")
        win = self._make_vertical_scroll_area(win)
        win = self._settings_card(win, pady=(4, 10))
        surface = self._settings_surface(win)

        mode_row = tk.Frame(win, bg=surface)
        mode_row.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(mode_row, text="映像モード:", bg=surface, fg=COL_TXT,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(0, 6))
        for mname in PICTURE_MODES:
            b = self._btn(mode_row, mname, lambda m=mname: self._apply_picture_mode(m),
                         bg=surface, pad=(8, 3))
            b.pack(side=tk.LEFT, padx=2)
            self._mode_btns[mname] = b
        self._set_button_selected(self._mode_btns[self._picture_mode], True, "accent")

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        for key, label, lo, hi, default in ADJ_PARAMS:
            row = tk.Frame(win, bg=surface)
            row.pack(fill=tk.X, padx=12, pady=3)
            tk.Label(row, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                     bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            var = tk.DoubleVar(value=default)
            self._adj_vars[key] = (var, default)
            sc = ttk.Scale(row, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var,
                           length=200, style="Adj.Horizontal.TScale",
                           command=lambda _v, k=key: self._on_adjust(k))
            sc.pack(side=tk.LEFT, padx=6)
            self._fix_scale_click(sc, var, lo, hi)
            disp = tk.StringVar(value=f"{default:+d}")
            tk.Label(row, textvariable=disp, width=5,
                     bg=surface, fg=COL_BLU, font=("Consolas", 9)).pack(side=tk.LEFT)
            var.trace_add("write",
                lambda *_, v=var, d=disp: d.set(f"{int(round(v.get())):+d}"))
            self._btn(row, "↺", lambda k=key: self._reset_adj(k),
                      bg=surface, pad=(5, 3)).pack(side=tk.LEFT, padx=4)

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(6, 2))

        tr = tk.Frame(win, bg=surface)
        tr.pack(fill=tk.X, padx=12, pady=3)
        tk.Label(tr, text="暗闇補正の閾値:", width=SETTING_LABEL_W, anchor="w",
                 bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._thresh_var = tk.DoubleVar(value=self._dark_thresh)
        ts = ttk.Scale(tr, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                       variable=self._thresh_var, length=200,
                       style="Adj.Horizontal.TScale",
                       command=lambda _: setattr(self, "_dark_thresh",
                                                 round(self._thresh_var.get(), 2)))
        ts.pack(side=tk.LEFT, padx=6)
        self._fix_scale_click(ts, self._thresh_var, 0.0, 1.0)
        td = tk.StringVar(value=f"{self._dark_thresh:.2f}")
        tk.Label(tr, textvariable=td, width=5,
                 bg=surface, fg=COL_YEL, font=("Consolas", 9)).pack(side=tk.LEFT)
        self._thresh_var.trace_add("write",
            lambda *_: td.set(f"{self._thresh_var.get():.2f}"))
        tk.Label(tr, text="← 鈍感   敏感 →",
                 bg=surface, fg=COL_DIM, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=6)

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        br = tk.Frame(win, bg=surface, pady=5)
        br.pack()
        reset_all_btn = self._btn(br, "↺ すべてリセット", self._reset_all_adj,
                                  fg=COL_RED, bg=BG_DANGER, pad=(10, 5))
        self._set_button_visual(reset_all_btn, BG_DANGER, COL_RED, BG_PRESSED)
        reset_all_btn.pack(side=tk.LEFT, padx=5)

        # シャドウリフト手動スライダー（AUTO停止中のみ操作可、AUTO稼働中はAUTOが制御）
        sl_row = tk.Frame(win, bg=surface)
        sl_row.pack(fill=tk.X, padx=12, pady=(0, 5))
        tk.Label(sl_row, text="シャドウリフト:", width=SETTING_LABEL_W, anchor="w",
                 bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._shadow_lift_scale = ttk.Scale(
            sl_row, from_=0, to=100, orient=tk.HORIZONTAL,
            variable=self._manual_shadow_lift, length=200,
            style="Adj.Horizontal.TScale",
            command=lambda _v: self._on_manual_shadow_lift())
        self._shadow_lift_scale.pack(side=tk.LEFT, padx=6)
        self._fix_scale_click(self._shadow_lift_scale, self._manual_shadow_lift, 0, 100)
        sl_disp = tk.StringVar(value=f"{int(round(self._manual_shadow_lift.get()))}%")
        tk.Label(sl_row, textvariable=sl_disp, width=5,
                 bg=surface, fg=COL_BLU, font=("Consolas", 9)).pack(side=tk.LEFT)
        self._manual_shadow_lift.trace_add("write",
            lambda *_, v=self._manual_shadow_lift, d=sl_disp: d.set(f"{int(round(v.get()))}%"))
        if self._rt_enabled:
            self._shadow_lift_scale.config(state=tk.DISABLED)

        br2 = tk.Frame(win, bg=surface, pady=4)
        br2.pack()
        self._btn(br2, "設定を保存", self._save_adj,
                  bg=surface, pad=(10, 5)).pack(side=tk.LEFT, padx=5)
        self._btn(br2, "設定を読み込む", self._load_adj,
                  bg=surface, pad=(10, 5)).pack(side=tk.LEFT, padx=5)

        # GPU/シェーダー設定は起動直後には表示されないため、空のタブだけを
        # 先に用意し、実際の大量のウィジェットは初回表示時に遅延構築する。
        self._advanced_tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._settings_tabs.add(self._advanced_tab, text="詳細設定")
        self._build_playback_settings_tab()
        self._build_about_settings_tab()
        self._build_settings_tab_bar()

    def _build_quick_settings(self, win):
        """初見の利用者が迷わず使える、日常的な設定だけを集約する。"""
        self._add_settings_tab_intro(win, "かんたん設定", "用途に合わせて選ぶだけで、よく使う設定をまとめて切り替えられます。")
        win = self._make_vertical_scroll_area(win)
        win = self._settings_card(win)
        surface = self._settings_surface(win)
        self._settings_section_heading(
            win, "用途別プリセット", "画質と負荷の組み合わせをまとめて切り替えます。")
        quality_row = tk.Frame(win, bg=surface)
        quality_row.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(quality_row, text="プリセット:", bg=surface, fg=COL_TXT,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(0, 6))
        self._quality_preset_btns = {}
        quality_tips = {
            "軽快": "負荷を抑えたいとき。Anime4Kと通常フレーム補間を停止します。",
            "標準": "普段使い向け。画質と軽快さのバランスを取ります。",
            "アニメ高画質": "Anime4Kとデバンディングでアニメをきれいに表示します。",
            "暗所優先": "AUTOを使う準備を整えます。動画を開くまでAUTOは開始しません。",
        }
        for pname in QUALITY_PRESETS:
            b = self._btn(quality_row, pname, lambda p=pname: self._apply_quality_preset(p),
                          bg=surface, pad=(7, 3), tooltip=quality_tips[pname])
            b.pack(side=tk.LEFT, padx=2)
            self._quality_preset_btns[pname] = b
        self._quality_preset_status = tk.StringVar()
        tk.Label(win, textvariable=self._quality_preset_status, bg=surface, fg=COL_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(0, 8))
        self._sync_quality_preset_buttons()

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(2, 0))
        self._settings_section_heading(
            win, "暗闇補正（AUTO）", "映像を解析して暗い場面の見やすさを自動調整します。")
        row = tk.Frame(win, bg=surface)
        row.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(row, text="暗闇補正:", bg=surface, fg=COL_TXT,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(0, 6))
        self._rt_btn = self._btn(row, "リアルタイム自動調整: OFF", self._toggle_rt_adj,
                                 bg=surface, pad=(10, 5))
        self._rt_btn.pack(side=tk.LEFT)
        modes = tk.Frame(win, bg=surface)
        modes.pack(fill=tk.X, padx=12, pady=(2, 6))
        tk.Label(modes, text="強さ:", bg=surface, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6))
        for mname in ["OFF"] + list(RT_MODES.keys()):
            b = self._btn(modes, mname, lambda m=mname: self._select_rt_mode(m),
                          bg=surface, pad=(8, 3))
            b.pack(side=tk.LEFT, padx=2)
            self._rt_mode_btns[mname] = b
        self._sync_rt_mode_buttons()
        self._auto_adj_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._auto_adj_status, bg=surface, fg=COL_GRN,
                 font=("Segoe UI", 8), pady=5).pack(anchor="w", padx=12)

    def _toggle_adj_win(self):
        self._close_quality_quick_panel()
        self._settings_tabs.select(self._quick_tab)
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
        surface = self._settings_surface(win)

        def _sync_row(label, lo, hi, default, unit, mpv_prop, fmt="{:+.1f}"):
            row = tk.Frame(win, bg=surface)
            row.pack(fill=tk.X, padx=12, pady=4)
            tk.Label(row, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                     bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            initial = self._sync_preferences.get(mpv_prop, default)
            var = tk.DoubleVar(value=initial)

            def _apply(_=None, var=var, prop=mpv_prop):
                try:
                    self.player[prop] = var.get()
                except Exception:
                    pass

            sc = ttk.Scale(row, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var,
                           length=200, style="Adj.Horizontal.TScale", command=_apply)
            sc.pack(side=tk.LEFT, padx=6)
            self._fix_scale_click(sc, var, lo, hi)
            disp = tk.StringVar(value=fmt.format(initial))
            tk.Label(row, textvariable=disp, width=6,
                     bg=surface, fg=COL_BLU, font=("Consolas", 9)).pack(side=tk.LEFT)
            var.trace_add("write", lambda *_, v=var, d=disp: d.set(fmt.format(v.get())))
            tk.Label(row, text=unit, bg=surface, fg=COL_DIM,
                     font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=4)

            def _reset(var=var, default=default):
                var.set(default)
                _apply()
            self._btn(row, "↺", _reset, bg=surface, pad=(5, 3)).pack(side=tk.LEFT, padx=4)
            _apply()
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

    def _add_advanced_section(self, parent, title, description):
        """詳細設定の項目を必要な時だけ開く折りたたみセクションとして作る。"""
        outer = tk.Frame(parent, bg=BG_CTRL, highlightbackground=BG_BORDER,
                         highlightthickness=1)
        outer.pack(fill=tk.X, padx=12, pady=(4, 2))
        body = tk.Frame(outer, bg=BG_CTRL)
        visible = tk.BooleanVar(value=False)

        def toggle():
            if visible.get():
                body.pack_forget()
                visible.set(False)
                button.config(text=f"▶ {title}")
            else:
                body.pack(fill=tk.X, pady=(2, 6))
                visible.set(True)
                button.config(text=f"▼ {title}")

        button = self._btn(outer, f"▶ {title}", toggle, bg=BG_CTRL,
                           font=("Segoe UI", 10, "bold"), pad=(10, 6))
        button.config(anchor="w")
        button.pack(fill=tk.X)
        tk.Label(outer, text=description, bg=BG_CTRL, fg=COL_DIM,
                 font=("Segoe UI", 8), anchor="w").pack(fill=tk.X, padx=10, pady=(0, 5))
        return body

    def _make_vertical_scroll_area(self, parent):
        """タブの見出しを固定したまま、長い設定内容だけを縦スクロールさせる。"""
        holder = tk.Frame(parent, bg=BG_ADJ)
        holder.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(holder, bg=BG_ADJ, highlightthickness=0)
        scrollbar = tk.Scrollbar(holder, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        content = tk.Frame(canvas, bg=BG_ADJ)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _update_scrollregion():
            # 内容が表示領域より短い時までCanvasを動かせると、上部に空白が生じる。
            # 領域を少なくとも表示サイズまでに固定し、常に先頭位置へ戻す。
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(),
                                           max(content.winfo_reqheight(), canvas.winfo_height())))
            if content.winfo_reqheight() <= canvas.winfo_height():
                canvas.yview_moveto(0)

        content.bind("<Configure>", lambda _e: _update_scrollregion())
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(window_id, width=e.width),
                                                _update_scrollregion()))

        def _contains(widget):
            """ホイール発生元がこのスクロール領域の子か判定する。"""
            while widget:
                if widget is content or widget is canvas:
                    return True
                widget = getattr(widget, "master", None)
            return False

        def _on_wheel(event):
            if not _contains(event.widget):
                return None
            if content.winfo_reqheight() <= canvas.winfo_height():
                canvas.yview_moveto(0)
                return "break"
            delta = -1 if event.delta > 0 else 1
            canvas.yview_scroll(delta * 3, "units")
            return "break"

        # 共通ホイール処理（_on_mousewheel）から呼び出す。後から動画側の
        # bind_allが登録されても設定タブ側の処理が上書きされないようにする。
        self._scroll_wheel_handlers.append(_on_wheel)
        def remove_handler(event):
            if event.widget is holder and _on_wheel in self._scroll_wheel_handlers:
                self._scroll_wheel_handlers.remove(_on_wheel)
        holder.bind("<Destroy>", remove_handler, add="+")
        return content

    def _ensure_gpu_settings(self):
        if not self._gpu_settings_built:
            self._build_gpu_win()

    def _build_gpu_win(self):
        if self._gpu_settings_built:
            return
        # 高度な項目は1タブに集め、必要な項目だけ開ける折りたたみ式にする。
        self._add_settings_tab_intro(self._advanced_tab, "詳細設定", "GPU・シェーダーなど、画質を細かく調整したいときに使います。")
        self._advanced_content = self._make_vertical_scroll_area(self._advanced_tab)
        self._smooth_tab = self._add_advanced_section(
            self._advanced_content, "なめらかさ", "フレーム補間・ノイズ軽減")
        self._decode_tab = self._add_advanced_section(
            self._advanced_content, "GPU再生支援", "GPUデコードの設定")
        self._shader_tab = self._add_advanced_section(
            self._advanced_content, "シェーダー", "拡大・Anime4K・外部GLSL")
        self._output_tab = self._add_advanced_section(
            self._advanced_content, "映像出力", "HDR変換・ディザリング・デインターレース")
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
            surface = self._settings_surface(win)
            r = tk.Frame(win, bg=surface)
            r.pack(fill=tk.X, padx=16, pady=5)
            lbl = tk.Label(r, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                           bg=surface, fg=COL_TXT, font=("Segoe UI", 10))
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
        self._style_toggle_button(self._deband_btn, self._gpu_deband)

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
        self._style_toggle_button(self._sigmoid_btn, self._gpu_sigmoid)

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
        self._style_toggle_button(self._correct_ds_btn, self._gpu_correct_ds)

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
        self._style_toggle_button(self._interpolate_btn, self._gpu_interpolate)

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
        self._style_toggle_button(self._amf_frc_btn, self._gpu_amf_frc)
        r = _row("ノイズ軽減")
        self._denoise_btn = tk.Button(
            r, text="ノイズ軽減: OFF", command=self._toggle_denoise,
            bg=BG_ADJ, fg=COL_TXT, relief=tk.FLAT, bd=0,
            font=("Segoe UI", 10), width=16, padx=6, pady=3, cursor="hand2",
            activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._denoise_btn.pack(side=tk.LEFT)
        self._style_toggle_button(self._denoise_btn, self._denoise)

        # シェーダー
        win = self._shader_tab
        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(6, 2))

        # Anime4Kプリセット
        ar = _row("Anime4Kプリセット")
        for pname in ANIME4K_PRESETS:
            b = self._btn(ar, pname, lambda p=pname: self._apply_anime4k_preset(p),
                         bg=BG_ADJ, pad=(6, 3), font=("Segoe UI", 8),
                         tooltip=ANIME4K_DESCRIPTIONS[pname])
            b.pack(side=tk.LEFT, padx=2)
            self._a4k_btns[pname] = b
        self._set_button_selected(self._a4k_btns["なし"], True, "accent")

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

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
            bg=BG_CTRL, fg=COL_BLU, selectbackground=BG_SELECTED,
            font=("Consolas", 8), relief=tk.FLAT, bd=0,
            activestyle="none")
        sb.config(command=self._glsl_listbox.yview)
        self._glsl_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        for p in self._gpu_glsl:
            self._glsl_listbox.insert(tk.END, os.path.basename(p))

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(2, 2))

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

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

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
        self._style_toggle_button(self._deinterlace_btn, self._gpu_deinterlace)

        tk.Frame(win, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(4, 2))

        br = tk.Frame(win, bg=BG_ADJ, pady=8)
        br.pack()
        reset_gpu_btn = self._btn(br, "↺ リセット", self._reset_gpu,
                                  fg=COL_RED, bg=BG_DANGER, pad=(10, 5))
        self._set_button_visual(reset_gpu_btn, BG_DANGER, COL_RED, BG_PRESSED)
        reset_gpu_btn.pack(side=tk.LEFT, padx=5)

        self._gpu_status = tk.StringVar(value="")
        tk.Label(win, textvariable=self._gpu_status,
                 bg=BG_ADJ, fg=COL_GRN,
                 font=("Segoe UI", 8), pady=6).pack()
        self._build_toolbar_settings_tab()
        for section in (self._smooth_tab, self._decode_tab,
                        self._shader_tab, self._output_tab):
            self._recolor_settings_tree(section, BG_CTRL)
        self._gpu_settings_built = True

    def _build_playback_settings_tab(self):
        tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._playback_tab = tab
        self._settings_tabs.add(tab, text="再生と字幕")
        self._add_settings_tab_intro(tab, "再生と字幕", "連続再生・再開位置・字幕と音声の同期を設定します。")
        tab = self._make_vertical_scroll_area(tab)
        tab = self._settings_card(tab)
        surface = self._settings_surface(tab)
        self._settings_section_heading(tab, "再生動作")

        def option_row(label, variable, options, command):
            row = tk.Frame(tab, bg=surface)
            row.pack(fill=tk.X, padx=12, pady=5)
            tk.Label(row, text=f"{label}:", width=SETTING_LABEL_W, anchor="w",
                     bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            menu = tk.OptionMenu(row, variable, *options, command=command)
            menu.config(bg=surface, fg=COL_TXT, activebackground=BG_BTN_H,
                        activeforeground=COL_TXT, highlightthickness=0,
                        relief=tk.FLAT, font=("Segoe UI", 10), width=22)
            menu["menu"].config(bg=BG_ADJ, fg=COL_TXT,
                                activebackground=BG_BTN_H, activeforeground=COL_TXT)
            menu.pack(side=tk.LEFT)

        self._eof_var = tk.StringVar(value=next(
            label for label, action in EOF_ACTION_OPTIONS.items()
            if action == self._playback_eof_action))
        option_row("再生終了時", self._eof_var, tuple(EOF_ACTION_OPTIONS), self._on_eof_setting)

        row = tk.Frame(tab, bg=surface)
        row.pack(fill=tk.X, padx=12, pady=5)
        tk.Label(row, text="前回位置から再開:", width=SETTING_LABEL_W, anchor="w",
                 bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._resume_btn = tk.Button(row, text="ON" if self._resume_enabled else "OFF",
                                     command=self._toggle_resume_enabled,
                                     bg=BG_ADJ, fg=COL_GRN if self._resume_enabled else COL_TXT,
                                     relief=tk.FLAT, bd=0, font=("Segoe UI", 10), width=8,
                                     padx=6, pady=3, cursor="hand2",
                                     activebackground=BG_BTN_H, activeforeground=COL_TXT)
        self._resume_btn.pack(side=tk.LEFT)
        self._style_toggle_button(self._resume_btn, self._resume_enabled)
        row = tk.Frame(tab, bg=surface)
        row.pack(fill=tk.X, padx=12, pady=5)
        tk.Label(row, text="起動時に手動画質を復元:", width=SETTING_LABEL_W, anchor="w",
                 bg=surface, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._restore_adj_btn = self._btn(row, "ON" if self._restore_manual_settings else "OFF",
                                         self._toggle_restore_manual_settings, bg=surface)
        self._restore_adj_btn.pack(side=tk.LEFT)
        self._style_toggle_button(self._restore_adj_btn, self._restore_manual_settings)

        self._end_var = tk.StringVar(value="先頭へ戻る" if self._folder_end_action == "loop" else "停止")
        option_row("フォルダ末尾", self._end_var, ("停止", "先頭へ戻る"), self._on_folder_end_setting)

        self._sort_var = tk.StringVar(value="更新日時順" if self._playlist_sort == "modified" else "ファイル名順")
        option_row("次の動画の並び", self._sort_var, ("ファイル名順", "更新日時順"), self._on_playlist_sort_setting)
        tk.Frame(tab, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=12, pady=(8, 0))
        self._settings_section_heading(
            tab, "字幕と音声の同期", "映像に合わせて字幕・音声のタイミングと字幕サイズを調整します。")
        self._build_sync_controls(tab)

    def _build_about_settings_tab(self):
        tab = tk.Frame(self._settings_tabs, bg=BG_ADJ)
        self._about_tab = tab
        self._settings_tabs.add(tab, text="アプリ情報")
        self._add_settings_tab_intro(
            tab, "Lumveilについて", "バージョン・更新・ライセンスを確認できます。")
        tab = self._make_vertical_scroll_area(tab)
        card = self._settings_card(tab)
        surface = self._settings_surface(card)
        app_row = tk.Frame(card, bg=surface)
        app_row.pack(fill=tk.X, padx=16, pady=(14, 10))
        tk.Label(app_row, text="LUMVEIL", bg=surface, fg=COL_BLU,
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(app_row, text=f"バージョン {APP_VERSION}", bg=surface, fg=COL_DIM,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 0))
        tk.Label(app_row, text="制作: ふぁん", bg=surface, fg=COL_TXT,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(8, 0))
        links = tk.Frame(card, bg=surface)
        links.pack(fill=tk.X, padx=16, pady=(0, 14))
        self._btn(links, "GitHubを開く", lambda: webbrowser.open(GITHUB_URL),
                  bg=surface, pad=(12, 5)).pack(side=tk.LEFT)
        self._btn(links, "第三者ライセンス", self._open_license_notices,
                  bg=surface, pad=(12, 5)).pack(side=tk.LEFT, padx=(8, 0))

        update = self._settings_card(tab)
        update_surface = self._settings_surface(update)
        self._settings_section_heading(
            update, "更新", "再生を続けながらGitHubの更新を確認できます。")
        toggle_row = tk.Frame(update, bg=update_surface)
        toggle_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(toggle_row, text="自動で更新を確認:", bg=update_surface,
                 fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._auto_update_button = self._btn(
            toggle_row, "ON" if self._auto_update_checks else "OFF",
            self._toggle_auto_update_checks, bg=update_surface, pad=(12, 4))
        self._auto_update_button.pack(side=tk.LEFT, padx=(10, 0))
        self._style_toggle_button(self._auto_update_button, self._auto_update_checks)

        actions = tk.Frame(update, bg=update_surface)
        actions.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._btn(actions, "更新を確認", lambda: self._check_for_updates(manual=True),
                  bg=update_surface, pad=(12, 5)).pack(side=tk.LEFT)
        self._update_download_button = self._btn(
            actions, "ダウンロードしてインストール", self._download_update,
            bg=BG_SELECTED, fg=COL_BLU, pad=(12, 5))
        self._update_download_button.pack(side=tk.LEFT, padx=(8, 0))
        self._update_download_button.config(state=tk.DISABLED)
        self._update_status_var = tk.StringVar(value="まだ確認していません。")
        tk.Label(update, textvariable=self._update_status_var, bg=update_surface,
                 fg=COL_DIM, justify=tk.LEFT, anchor="w", wraplength=590,
                 font=("Segoe UI", 9)).pack(fill=tk.X, padx=12, pady=(0, 14))

    def _on_eof_setting(self, value):
        self._playback_eof_action = EOF_ACTION_OPTIONS.get(value, "next")
        self._apply_playback_eof_action()
        if self._playback_eof_action == "repeat" and self._current_path:
            try:
                if self.player.eof_reached:
                    self.player.seek(0, reference="absolute", precision="exact")
                    self.player.pause = False
            except Exception as e:
                self._set_settings_error("リピートの開始", e)
        self._save_player_settings()

    def _apply_playback_eof_action(self):
        """Let mpv loop in place, preserving AUTO, resume and playlist state."""
        try:
            self.player["loop-file"] = "inf" if self._playback_eof_action == "repeat" else "no"
        except Exception as e:
            self._set_settings_error("再生終了時の設定", e)

    def _toggle_resume_enabled(self):
        self._resume_enabled = not self._resume_enabled
        self._resume_btn.config(text="ON" if self._resume_enabled else "OFF")
        self._style_toggle_button(self._resume_btn, self._resume_enabled)
        if not self._resume_enabled:
            self._resume_positions.clear()
        self._save_player_settings()

    def _on_folder_end_setting(self, value):
        self._folder_end_action = "loop" if value == "先頭へ戻る" else "stop"
        self._save_player_settings()

    def _toggle_restore_manual_settings(self):
        self._restore_manual_settings = not self._restore_manual_settings
        self._restore_adj_btn.config(text="ON" if self._restore_manual_settings else "OFF")
        self._style_toggle_button(self._restore_adj_btn, self._restore_manual_settings)
        self._save_player_settings()

    def _on_playlist_sort_setting(self, value):
        self._playlist_sort = "modified" if value == "更新日時順" else "name"
        if self._current_path and getattr(self, "_playlist_source", "folder") == "folder":
            self._build_folder_playlist(self._current_path)
        self._save_player_settings()

    def _build_toolbar_settings_tab(self):
        """常時表示の切替と並び順を、実物に近いプレビューで設定する。"""
        if hasattr(self, "_toolbar_section"):
            self._toolbar_section.destroy()
        self._toolbar_section = tk.Frame(self._advanced_content, bg=BG_ADJ)
        self._toolbar_section.pack(fill=tk.X, pady=(3, 0))
        tab = self._add_advanced_section(
            self._toolbar_section, "操作バー", "表示するボタンと並び順")
        self._toolbar_tab = tab
        self._toolbar_icons = {
            "fullscreen": self._icons["fullscreen"], "pin": self._icons["pin"],
            "about": self._icons["info"], "auto_adjust": "AUTO", "quality": "画質", "gpu": "GPU",
            "recent": self._icons["recent"], "playlist": self._icons["playlist"],
            "audio": self._icons["audio"], "subtitles": self._icons["subtitles"],
            "ab_repeat": "A-B", "screenshot": self._icons["screenshot"],
            "bookmark": self._icons["bookmark"], "speed": "1.00×",
        }
        tk.Label(tab, text="操作バー", bg=BG_ADJ, fg=COL_TXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        tk.Label(tab, text="チェックで常時表示を切り替え、⠿ をドラッグして並び順を変えます。",
                 bg=BG_ADJ, fg=COL_DIM, font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        tk.Label(tab, text="プレビュー", bg=BG_ADJ, fg=COL_BLU,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 3))
        self._toolbar_preview = tk.Frame(tab, bg=BG_APP, bd=1, relief=tk.SOLID)
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
            icon_font = (self._icon_font if key in {
                "fullscreen", "pin", "about", "recent", "playlist", "audio",
                "subtitles", "screenshot", "bookmark"
            } else ("Segoe UI", 10))
            tk.Label(row, text=self._toolbar_icons[key], width=7, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=icon_font).pack(side=tk.LEFT)
            tk.Label(row, text=definitions[key][0], width=24, anchor="w",
                     bg=BG_ADJ, fg=COL_TXT, font=("Segoe UI", 10)).pack(side=tk.LEFT)
            var = tk.BooleanVar(value=key in self._toolbar_visible)
            tk.Checkbutton(row, text="常時表示", variable=var,
                           command=lambda k=key, v=var: self._set_toolbar_item_visible(k, v),
                           bg=BG_ADJ, fg=COL_TXT, selectcolor=BG_BTN,
                           activebackground=BG_ADJ, activeforeground=COL_TXT,
                           font=("Segoe UI", 10), bd=0, highlightthickness=0).pack(side=tk.RIGHT)

        tk.Frame(tab, bg=BG_BORDER, height=1).pack(fill=tk.X, padx=16, pady=(2, 8))
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
        self._recolor_settings_tree(tab, BG_CTRL)

    def _toggle_gpu_win(self):
        self._close_quality_quick_panel()
        self._ensure_gpu_settings()
        self._settings_tabs.select(self._advanced_tab)
        if not self._settings_win.winfo_viewable():
            x = self.root.winfo_rootx() + 20
            y = self.root.winfo_rooty() + 40
            self._settings_win.geometry(f"+{x}+{y}")
            self._settings_win.deiconify()
        self._settings_win.lift()

    def _sync_quality_preset_buttons(self):
        """用途別プリセットの選択状態を画質タブへ反映する。"""
        if not hasattr(self, "_quality_preset_btns"):
            return
        for name, btn in self._quality_preset_btns.items():
            self._set_button_selected(btn, name == self._quality_preset, "accent")
        if hasattr(self, "_quality_preset_status"):
            text = (f"選択中: {self._quality_preset}"
                    if self._quality_preset in QUALITY_PRESETS
                    else "選択中: カスタム（詳細設定を手動変更）")
            self._quality_preset_status.set(text)

    def _mark_quality_custom(self):
        """用途別プリセットの管理対象を手動変更したことを表示する。"""
        if not self._applying_quality_preset and self._quality_preset != "カスタム":
            self._quality_preset = "カスタム"
            self._sync_quality_preset_buttons()

    def _apply_quality_preset(self, name):
        """用途別プリセットをまとめて適用する。外部GLSLは保持する。"""
        preset = QUALITY_PRESETS.get(name)
        if preset is None:
            return
        self._applying_quality_preset = True
        try:
            self._gpu_scale = preset["scale"]
            self._gpu_cscale = preset["cscale"]
            self._gpu_deband = preset["deband"]
            self._gpu_interpolate = preset["interpolate"]
            # AMF補間と通常補間の二重負荷を避ける。用途別プリセットではAMFを使わない。
            self._gpu_amf_frc = False
            self._apply_anime4k_preset(preset["anime"], save=False, show_status=False)
            if preset["rt_mode"]:
                self._rt_mode = preset["rt_mode"]
                self._sync_rt_mode_buttons()
            self._apply_gpu_settings()
            self._apply_vf_chain()

            # 詳細設定UIがまだ構築されていない場合も、内部設定と再生中の
            # mpvだけを更新する。詳細設定を初めて開いた時に現在値で生成される。
            if self._gpu_settings_built:
                self._gpu_scale_var.set(next(label for value, label in self._SCALE_OPTIONS
                                             if value == self._gpu_scale))
                self._gpu_cscale_var.set(next(label for value, label in self._CSCALE_OPTIONS
                                              if value == self._gpu_cscale))
                self._deband_btn.config(text="ON" if self._gpu_deband else "OFF",
                                        fg=COL_GRN if self._gpu_deband else COL_TXT)
                self._interpolate_btn.config(text="ON" if self._gpu_interpolate else "OFF",
                                             fg=COL_GRN if self._gpu_interpolate else COL_TXT)
                self._amf_frc_btn.config(text="OFF", fg=COL_TXT)
                self._style_toggle_button(self._deband_btn, self._gpu_deband)
                self._style_toggle_button(self._interpolate_btn, self._gpu_interpolate)
                self._style_toggle_button(self._amf_frc_btn, False)
            self._quality_preset = name
            self._sync_quality_preset_buttons()
            self._save_gpu_settings()
            suffix = "（AUTOは動画を開いてから開始できます）" if name == "暗所優先" else ""
            if hasattr(self, "_gpu_status"):
                self._gpu_status.set(f"✓ 用途別プリセット: {name}{suffix}")
        finally:
            self._applying_quality_preset = False

    def _on_gpu_scale(self, lbl):
        val = next(v for v, l in self._SCALE_OPTIONS if l == lbl)
        self._gpu_scale = val
        self._mark_quality_custom()
        try:
            self.player["scale"] = val
            self._gpu_status.set(f"✓ スケール: {val}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_cscale(self, lbl):
        val = next(v for v, l in self._CSCALE_OPTIONS if l == lbl)
        self._gpu_cscale = val
        self._mark_quality_custom()
        try:
            self.player["cscale"] = val
            self._gpu_status.set(f"✓ クロマスケール: {val}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _on_gpu_deband(self):
        self._gpu_deband = not self._gpu_deband
        self._mark_quality_custom()
        self._deband_btn.config(
            text="ON" if self._gpu_deband else "OFF",
            fg=COL_GRN if self._gpu_deband else COL_TXT)
        self._style_toggle_button(self._deband_btn, self._gpu_deband)
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
        self._schedule_gpu_settings_save()

    def _on_gpu_sigmoid(self):
        self._gpu_sigmoid = not self._gpu_sigmoid
        on = self._gpu_sigmoid
        self._sigmoid_btn.config(text="ON" if on else "OFF",
                                 fg=COL_GRN if on else COL_TXT)
        self._style_toggle_button(self._sigmoid_btn, on)
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
        self._style_toggle_button(self._correct_ds_btn, on)
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
        self._mark_quality_custom()
        on = self._gpu_interpolate
        if on and self._gpu_amf_frc:
            # フレーム補間とAMD AMFフレーム補間は目的が重複し二重負荷になるため排他化
            self._gpu_amf_frc = False
            self._amf_frc_btn.config(text="OFF", fg=COL_TXT)
            self._style_toggle_button(self._amf_frc_btn, False)
            self._apply_vf_chain()
        self._interpolate_btn.config(text="ON" if on else "OFF",
                                     fg=COL_GRN if on else COL_TXT)
        self._style_toggle_button(self._interpolate_btn, on)
        self._set_interpolate_mpv(on)
        self._gpu_status.set(f"✓ フレーム補間: {'ON' if on else 'OFF'}")
        self._save_gpu_settings()

    def _on_gpu_amf_frc(self):
        self._gpu_amf_frc = not self._gpu_amf_frc
        on = self._gpu_amf_frc
        if on and self._denoise:
            # hqdn3d(ソフトウェア)とamf_frc(GPUサーフェス直結)は同時使用不可のため排他化
            self._denoise = False
            self._denoise_btn.config(text="ノイズ軽減: OFF", fg=COL_TXT)
            self._style_toggle_button(self._denoise_btn, False)
        if on and self._gpu_interpolate:
            # フレーム補間とAMD AMFフレーム補間は目的が重複し二重負荷になるため排他化
            self._gpu_interpolate = False
            self._interpolate_btn.config(text="OFF", fg=COL_TXT)
            self._style_toggle_button(self._interpolate_btn, False)
            self._set_interpolate_mpv(False)
        self._amf_frc_btn.config(text="ON" if on else "OFF",
                                 fg=COL_GRN if on else COL_TXT)
        self._style_toggle_button(self._amf_frc_btn, on)
        self._apply_vf_chain()
        if on and self.fps > AMF_FRC_MAX_FPS:
            self._gpu_status.set(
                f"⚠ AMD AMFフレーム補間: ON（高フレームレート素材のため自動スキップ中）")
        else:
            self._gpu_status.set(
                f"✓ AMD AMFフレーム補間: {'ON' if on else 'OFF'}"
                + ("（非AMD環境では自動的に無効のままです）" if on else ""))
        self._save_gpu_settings()

    def _apply_anime4k_preset(self, name, *, save=True, show_status=True):
        files = ANIME4K_PRESETS.get(name)
        if files is None:
            return
        # 既存のAnime4K_*シェーダーだけを外し、ユーザーが手動追加した
        # 他のシェーダー（自作のもの等）はそのまま残す
        self._gpu_glsl = [p for p in self._gpu_glsl
                          if not os.path.basename(p).startswith("Anime4K_")]
        for fname in files:
            self._gpu_glsl.append(os.path.join(_SHADER_DIR, fname))
        if hasattr(self, "_glsl_listbox"):
            self._glsl_listbox.delete(0, tk.END)
            for p in self._gpu_glsl:
                self._glsl_listbox.insert(tk.END, os.path.basename(p))
        self._apply_glsl_shaders()
        if save:
            self._mark_quality_custom()
            self._save_gpu_settings()
        if show_status and hasattr(self, "_gpu_status"):
            self._gpu_status.set(f"✓ Anime4Kプリセット適用: {name}")
        for mname, btn in self._a4k_btns.items():
            self._set_button_selected(btn, mname == name, "accent")

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
            self._mark_quality_custom()
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
        self._mark_quality_custom()
        self._apply_glsl_shaders()
        self._save_gpu_settings()
        self._gpu_status.set(f"✓ シェーダー削除 ({len(self._gpu_glsl)}件残)")

    def _on_gpu_glsl_clear(self):
        self._gpu_glsl.clear()
        self._glsl_listbox.delete(0, tk.END)
        self._mark_quality_custom()
        self._apply_glsl_shaders()
        self._save_gpu_settings()
        self._gpu_status.set("✓ シェーダー全クリア")

    def _apply_glsl_shaders(self):
        try:
            paths = self._gpu_glsl + [_RT_CONTRAST_SHADER_PATH, _RT_SHADOW_SHADER_PATH]
            for path in paths:
                if not os.path.isfile(path):
                    raise FileNotFoundError(path)
            self.player.command("change-list", "glsl-shaders", "clr", "")
            for p in self._gpu_glsl:
                self.player.command("change-list", "glsl-shaders", "append", p)
            # 追加コントラスト・シャドウリフトは手動でも使えるため、常時読込する（強度0なら無処理）。
            self.player.command("change-list", "glsl-shaders", "append", _RT_CONTRAST_SHADER_PATH)
            self.player.command("change-list", "glsl-shaders", "append", _RT_SHADOW_SHADER_PATH)
            self._rt_analysis_revision += 1
            self._last_shader_opts = None
            if "contrast" in self._adj_vars:
                self._apply_effective_contrast(self._adj_vars["contrast"][0].get())
            self._apply_shadow_lift(self._shader_opts.get("shadow_lift", 0.0))
        except Exception as e:
            self._set_settings_error("シェーダーの適用", e)

    def _apply_shader_opts(self):
        """glsl-shader-optsは一括上書きのため、複数パラメータを一元管理してまとめて適用する。"""
        try:
            opts = ",".join(f"{k}={v:.3f}" for k, v in self._shader_opts.items())
            if opts == self._last_shader_opts:
                return
            self.player.command("set", "glsl-shader-opts", opts)
            self._last_shader_opts = opts
        except Exception as e:
            self._set_settings_error("シェーダー設定の反映", e)

    def _apply_effective_contrast(self, value, *, flush=True):
        """-100〜+300の実効値を、MPV(+100まで)と拡張シェーダーへ連続して配分する。"""
        value = max(-100.0, min(300.0, float(value)))
        mpv_value = min(100.0, value)
        strength = max(0.0, (value - 100.0) / 100.0)
        try:
            self.player["contrast"] = int(round(mpv_value))
        except Exception:
            return False
        self._shader_opts["auto_contrast"] = strength
        if flush:
            self._apply_shader_opts()
        return True

    def _apply_shadow_lift(self, value, *, flush=True):
        """0.0〜1.0にクランプしてシャドウリフトシェーダーへ適用する。"""
        value = max(0.0, min(1.0, float(value)))
        self._shader_opts["shadow_lift"] = value
        if flush:
            self._apply_shader_opts()

    def _on_manual_shadow_lift(self):
        """画質タブのシャドウリフトスライダー操作時に呼ばれる。AUTO稼働中は無視する
        （スライダー自体もAUTO ON時はdisabledになるが、念のため二重に防ぐ）。"""
        if not self._rt_enabled:
            self._apply_shadow_lift(self._manual_shadow_lift.get() / 100.0)

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
        self._style_toggle_button(self._deinterlace_btn, self._gpu_deinterlace)
        try:
            self.player["deinterlace"] = self._gpu_deinterlace
            self._gpu_status.set(
                f"✓ デインターレース: {'ON' if self._gpu_deinterlace else 'OFF'}")
        except Exception as e:
            self._gpu_status.set(f"⚠ {e}")
        self._save_gpu_settings()

    def _reset_gpu(self):
        self._mark_quality_custom()
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
        for button in (self._deband_btn, self._sigmoid_btn, self._correct_ds_btn,
                       self._interpolate_btn, self._amf_frc_btn,
                       self._deinterlace_btn):
            self._style_toggle_button(button, False)
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
            "quality_preset": self._quality_preset,
        }
        try:
            _atomic_write_json(GPU_SETTINGS, data, indent=2, ensure_ascii=False)
        except Exception as e:
            self._set_settings_error("GPU設定の保存", e)

    def _schedule_gpu_settings_save(self):
        """スライダー連続操作中の小さなJSON書き込みをまとめる。"""
        if self._gpu_save_after_id is not None:
            try:
                self.root.after_cancel(self._gpu_save_after_id)
            except tk.TclError:
                pass
        self._gpu_save_after_id = self.root.after(250, self._flush_gpu_settings_save)

    def _flush_gpu_settings_save(self):
        pending = self._gpu_save_after_id is not None
        self._gpu_save_after_id = None
        if pending:
            self._save_gpu_settings()

    def _load_gpu_settings(self):
        if not os.path.exists(GPU_SETTINGS):
            return
        try:
            with open(GPU_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            for key, attr, choices in (
                ("scale", "_gpu_scale", self._SCALE_OPTIONS),
                ("cscale", "_gpu_cscale", self._CSCALE_OPTIONS),
                ("hwdec", "_gpu_hwdec", self._HWDEC_OPTIONS),
                ("dither", "_gpu_dither", self._DITHER_OPTIONS),
                ("tonemapping", "_gpu_tonemapping", self._TONEMAP_OPTIONS),
            ):
                if data.get(key) in [value for value, _ in choices]:
                    setattr(self, attr, data[key])
            for key in ("deband", "sigmoid", "correct_ds", "interpolate", "deinterlace", "amf_frc"):
                if isinstance(data.get(key), bool):
                    setattr(self, "_gpu_" + key, data[key])
            self._gpu_antiring = _bounded_number(data.get("antiring"), 0, 0, 1)
            paths = data.get("glsl", [])
            self._gpu_glsl = [p for p in paths if isinstance(p, str) and os.path.isfile(p)] if isinstance(paths, list) else []
            if self._gpu_amf_frc:
                self._gpu_interpolate = False
            saved_preset = data.get("quality_preset", "カスタム")
            self._quality_preset = saved_preset if saved_preset in QUALITY_PRESETS else "カスタム"
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
            self._set_interpolate_mpv(self._gpu_interpolate)
        except Exception as e:
            self._set_settings_error("GPU設定の適用", e)
        self._apply_glsl_shaders()
        self._apply_vf_chain()

    # ── スライダークリック修正 ─────────────────────────────────────────────

    @staticmethod
    def _fix_scale_click(scale, var, lo, hi):
        def _jump(e):
            ratio = max(0.0, min(1.0, e.x / max(scale.winfo_width(), 1)))
            # ttk.Scale.set also runs its command; Variable.set only changes
            # the label and leaves the native player at the previous value.
            scale.after(1, lambda: scale.set(lo + ratio * (hi - lo)))
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
            self._close_volume_popup()
            return
        self._open_volume_popup()

    def _open_volume_popup(self, auto_hide=False):
        if self._volume_popup and self._volume_popup.winfo_exists():
            self._volume_popup.focus_force()
            if auto_hide:
                if self._volume_popup_after_id:
                    self.root.after_cancel(self._volume_popup_after_id)
                self._volume_popup_after_id = self.root.after(
                    1000, self._close_volume_popup)
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
                         bg=BG_ADJ, fg=COL_TXT, troughcolor=BG_BORDER,
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
        if auto_hide:
            self._volume_popup_after_id = self.root.after(
                1000, self._close_volume_popup)

    def _close_volume_popup(self):
        if self._volume_popup_after_id:
            try:
                self.root.after_cancel(self._volume_popup_after_id)
            except tk.TclError:
                pass
            self._volume_popup_after_id = None
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
        self._open_volume_popup(auto_hide=True)
        return "break"

    def toggle_mute(self):
        self._muted = not self._muted
        try:
            self.player.mute = self._muted
        except Exception:
            pass
        self._mute_btn.config(text=self._icons["mute"] if self._muted else self._icons["volume"])
        self._set_button_selected(self._mute_btn, self._muted, "warning")

    def _on_volume(self, val):
        v = int(float(val))
        if hasattr(self, "_volume_value_label"):
            self._volume_value_label.set(f"{v}%")
        if self._muted and v > 0:
            self._muted = False
            self._mute_btn.config(text=self._icons["volume"])
            self._set_button_selected(self._mute_btn, False)
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
        self._open_volume_popup(auto_hide=True)

    def _cancel_control_hide(self):
        if self._control_hide_after_id:
            try:
                self.root.after_cancel(self._control_hide_after_id)
            except tk.TclError:
                pass
            self._control_hide_after_id = None

    def _place_control_dock(self):
        if self.root.attributes("-fullscreen"):
            self.ctrl_bar.place(relx=0, rely=1.0, anchor="sw",
                                relwidth=1.0, y=0)
        else:
            self.ctrl_bar.place_forget()
            self.ctrl_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.ctrl_bar.lift()

    def _show_main_controls(self, schedule=False):
        if self.root.attributes("-fullscreen"):
            return
        self._cancel_control_hide()
        self._place_control_dock()
        self._controls_visible = True

    def _hide_main_controls(self):
        self._control_hide_after_id = None
        # Normal playback keeps the v1.8-style bottom bar fixed.  Hiding is
        # reserved for the fullscreen hover bar.

    def _on_player_motion(self, _event=None):
        return

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
        # Hide the normal dock so fullscreen starts with a clean video surface.
        self._cancel_control_hide()
        self.ctrl_bar.pack_forget()
        self._fs_bar_visible = False
        self._fs_hide_after_id = None
        self.root.bind("<Motion>", self._on_fullscreen_motion, add="+")

    def _exit_fullscreen_ui(self):
        self.root.unbind("<Motion>")
        if self._fs_hide_after_id:
            self.root.after_cancel(self._fs_hide_after_id)
            self._fs_hide_after_id = None
        self.ctrl_bar.place_forget()
        self._show_main_controls(schedule=True)

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
            self._place_control_dock()
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
        self._rt_analysis_revision += 1
        self._rt_applied_values.pop(key, None)
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
        self._manual_shadow_lift.set(0)
        self._on_manual_shadow_lift()

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
            self._set_button_selected(btn, mname == name, "accent")

    def _load_rt_mode(self):
        """起動時に adj_settings_mpv.json から rt_mode のみ復元する。
        スライダー値には触れず、AUTOも自動開始しない。"""
        if not os.path.exists(ADJ_SETTINGS):
            return
        try:
            with open(ADJ_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            rt_mode = data.get("rt_mode")
            if rt_mode in RT_MODES:
                self._rt_mode = rt_mode
        except Exception:
            pass
        # __init__の早い段階で呼ばれるため、UI(セレクタ)構築前はスキップする。
        if getattr(self, "_rt_mode_btns", None):
            self._sync_rt_mode_buttons()

    def _save_adj(self, quiet=False):
        data = {k: int(round(v.get())) for k, (v, _) in self._adj_vars.items()}
        if self._rt_enabled:
            data.update({k: int(round(v)) for k, v in self._rt_base_adj.items()})
        data["rt_mode"] = self._rt_mode
        data["shadow_lift"] = int(round(self._manual_shadow_lift.get()))
        data["dark_thresh"] = self._dark_thresh
        try:
            _atomic_write_json(ADJ_SETTINGS, data, indent=2, ensure_ascii=False)
            if not quiet:
                self._settings_status.set("手動画質を保存しました。")
        except Exception as e:
            self._set_settings_error("手動画質の保存", e)

    def _load_adj(self, quiet=False):
        if not os.path.exists(ADJ_SETTINGS):
            if not quiet:
                self._settings_status.set("保存された手動画質がありません。")
            return
        try:
            with open(ADJ_SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("設定ファイルの形式が正しくありません。")
            if self._rt_enabled:
                self._toggle_rt_adj()
            for k, _label, lo, hi, default in ADJ_PARAMS:
                if k in data:
                    self._adj_vars[k][0].set(round(_bounded_number(data[k], default, lo, hi)))
                    self._on_adjust(k)
            rt_mode = data.get("rt_mode")
            if rt_mode in RT_MODES:
                self._rt_mode = rt_mode
            else:
                self._rt_mode = "標準"
            self._sync_rt_mode_buttons()
            if "shadow_lift" in data:
                self._manual_shadow_lift.set(round(_bounded_number(data["shadow_lift"], 0, 0, 100)))
                if not self._rt_enabled:
                    self._on_manual_shadow_lift()
            # 旧形式の「追加コントラスト」は実効コントラストへ移行する。
            if "extra_contrast" in data:
                base = _bounded_number(data.get("contrast"), 0, -100, 100)
                extra = _bounded_number(data["extra_contrast"], 0, 0, 200)
                self._adj_vars["contrast"][0].set(min(300.0, base + extra))
                self._on_adjust("contrast")
            self._dark_thresh = _bounded_number(data.get("dark_thresh"), 1, 0, 1)
            self._thresh_var.set(self._dark_thresh)
            if not quiet:
                self._settings_status.set("手動画質を読み込みました。")
        except Exception as e:
            self._set_settings_error("手動画質の読込", e)

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
        self._after_current_file(50, _set_target)

    def _toggle_denoise(self):
        self._denoise = not self._denoise
        on = self._denoise
        if on and self._gpu_amf_frc:
            self._gpu_amf_frc = False
            self._amf_frc_btn.config(text="OFF", fg=COL_TXT)
            self._style_toggle_button(self._amf_frc_btn, False)
            self._save_gpu_settings()
        self._apply_vf_chain()
        self._denoise_btn.config(
            text=f"ノイズ軽減: {'ON' if on else 'OFF'}")
        self._set_button_selected(self._denoise_btn, on, "success")

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

    def _open_external_paths(self, paths):
        """Open paths from startup or a second-instance handoff."""
        files = []
        seen = set()
        for path in paths or []:
            if not isinstance(path, str):
                continue
            try:
                path = os.path.abspath(path)
                key = os.path.normcase(path)
                if key not in seen and os.path.isfile(path):
                    files.append(path)
                    seen.add(key)
            except (OSError, TypeError):
                continue
        if len(files) > 1:
            self._play_list(files, 0)
        elif files:
            self._open_path(files[0])
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _open_path(self, path, _from_playlist=False):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            self._show_error_popup(f"ファイルが見つかりません:\n{path}")
            return
        self._hide_preview()
        self._update_resume_position()  # 切り替え前のファイルの位置を保存
        path = os.path.abspath(path)
        with self._mpv_event_lock:
            self._media_generation += 1
            self._current_path = path
            self._mpv_pending_eof = None
        self._cached_duration_ms = 0.0
        self._cached_time_ms = 0.0
        self._rt_baseline = None
        self._rt_targets = dict(self._rt_base_adj, shadow_lift=0.0)
        self._rt_applied_values.clear()
        self._manual_status_stats = None
        self._thumb_cache.clear()
        try:
            self.player.play(path)
            self.player["ab-loop-a"] = "no"
            self.player["ab-loop-b"] = "no"
        except Exception as exc:
            self._report_playback_error(self._media_generation, path, str(exc))
            return
        self._ab_state = 0
        self._ab_btn.config(text="A-B")
        self._set_button_selected(self._ab_btn, False)
        self.root.title(f"Lumveil — {os.path.basename(path)}")
        self._show_main_controls(schedule=True)
        self._after_current_file(600, self._fetch_fps)
        self._add_recent_file(path)
        if not _from_playlist:
            self._build_folder_playlist(path)
        self._refresh_playlist_popup()

    # ── プレイリスト・連続再生 ────────────────────────────────────────────

    def _build_folder_playlist(self, path):
        """単体でファイルを開いた際、同じフォルダ内の動画を連続再生の対象にする。"""
        self._playlist_scan_token += 1
        self._playlist_source = "folder"
        scan_token = self._playlist_scan_token
        sort_mode = self._playlist_sort
        self._playlist, self._playlist_idx = [path], 0
        folder = os.path.dirname(path)
        threading.Thread(
            target=self._scan_folder_playlist,
            args=(scan_token, path, folder, sort_mode),
            name="LumveilPlaylistScan",
            daemon=True,
        ).start()

    def _scan_folder_playlist(self, scan_token, path, folder, sort_mode):
        """Scan a folder away from Tk so opening a video is immediately responsive."""
        files = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if (not entry.is_file() or
                                os.path.splitext(entry.name)[1].lower() not in VIDEO_EXTS):
                            continue
                        entry_path = os.path.abspath(entry.path)
                        if sort_mode == "modified":
                            files.append((entry.stat().st_mtime_ns,
                                          entry.name.casefold(), entry_path))
                        else:
                            files.append((entry.name.casefold(), entry_path))
                    except OSError:
                        continue
            if sort_mode == "modified":
                files.sort(key=lambda item: (item[0], item[1]))
                files = [item[2] for item in files]
            else:
                files.sort(key=lambda item: item[0])
                files = [item[1] for item in files]
        except OSError:
            files = []

        target = os.path.normcase(os.path.abspath(path))
        current_index = next(
            (idx for idx, candidate in enumerate(files)
             if os.path.normcase(os.path.abspath(candidate)) == target),
            -1,
        )
        if current_index < 0:
            files.append(path)
            current_index = len(files) - 1
        self._post_ui(self._finish_folder_playlist,
                      scan_token, path, files, current_index)

    def _finish_folder_playlist(self, scan_token, path, files, current_index):
        if (scan_token != self._playlist_scan_token or
                path != self._current_path):
            return
        self._playlist = files
        self._playlist_idx = current_index
        self._refresh_playlist_popup()

    def _play_list(self, files, start_idx):
        self._playlist_scan_token += 1
        self._playlist_source = "manual"
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
            self._ab_btn.config(text="A-B: A")
            self._set_button_selected(self._ab_btn, True, "warning")
        elif self._ab_state == 1:
            if pos is None:
                return
            try:
                self.player["ab-loop-b"] = pos
            except Exception:
                pass
            self._ab_state = 2
            self._ab_btn.config(text="A-B: ON")
            self._set_button_selected(self._ab_btn, True, "success")
        else:
            try:
                self.player["ab-loop-a"] = "no"
                self.player["ab-loop-b"] = "no"
            except Exception:
                pass
            self._ab_state = 0
            self._ab_btn.config(text="A-B")
            self._set_button_selected(self._ab_btn, False)

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
            if hasattr(self, "_gpu_status"):
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

    def _tool_window(self, title, geometry="460x300"):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_ADJ)
        win.transient(self.root)
        x, y = self.root.winfo_rootx() + 30, self.root.winfo_rooty() + 40
        win.geometry(f"{geometry}+{max(0, x)}+{max(0, y)}")
        _apply_dark_titlebar(win)
        win.bind("<Escape>", lambda _e: win.destroy())
        return win

    def _seek_to_time(self, value):
        seconds = _parse_seek_time(value)
        duration = self._get_duration_ms() / 1000
        if not self._current_path or duration <= 0:
            raise ValueError("シークできる動画を開いてください。")
        if seconds > duration:
            raise ValueError(f"動画の長さ {self._fmt(int(duration * 1000))} 以内で指定してください。")
        self.player.seek(seconds, reference="absolute", precision="exact")

    def _show_time_jump(self):
        win = self._tool_window("指定時刻へ移動", "400x170")
        tk.Label(win, text="秒 / 分:秒 / 時:分:秒（例: 1:23:45）", bg=BG_ADJ,
                 fg=COL_TXT, font=("Segoe UI", 10)).pack(pady=(14, 6))
        entry = tk.Entry(win, bg=BG_CTRL, fg=COL_TXT, insertbackground=COL_TXT,
                         font=("Consolas", 12))
        entry.pack(padx=20, fill=tk.X)
        entry.insert(0, self._fmt(int(self._get_time_ms())))
        error = tk.StringVar()
        tk.Label(win, textvariable=error, bg=BG_ADJ, fg=COL_RED,
                 wraplength=360, font=("Segoe UI", 9)).pack(pady=4)
        def jump(_event=None):
            try:
                self._seek_to_time(entry.get())
                win.destroy()
            except Exception as exc:
                error.set(str(exc))
        self._btn(win, "移動", jump, bg=BG_SELECTED, fg=COL_BLU).pack()
        entry.bind("<Return>", jump)
        entry.select_range(0, tk.END)
        entry.focus_force()

    def _chapter_list(self):
        try:
            return [chapter for chapter in (self.player.chapter_list or [])
                    if isinstance(chapter, dict) and isinstance(chapter.get("time"), (int, float))
                    and math.isfinite(chapter["time"]) and chapter["time"] >= 0]
        except Exception:
            return []

    def _show_chapters(self):
        chapters = self._chapter_list()
        win = self._tool_window("チャプター")
        if not chapters:
            tk.Label(win, text="この動画にはチャプターがありません。", bg=BG_ADJ,
                     fg=COL_DIM, font=("Segoe UI", 10)).pack(pady=25)
            return
        body = tk.Frame(win, bg=BG_ADJ)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        scroll = tk.Scrollbar(body)
        listing = tk.Listbox(body, bg=BG_CTRL, fg=COL_TXT, selectbackground=BG_SELECTED,
                             relief=tk.FLAT, font=("Segoe UI", 10), yscrollcommand=scroll.set)
        listing.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.configure(command=listing.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        for i, chapter in enumerate(chapters):
            listing.insert(tk.END, f"{self._fmt(int(chapter['time'] * 1000))}  {chapter.get('title') or f'チャプター {i + 1}'}")
        def pick(_event=None):
            selection = listing.curselection()
            if selection:
                try:
                    self._seek_to_time(str(chapters[selection[0]]["time"]))
                    win.destroy()
                except Exception as exc:
                    self._show_error_popup(str(exc))
        listing.bind("<Double-Button-1>", pick)
        listing.bind("<Return>", pick)
        self._btn(win, "選択したチャプターへ", pick, bg=BG_ADJ).pack(pady=(0, 10))

    def _play_relative_chapter(self, delta):
        chapters = self._chapter_list()
        if not chapters:
            return
        position = self._get_time_ms() / 1000
        if delta > 0:
            target = next((c["time"] for c in chapters if c["time"] > position + .1), None)
        else:
            target = next((c["time"] for c in reversed(chapters) if c["time"] < position - 1), None)
        if target is not None:
            try:
                self._seek_to_time(str(target))
            except Exception as exc:
                self._show_error_popup(str(exc))

    def _playback_info_text(self):
        def prop(name):
            try:
                return getattr(self.player, name.replace("-", "_"))
            except Exception:
                return None
        if not self._current_path:
            return "動画を開くと、実際の再生状態を表示します。"
        hwdec = prop("hwdec-current")
        decoding = "再生準備中" if not prop("video-params") else (
            f"GPU: {hwdec}" if hwdec and hwdec != "no" else "CPU（ソフトウェアデコード）")
        params = prop("video-params") or {}
        return (f"ファイル: {os.path.basename(self._current_path)}\n\n"
                f"映像デコード（実際）: {decoding}\n"
                f"GPU支援（設定）: {self._gpu_hwdec}\n"
                f"映像: {params.get('w', '--')} × {params.get('h', '--')}\n"
                f"元のフレームレート: {prop('container-fps') or '--'} fps\n"
                f"ドロップフレーム: {prop('frame-drop-count') or 0}\n"
                f"音声コーデック: {prop('audio-codec-name') or 'なし'}\n"
                f"描画出力: {prop('current-vo') or '--'}")

    def _show_playback_info(self):
        win = self._tool_window("再生情報 / GPU使用状態", "520x300")
        status = tk.StringVar()
        tk.Label(win, textvariable=status, bg=BG_ADJ, fg=COL_TXT,
                 justify=tk.LEFT, anchor="nw", wraplength=490,
                 font=("Segoe UI", 10)).pack(fill=tk.BOTH, expand=True, padx=14, pady=12)
        def refresh():
            if self._closing or not win.winfo_exists():
                return
            status.set(self._playback_info_text())
            self.root.after(1000, refresh)
        refresh()

    def _show_shortcuts(self):
        win = self._tool_window("ショートカット一覧", "500x440")
        content = self._make_vertical_scroll_area(win)
        for keys, label in (
            ("Ctrl+O", "動画を開く"), ("Space", "再生 / 一時停止"),
            ("← / →", "5秒戻る / 進む"), ("Ctrl+← / →", "前 / 次の動画"),
            (", / .", "コマ戻し / コマ送り"), ("↑ / ↓", "音量を上げる / 下げる"),
            ("M", "ミュート"), ("F11", "全画面切替"), ("Esc", "全画面解除 / 補助画面を閉じる"),
            ("J", "指定時刻へ移動"), ("PageUp / PageDown", "前 / 次のチャプター"),
            ("T", "常に手前に表示"), ("I", "再生情報 / GPU使用状態"),
            ("F1", "この一覧を表示"),
        ):
            row = tk.Frame(content, bg=BG_ADJ)
            row.pack(fill=tk.X, padx=14, pady=5)
            tk.Label(row, text=keys, width=22, anchor="w", bg=BG_ADJ,
                     fg=COL_BLU, font=("Consolas", 10)).pack(side=tk.LEFT)
            tk.Label(row, text=label, bg=BG_ADJ, fg=COL_TXT,
                     font=("Segoe UI", 10)).pack(side=tk.LEFT)

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
        # Show the preview shell immediately while thumbnail extraction stays
        # deferred briefly to avoid starting ffmpeg for every mouse event.
        self._show_preview_shell(event.x)
        if self._prev_after_id:
            self.root.after_cancel(self._prev_after_id)
        self._prev_after_id = self.root.after(
            20, lambda x=event.x: self._schedule_preview(x))

    def _show_preview_shell(self, hover_x):
        dur_ms = self._get_duration_ms()
        if dur_ms <= 0:
            return
        w = self.seekbar.winfo_width()
        ratio = max(0.0, min(1.0, hover_x / max(w, 1)))
        pos_ms = int(ratio * dur_ms)
        self.prev_time_label.config(text=self._fmt(pos_ms))
        rx = self.seekbar.winfo_rootx() + hover_x - PREV_W // 2
        ry = self.seekbar.winfo_rooty() - PREV_H - 30
        self.prev_popup.geometry(f"{PREV_W}x{PREV_H + 22}+{rx}+{ry}")
        self.prev_popup.deiconify()
        self.prev_popup.lift()

    def _schedule_preview(self, hover_x):
        dur_ms = self._get_duration_ms()
        if dur_ms <= 0:
            return
        w       = self.seekbar.winfo_width()
        ratio   = max(0.0, min(1.0, hover_x / max(w, 1)))
        pos_ms  = int(ratio * dur_ms)
        pos_sec = pos_ms / 1000.0
        key     = (self._current_path, int(pos_sec / SNAP_STEP))
        self._preview_key = key

        self.prev_time_label.config(text=self._fmt(pos_ms))
        rx = self.seekbar.winfo_rootx() + hover_x - PREV_W // 2
        ry = self.seekbar.winfo_rooty() - PREV_H - 30
        self.prev_popup.geometry(f"{PREV_W}x{PREV_H + 22}+{rx}+{ry}")
        self.prev_popup.deiconify()
        self.prev_popup.lift()

        cached = self._thumb_cache.get(key)
        if cached:
            self._preview_pending_key = None
            self._apply_preview_img(cached)
            return

        if self._preview_pending_key == key:
            return
        self._preview_pending_key = key
        self._ensure_preview_worker()
        with self._preview_condition:
            # 常に最新の要求だけを残し、ポインター移動中にffmpegプロセスが
            # 増殖しないようにする。実行中の1件は完了後に結果を破棄できる。
            self._preview_job = (self._current_path, pos_sec, key)
            self._preview_condition.notify()

    def _ensure_preview_worker(self):
        if self._preview_worker and self._preview_worker.is_alive():
            return
        self._preview_worker_stop.clear()
        self._preview_worker = threading.Thread(
            target=self._preview_worker_loop,
            name="LumveilPreview",
            daemon=True,
        )
        self._preview_worker.start()

    def _preview_worker_loop(self):
        while not self._preview_worker_stop.is_set():
            with self._preview_condition:
                while (self._preview_job is None and
                       not self._preview_worker_stop.is_set()):
                    self._preview_condition.wait()
                if self._preview_worker_stop.is_set():
                    return
                path, pos_sec, key = self._preview_job
                self._preview_job = None

            img = ffmpeg_thumbnail(path, pos_sec)
            if self._preview_worker_stop.is_set():
                return
            if not self._post_ui(self._finalize_preview, img, key):
                return

    def _stop_preview_worker(self):
        if not self._preview_worker:
            return
        self._preview_worker_stop.set()
        with self._preview_condition:
            self._preview_job = None
            self._preview_condition.notify_all()

    def _finalize_preview(self, img_pil, key):
        if key != self._preview_key:
            return
        self._preview_pending_key = None
        if img_pil is None:
            return
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
        self._preview_pending_key = None
        with self._preview_condition:
            self._preview_job = None
        self._preview_key = None
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
            for overlay in (getattr(self, "ctrl_bar", None),):
                if overlay and overlay.winfo_viewable():
                    ox = overlay.winfo_rootx()
                    oy = overlay.winfo_rooty()
                    ow = overlay.winfo_width()
                    oh = overlay.winfo_height()
                    if ox <= px <= ox + ow and oy <= py <= oy + oh:
                        return True
        except Exception:
            pass
        TITLE_H = 35
        # Include all current popups, including quality/volume/navigation.
        for win in self.root.winfo_children():
            if not isinstance(win, tk.Toplevel):
                continue
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
        self.root.bind("<Control-o>", lambda e: self.open_file())
        self.root.bind("<F1>", lambda e: self._show_shortcuts())
        self.root.bind("j", lambda e: self._show_time_jump())
        self.root.bind("<Prior>", lambda e: self._play_relative_chapter(-1))
        self.root.bind("<Next>", lambda e: self._play_relative_chapter(1))
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
        self.root.bind("i",         lambda e: self._show_playback_info())
        self.root.bind("I",         lambda e: self._show_playback_info())
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-3>", self._on_right_click)

    def _on_mousewheel(self, event):
        for handler in self._scroll_wheel_handlers:
            if handler(event) == "break":
                return "break"
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

    def _select_rt_mode(self, name):
        """画質タブのAUTO強度4段階セレクタから呼ばれる。"""
        if name == "OFF":
            if self._rt_enabled:
                self._toggle_rt_adj()
            else:
                self._sync_rt_mode_buttons()
            return
        self._rt_mode = name
        self._mark_quality_custom()
        if not self._rt_enabled:
            self._toggle_rt_adj()
        else:
            # スレッドは毎周期 self._rt_mode を読むため再起動不要。
            self._sync_rt_mode_buttons()

    def _sync_rt_mode_buttons(self):
        current = self._rt_mode if self._rt_enabled else "OFF"
        if self._rt_mode_btns:
            for mname, btn in self._rt_mode_btns.items():
                self._set_button_selected(btn, mname == current, "accent")
        if hasattr(self, "_auto_btn"):
            # AUTO is a state chip: the active mode is visible without opening a menu.
            toolbar_label = RT_MODE_TOOLBAR_LABELS.get(current, current)
            self._auto_btn.config(text=f"AUTO · {toolbar_label}")
            self._set_button_selected(self._auto_btn, current != "OFF", "accent")

    def _show_auto_menu(self):
        """⚡AUTOボタンからAUTO強度モードを直接選択するポップアップメニュー。"""
        menu = tk.Menu(self.root, tearoff=0, bg=BG_CTRL, fg=COL_TXT,
                        activebackground=BG_BTN_H, activeforeground=COL_TXT)
        current = self._rt_mode if self._rt_enabled else "OFF"
        var = tk.StringVar(value=current)
        for name in ["OFF"] + list(RT_MODES.keys()):
            # ボタンの文字色とメニュー項目色を揃え、色→モードの対応を覚えやすくする。
            menu.add_radiobutton(label=name, variable=var, value=name,
                                  foreground=RT_MODE_COLORS.get(name, COL_TXT),
                                  command=lambda n=name: self._select_rt_mode(n))
        menu.update_idletasks()
        mh = menu.winfo_reqheight()
        x = self._auto_btn.winfo_rootx()
        y = self._auto_btn.winfo_rooty() - mh
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _toggle_rt_adj(self):
        if self._rt_enabled:
            self._rt_enabled = False
            self._rt_generation += 1
            self._rt_stop.set()
            self._rt_btn.config(text="リアルタイム自動調整: OFF")
            self._set_button_selected(self._rt_btn, False)
            self._auto_adj_status.set("")
            self._sync_rt_mode_buttons()
            if self._pre_rt_adj:
                for k, v in self._pre_rt_adj.items():
                    self._adj_vars[k][0].set(v)
                    try:
                        self.player[k] = int(v)
                    except Exception:
                        pass
                self._pre_rt_adj = None
            # AUTO停止時はシャドウリフトを0に戻すのではなく、手動スライダーの値を復元する。
            manual_sl = self._manual_shadow_lift.get() / 100.0
            self._rt_current["shadow_lift"] = manual_sl
            self._rt_targets["shadow_lift"] = manual_sl
            self._apply_shadow_lift(manual_sl)
            self._apply_glsl_shaders()
            if hasattr(self, "_shadow_lift_scale"):
                self._shadow_lift_scale.config(state=tk.NORMAL)
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
            self._rt_generation += 1
            self._rt_baseline = None
            self._manual_status_stats = None
            # Never clear an event an older worker still owns.
            self._rt_stop = threading.Event()
            self._rt_applied_values.clear()
            self._apply_glsl_shaders()
            if hasattr(self, "_shadow_lift_scale"):
                self._shadow_lift_scale.config(state=tk.DISABLED)
            self._rt_btn.config(text="リアルタイム自動調整: ON")
            self._set_button_selected(self._rt_btn, True, "success")
            self._auto_adj_status.set("ベースライン解析中...")
            self._sync_rt_mode_buttons()
            self._rt_thread = threading.Thread(
                target=self._rt_loop,
                args=(self._rt_stop, self._rt_generation),
                name="LumveilAutoAdjust", daemon=True)
            self._rt_threads = [thread for thread in self._rt_threads if thread.is_alive()]
            self._rt_threads.append(self._rt_thread)
            self._rt_thread.start()

    def _rt_worker_valid(self, stop_event, generation, media_generation=None):
        return (not self._closing and self._rt_enabled and not stop_event.is_set()
                and generation == self._rt_generation
                and (media_generation is None
                     or media_generation == self._media_generation))

    def _finish_rt_result(self, stop_event, generation, media_generation,
                          baseline, targets, status):
        """Only the UI thread may commit a worker's correction/status snapshot."""
        if not self._rt_worker_valid(stop_event, generation, media_generation):
            return
        self._rt_baseline = baseline
        if targets is not None:
            self._rt_targets = targets
        self._auto_adj_status.set(status)

    def _rt_establish_baseline(self, path, duration_sec, stop_event=None,
                              generation=None, media_generation=None):
        stop_event = stop_event if stop_event is not None else self._rt_stop

        def cancelled():
            return (stop_event.is_set() or self._closing
                    or (generation is not None and
                        not self._rt_worker_valid(stop_event, generation, media_generation))
                    or (media_generation is not None and
                        media_generation != self._media_generation))

        if cancelled():
            return None
        try:
            stat = os.stat(path)
            cache_key = (os.path.normcase(os.path.abspath(path)), stat.st_mtime_ns,
                         stat.st_size, round(duration_sec, 3))
        except OSError:
            cache_key = None
        if cache_key is not None:
            with self._rt_baseline_cache_lock:
                if cache_key in self._rt_baseline_cache:
                    baseline = self._rt_baseline_cache.pop(cache_key)
                    self._rt_baseline_cache[cache_key] = baseline
                    return dict(baseline) if baseline is not None else None
        # 32点サンプル（均等分布 + 前後端）で正常フレームをより多く確保
        ratios = [i / 31 for i in range(1, 31)] + [0.02, 0.98]
        samples = []
        for r in ratios:
            if cancelled():
                return None
            stats = analyze_frame(path, r * duration_sec)
            if cancelled():
                return None
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
        # 上位15件平均でベースラインを安定化
        top = sorted(good, key=lambda s: s["lum_mean"] * s["lum_std"], reverse=True)[:15]
        baseline = {
            "lum_mean": sum(s["lum_mean"] for s in top) / len(top),
            "lum_std":  sum(s["lum_std"]  for s in top) / len(top),
            "chroma":   sum(s["chroma"]   for s in top) / len(top),
        } if top else None
        if cache_key is not None and not cancelled():
            try:
                stat = os.stat(path)
                unchanged = (stat.st_mtime_ns, stat.st_size) == cache_key[1:3]
            except OSError:
                unchanged = False
            if not unchanged:
                return None
            with self._rt_baseline_cache_lock:
                self._rt_baseline_cache[cache_key] = baseline
                while len(self._rt_baseline_cache) > 8:
                    del self._rt_baseline_cache[next(iter(self._rt_baseline_cache))]
        return baseline

    @staticmethod
    def _rt_sample_due(paused, signature, previous_signature, elapsed, force=False):
        return (not paused or force or signature != previous_signature or elapsed >= 5.0)

    def _rt_loop(self, stop_event, generation):
        # MPV整数空間（0=中立）で暗闇補正を計算する。
        EXTREME_RATIO = 0.05
        INTENT_SECS   = 3.0

        base_adj = dict(self._rt_base_adj)
        bl_generation = None
        bl = None
        dark_start_time = None
        sample_signature = None
        sample_time = 0.0
        sample_wall_time = 0.0

        while self._rt_worker_valid(stop_event, generation):
            media_generation = self._media_generation
            path = self._current_path

            def post_result(status, targets=None):
                self._post_ui(self._finish_rt_result, stop_event, generation,
                              media_generation, bl, targets, status)

            # 動画が切り替わった場合、古い動画のベースラインを新動画に適用し続けないよう
            # 補正ターゲットを基準値へ戻した上でベースラインを再解析する。
            if media_generation != bl_generation:
                bl = None
                post_result("ベースライン解析中...", dict(base_adj, shadow_lift=0.0))
                duration = self._get_duration_ms() / 1000.0
                if not path or duration <= 0:
                    # 動画切替直後は duration が未確定な場合があるため、次周期に持ち越す。
                    stop_event.wait(0.5)
                    continue
                bl = self._rt_establish_baseline(path, duration, stop_event,
                                                generation, media_generation)
                if not self._rt_worker_valid(stop_event, generation, media_generation):
                    continue
                bl_generation = media_generation
                dark_start_time = None
                if bl:
                    post_result(f"ベースライン確立  輝度:{bl['lum_mean']:.0f}  "
                                f"コントラスト指標:{bl['lum_std']:.0f}")
                else:
                    post_result("⚠ 明るいフレームが見つかりません")

            pos_ms = self._get_time_ms()
            if path and pos_ms >= 0 and bl:
                # screenshot_rawはmpv equalizer適用後の画を返すため、測定値を
                # ソース空間(ベースラインと同じ空間)へ逆補正する。これを怠ると
                # 補正量が測定に跳ね返る自己フィードバックで発振する(実測で確認済み)。
                # mpv実式: out = in*k_c + 2.55*b(実測校正)、chroma_out = chroma*k_s
                cur_adj = dict(self._rt_current)
                signature = (media_generation, round(pos_ms, 1), self._rt_analysis_revision,
                             self._rt_mode, self._dark_thresh,
                             tuple(int(round(cur_adj.get(key, 0.0))) for key in
                                   ("brightness", "contrast", "gamma", "saturation")),
                             round(cur_adj.get("shadow_lift", 0.0), 3))
                # Finish the existing three-second intentional-darkness decay even
                # if playback was paused before it reached its floor.
                intent_pending = (dark_start_time is not None and
                                  sample_wall_time < dark_start_time + INTENT_SECS)
                if not self._rt_sample_due(self._cached_pause, signature, sample_signature,
                                           time.monotonic() - sample_time, intent_pending):
                    stop_event.wait(0.5)
                    continue
                # コントラストの+100超過分はGLSL側でありscreenshot_rawに映らないため、
                # 逆補正はmpvへ実際に渡る+100までで頭打ちにする。
                k_c = max(0.01, 1.0 + min(100.0, cur_adj.get("contrast", 0.0)) / 100.0)
                k_s = max(0.01, 1.0 + cur_adj.get("saturation", 0.0) / 100.0)
                b_adj = cur_adj.get("brightness", 0.0)
                dark_thresh = min(255, int(60 * k_c + 2.55 * b_adj))

                stats = analyze_current_frame(self.player, dark_thresh=dark_thresh)
                if stats and self._rt_worker_valid(stop_event, generation, media_generation):
                    sample_signature = signature
                    sample_time = time.monotonic()
                    sample_wall_time = time.time()
                    cur_mean = max(0.0, (stats["lum_mean"] - 2.55 * b_adj) / k_c)
                    cur_std  = stats["lum_std"] / k_c
                    cur_chroma = stats["chroma"] / k_s
                    norm     = cur_mean / 255.0
                    norm_tgt = bl["lum_mean"] / 255.0
                    ratio_mean = cur_mean / max(bl["lum_mean"], 1.0)
                    ratio_std  = cur_std  / max(bl["lum_std"],  1.0)

                    raw_ratio   = min(ratio_mean, ratio_std)
                    dark_factor = max(0.0, min(1.0,
                        (self._dark_thresh - raw_ratio)
                        / max(self._dark_thresh, 0.01)))

                    # 誤検知ガード1: 平均輝度が絶対的・相対的に十分高いなら
                    # 標準偏差（コントラスト）だけで暗所判定しない
                    # （霧・白基調・低コントラストな明所シーンの誤判定防止）。
                    if cur_mean >= 110 or ratio_mean >= 0.60:
                        dark_factor = 0.0

                    # 誤検知ガード2: 画面の35%以上が暗部画素でなければ暗所補正しない。
                    if dark_factor >= 0.02 and stats.get("dark_ratio", 1.0) < 0.35:
                        dark_factor = 0.0

                    if dark_factor < 0.02:
                        dark_start_time = None
                        post_result(f"✓ 補正なし(十分明るい場面)  輝度比:{ratio_mean:.2f}  "
                                    f"コントラスト比:{ratio_std:.2f}",
                                    dict(base_adj, shadow_lift=0.0))
                    else:
                        strength, intent_floor = RT_MODES.get(
                            self._rt_mode, RT_MODES["標準"])

                        is_extreme = raw_ratio < EXTREME_RATIO
                        if is_extreme:
                            if dark_start_time is None:
                                dark_start_time = time.time()
                            elapsed = time.time() - dark_start_time
                            intent_factor = max(intent_floor,
                                1.0 - (1.0 - intent_floor) * min(1.0, elapsed / INTENT_SECS))
                        else:
                            dark_start_time = None
                            intent_factor   = 1.0

                        scale = dark_factor * intent_factor

                        # ── MPV整数空間で直接計算（提案アルゴリズム）────────
                        lum_ratio = norm / max(norm_tgt, 0.001)

                        # シャドウリフト（主処理）: 暗部画素だけを構造的に持ち上げる。
                        # 全域コントラスト拡張と違い中間調・ハイライトを潰さないため、
                        # AUTOの暗所補正はこれを主とする。
                        shadow_lift_tgt = min(1.0, dark_factor * intent_factor * 1.2 * strength)

                        # コントラスト（副処理）: シャドウリフトが主処理になったため、
                        # 全域コントラスト拡張は控えめな補助に縮小する。
                        # 上限もMPV範囲(+100)内に収まる60までとし、AUTO時はGLSL側の
                        # コントラスト拡張（100超過分）は実質使われない
                        # （手動スライダーで+100超を指定した場合のみ使用される）。
                        # コントラスト係数: シャドウリフト単体で十分改善するとの実映像評価
                        # を受けて縮小(dark_factor項 40.0→20.0)。
                        std_ratio    = bl["lum_std"] / max(cur_std, 1.0)
                        contrast_adj = min(60.0, max(0.0,
                            (dark_factor * 20.0 * intent_factor
                             + (std_ratio - 1.0) * 30.0 * scale) * strength))

                        # 輝度: 全体を白側へ寄せやすいので、必要な時だけごく少量に留める。
                        brightness_adj = min(3.0, max(0.0, 3.0 * scale * strength))

                        # 極暗モード(手動選択): 黒潰れ動画向けにユーザー実測レシピ(輝度+33/コントラスト+89
                        # で黒潰れが視認可)相当を投入する。自動判定は誤発動・発動漏れの両側で破綻したため
                        # 撤去し、ユーザーが明示的に選ぶ方式にした(局所白飛び検出を手動化したのと同じ判断)。
                        # 明るい場面まで持ち上げないよう、暗所度に連動させる(dark_factor 0.5以上でフル)。
                        if self._rt_mode == "極暗":
                            boost = min(1.0, dark_factor * 2.0) * intent_factor
                            brightness_adj = min(45.0, brightness_adj + 30.0 * boost)
                            contrast_adj   = min(95.0, contrast_adj + 80.0 * boost)

                        # 彩度: 暗部を持ち上げた時の眠い見え方を補う。
                        # シャドウリフトはコントラスト拡張よりも彩度低下が小さいため、
                        # 上限・係数とも縮小する。実映像できつすぎたため係数・上限とも半減。
                        # さらにシャドウリフト単体で十分改善するとの実映像評価を受けて縮小
                        # (dark_factor項 6.0→3.0、chroma_ratio項 16.0→8.0、上限 24.0→12.0)。
                        chroma_ratio = bl["chroma"] / max(cur_chroma, 1.0)
                        saturation_adj = min(12.0, max(0.0,
                            (dark_factor * 3.0 * intent_factor
                             + max(0.0, chroma_ratio - 1.0) * 8.0 * scale) * strength))

                        def with_base(key, adjustment):
                            return max(-100.0, min(100.0,
                                base_adj[key] + adjustment))

                        brightness_tgt = with_base("brightness", brightness_adj)
                        contrast_total = max(-100.0, min(300.0,
                            base_adj["contrast"] + contrast_adj))
                        contrast_tgt = contrast_total
                        # ガンマの自動連動は撤去。実効コントラストのシェーダーを
                        # 中間点(pivot)基準の計算に修正した結果、以前のように
                        # コントラストを上げるほど白側だけが伸びる挙動ではなく
                        # なったため、白側を締めるための補正ガンマは不要かつ
                        # コントラストの効きを打ち消してしまう（実測確認済み）。
                        gamma_tgt = with_base("gamma", 0.0)
                        saturation_tgt = with_base("saturation", saturation_adj)

                        targets = {
                            "gamma":       gamma_tgt,
                            "brightness":  brightness_tgt,
                            "contrast":    contrast_tgt,
                            "saturation":  saturation_tgt,
                            "shadow_lift": shadow_lift_tgt,
                        }

                        mode_name = self._rt_mode
                        if is_extreme:
                            status_text = (f"🌑 演出保護のため補正を抑制中({mode_name})"
                                           f"  強さ:{shadow_lift_tgt:.2f}")
                        elif mode_name == "極暗":
                            status_text = (f"🌌 極暗モードで補正中"
                                           f"  強さ:{shadow_lift_tgt:.2f}")
                        else:
                            status_text = (f"🔄 暗部を補正中({mode_name})"
                                           f"  強さ:{shadow_lift_tgt:.2f}")
                        post_result(status_text, targets)
            stop_event.wait(0.5)

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
                if self._adj_vars[key][0].get() != mpv_val:
                    self._adj_vars[key][0].set(mpv_val)
                if self._rt_applied_values.get(key) == mpv_val:
                    continue
                if key == "contrast":
                    if self._apply_effective_contrast(mpv_val, flush=False):
                        self._rt_applied_values[key] = mpv_val
                else:
                    try:
                        self.player[key] = mpv_val
                        self._rt_applied_values[key] = mpv_val
                    except Exception:
                        pass

            # シャドウリフト（float 0.0〜1.0のGLSL空間、int丸めはしない）
            sl_cur = self._rt_current.get("shadow_lift", 0.0)
            sl_tgt = self._rt_targets.get("shadow_lift", 0.0)
            if abs(sl_cur - sl_tgt) > 0.005:
                sl_new = sl_cur + (sl_tgt - sl_cur) * ALPHA
                self._rt_current["shadow_lift"] = sl_new
                self._apply_shadow_lift(sl_new, flush=False)
            self._apply_shader_opts()

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
                stats = self._manual_status_stats
                if stats:
                    ratio = f"{stats['lum_mean'] / max(self._rt_baseline['lum_mean'], 1.0):.2f}"
                self._schedule_manual_status_stats()
            self._auto_adj_status.set(
                f"手動  比率:{ratio}  補正:{correction}"
                f"  B:{values['brightness']:+d}  γ:{values['gamma']:+d}"
                f"  C:{values['contrast']:+d}  S:{values['saturation']:+d}")
        self.root.after(1000, self._manual_status_loop)

    def _schedule_manual_status_stats(self):
        """UIを止めずに手動画面の明るさ比率を更新する。"""
        if (self._manual_status_pending or not self._current_path or
                not self._rt_baseline):
            return
        self._manual_status_pending = True
        path = self._current_path
        baseline = self._rt_baseline
        threading.Thread(
            target=self._manual_status_stats_worker,
            args=(path, baseline),
            name="LumveilManualStats",
            daemon=True,
        ).start()

    def _manual_status_stats_worker(self, path, baseline):
        try:
            stats = analyze_current_frame(self.player)
        except Exception:
            stats = None
        self._post_ui(self._finish_manual_status_stats,
                      path, baseline, stats)

    def _finish_manual_status_stats(self, path, baseline, stats):
        self._manual_status_pending = False
        if (self._rt_enabled or path != self._current_path or
                baseline is not self._rt_baseline):
            return
        self._manual_status_stats = stats

    # ── 定期更新ループ ────────────────────────────────────────────────────

    def _post_ui(self, callback, *args, **kwargs):
        """Queue a worker result without entering Tcl from a worker thread."""
        if self._closing:
            return False
        try:
            self._ui_dispatch_queue.put_nowait((callback, args, kwargs))
            return True
        except Exception:
            return False

    def _drain_ui_queue(self):
        """Run a bounded batch of worker callbacks on Tk's main thread."""
        if self._closing:
            while True:
                try:
                    self._ui_dispatch_queue.get_nowait()
                except queue.Empty:
                    return
        for _ in range(64):
            try:
                callback, args, kwargs = self._ui_dispatch_queue.get_nowait()
            except queue.Empty:
                return
            try:
                callback(*args, **kwargs)
            except (tk.TclError, RuntimeError):
                if self._closing:
                    return

    def _update_loop(self):
        if self._closing:
            return
        self._drain_ui_queue()
        self._drain_mpv_events()
        self._refresh_playback_state()
        if not self.is_seeking:
            dur_ms = self._get_duration_ms()
            pos_ms = self._get_time_ms()
            self._cached_time_ms = pos_ms
            if dur_ms > 0:
                self.seek_var.set(pos_ms / dur_ms * 1000)
            tc = self._fmt(int(pos_ms))
            td = self._fmt(int(dur_ms))
            self.time_var.set(tc)
            self.dur_var.set(td)

        is_playing = (self._cached_pause is False)
        new_icon = self._icons["pause"] if is_playing else self._icons["play"]
        if self.play_btn.cget("text") != new_icon:
            self.play_btn.config(text=new_icon)

        self.root.after(200, self._update_loop)

    def _refresh_playback_state(self):
        state = []
        if self._playback_eof_action == "repeat":
            state.append("リピート")
        if self._muted:
            state.append("ミュート")
        if self._rt_enabled:
            state.append("AUTO解析中" if self._rt_baseline is None else f"AUTO: {self._rt_mode}")
        self._playback_state_var.set(" / ".join(state))

    def _drain_mpv_events(self):
        """Apply coalesced mpv notifications on Tk's main thread only."""
        with self._mpv_event_lock:
            duration = self._mpv_pending_duration
            pause = self._mpv_pending_pause
            eof = self._mpv_pending_eof
            file_loaded = self._mpv_pending_file_loaded
            self._mpv_pending_duration = _MPV_EVENT_PENDING
            self._mpv_pending_pause = _MPV_EVENT_PENDING
            self._mpv_pending_eof = None
            self._mpv_pending_file_loaded = False

        if duration is not _MPV_EVENT_PENDING:
            self._on_duration_prop(duration)
        if pause is not _MPV_EVENT_PENDING:
            self._on_pause_prop(pause)
        if not self._closing and self._eof_matches_current_file(eof):
            self._on_eof_reached()
        if file_loaded and not self._closing:
            self._handle_mpv_file_loaded()

    def _on_mpv_eof(self, value):
        with self._mpv_event_lock:
            self._mpv_pending_eof = ((self._media_generation, self._current_path)
                                     if value else None)

    def _eof_matches_current_file(self, eof):
        if not self._current_path or eof != (self._media_generation, self._current_path):
            return False
        try:
            # A notification from the previous load can arrive after play().
            # Confirm the native player really ended the current file as well.
            return (bool(self.player.eof_reached) and
                    os.path.normcase(os.path.abspath(self.player.path)) ==
                    os.path.normcase(os.path.abspath(self._current_path)))
        except Exception:
            return False

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


def _launch_file_paths(args):
    """Return unique existing paths supplied by the shell/file association."""
    paths = []
    seen = set()
    for raw_path in args:
        if not isinstance(raw_path, str):
            continue
        try:
            path = os.path.abspath(raw_path)
            key = os.path.normcase(path)
            if key not in seen and os.path.isfile(path):
                paths.append(path)
                seen.add(key)
        except (OSError, TypeError):
            continue
    return paths


def main():
    single_instance = _SingleInstance.acquire()
    launch_paths = _launch_file_paths(sys.argv[1:])
    if not single_instance.primary:
        # The primary may still be constructing Tk/mpv, so forward with a
        # bounded retry instead of accidentally starting a second player.
        single_instance.forward_paths(launch_paths)
        return

    forwarded_messages = queue.Queue()
    single_instance.start_server(forwarded_messages)
    root = None
    try:
        root = TkinterDnD.Tk()
        _restore_window_geometry(root)
        app = VideoPlayer(root)

        def drain_forwarded_messages():
            try:
                while True:
                    app._open_external_paths(forwarded_messages.get_nowait())
            except queue.Empty:
                pass
            except tk.TclError:
                return
            try:
                root.after(100, drain_forwarded_messages)
            except tk.TclError:
                pass

        if launch_paths:
            # Put the original launch before any second-instance handoffs so
            # near-simultaneous file opens retain FIFO ordering.
            forwarded_messages.put(launch_paths)
        root.after(0, drain_forwarded_messages)
        root.mainloop()
    finally:
        single_instance.close()


if __name__ == "__main__":
    main()
