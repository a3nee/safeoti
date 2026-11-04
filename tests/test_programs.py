"""Testes da lógica pura de core/programs.py (sem registo nem desinstalações)."""

import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import programs  # noqa: E402
from core.programs import Program  # noqa: E402


def _values(**extra):
    base = {"DisplayName": "App", "UninstallString": r"C:\App\uninst.exe"}
    base.update(extra)
    return base


class SplitCommandTests(unittest.TestCase):
    def test_quoted_path(self):
        self.assertEqual(
            programs.split_command(r'"C:\Program Files\App\unins000.exe" /SILENT'),
            (r"C:\Program Files\App\unins000.exe", "/SILENT"),
        )

    def test_quoted_without_args(self):
        self.assertEqual(programs.split_command(r'"C:\Program Files\App\u.exe"'), (r"C:\Program Files\App\u.exe", ""))

    def test_unquoted_with_spaces_existing_file(self):
        exe = r"C:\Program Files\My App\uninstall.exe"
        result = programs.split_command(exe + " /S --x", exists=lambda p: p == exe)
        self.assertEqual(result, (exe, "/S --x"))

    def test_unquoted_with_spaces_missing_file_cuts_at_exe(self):
        self.assertEqual(
            programs.split_command(r"C:\Program Files\My App\uninstall.exe /S", exists=lambda p: False),
            (r"C:\Program Files\My App\uninstall.exe", "/S"),
        )

    def test_unquoted_without_extension(self):
        exe = r"C:\Tools\My Tool\remove"
        result = programs.split_command(exe + " now", exists=lambda p: p == exe + ".exe")
        self.assertEqual(result, (exe + ".exe", "now"))

    def test_rundll32(self):
        exe, args = programs.split_command(
            r"rundll32.exe dfshim.dll,ShArpMaintain App.application, Culture=neutral", exists=lambda p: False)
        self.assertEqual(exe, "rundll32.exe")
        self.assertEqual(args, "dfshim.dll,ShArpMaintain App.application, Culture=neutral")

    def test_rundll32_full_path(self):
        exe, args = programs.split_command(
            r"C:\Windows\System32\rundll32.exe C:\Program Files\X\x.dll,Uninstall", exists=lambda p: False)
        self.assertEqual(exe, r"C:\Windows\System32\rundll32.exe")
        self.assertEqual(args, r"C:\Program Files\X\x.dll,Uninstall")

    def test_empty(self):
        self.assertEqual(programs.split_command("  "), ("", ""))


class CommandLineTests(unittest.TestCase):
    def test_msi_uses_interactive_msiexec(self):
        prog = Program("App", uninstall_string="MsiExec.exe /I{12345678-ABCD-ABCD-ABCD-1234567890AB}",
                       key_name="{12345678-abcd-abcd-abcd-1234567890ab}")
        self.assertEqual(programs.uninstall_command_line(prog), "msiexec.exe /x {12345678-ABCD-ABCD-ABCD-1234567890AB}")

    def test_windows_installer_flag(self):
        prog = Program("App", uninstall_string="whatever", windows_installer=True,
                       key_name="{12345678-ABCD-ABCD-ABCD-1234567890AB}")
        self.assertTrue(prog.is_msi)
        self.assertIn("/x {12345678", programs.uninstall_command_line(prog))

    def test_exe_with_spaces_is_quoted(self):
        prog = Program("App", uninstall_string=r'"C:\Program Files\App\u.exe" /a')
        self.assertEqual(programs.uninstall_command_line(prog, exists=lambda p: False), r'"C:\Program Files\App\u.exe" /a')

    def test_quiet_string_never_used(self):
        prog = Program("App", uninstall_string=r"C:\App\u.exe", quiet_uninstall_string=r"C:\App\u.exe /S")
        self.assertNotIn("/S", programs.uninstall_command_line(prog, exists=lambda p: False))


class FilterTests(unittest.TestCase):
    def test_visible(self):
        self.assertFalse(programs.is_hidden_entry(_values()))

    def test_missing_name_or_uninstall(self):
        self.assertTrue(programs.is_hidden_entry({"UninstallString": "x"}))
        self.assertTrue(programs.is_hidden_entry({"DisplayName": "App"}))

    def test_system_component(self):
        self.assertTrue(programs.is_hidden_entry(_values(SystemComponent=1)))

    def test_updates(self):
        self.assertTrue(programs.is_hidden_entry(_values(ParentKeyName="OperatingSystem")))
        for kind in ("Update", "Hotfix", "Security Update"):
            self.assertTrue(programs.is_hidden_entry(_values(ReleaseType=kind)), kind)
        self.assertTrue(programs.is_hidden_entry(_values(DisplayName="Security Update for X (KB2565063)")))
        self.assertTrue(programs.is_hidden_entry(_values(), key_name="KB4023057"))

    def test_program_from_values(self):
        prog = programs.program_from_values(
            _values(DisplayVersion="1.2", Publisher="ACME", InstallDate="20240131", EstimatedSize=2048,
                    WindowsInstaller=1, InstallLocation='"C:\\App"'),
            "HKLM", r"HKLM\X\App", "App")
        self.assertEqual(prog.install_date, date(2024, 1, 31))
        self.assertEqual(prog.size_bytes, 2048 * 1024)
        self.assertTrue(prog.windows_installer)
        self.assertTrue(prog.requires_admin)
        self.assertEqual(prog.install_location, r"C:\App")
        self.assertIsNone(programs.program_from_values(_values(SystemComponent=1), "HKLM", "", ""))


class DedupeTests(unittest.TestCase):
    def test_same_name_version_keeps_richest(self):
        poor = Program("App", "1.0", hive="HKLM", key_path="a")
        rich = Program("app ", "1.0", publisher="ACME", size_bytes=10, hive="HKLM", key_path="b")
        other = Program("App", "2.0", key_path="c")
        result = programs.dedupe([poor, rich, other])
        self.assertEqual([p.key_path for p in result], ["b", "c"])

    def test_keeps_first_on_tie(self):
        a, b = Program("App", "1", key_path="a"), Program("App", "1", key_path="b")
        self.assertEqual([p.key_path for p in programs.dedupe([a, b])], ["a"])


class DateTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(programs.parse_install_date("20231105"), date(2023, 11, 5))
        self.assertEqual(programs.parse_install_date(20231105), date(2023, 11, 5))
        self.assertEqual(programs.parse_install_date("2023-11-05"), date(2023, 11, 5))
        self.assertEqual(programs.parse_install_date("11/5/2023"), date(2023, 11, 5))
        self.assertEqual(programs.parse_install_date("25/12/2023"), date(2023, 12, 25))

    def test_invalid(self):
        for value in (None, "", "2023", "20231340", "abc", "19000101", "99991231"):
            self.assertIsNone(programs.parse_install_date(value), value)


class StoreTests(unittest.TestCase):
    def test_parse_single_and_list(self):
        one = '{"Name":"A.B","PackageFullName":"A.B_1_x64__x","Version":"1.0","Publisher":"CN=X, O=Contoso, C=US"}'
        apps = programs.parse_appx_json(one)
        self.assertEqual(len(apps), 1)
        self.assertTrue(apps[0].is_store)
        self.assertEqual(apps[0].publisher, "Contoso")
        self.assertFalse(apps[0].requires_admin)
        self.assertEqual(len(programs.parse_appx_json("[" + one + "," + one + "]")), 2)
        self.assertEqual(programs.parse_appx_json(""), [])

    def test_guid_publisher_hidden(self):
        self.assertEqual(programs.publisher_from_dn("CN=58D26209-1D57-482C-B403-B655571B5C7B"), "")

    def test_resource_candidates(self):
        cands = programs.resource_candidates("ms-resource:AppName", "P_1_x64__h", "P")
        self.assertEqual(cands[0], "@{P_1_x64__h?ms-resource://P/resources/AppName}")
        self.assertEqual(programs.resource_candidates("Plain", "P", "P"), [])


class SafetyTests(unittest.TestCase):
    def test_self_uninstall_blocked(self):
        with self.assertRaises(programs.SelfUninstallError):
            programs.uninstall(Program("SafeOti 1.0", uninstall_string=r"C:\SafeOti\unins000.exe"))

    def test_hklm_requires_admin(self):
        prog = Program("App", uninstall_string=r"C:\App\u.exe", hive="HKLM")
        with mock.patch.object(programs.admin, "is_admin", return_value=False), \
                mock.patch.object(programs.subprocess, "Popen") as popen:
            with self.assertRaises(programs.ProgramsPermissionError):
                programs.uninstall(prog)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
