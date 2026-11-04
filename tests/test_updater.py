"""Testes do core.updater (sem rede: urllib é simulado)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from core import updater
from core.updater import ReleaseAsset, UpdateError, parse_version
from core.utils import OperationCancelled


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str = "https://github.com/x", headers=None):
        super().__init__(data)
        self._url = url
        self.headers = headers or {"Content-Length": str(len(data))}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _release_json(**over):
    data = {
        "tag_name": "v1.2.0",
        "name": "SafeOti 1.2.0",
        "body": "## Novidades\n- Coisa\n\nSHA256: " + "a" * 64,
        "html_url": "https://github.com/a3nee/safeoti/releases/tag/v1.2.0",
        "published_at": "2026-11-01T10:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "SafeOti.exe", "browser_download_url": "https://github.com/a3nee/safeoti/releases/download/v1.2.0/SafeOti.exe", "size": 10},
            {"name": "SafeOti-Setup.exe", "browser_download_url": "https://github.com/a3nee/safeoti/releases/download/v1.2.0/SafeOti-Setup.exe", "size": 20},
        ],
    }
    data.update(over)
    return data


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_version("1.2.3").numbers, (1, 2, 3))
        self.assertEqual(parse_version("1.2").numbers, (1, 2))
        v = parse_version("v1.2.3-beta")
        self.assertEqual(v.numbers, (1, 2, 3))
        self.assertTrue(v.is_prerelease)
        self.assertEqual(str(v), "1.2.3-beta")
        self.assertIsNone(parse_version("abc"))
        self.assertIsNone(parse_version(""))
        self.assertFalse(parse_version("1.0.0+build5").is_prerelease)

    def test_compare(self):
        self.assertEqual(parse_version("1.2"), parse_version("1.2.0"))
        self.assertLess(parse_version("1.2.3-beta"), parse_version("1.2.3"))
        self.assertLess(parse_version("1.2.3-alpha"), parse_version("1.2.3-beta"))
        self.assertLess(parse_version("1.2.3-beta.2"), parse_version("1.2.3-beta.10"))
        self.assertLess(parse_version("1.9.9"), parse_version("1.10.0"))
        self.assertGreater(parse_version("2.0"), parse_version("1.99.99"))

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(updater.is_newer("v1.1", "1.0.0"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("1.0", "1.0.0"))
        self.assertFalse(updater.is_newer("1.0.0-rc1", "1.0.0"))
        self.assertTrue(updater.is_newer("1.0.0", "1.0.0-rc1"))
        self.assertFalse(updater.is_newer("lixo", "1.0.0"))


class AssetAndShaTests(unittest.TestCase):
    def test_select_asset(self):
        assets = [ReleaseAsset("SafeOti.exe", "u1"), ReleaseAsset("notes.txt", "u0"),
                  ReleaseAsset("SafeOti-Setup-1.2.exe", "u2")]
        self.assertEqual(updater.select_asset(assets).name, "SafeOti-Setup-1.2.exe")
        self.assertEqual(updater.select_asset(assets[:2]).name, "SafeOti.exe")
        self.assertIsNone(updater.select_asset([ReleaseAsset("source.zip", "u")]))

    def test_select_sha_asset(self):
        target = ReleaseAsset("SafeOti-Setup.exe", "u")
        assets = [target, ReleaseAsset("SafeOti.exe.sha256", "a"), ReleaseAsset("SafeOti-Setup.exe.sha256", "b")]
        self.assertEqual(updater.select_sha256_asset(assets, target).url, "b")
        self.assertIsNone(updater.select_sha256_asset([target], target))

    def test_parse_sha_text(self):
        h1, h2 = "a" * 64, "B" * 64
        self.assertEqual(updater.parse_sha256_text(h1), h1)
        text = f"{h1}  SafeOti.exe\n{h2} *SafeOti-Setup.exe\n"
        self.assertEqual(updater.parse_sha256_text(text, "SafeOti-Setup.exe"), h2.lower())
        self.assertEqual(updater.parse_sha256_text("nada"), "")

    def test_parse_sha_body(self):
        h1, h2 = "1" * 64, "2" * 64
        body = f"Notas\nSHA256: {h1}\nSHA-256 (SafeOti-Setup.exe): `{h2}`\n"
        self.assertEqual(updater.parse_sha256_from_body(body), h1)
        self.assertEqual(updater.parse_sha256_from_body(body, "SafeOti-Setup.exe"), h2)
        self.assertEqual(updater.parse_sha256_from_body("sem hash"), "")


class HostTests(unittest.TestCase):
    def test_allowed(self):
        ok = updater.is_allowed_download_url
        self.assertTrue(ok("https://github.com/a3nee/safeoti/releases/download/v1/SafeOti-Setup.exe"))
        self.assertTrue(ok("https://objects.githubusercontent.com/abc"))
        self.assertFalse(ok("http://github.com/a"))
        self.assertFalse(ok("https://github.com.evil.com/a"))
        self.assertFalse(ok("https://evil.com/github.com"))
        self.assertFalse(ok("https://user:pw@github.com/a"))
        self.assertFalse(ok("https://github.com:8443/a"))
        self.assertFalse(ok("file:///C:/x.exe"))
        self.assertFalse(ok(""))


class ReleaseParsingTests(unittest.TestCase):
    def test_parse_release(self):
        rel = updater.parse_release(_release_json())
        self.assertEqual(rel.version, "1.2.0")
        self.assertEqual(rel.asset.name, "SafeOti-Setup.exe")
        self.assertEqual(rel.asset.size, 20)
        self.assertEqual(rel.sha256, "a" * 64)
        self.assertTrue(rel.is_installer)
        self.assertEqual(rel.published_at[:10], "2026-11-01")

    def _urlopen(self, data):
        return mock.patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps(data).encode()))

    def test_check_latest_update(self):
        with self._urlopen(_release_json()) as m:
            result = updater.check_latest(current="1.0.0")
        self.assertEqual(result.status, updater.STATUS_UPDATE)
        req = m.call_args[0][0]
        self.assertEqual(req.get_header("Accept"), "application/vnd.github+json")
        self.assertTrue(req.get_header("User-agent").startswith("SafeOti-Updater/"))
        self.assertTrue(updater.should_notify(result, ""))
        self.assertFalse(updater.should_notify(result, "1.2.0"))
        self.assertTrue(updater.should_notify(result, "1.1.0"))
        self.assertTrue(updater.should_notify(result, "1.2.0", manual=True))

    def test_check_latest_up_to_date(self):
        with self._urlopen(_release_json()):
            result = updater.check_latest(current="1.2.0")
        self.assertEqual(result.status, updater.STATUS_UP_TO_DATE)
        self.assertFalse(updater.should_notify(result, "", manual=True))

    def test_prerelease_and_draft_skipped(self):
        for over in ({"prerelease": True}, {"draft": True}, {"tag_name": "v9.0.0-beta"}):
            with self._urlopen(_release_json(**over)):
                result = updater.check_latest(current="1.0.0")
            self.assertFalse(result.update_available, over)

    def test_404_means_no_releases(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            result = updater.check_latest()
        self.assertEqual(result.status, updater.STATUS_NO_RELEASES)

    def test_network_errors(self):
        for exc in (urllib.error.URLError("offline"), TimeoutError(),
                    urllib.error.HTTPError("u", 403, "rate", {}, None),
                    urllib.error.HTTPError("u", 500, "x", {}, None)):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(UpdateError):
                    updater.check_latest()

    def test_bad_json(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"<html>")):
            with self.assertRaises(UpdateError):
                updater.check_latest()


class DownloadTests(unittest.TestCase):
    URL = "https://github.com/a3nee/safeoti/releases/download/v1.2.0/SafeOti-Setup.exe"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self.tmp.name, "SafeOti-Setup.exe")

    def tearDown(self):
        self.tmp.cleanup()

    def _opener(self, data: bytes, url: str = URL):
        opener = mock.Mock()
        opener.open.return_value = _FakeResponse(data, url)
        return mock.patch("urllib.request.build_opener", return_value=opener)

    def test_ok_with_sha(self):
        data = b"x" * 200000
        sha = hashlib.sha256(data).hexdigest()
        with self._opener(data):
            path = updater.download_asset(self.URL, self.dest, expected_size=len(data), expected_sha256=sha)
        self.assertEqual(path, self.dest)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_sha_mismatch_deletes(self):
        with self._opener(b"abc"):
            with self.assertRaises(UpdateError):
                updater.download_asset(self.URL, self.dest, expected_size=3, expected_sha256="0" * 64)
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_size_mismatch(self):
        with self._opener(b"abc"):
            with self.assertRaises(UpdateError):
                updater.download_asset(self.URL, self.dest, expected_size=10)
        self.assertFalse(os.path.exists(self.dest))

    def test_rejects_untrusted_url_and_redirect(self):
        with self.assertRaises(UpdateError):
            updater.download_asset("http://evil.com/x.exe", self.dest)
        with self._opener(b"abc", url="https://evil.com/x.exe"):
            with self.assertRaises(UpdateError):
                updater.download_asset(self.URL, self.dest)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_cancel(self):
        with self._opener(b"abc"):
            with self.assertRaises(OperationCancelled):
                updater.download_asset(self.URL, self.dest, should_cancel=lambda: True)
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_download_release_uses_sha_file(self):
        data = b"setup-bytes"
        sha = hashlib.sha256(data).hexdigest()
        rel = updater.parse_release(_release_json(body="", assets=[
            {"name": "SafeOti-Setup.exe", "browser_download_url": self.URL, "size": len(data)},
            {"name": "SafeOti-Setup.exe.sha256", "browser_download_url": self.URL + ".sha256", "size": 80},
        ]))
        opener = mock.Mock()
        opener.open.side_effect = [
            _FakeResponse(f"{sha}  SafeOti-Setup.exe\n".encode(), self.URL + ".sha256"),
            _FakeResponse(data, self.URL),
        ]
        with mock.patch("urllib.request.build_opener", return_value=opener):
            path = updater.download_release(rel, dest_dir=self.tmp.name)
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
