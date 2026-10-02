"""Distribution regressions using an in-memory registry; never touch Windows registry."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ASSOC_SOURCE = ROOT / "lumveil_associate.py"
INSTALLER_SOURCE = ROOT / "installer" / "Lumveil.nsi"


class _Key:
    def __init__(self, registry, path):
        self.registry = registry
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _MemoryWinreg(types.ModuleType):
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_DWORD = 4

    def __init__(self):
        super().__init__("winreg")
        self.keys = {"": {}}
        self.fail_delete = None

    @staticmethod
    def _norm(path):
        return path.replace("/", "\\").strip("\\").casefold()

    def CreateKey(self, _root, path):
        norm = self._norm(path)
        parts = norm.split("\\")
        for index in range(len(parts)):
            self.keys.setdefault("\\".join(parts[:index + 1]), {})
        return _Key(self, norm)

    def OpenKey(self, _root, path, *_args):
        norm = self._norm(path)
        if norm not in self.keys:
            raise FileNotFoundError(path)
        return _Key(self, norm)

    def SetValueEx(self, key, name, _reserved, value_type, value):
        self.keys[key.path][name] = (value, value_type)

    def QueryValueEx(self, key, name):
        try:
            return self.keys[key.path][name]
        except KeyError as error:
            raise FileNotFoundError(name) from error

    def DeleteValue(self, key, name):
        if (key.path, name) == self.fail_delete:
            raise PermissionError("injected registry write failure")
        try:
            del self.keys[key.path][name]
        except KeyError as error:
            raise FileNotFoundError(name) from error

    def DeleteKey(self, _root, path):
        norm = self._norm(path)
        if norm not in self.keys:
            raise FileNotFoundError(path)
        prefix = norm + "\\"
        if any(key.startswith(prefix) for key in self.keys):
            raise OSError("key has child keys")
        del self.keys[norm]

    def QueryInfoKey(self, key):
        prefix = key.path + "\\"
        subkeys = sum(
            child.startswith(prefix) and "\\" not in child[len(prefix):]
            for child in self.keys
        )
        return subkeys, len(self.keys[key.path]), 0

    def put(self, path, name, value, value_type=REG_SZ):
        key = self.CreateKey(self.HKEY_CURRENT_USER, path)
        self.SetValueEx(key, name, 0, value_type, value)

    def get(self, path, name):
        return self.QueryValueEx(self.OpenKey(self.HKEY_CURRENT_USER, path), name)[0]


def _load_association_module():
    fake = _MemoryWinreg()
    spec = importlib.util.spec_from_file_location("lumveil_associate_test", ASSOC_SOURCE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"winreg": fake}):
        spec.loader.exec_module(module)
    module.winreg = fake
    return module, fake


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.assoc, self.registry = _load_association_module()
        self.exe = r"C:\Program Files\Lumveil\Lumveil.exe"
        self.assoc._exe_path = lambda: self.exe

    def test_unassociate_restores_prior_default_and_preserves_extension_data(self):
        extension = r"Software\Classes\.mp4"
        child = extension + r"\OpenWithProgids"
        self.registry.put(extension, "", "VLC.mp4", self.registry.REG_EXPAND_SZ)
        self.registry.put(extension, "CustomValue", "keep me")
        self.registry.put(child, "VLC.mp4", "")

        self.assertTrue(self.assoc._associate(".mp4", self.exe))
        self.assertEqual(self.registry.get(extension, ""), "Lumveilmp4")
        self.assertTrue(self.assoc._unassociate(".mp4"))

        self.assertEqual(self.registry.get(extension, ""), "VLC.mp4")
        self.assertEqual(
            self.registry.QueryValueEx(
                self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, extension), "")[1],
            self.registry.REG_EXPAND_SZ)
        self.assertEqual(self.registry.get(extension, "CustomValue"), "keep me")
        self.assertEqual(self.registry.get(child, "VLC.mp4"), "")

    def test_legacy_unassociate_removes_only_lumveil_default(self):
        extension = r"Software\Classes\.mkv"
        child = extension + r"\OpenWithProgids"
        self.registry.put(extension, "", "Lumveilmkv")
        self.registry.put(extension, "OtherValue", "preserve")
        self.registry.put(child, "OtherPlayer.mkv", "")
        self.registry.put(r"Software\Classes\Lumveilmkv\shell\open\command",
                          "", f'"{self.exe}" "%1"')
        self.registry.put(r"Software\Classes\Lumveilmkv\DefaultIcon", "",
                          f'"{self.exe}",0')

        self.assertTrue(self.assoc._unassociate(".mkv"))
        self.assertNotIn("", self.registry.keys[self.registry._norm(extension)])
        self.assertEqual(self.registry.get(extension, "OtherValue"), "preserve")
        self.assertEqual(self.registry.get(child, "OtherPlayer.mkv"), "")

    def test_unassociate_preserves_unrelated_values_on_lumveil_progid(self):
        extension = r"Software\Classes\.mov"
        prog_key = r"Software\Classes\Lumveilmov"
        self.registry.put(extension, "", "Lumveilmov")
        self.registry.put(prog_key, "CustomProgIdData", "preserve")

        self.assertTrue(self.assoc._unassociate(".mov"))

        self.assertIn(self.registry._norm(prog_key), self.registry.keys)
        self.assertEqual(self.registry.get(prog_key, "CustomProgIdData"), "preserve")

    def test_unassociate_preserves_a_foreign_default(self):
        extension = r"Software\Classes\.avi"
        self.registry.put(extension, "", "OtherPlayer.avi")
        self.registry.put(extension, "CustomValue", "preserve")
        self.assertTrue(self.assoc._unassociate(".avi"))
        self.assertEqual(self.registry.get(extension, ""), "OtherPlayer.avi")
        self.assertEqual(self.registry.get(extension, "CustomValue"), "preserve")

    def test_reassociate_records_the_latest_foreign_default(self):
        extension = r"Software\Classes\.webm"
        self.registry.put(extension, "", "FirstPlayer.webm")
        self.assertTrue(self.assoc._associate(".webm", self.exe))
        self.registry.put(extension, "", "SecondPlayer.webm")
        self.assertTrue(self.assoc._associate(".webm", self.exe))
        self.assertTrue(self.assoc._unassociate(".webm"))
        self.assertEqual(self.registry.get(extension, ""), "SecondPlayer.webm")

    def test_unassociate_reports_registry_write_failure(self):
        extension = r"Software\Classes\.mov"
        self.registry.put(extension, "", "Lumveilmov")
        self.registry.fail_delete = (self.registry._norm(extension), "")
        self.assertFalse(self.assoc._unassociate(".mov"))
        self.assertEqual(self.registry.get(extension, ""), "Lumveilmov")

    def test_installer_contains_running_process_guard_and_copy_rollback(self):
        source = INSTALLER_SOURCE.read_text(encoding="utf-8")
        install_section = source.split('Section "Lumveil" SEC_MAIN', 1)[1].split(
            'Section "Uninstall"', 1)[0]
        self.assertIn('tasklist /FI "IMAGENAME eq ${APP_EXE}"', source)
        self.assertIn("Close Lumveil", source)
        self.assertIn("Abort", source)
        self.assertIn('!define PAYLOAD "dist\\Lumveil"', source)
        self.assertIn('File /r "${PAYLOAD}\\*.*"', install_section)
        self.assertIn("IfErrors stage_failed", install_section)
        self.assertIn('StrCmp $0 "0" _lumveil_process_check_done _lumveil_process_check_failed', source)
        self.assertIn('SetOutPath "$TEMP"', install_section)
        self.assertIn('Rename "$INSTDIR" "$BackupDir"', install_section)
        self.assertIn('Rename "$BackupDir" "$INSTDIR"', install_section)
        self.assertIn("install_failed_restore_failed:", install_section)
        self.assertIn("Its backup remains at: $BackupDir", source)
        self.assertIn("$INSTDIR.previous-$0", install_section)
        self.assertNotIn('RMDir /r "$INSTDIR\\_internal"', install_section)
        self.assertNotIn('RMDir /r "$INSTDIR"', install_section)
        self.assertIn('!define MUI_FINISHPAGE_RUN_TEXT "$(LumveilRunText)"', source)
        langstrings = {}
        for line in source.splitlines():
            match = re.match(r"LangString\s+(\w+)\s+\$\{(LANG_\w+)\}", line)
            if match:
                langstrings.setdefault(match.group(1), set()).add(match.group(2))
            if line.strip().startswith("MessageBox"):
                self.assertIn('"$(Lumveil', line)
        self.assertTrue(langstrings)
        self.assertTrue(all(languages == {"LANG_JAPANESE", "LANG_ENGLISH"}
                            for languages in langstrings.values()))
        self.assertIn("Lumveilが起動中です。", source)

    def test_mock_install_copy_and_rename_failures_preserve_previous_files(self):
        """Mock the installer transaction with temp files; never run the installer."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temp:
            root = Path(temp)
            install = root / "Lumveil"
            stage = root / "Lumveil.stage"
            backup = root / "Lumveil.previous"
            install.mkdir()
            (install / "Lumveil.exe").write_bytes(b"old release")
            (install / "user-note.txt").write_bytes(b"keep")

            # A staging copy error occurs before the prior install is renamed.
            stage.mkdir()
            (stage / "Lumveil.exe").write_bytes(b"partial new release")
            with patch("shutil.copy2", side_effect=OSError("injected copy failure")):
                with self.assertRaises(OSError):
                    shutil.copy2(root / "missing-source", stage / "missing-target")
            shutil.rmtree(stage)
            self.assertEqual((install / "Lumveil.exe").read_bytes(), b"old release")
            self.assertEqual((install / "user-note.txt").read_bytes(), b"keep")
            self.assertFalse(backup.exists())

            # If the final stage rename fails, restore the moved old directory.
            stage.mkdir()
            (stage / "Lumveil.exe").write_bytes(b"new release")
            install.rename(backup)
            with patch.object(Path, "rename", side_effect=OSError("injected rename failure")):
                with self.assertRaises(OSError):
                    stage.rename(install)
            backup.rename(install)
            shutil.rmtree(stage)

            self.assertEqual((install / "Lumveil.exe").read_bytes(), b"old release")
            self.assertEqual((install / "user-note.txt").read_bytes(), b"keep")
            self.assertFalse(stage.exists())
            self.assertFalse(backup.exists())

            # If rollback itself fails, preserve the complete backup for recovery.
            install.rename(backup)
            with patch.object(Path, "rename", side_effect=OSError("injected rollback failure")):
                with self.assertRaises(OSError):
                    backup.rename(install)
            self.assertEqual((backup / "Lumveil.exe").read_bytes(), b"old release")
            self.assertEqual((backup / "user-note.txt").read_bytes(), b"keep")
            self.assertFalse(install.exists())


if __name__ == "__main__":
    unittest.main()
