"""Lumveil ファイル関連付けツール"""
import os
import sys
import ctypes
import winreg
import tkinter as tk
from tkinter import ttk, messagebox

EXE_NAME = "Lumveil.exe"

EXTENSIONS = [
    ".mp4", ".mkv", ".avi", ".mov", ".wmv",
    ".flv", ".webm", ".m4v", ".ts", ".m2ts",
    ".vob", ".ogv", ".3gp", ".rmvb", ".rm",
]

DEFAULT_CHECKED = {".mp4", ".mkv", ".avi", ".mov", ".wmv"}

BG      = "#1a1a1a"
BG_CARD = "#222222"
COL_TXT = "#dddddd"
COL_BLU = "#6ab0f5"
COL_ORG = "#f0a060"
COL_GRN = "#7ec8a0"
COL_DIM = "#888888"
COL_RED = "#f07070"


def _is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def _exe_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), EXE_NAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), EXE_NAME)


def _query_value(path, name):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
        return winreg.QueryValueEx(key, name)


def _associate(ext, exe):
    prog_id = f"Lumveil{ext.replace('.', '')}"
    ext_key = rf"Software\Classes\{ext}"
    prog_key = rf"Software\Classes\{prog_id}"
    try:
        extension = winreg.CreateKey(winreg.HKEY_CURRENT_USER, ext_key)
        try:
            previous, previous_type = winreg.QueryValueEx(extension, "")
            had_previous = True
        except OSError:
            previous, previous_type, had_previous = None, winreg.REG_SZ, False

        # Keep the prior default so the user's previous choice can be restored.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, prog_key) as k:
            try:
                saved_present, _ = winreg.QueryValueEx(
                    k, "LumveilPreviousDefaultPresent")
                saved, _ = winreg.QueryValueEx(k, "LumveilPreviousDefault")
                saved_exists = bool(saved_present)
            except OSError:
                saved, saved_exists = None, False
            if previous != prog_id or not saved_exists:
                if had_previous and previous != prog_id:
                    winreg.SetValueEx(k, "LumveilPreviousDefault", 0,
                                      previous_type, previous)
                    winreg.SetValueEx(k, "LumveilPreviousDefaultPresent", 0,
                                      winreg.REG_DWORD, 1)
                else:
                    try:
                        winreg.DeleteValue(k, "LumveilPreviousDefault")
                    except OSError:
                        pass
                    winreg.SetValueEx(k, "LumveilPreviousDefaultPresent", 0,
                                      winreg.REG_DWORD, 0)

        try:
            extension.Close()
        except AttributeError:
            pass

        # HKCU\Software\Classes\<ext> → ProgID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ext_key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, prog_id)

        # HKCU\Software\Classes\<ProgID>\shell\open\command
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{prog_id}\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')

        # DefaultIcon
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{prog_id}\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}",0')

        return True
    except Exception as e:
        return False


def _delete_value_if_matches(path, name, expected):
    """Delete a Lumveil-owned registry value without removing sibling data."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            value, _ = winreg.QueryValueEx(key, name)
            if value == expected:
                winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _remove_empty_key(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ) as key:
            subkeys, values, _ = winreg.QueryInfoKey(key)
        if subkeys or values:
            return
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        # Missing, inaccessible, or newly populated keys are left untouched.
        pass


def _unassociate(ext, *, expected_exe=None):
    prog_id = f"Lumveil{ext.replace('.', '')}"
    ext_key = rf"Software\Classes\{ext}"
    prog_key = rf"Software\Classes\{prog_id}"
    exe = expected_exe or _exe_path()
    command = f'"{exe}" "%1"'
    icon = f'"{exe}",0'
    if expected_exe is not None:
        # An older backup's uninstaller must not unregister a newer install.
        try:
            owned_command, _ = _query_value(rf"{prog_key}\shell\open\command", "")
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if owned_command != command:
            return True
    current = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key,
                            0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            current, _ = winreg.QueryValueEx(key, "")
            if current == prog_id:
                try:
                    previous_present, _ = _query_value(
                        prog_key, "LumveilPreviousDefaultPresent")
                except OSError:
                    previous_present = 0
                if previous_present:
                    previous, previous_type = _query_value(
                        prog_key, "LumveilPreviousDefault")
                    winreg.SetValueEx(key, "", 0, previous_type, previous)
                else:
                    winreg.DeleteValue(key, "")
    except FileNotFoundError:
        current = None
    except OSError:
        return False

    if current != prog_id and expected_exe is None:
        return not _is_associated(ext)

    # A foreign default is never modified. Legacy installs without a saved
    # default have their Lumveil value removed while preserving the ext key.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key,
                            0, winreg.KEY_READ) as key:
            try:
                remaining, _ = winreg.QueryValueEx(key, "")
            except FileNotFoundError:
                remaining = None
    except FileNotFoundError:
        remaining = None
    except OSError:
        return False
    if remaining == prog_id:
        return False

    # Remove only the exact command/icon values created by this application.
    # Leave the command until the icon is handled, so a failed uninstall can
    # retry ownership detection instead of silently leaving a dangling path.
    if not _delete_value_if_matches(rf"{prog_key}\DefaultIcon", "", icon):
        return False
    if not _delete_value_if_matches(rf"{prog_key}\shell\open\command", "", command):
        return False
    for path in (rf"{prog_key}\shell\open\command",
                 rf"{prog_key}\shell\open", rf"{prog_key}\shell",
                 rf"{prog_key}\DefaultIcon"):
        _remove_empty_key(path)

    # Drop saved metadata only after the extension default was removed/restored.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, prog_key, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            for name in ("LumveilPreviousDefaultPresent",
                         "LumveilPreviousDefault"):
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
    except OSError:
        pass
    for path in (rf"{prog_key}\shell\open\command",
                 rf"{prog_key}\shell\open", rf"{prog_key}\shell",
                 rf"{prog_key}\DefaultIcon", prog_key):
        _remove_empty_key(path)
    return not _is_associated(ext)


def _notify_shell():
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def _is_associated(ext):
    prog_id = f"Lumveil{ext.replace('.', '')}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{ext}") as k:
            val, _ = winreg.QueryValueEx(k, "")
            return val == prog_id
    except Exception:
        return False


class AssocTool:
    def __init__(self, root):
        self.root = root
        self.root.title("Lumveil - ファイル関連付け")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        self._exe = _exe_path()
        self._vars = {}
        self._build_ui()

        w, h = 360, 420
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    def _build_ui(self):
        tk.Label(self.root, text="Lumveil",
                 bg=BG, fg=COL_ORG,
                 font=("Segoe UI", 16, "bold")).pack(pady=(20, 2))
        tk.Label(self.root, text="ファイル関連付け設定",
                 bg=BG, fg=COL_DIM,
                 font=("Segoe UI", 9)).pack()

        tk.Frame(self.root, bg="#333333", height=1).pack(fill=tk.X, padx=20, pady=12)

        # EXEパス表示
        exe_frame = tk.Frame(self.root, bg=BG)
        exe_frame.pack(fill=tk.X, padx=20, pady=(0, 8))
        tk.Label(exe_frame, text="対象EXE:",
                 bg=BG, fg=COL_DIM, font=("Segoe UI", 8)).pack(anchor="w")
        tk.Label(exe_frame, text=self._exe,
                 bg=BG, fg=COL_BLU, font=("Segoe UI", 7),
                 wraplength=320, justify=tk.LEFT).pack(anchor="w")

        tk.Frame(self.root, bg="#333333", height=1).pack(fill=tk.X, padx=20, pady=(0, 10))

        tk.Label(self.root, text="関連付ける拡張子を選択:",
                 bg=BG, fg=COL_TXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20)

        # チェックボックスグリッド
        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill=tk.X, padx=24, pady=8)

        for i, ext in enumerate(EXTENSIONS):
            already = _is_associated(ext)
            var = tk.BooleanVar(value=already or ext in DEFAULT_CHECKED)
            self._vars[ext] = var
            cb = tk.Checkbutton(
                grid, text=ext,
                variable=var,
                bg=BG, fg=COL_GRN if already else COL_TXT,
                selectcolor=BG_CARD,
                activebackground=BG, activeforeground=COL_ORG,
                font=("Segoe UI", 9),
                width=7, anchor="w")
            cb.grid(row=i // 3, column=i % 3, sticky="w", pady=2)

        tk.Frame(self.root, bg="#333333", height=1).pack(fill=tk.X, padx=20, pady=10)

        # ボタン
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(pady=(0, 8))

        tk.Button(btn_frame, text="関連付ける",
                  command=self._do_associate,
                  bg=COL_ORG, fg="#111111",
                  font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, bd=0,
                  padx=20, pady=6, cursor="hand2",
                  activebackground="#f8b880").pack(side=tk.LEFT, padx=6)

        tk.Button(btn_frame, text="選択を解除",
                  command=self._do_unassociate,
                  bg=BG_CARD, fg=COL_RED,
                  font=("Segoe UI", 10),
                  relief=tk.FLAT, bd=0,
                  padx=20, pady=6, cursor="hand2",
                  activebackground="#3a3a3a").pack(side=tk.LEFT, padx=6)

        self._status = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self._status,
                 bg=BG, fg=COL_GRN,
                 font=("Segoe UI", 8)).pack()

    def _do_associate(self):
        if not os.path.exists(self._exe):
            messagebox.showerror("エラー",
                f"Lumveil.exe が見つかりません。\n{self._exe}")
            return
        targets = [ext for ext, var in self._vars.items() if var.get()]
        if not targets:
            messagebox.showwarning("警告", "拡張子を1つ以上選択してください")
            return
        ok = all(_associate(ext, self._exe) for ext in targets)
        _notify_shell()
        if ok:
            self._status.set(f"✓ {len(targets)}件 関連付けました")
        else:
            self._status.set("⚠ 一部の関連付けに失敗しました")

    def _do_unassociate(self):
        targets = [ext for ext, var in self._vars.items() if var.get()]
        if not targets:
            messagebox.showwarning("警告", "拡張子を1つ以上選択してください")
            return
        results = [_unassociate(ext) for ext in targets]
        _notify_shell()
        succeeded = sum(results)
        if succeeded == len(targets):
            self._status.set(f"✓ {succeeded}件 解除しました")
        else:
            self._status.set(f"⚠ {succeeded}/{len(targets)}件を解除しました")


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        if arguments != ["--unassociate-all"]:
            return 2
        # No GUI/elevation recursion; the uninstaller supplies its own context.
        exe = _exe_path()
        results = [_unassociate(ext, expected_exe=exe) for ext in EXTENSIONS]
        _notify_shell()
        return 0 if all(results) else 1
    if not _is_admin():
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit()

    root = tk.Tk()
    AssocTool(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
