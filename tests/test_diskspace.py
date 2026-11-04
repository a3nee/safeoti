"""Testes de core/diskspace.py (sem Qt)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import diskspace  # noqa: E402
from core.utils import OperationCancelled  # noqa: E402


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)


class DiskSpaceScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ds_test_")
        self.root = os.path.join(self.tmp, "root")
        _write(os.path.join(self.root, "a.bin"), 100)
        _write(os.path.join(self.root, "sub1", "b.bin"), 1000)
        _write(os.path.join(self.root, "sub1", "c.bin"), 10)
        _write(os.path.join(self.root, "sub1", "deep", "d.bin"), 5000)
        _write(os.path.join(self.root, "sub2", "e.bin"), 300)
        os.makedirs(os.path.join(self.root, "empty"))
        self.outside = os.path.join(self.tmp, "outside")
        _write(os.path.join(self.outside, "big.bin"), 100_000)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sizes_aggregate(self) -> None:
        result = diskspace.scan(self.root)
        root = result.root
        self.assertEqual(root.size, 6410)
        self.assertEqual(root.file_count, 5)
        self.assertEqual(root.own_size, 100)
        self.assertEqual(root.own_files, 1)
        self.assertEqual(result.dir_count, 5)
        children = {c.name: c for c in root.children}
        self.assertEqual(set(children), {"sub1", "sub2", "empty"})
        self.assertEqual(children["sub1"].size, 6010)
        self.assertEqual(children["sub1"].file_count, 3)
        self.assertEqual(children["sub1"].own_size, 1010)
        self.assertEqual(children["sub2"].size, 300)
        self.assertEqual(children["empty"].size, 0)
        self.assertEqual([c.name for c in root.sorted_children()], ["sub1", "sub2", "empty"])
        deep = children["sub1"].children[0]
        self.assertEqual(deep.path, os.path.join(self.root, "sub1", "deep"))
        self.assertIs(result.find_dir(deep.path), deep)
        self.assertIsNone(result.find_dir(self.outside))

    def test_largest_files(self) -> None:
        result = diskspace.scan(self.root, top_n=3)
        self.assertEqual([f.size for f in result.largest], [5000, 1000, 300])
        self.assertEqual(result.largest[0].name, "d.bin")
        self.assertEqual(result.largest[0].folder, os.path.join(self.root, "sub1", "deep"))

    def test_progress_and_cancel(self) -> None:
        calls = []
        diskspace.scan(self.root, progress=lambda pct, msg: calls.append((pct, msg)))
        self.assertTrue(calls)
        self.assertEqual(calls[-1][0], 100)
        with self.assertRaises(OperationCancelled):
            diskspace.scan(self.root, should_cancel=lambda: True)

    def test_remove_updates_tree(self) -> None:
        result = diskspace.scan(self.root)
        sub1 = result.find_dir(os.path.join(self.root, "sub1"))
        result.remove_dir(sub1)
        self.assertEqual(result.root.size, 400)
        self.assertEqual(result.root.file_count, 2)
        self.assertNotIn("d.bin", [f.name for f in result.largest])
        result.remove_file(os.path.join(self.root, "sub2", "e.bin"), 300)
        self.assertEqual(result.root.size, 100)
        self.assertEqual(result.find_dir(os.path.join(self.root, "sub2")).size, 0)

    def test_missing_root(self) -> None:
        with self.assertRaises(OSError):
            diskspace.scan(os.path.join(self.tmp, "nao_existe"))

    def test_symlink_not_followed(self) -> None:
        link = os.path.join(self.root, "link_dir")
        try:
            os.symlink(self.outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("sem permissão para criar symlinks")
        result = diskspace.scan(self.root)
        self.assertEqual(result.root.size, 6410)
        self.assertNotIn("link_dir", [c.name for c in result.root.children])
        self.assertGreaterEqual(result.links_skipped, 1)

    @unittest.skipUnless(sys.platform == "win32", "junctions só existem no Windows")
    def test_junction_not_followed(self) -> None:
        try:
            import _winapi

            _winapi.CreateJunction(self.outside, os.path.join(self.root, "junction"))
        except (ImportError, AttributeError, OSError):
            self.skipTest("não foi possível criar uma junction")
        result = diskspace.scan(self.root)
        self.assertEqual(result.root.size, 6410)
        self.assertNotIn("junction", [c.name for c in result.root.children])
        self.assertGreaterEqual(result.links_skipped, 1)
        self.assertIsNotNone(diskspace.protected_reason(os.path.join(self.root, "junction")))
        os.rmdir(os.path.join(self.root, "junction"))


class ProtectedPathTests(unittest.TestCase):
    def test_protected_locations(self) -> None:
        self.assertIsNotNone(diskspace.protected_reason("relative\\path"))
        self.assertIsNotNone(diskspace.protected_reason(os.path.expanduser("~")))
        drive = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
        self.assertIsNotNone(diskspace.protected_reason(drive))
        windir = os.environ.get("SystemRoot")
        if windir:
            self.assertIsNotNone(diskspace.protected_reason(windir))
            self.assertIsNotNone(diskspace.protected_reason(os.path.join(windir, "notepad.exe")))
        program_files = os.environ.get("ProgramFiles")
        if program_files and os.path.isdir(program_files):
            self.assertIsNotNone(diskspace.protected_reason(program_files))
        users = os.path.dirname(os.path.expanduser("~"))
        self.assertIsNotNone(diskspace.protected_reason(users))

    def test_ordinary_temp_folder_allowed(self) -> None:
        tmp = tempfile.mkdtemp(prefix="ds_prot_")
        try:
            path = os.path.join(tmp, "x.txt")
            _write(path, 10)
            self.assertIsNone(diskspace.protected_reason(path))
            self.assertIsNone(diskspace.protected_reason(tmp))
            self.assertIsNotNone(diskspace.protected_reason(os.path.join(tmp, "nao_existe")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DriveTests(unittest.TestCase):
    def test_list_drives(self) -> None:
        drives = diskspace.list_drives()
        for drive in drives:
            self.assertGreaterEqual(drive.total, drive.used)
            self.assertTrue(drive.mountpoint)


if __name__ == "__main__":
    unittest.main()
