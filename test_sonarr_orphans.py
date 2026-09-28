"""Offline regression tests for scanning, Sonarr responses, and CSV reports."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import csv
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch


loader = importlib.machinery.SourceFileLoader("sonarr_orphans", str(Path(__file__).with_name("sonarr-orphans")))
spec = importlib.util.spec_from_loader(loader.name, loader)
scanner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scanner
loader.exec_module(scanner)


class UnicodePathTests(unittest.TestCase):
    def test_smb_unicode_spelling_does_not_require_sonarr_path_to_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracked = set()
            for title in ("S02E11 - Jeu Monégasque", "S08E08 - Auflösung"):
                tracked.add(root / (title + ".mkv"))
                (root / unicodedata.normalize("NFD", title + ".mkv")).write_bytes(b"video")
            orphan = root / "S08E08 - Auflösung - duplicate.mkv"
            orphan.write_bytes(b"duplicate")
            # Reproduce an SMB mount that cannot resolve Sonarr's NFC spelling.
            with patch.object(scanner.sys, "platform", "darwin"), patch.object(
                scanner.os.path, "samefile", side_effect=FileNotFoundError
            ):
                findings, scanned, videos, sidecars = scanner.scan_library(root, tracked, [])
            self.assertEqual([finding.path for finding in findings], [orphan])
            self.assertEqual((scanned, videos, sidecars), (3, 1, 0))

    def test_case_only_difference_still_requires_filesystem_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Archer.mkv"
            video.write_bytes(b"video")
            with patch.object(scanner.sys, "platform", "darwin"), patch.object(
                scanner.os.path, "samefile", return_value=False
            ):
                findings, _, _, _ = scanner.scan_library(root, {root / "archer.mkv"}, [])
            self.assertEqual([finding.path for finding in findings], [video])

    def test_case_only_difference_is_tracked_when_filesystem_confirms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Archer.mkv").write_bytes(b"video")
            with patch.object(scanner.sys, "platform", "darwin"), patch.object(
                scanner.os.path, "samefile", return_value=True
            ):
                findings, _, videos, _ = scanner.scan_library(root, {root / "archer.mkv"}, [])
            self.assertEqual(findings, [])
            self.assertEqual(videos, 0)

    def test_unresolvable_case_only_path_remains_an_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Archer.mkv"
            video.write_bytes(b"video")
            with patch.object(scanner.sys, "platform", "darwin"), patch.object(
                scanner.os.path, "samefile", side_effect=FileNotFoundError
            ):
                findings, _, _, _ = scanner.scan_library(root, {root / "archer.mkv"}, [])
            self.assertEqual([finding.path for finding in findings], [video])

    def test_linux_keeps_distinct_unicode_spellings_separate(self):
        root = Path("/library")
        composed = "Auflösung.mkv"
        decomposed = unicodedata.normalize("NFD", composed)
        # Supply directory entries explicitly so the host filesystem's Unicode
        # behavior cannot affect this simulated Linux scan.
        with patch.object(scanner.sys, "platform", "linux"), patch.object(
            scanner.os, "walk", return_value=[(str(root), [], [decomposed])]
        ), patch.object(Path, "stat") as stat:
            stat.return_value.st_size = 5
            findings, _, _, _ = scanner.scan_library(root, {root / composed}, [])
        self.assertEqual([finding.path for finding in findings], [root / decomposed])


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def create(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        return path

    def test_mixed_library_reports_only_untracked_videos_and_unmatched_sidecars(self):
        series = self.root / "Archer"
        tracked = self.create("Archer/Season 02/episode.MKV")
        duplicate = self.create("Archer/Season 02/episode - duplicate.mp4")
        for name in ("episode.nfo", "episode.en.srt", "episode.EN-us.forced.SDH.SRT",
                     "episode - duplicate.en.srt", "TVSHOW.NFO", "season.nfo", "cover.jpg"):
            self.create("Archer/Season 02/" + name)
        missing_subtitle = self.create("Archer/Season 02/missing.nl.srt")
        missing_nfo = self.create("Archer/Season 02/missing.nfo")
        findings, scanned, videos, sidecars = scanner.scan_library(
            self.root, {tracked}, [(series, "Archer (2009)")]
        )
        self.assertEqual([finding.path for finding in findings], sorted([duplicate, missing_subtitle, missing_nfo]))
        self.assertEqual((scanned, videos, sidecars), (11, 1, 2))
        self.assertEqual({finding.series for finding in findings}, {"Archer (2009)"})
        self.assertEqual({finding.file_type for finding in findings}, {"video", "subtitle", "nfo"})
        self.assertTrue(all(finding.size_bytes == 7 for finding in findings))

    def test_subtitle_cannot_match_a_video_in_another_season(self):
        video = self.create("Archer/Season 01/episode.mkv")
        subtitle = self.create("Archer/Season 02/episode.en.srt")
        findings, _, _, _ = scanner.scan_library(self.root, {video}, [])
        self.assertEqual([finding.path for finding in findings], [subtitle])

    def test_unknown_subtitle_suffix_is_not_stripped(self):
        video = self.create("episode.mkv")
        subtitle = self.create("episode.commentary.srt")
        findings, _, _, _ = scanner.scan_library(self.root, {video}, [])
        self.assertEqual([finding.path for finding in findings], [subtitle])

    def test_literal_subtitle_stem_takes_precedence_over_language_tags(self):
        video = self.create("episode.en.mkv")
        self.create("episode.en.srt")
        findings, _, _, _ = scanner.scan_library(self.root, {video}, [])
        self.assertEqual(findings, [])

    def test_unreadable_file_aborts_scan(self):
        self.create("episode.mkv")
        with patch.object(Path, "stat", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(scanner.ScanError, "cannot read"):
                scanner.scan_library(self.root, set(), [])

    def test_unreadable_directory_aborts_scan(self):
        def denied_walk(root, onerror):
            onerror(PermissionError("denied"))
            return iter(())

        with patch.object(scanner.os, "walk", side_effect=denied_walk):
            with self.assertRaisesRegex(scanner.ScanError, "cannot scan library"):
                scanner.scan_library(self.root, set(), [])


class SonarrTests(unittest.TestCase):
    def test_path_mapping_preserves_series_and_season(self):
        actual = scanner.map_sonarr_path_to_local(
            "/data/Shows/Archer/Season 02/../Season 02/episode.mkv",
            Path("/data/Shows"), Path("/library")
        )
        self.assertEqual(actual, Path("/library/Archer/Season 02/episode.mkv"))

    def test_path_mapping_rejects_missing_relative_and_outside_paths(self):
        for path in (None, 42, "Archer/episode.mkv", "/data/Shows-other/episode.mkv",
                     "/data/Shows/../Other/episode.mkv"):
            with self.subTest(path=path), self.assertRaises(scanner.ScanError):
                scanner.map_sonarr_path_to_local(path, Path("/data/Shows"), Path("/library"))

    def test_api_uses_get_and_sends_key_in_header(self):
        with patch.object(scanner, "urlopen", return_value=io.BytesIO(b'[{"id": 59}]')) as open_url:
            result = scanner.api_get("https://sonarr.example/", "test-key", "episodefile", {"seriesId": 59})
        self.assertEqual(result, [{"id": 59}])
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://sonarr.example/api/v3/episodefile?seriesId=59")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-api-key"), "test-key")

    def test_api_rejects_invalid_json_and_unexpected_shapes(self):
        for payload in (b"not JSON", b"{}", b"null", b"[1]", b'[{}, "bad"]'):
            with self.subTest(payload=payload), patch.object(scanner, "urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaises(scanner.ScanError):
                    scanner.api_get("https://sonarr.example", "test-key", "series")

    def test_api_errors_explain_failure(self):
        cases = [
            (HTTPError("https://sonarr.example", code, "error", {}, None), "authentication failed")
            for code in (401, 403)
        ] + [
            (HTTPError("https://sonarr.example", 500, "error", {}, None), "HTTP 500"),
            (URLError("offline"), "cannot reach Sonarr"),
            (TimeoutError(), "cannot reach Sonarr"),
        ]
        for error, message in cases:
            with self.subTest(error=error), patch.object(scanner, "urlopen", side_effect=error):
                with self.assertRaisesRegex(scanner.ScanError, message):
                    scanner.api_get("https://sonarr.example", "test-key", "series")

    def test_tracked_files_include_unmonitored_series_and_nested_series(self):
        responses = [
            [{"id": 1, "title": "Parent", "path": "/data/Shows/Parent", "monitored": False},
             {"id": 2, "title": "Nested", "path": "/data/Shows/Parent/Nested"}],
            [{"path": "/data/Shows/Parent/episode.mkv"}],
            [{"path": "/data/Shows/Parent/Nested/episode.mkv"}],
        ]
        with patch.object(scanner, "api_get", side_effect=responses):
            tracked, series = scanner.get_tracked_episode_files(
                "https://sonarr.example", "test-key", Path("/data/Shows"), Path("/library")
            )
        self.assertEqual(tracked, {Path("/library/Parent/episode.mkv"), Path("/library/Parent/Nested/episode.mkv")})
        self.assertEqual(scanner.series_for(Path("/library/Parent/Nested/episode.mkv"), Path("/library"), series), "Nested")

    def test_missing_series_id_aborts_fetch(self):
        with patch.object(scanner, "api_get", return_value=[{"path": "/data/Shows/Archer"}]):
            with self.assertRaisesRegex(scanner.ScanError, "no valid id"):
                scanner.get_tracked_episode_files("https://sonarr.example", "test-key", Path("/data/Shows"), Path("/library"))


class ReportTests(unittest.TestCase):
    def test_csv_round_trips_unicode_commas_quotes_and_matching_video_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            findings = [
                scanner.Finding("ORPHAN_VIDEO", "video", 'Archer, "2009"', root / 'Monégasque, "episode".mkv', 2048, "Untracked", None),
                scanner.Finding("ORPHAN_SIDECAR", "subtitle", "Archer", root / "missing.en.srt", 10, "Missing video", False),
            ]
            output = root / "report.csv"
            scanner.write_csv(output, root, findings)
            with output.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, list(scanner.CSV_COLUMNS))
            self.assertEqual(rows[0]["Series"], 'Archer, "2009"')
            self.assertEqual(rows[0]["Relative Path"], findings[0].path.name)
            self.assertEqual(rows[0]["Absolute Path"], str(findings[0].path))
            self.assertEqual(rows[0]["Size Bytes"], "2048")
            self.assertEqual(rows[0]["Matching Video"], "")
            self.assertEqual(rows[1]["Matching Video"], "false")

    def test_empty_report_still_has_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.csv"
            scanner.write_csv(output, Path(directory), [])
            with output.open(newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [list(scanner.CSV_COLUMNS)])

    def test_failed_scan_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.csv"
            output.write_text("previous report", encoding="utf-8")
            args = argparse.Namespace(sonarr_url="https://sonarr.example", api_key="test-key",
                                      sonarr_root=Path("/data/Shows"), local_root=root, output=output)
            with patch.object(scanner, "parse_args", return_value=args), patch.object(
                scanner, "get_tracked_episode_files", return_value=(set(), [])
            ), patch.object(scanner, "scan_library", side_effect=scanner.ScanError("scan denied")), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(io.StringIO()) as errors:
                result = scanner.main()
            self.assertEqual(result, 1)
            self.assertIn("scan denied", errors.getvalue())
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report")


if __name__ == "__main__":
    unittest.main()
