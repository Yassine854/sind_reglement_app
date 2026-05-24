import asyncio
from io import BytesIO
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from starlette.datastructures import UploadFile
from app import (
    _cache,
    file_uri_to_fs_path,
    get_source_status,
    get_dashboard_for_filter,
    get_dashboard_for_range,
    get_default_dashboard,
    import_folder,
    parse_lines,
    read_text_file,
    reload_cache,
)


class ReglementDateLoadingTests(unittest.TestCase):
    def setUp(self):
        # Reset in-memory cache so each test starts with a clean slate and
        # get_or_reload_cache() will call reload_cache() with the patched paths.
        _cache.update({
            "all_rows": [],
            "current_rows": [],
            "source_files": [],
            "current_source_files": [],
            "warnings": [],
            "current_warnings": [],
            "loaded_at": None,
            "coverage_start": None,
            "coverage_end": None,
            "history_file_count": 0,
            "source_diagnostics": {},
            "needs_client_loading": False,
            "sync": {},
            "import_context": {},
        })

    def test_parse_lines_uses_reglement_date_column(self):
        text = "CTRT-26-03-0000002;26-99-CAM50-00159;20260304;20260525;SFX;TRT;CLS06386;;ATB;ACF-SFX-26-00003;188.249"

        rows = parse_lines(text)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reglement_date_iso"], "2026-03-04")

    def test_first_date_field_drives_coverage_and_filtering(self):
        """The first date column (parts[2]) must be used; parts[3] is settlement date."""
        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = Path(tmpdir) / "Réglements"
            history_dir.mkdir()
            (history_dir / "REGLEMENT_avril.txt").write_text(
                "CTRT-26-04-0000078;;20260427;20260831;TUN;TRT;CLT06449;;BT;FAC-TUN-26-13006;1538.2\n",
                encoding="utf-8",
            )

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", ""), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", str(history_dir)
            ):
                reload_cache()
                self.assertEqual(_cache["coverage_start"], "2026-04-27")
                self.assertEqual(_cache["coverage_end"], "2026-04-27")

                april = json.loads(asyncio.run(
                    get_dashboard_for_range(start_date="2026-04-01", end_date="2026-04-30")
                ).body)
                august = json.loads(asyncio.run(
                    get_dashboard_for_range(start_date="2026-08-01", end_date="2026-08-31")
                ).body)

        self.assertEqual(april["grand_count"], 1)
        self.assertEqual(august["grand_count"], 0)

    def test_default_dashboard_reads_current_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            current_file = Path(tmpdir) / "REGLEMENT.txt"
            current_file.write_text(
                "CESP-26-05-0000100;26-99-CAM39-00415;20260501;20260501;BJSSE;ESP;CLS03581;;;FAC-BJS-26-00414;71.9\n",
                encoding="utf-8",
            )

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", str(current_file)):
                response = asyncio.run(get_default_dashboard())

        payload = json.loads(response.body)
        self.assertEqual(payload["mode"], "default")
        self.assertEqual(payload["grand_count"], 1)
        self.assertEqual(payload["source_files"], [str(current_file)])
        self.assertEqual(payload["warnings"], [])

    def test_default_dashboard_reads_current_file_uri_without_stripping_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            current_file = Path(tmpdir) / "REGLEMENT mai.txt"
            current_file.write_text(
                "CESP-26-05-0000100;26-99-CAM39-00415;20260501;20260501;BJSSE;ESP;CLS03581;;;FAC-BJS-26-00414;71.9\n",
                encoding="utf-8",
            )
            current_uri = current_file.as_uri()

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", current_uri), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", ""
            ):
                response = asyncio.run(get_default_dashboard())

        payload = json.loads(response.body)
        self.assertEqual(payload["grand_count"], 1)
        self.assertEqual(payload["source_files"], [current_uri])
        self.assertTrue(payload["source_files"][0].startswith("file://"))

    def test_date_range_merges_history_and_current_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            history_dir = base / "Réglements"
            history_dir.mkdir()
            current_file = base / "REGLEMENT.txt"

            (history_dir / "REGLEMENT_avril2026.txt").write_text(
                "\n".join(
                    [
                        "CTRT-26-04-0000001;26-99-CAM50-00159;20260401;20260424;NAB;TRT;CLS06386;;ATB;ACF-NAB-26-00001;100.0",
                        "CTRT-26-04-0000002;26-99-CAM50-00160;20260402;20260425;NAB;TRT;CLS06386;;ATB;ACF-NAB-26-00002;200.0",
                    ]
                ),
                encoding="utf-8",
            )
            (history_dir / "REGLEMENT_mai2026.txt").write_text(
                "CESP-26-05-0000826;26-99-CAM39-00416;20260502;20260510;BJSSE;ESP;CLS05585;;;FAC-BJS-26-00415;120.1\n",
                encoding="utf-8",
            )
            current_file.write_text(
                "\n".join(
                    [
                        "CESP-26-05-0000838;26-99-CAM39-00418;20260502;20260520;BJSSE;ESP;CLS05056;;;FAC-BJS-26-00417;256.79",
                        "CESP-26-05-0000900;26-99-CAM39-00419;20260502;20260526;BJSSE;ESP;CLS05057;;;FAC-BJS-26-00418;300.0",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", str(current_file)), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", str(history_dir)
            ):
                response = asyncio.run(
                    get_dashboard_for_range(start_date="2026-04-25", end_date="2026-05-25")
                )

        payload = json.loads(response.body)
        self.assertEqual(payload["mode"], "date_range")
        self.assertEqual(payload["grand_count"], 3)
        self.assertAlmostEqual(payload["grand_total"], 676.89, places=3)
        self.assertEqual(payload["date_range"], {"start": "2026-04-25", "end": "2026-05-25"})
        self.assertEqual(len(payload["source_files"]), 3)

    def test_history_directory_listing_preserves_file_uri_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = Path(tmpdir) / "Réglements"
            history_dir.mkdir()
            history_file = history_dir / "REGLEMENT au 30 avril2026.txt"
            history_file.write_text(
                "CTRT-26-04-0000001;26-99-CAM50-00159;20260430;20260524;NAB;TRT;CLS06386;;ATB;ACF-NAB-26-00001;100.0\n",
                encoding="utf-8",
            )
            history_uri = history_dir.as_uri()

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", ""), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", history_uri
            ):
                response = asyncio.run(
                    get_dashboard_for_range(start_date="2026-04-01", end_date="2026-05-31")
                )

        payload = json.loads(response.body)
        self.assertEqual(payload["grand_count"], 1)
        self.assertEqual(payload["source_files"], [history_file.as_uri()])
        self.assertTrue(payload["source_files"][0].startswith("file://"))

    def test_file_uri_to_fs_path_decodes_percent_encoded_segments(self):
        uri = "file://172.16.100.34/Users/chokri.jdir/Desktop/TDB_SINDBAD_Mens/R%C3%A9glements/"
        resolved = file_uri_to_fs_path(uri)
        self.assertIn("Réglements", resolved)
        self.assertNotIn("%C3%A9", resolved)

    def test_file_uri_to_fs_path_uses_mount_fallback_when_configured(self):
        uri = "file://172.16.100.34/Users/chokri.jdir/Desktop/TDB_SINDBAD/REGLEMENT.txt"
        with patch.dict("os.environ", {"FILE_URI_MOUNT_ROOT": "/mnt/reglement"}, clear=False):
            resolved = file_uri_to_fs_path(uri)
        self.assertTrue(resolved.endswith("/Users/chokri.jdir/Desktop/TDB_SINDBAD/REGLEMENT.txt"))
        self.assertTrue(resolved.startswith("/mnt/reglement"))

    def test_file_uri_mount_fallback_sanitizes_parent_segments(self):
        uri = "file://172.16.100.34/../../etc/passwd"
        with patch.dict("os.environ", {"FILE_URI_MOUNT_ROOT": "/mnt/reglement"}, clear=False):
            resolved = file_uri_to_fs_path(uri)
        self.assertEqual(resolved, "/mnt/reglement/etc/passwd")
        self.assertEqual(os.path.commonpath(["/mnt/reglement", resolved]), "/mnt/reglement")

    def test_read_text_file_uses_resolved_path_for_file_uri(self):
        uri = "file://172.16.100.34/Users/chokri.jdir/Desktop/TDB_SINDBAD/REGLEMENT.txt"
        with patch("app.file_uri_to_fs_path", return_value="/tmp/resolved/REGLEMENT.txt") as resolver, patch(
            "builtins.open", side_effect=FileNotFoundError
        ) as mocked_open:
            _, error = read_text_file(uri)

        resolver.assert_called_once_with(uri)
        mocked_open.assert_called_once_with("/tmp/resolved/REGLEMENT.txt", "rb")
        self.assertIn("Fichier introuvable : /tmp/resolved/REGLEMENT.txt", error)
        self.assertIn("source:", error)

    def test_default_dashboard_returns_warning_when_file_is_missing(self):
        missing = Path("/tmp/does-not-exist/REGLEMENT.txt")
        with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", str(missing)):
            response = asyncio.run(get_default_dashboard())

        payload = json.loads(response.body)
        self.assertEqual(payload["grand_count"], 0)
        self.assertTrue(payload["warnings"])
        self.assertIn("Fichier introuvable", payload["warnings"][0])
        self.assertIn("runtime:", payload["warnings"][0])

    def test_source_status_exposes_runtime_and_path_diagnostics(self):
        missing_file = "/tmp/does-not-exist/REGLEMENT.txt"
        missing_history = "file://172.16.100.34/Users/chokri.jdir/Desktop/TDB_SINDBAD_Mens/R%C3%A9glements/"
        with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", missing_file), patch(
            "app.DEFAULT_HISTORY_REGLEMENTS_DIR", missing_history
        ), patch.dict("os.environ", {"FILE_URI_MOUNT_ROOT": "/mnt/reglement"}, clear=False):
            app_module.reload_cache()
            status = get_source_status()

        self.assertIn("runtime_label", status)
        self.assertEqual(status["current_diagnostic"]["configured_source"], missing_file)
        self.assertEqual(status["current_diagnostic"]["error_kind"], "missing")
        self.assertEqual(status["history_diagnostic"]["configured_source"], missing_history)
        self.assertTrue(status["history_diagnostic"]["resolved_path"].startswith("/mnt/reglement"))
        self.assertEqual(status["history_diagnostic"]["error_kind"], "missing")

    def test_folder_import_loads_current_and_history_files(self):
        monthly_line = (
            "CESP-26-05-0000100;26-99-CAM39-00415;20260501;20260501;BJSSE;ESP;CLS03581;;;FAC-BJS-26-00414;71.9\n"
        )
        history_line = (
            "CTRT-26-04-0000078;;20260427;20260831;TUN;TRT;CLT06449;;BT;FAC-TUN-26-13006;1538.2\n"
        )
        files = [
            UploadFile(
                filename="Fichiers Sources/REGLEMENT.txt",
                file=BytesIO(monthly_line.encode("utf-8")),
            ),
            UploadFile(
                filename="Fichiers Sources/Réglements/REGLEMENT_historique.txt",
                file=BytesIO(history_line.encode("utf-8")),
            ),
        ]

        response = asyncio.run(import_folder(files))
        status = json.loads(response.body)

        self.assertEqual(status["source_mode"], "uploaded_folder")
        self.assertEqual(status["uploaded_root_name"], "Fichiers Sources")
        self.assertTrue(status["current_found"])
        self.assertTrue(status["history_found"])
        self.assertEqual(status["source_file_count"], 2)
        self.assertEqual(status["history_file_count"], 1)
        self.assertEqual(_cache["coverage_start"], "2026-04-27")
        self.assertEqual(_cache["coverage_end"], "2026-05-01")

    def test_folder_import_reports_missing_reglements_folder(self):
        files = [
            UploadFile(
                filename="Fichiers Sources/REGLEMENT.txt",
                file=BytesIO(
                    "CESP-26-05-0000100;26-99-CAM39-00415;20260501;20260501;BJSSE;ESP;CLS03581;;;FAC-BJS-26-00414;71.9\n".encode(
                        "utf-8"
                    )
                ),
            )
        ]

        response = asyncio.run(import_folder(files))
        status = json.loads(response.body)

        self.assertTrue(status["current_found"])
        self.assertFalse(status["history_found"])
        self.assertTrue(status["warnings"])
        self.assertTrue(any("Réglements" in warning for warning in status["warnings"]))

    def test_filter_endpoint_alias_works_with_start_and_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            history_dir = base / "Réglements"
            history_dir.mkdir()
            current_file = base / "REGLEMENT.txt"
            current_file.write_text(
                "CESP-26-05-0000838;26-99-CAM39-00418;20260502;20260520;BJSSE;ESP;CLS05056;;;FAC-BJS-26-00417;256.79\n",
                encoding="utf-8",
            )

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", str(current_file)), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", str(history_dir)
            ):
                response = asyncio.run(
                    get_dashboard_for_filter(start_date="2026-05-01", end_date="2026-05-31")
                )

        payload = json.loads(response.body)
        self.assertEqual(payload["mode"], "date_range")
        self.assertEqual(payload["grand_count"], 1)

    def test_default_dashboard_hosted_friendly_message_when_no_path_configured(self):
        with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", ""):
            response = asyncio.run(get_default_dashboard())

        payload = json.loads(response.body)
        self.assertEqual(payload["grand_count"], 0)
        self.assertTrue(payload["warnings"])
        self.assertIn("Aucune donnée importée", payload["warnings"][0])

    def test_range_endpoint_hosted_friendly_message_when_no_paths_configured(self):
        with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", ""), patch(
            "app.DEFAULT_HISTORY_REGLEMENTS_DIR", ""
        ):
            response = asyncio.run(
                get_dashboard_for_range(start_date="2026-01-01", end_date="2026-05-31")
            )

        payload = json.loads(response.body)
        self.assertEqual(payload["grand_count"], 0)
        self.assertTrue(payload["warnings"])
        self.assertIn("Aucune donnée importée", payload["warnings"][0])

    def test_coverage_range_uses_true_bounds_for_unsorted_file_rows(self):
        """Display range must use min/max of all parsed row dates, not first/last row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            history_dir = Path(tmpdir) / "Réglements"
            history_dir.mkdir()
            # File rows are intentionally unsorted by règlement date.
            (history_dir / "REGLEMENT_mai2026.txt").write_text(
                "\n".join([
                    "CESP-26-05-0007369;26-99-CAM19-01076;20260519;20260519;TUN;ESP;CLT06374;;;FAC-TUN-26-16975;365.609",
                    "CCHQR-26-05-0000001;26-99-CAM53-01127;20260501;20260501;NAB;CHQ;CLN00102;0000658;STB;;0.399",
                    "CESP-26-05-0007370;26-99-CAM19-01077;20260512;20260512;TUN;ESP;CLT06147;;;FAC-TUN-26-16971;1096.423",
                ]),
                encoding="utf-8",
            )

            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", ""), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", str(history_dir)
            ):
                reload_cache()

        # Range must be min/max across all valid rows, independent of row order.
        self.assertEqual(_cache["coverage_start"], "2026-05-01")
        self.assertEqual(_cache["coverage_end"], "2026-05-19")


if __name__ == "__main__":
    unittest.main()
