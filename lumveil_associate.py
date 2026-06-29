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


def _associate(ext, exe):
    prog_id = f"Lumveil{ext.replace('.', '')}"
    try:
        # HKCU\Software\Classes\<ext> → ProgID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{ext}") as k:
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


def _unassociate(ext):
    prog_id = f"Lumveil{ext.replace('.', '')}"
    try:
        # ext キーの既定値が自分のものなら削除
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"Software\Classes\{ext}",
                            0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, "")
            if val != prog_id:
                return True  # 他アプリの関連付けは触らない

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Classes\{ext}")
    except Exception:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Classes\{prog_id}\shell\open\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Classes\{prog_id}\shell\open")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Classes\{prog_id}\shell")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Classes\{prog_id}\DefaultIcon")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Classes\{prog_id}")
    except Exception:
        pass
    return True


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
        for ext in targets:
            _unassociate(ext)
        _notify_shell()
        self._status.set(f"✓ {len(targets)}件 解除しました")


def main():
    if not _is_admin():
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit()

    root = tk.Tk()
    AssocTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()
