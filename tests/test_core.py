"""Testes da lógica de core/ (correm em qualquer sistema: python -m unittest discover tests)."""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cleaner, gaming, settings, startup  # noqa: E402
from core.utils import OperationCancelled, format_bytes  # noqa: E402


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)


class CleanerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="safeoti_test_")
        self.root = os.path.join(self.base, "cache", "Temp")
        self.outside = os.path.join(self.base, "importante")
        _write(os.path.join(self.root, "a.tmp"), 100)
        _write(os.path.join(self.root, "sub", "b.tmp"), 200)
        _write(os.path.join(self.root, "sub", "deep", "c.tmp"), 300)
        _write(os.path.join(self.outside, "documento.txt"), 50)
        self.can_symlink = True
        try:
            os.symlink(self.outside, os.path.join(self.root, "link_pasta"), target_is_directory=True)
            os.symlink(os.path.join(self.outside, "documento.txt"), os.path.join(self.root, "link_ficheiro.txt"))
        except (OSError, NotImplementedError):
            self.can_symlink = False
        patcher = mock.patch.object(cleaner, "_targets_for", lambda cid: [cleaner.Target(self.root)])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def test_scan_counts_only_real_files(self) -> None:
        result = cleaner.scan(["user_temp"])["user_temp"]
        self.assertEqual(result.files, 3)
        self.assertEqual(result.bytes, 600)

    def test_clean_deletes_contents_but_not_root_or_links_targets(self) -> None:
        summary = cleaner.clean(["user_temp"])
        self.assertEqual(summary.freed_bytes, 600)
        self.assertEqual(summary.deleted_files, 3)
        self.assertTrue(os.path.isdir(self.root), "a pasta-raiz nunca é apagada")
        self.assertTrue(os.path.exists(os.path.join(self.outside, "documento.txt")), "nunca seguir links")
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub")), "subpastas vazias são removidas")
        if self.can_symlink:
            self.assertTrue(os.path.islink(os.path.join(self.root, "link_pasta")), "links não são apagados")

    def test_cancel_stops_and_reports(self) -> None:
        summary = cleaner.clean(["user_temp"], should_cancel=lambda: True)
        self.assertTrue(summary.cancelled)
        self.assertTrue(os.path.exists(os.path.join(self.root, "a.tmp")))

    def test_scan_cancel_raises(self) -> None:
        with self.assertRaises(OperationCancelled):
            cleaner.scan(["user_temp"], should_cancel=lambda: True)

    def test_root_that_is_symlink_is_rejected(self) -> None:
        if not self.can_symlink:
            self.skipTest("sem suporte para symlinks")
        ok, _ = cleaner.is_safe_root(os.path.join(self.root, "link_pasta"))
        self.assertFalse(ok)

    def test_dangerous_roots_are_rejected(self) -> None:
        self.assertFalse(cleaner.is_safe_root(os.path.expanduser("~"))[0])
        self.assertFalse(cleaner.is_safe_root(os.path.abspath(os.sep))[0])
        self.assertFalse(cleaner.is_safe_root("relativo/pasta")[0])

    def test_is_within(self) -> None:
        self.assertTrue(cleaner.is_within(os.path.join(self.root, "x"), self.root))
        self.assertFalse(cleaner.is_within(self.root, self.root))
        self.assertFalse(cleaner.is_within(self.outside, self.root))
        self.assertFalse(cleaner.is_within(self.root + "_irmao", self.root))

    def test_patterns_only_match_cache_files(self) -> None:
        explorer = os.path.join(self.base, "Local", "Explorer")
        _write(os.path.join(explorer, "thumbcache_256.db"), 10)
        _write(os.path.join(explorer, "IconCache_32.db"), 20)
        _write(os.path.join(explorer, "outro.db"), 40)
        target = cleaner.Target(explorer, patterns=("thumbcache_*.db", "iconcache_*.db"), recursive=False)
        with mock.patch.object(cleaner, "_targets_for", lambda cid: [target]):
            summary = cleaner.clean(["thumbnails"])
        self.assertEqual(summary.freed_bytes, 30)
        self.assertTrue(os.path.exists(os.path.join(explorer, "outro.db")))


class StartupApprovedTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertTrue(startup.parse_approved(None))
        self.assertTrue(startup.parse_approved(bytes([0x02] + [0] * 11)))
        self.assertTrue(startup.parse_approved(bytes([0x06] + [0] * 11)))
        self.assertFalse(startup.parse_approved(bytes([0x03] + [0] * 11)))
        self.assertFalse(startup.parse_approved(bytes([0x07] + [0] * 11)))

    def test_disabled_value_has_filetime(self) -> None:
        value = startup.approved_disabled_value(now=0)
        self.assertEqual(len(value), 12)
        self.assertEqual(value[0], 0x03)
        self.assertEqual(struct.unpack("<Q", value[4:])[0], 11644473600 * 10_000_000)
        self.assertFalse(startup.parse_approved(value))
        self.assertTrue(startup.parse_approved(startup.APPROVED_ENABLED))

    def test_extract_executable(self) -> None:
        self.assertEqual(startup.extract_executable('"C:\\Prog Files\\app.exe" --x'), "C:\\Prog Files\\app.exe")
        self.assertEqual(startup.extract_executable("C:\\Tools\\a.exe -b"), "C:\\Tools\\a.exe")
        self.assertIsNone(startup.extract_executable("   "))


class FakeWinreg:
    """Registo do Windows simulado em memória (só o necessário para core.startup)."""

    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ, KEY_SET_VALUE, KEY_WOW64_64KEY = 1, 2, 4
    REG_SZ, REG_EXPAND_SZ, REG_BINARY = 1, 2, 3

    class _Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self, readonly_hives=()):
        self.data = {}
        self.readonly = set(readonly_hives)

    def put(self, hive, path, name, value, vtype):
        self.data.setdefault((hive, path.lower()), {})[name] = (value, vtype)

    def OpenKey(self, hive, path, reserved=0, access=0):  # noqa: N802
        key = (hive, path.lower())
        if key not in self.data:
            raise FileNotFoundError(path)
        return self._Key(self.data[key])

    def CreateKeyEx(self, hive, path, reserved=0, access=0):  # noqa: N802
        if hive in self.readonly:
            raise PermissionError("Acesso negado")
        return self._Key(self.data.setdefault((hive, path.lower()), {}))

    def EnumValue(self, key, index):  # noqa: N802
        items = list(key.values.items())
        if index >= len(items):
            raise OSError("sem mais valores")
        name, (value, vtype) = items[index]
        return name, value, vtype

    def SetValueEx(self, key, name, reserved, vtype, value):  # noqa: N802
        key.values[name] = (value, vtype)


class StartupRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = FakeWinreg(readonly_hives=("HKLM",))
        self.reg.put("HKCU", startup.RUN_PATH, "Discord", '"C:\\Discord\\Update.exe" --start', FakeWinreg.REG_SZ)
        self.reg.put("HKLM", startup.RUN_PATH_WOW, "Driver", "C:\\x\\drv.exe", FakeWinreg.REG_SZ)
        self.reg.put("HKCU", startup.APPROVED_BASE + "\\Run", "Discord", bytes([3] + [0] * 11), FakeWinreg.REG_BINARY)
        self.tmp = tempfile.mkdtemp(prefix="safeoti_startup_")
        folder = os.path.join(self.tmp, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        _write(os.path.join(folder, "Spotify.lnk"), 1)
        _write(os.path.join(folder, "desktop.ini"), 1)
        for patcher in (
            mock.patch.object(startup, "winreg", self.reg),
            mock.patch.object(startup, "IS_WINDOWS", True),
            mock.patch.dict(os.environ, {"APPDATA": self.tmp, "ProgramData": os.path.join(self.tmp, "nada")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entries(self):
        return {e.display_name: e for e in startup.list_entries()}

    def test_lists_registry_and_folder_entries(self) -> None:
        entries = self._entries()
        self.assertEqual(set(entries), {"Discord", "Driver", "Spotify"})
        self.assertFalse(entries["Discord"].enabled)
        self.assertTrue(entries["Driver"].enabled)
        self.assertEqual(entries["Driver"].source.approved_subkey, "Run32")
        self.assertTrue(entries["Driver"].requires_admin)
        self.assertEqual(entries["Spotify"].source.approved_subkey, "StartupFolder")

    def test_enable_disable_is_reversible_and_keeps_entry(self) -> None:
        entry = self._entries()["Discord"]
        startup.set_enabled(entry, True)
        self.assertTrue(self._entries()["Discord"].enabled)
        startup.set_enabled(self._entries()["Spotify"], False)
        self.assertFalse(self._entries()["Spotify"].enabled)
        # A entrada original continua na chave Run.
        run_values = self.reg.data[("HKCU", startup.RUN_PATH.lower())]
        self.assertIn("Discord", run_values)

    def test_hklm_without_admin_raises_permission_error(self) -> None:
        with self.assertRaises(startup.StartupPermissionError):
            startup.set_enabled(self._entries()["Driver"], False)


class GamingTests(unittest.TestCase):
    OUTPUT = """
Existing Power Schemes (* Active)
-----------------------------------
Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced) *
Power Scheme GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (High performance)
Power Scheme GUID: a1841308-3541-4fab-bc81-f71556f20b4a  (Power saver)
"""

    def test_parse_schemes(self) -> None:
        schemes = gaming.parse_schemes(self.OUTPUT)
        self.assertEqual(len(schemes), 3)
        self.assertTrue(schemes[0].active)
        self.assertEqual(schemes[0].name, "Balanced")

    def test_prefers_ultimate(self) -> None:
        schemes = gaming.parse_schemes(
            self.OUTPUT + "Power Scheme GUID: 11111111-2222-3333-4444-555555555555  (Desempenho máximo)\n"
        )
        self.assertEqual(gaming.choose_performance_scheme(schemes).name, "Desempenho máximo")

    def test_falls_back_to_high(self) -> None:
        chosen = gaming.choose_performance_scheme(gaming.parse_schemes(self.OUTPUT))
        self.assertEqual(chosen.guid, gaming.GUID_HIGH_PERFORMANCE)

    def test_none_when_missing(self) -> None:
        only_balanced = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Equilibrado) *"
        self.assertIsNone(gaming.choose_performance_scheme(gaming.parse_schemes(only_balanced)))


class GamingFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="safeoti_gaming_")
        self.calls = []
        self.active = gaming.GUID_BALANCED

        def fake_run(args, timeout=60, encoding=None):
            from core.utils import CommandResult

            self.calls.append(list(args))
            if args[1] == "/getactivescheme":
                return CommandResult(0, f"Power Scheme GUID: {self.active}  (Equilibrado)", "")
            if args[1] == "/list":
                return CommandResult(0, GamingTests.OUTPUT, "")
            if args[1] == "/setactive":
                self.active = args[2]
                return CommandResult(0, "", "")
            return CommandResult(1, "", "?")

        for patcher in (
            mock.patch.dict(os.environ, {"APPDATA": self.tmp}),
            mock.patch.object(gaming, "IS_WINDOWS", True),
            mock.patch.object(gaming, "run_hidden", fake_run),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_activate_then_deactivate_restores_plan(self) -> None:
        report = gaming.activate(gaming.GamingOptions(close_apps=False))
        self.assertEqual(self.active, gaming.GUID_HIGH_PERFORMANCE)
        self.assertTrue(gaming.is_active())
        self.assertTrue(any("Plano de energia" in c for c in report.changes))
        with self.assertRaises(gaming.GamingError):
            gaming.activate(gaming.GamingOptions(close_apps=False))
        gaming.deactivate()
        self.assertEqual(self.active, gaming.GUID_BALANCED)
        self.assertFalse(gaming.is_active())


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="safeoti_cfg_")
        patcher = mock.patch.dict(os.environ, {"APPDATA": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_validation(self) -> None:
        s = settings.Settings(gaming_close_apps=["discord", "Discord.exe", " C:\\x\\Spotify.exe "],
                              monitor_interval_ms=50, start_with_windows=True)
        settings.save_settings(s)
        loaded = settings.load_settings()
        self.assertEqual(loaded.gaming_close_apps, ["discord.exe", "Spotify.exe"])
        self.assertEqual(loaded.monitor_interval_ms, settings.MIN_MONITOR_INTERVAL_MS)
        self.assertTrue(loaded.start_with_windows)

    def test_corrupted_file_falls_back_to_defaults(self) -> None:
        path = settings.settings_path()
        path.write_text("{ isto não é json", encoding="utf-8")
        loaded = settings.load_settings()
        self.assertEqual(loaded.gaming_close_apps, settings.DEFAULT_GAMING_APPS)


class UtilsTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1536), "1,5 KB")
        self.assertEqual(format_bytes(5 * 1024 ** 3), "5,0 GB")


if __name__ == "__main__":
    unittest.main()
