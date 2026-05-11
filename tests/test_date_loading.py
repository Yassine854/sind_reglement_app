import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import get_dashboard_for_filter, get_dashboard_for_range, get_default_dashboard, parse_lines


class ReglementDateLoadingTests(unittest.TestCase):
    def test_parse_lines_uses_reglement_date_column(self):
        text = "CTRT-26-03-0000002;26-99-CAM50-00159;20260304;20260525;SFX;TRT;CLS06386;;ATB;ACF-SFX-26-00003;188.249"

        rows = parse_lines(text)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reglement_date_iso"], "2026-05-25")

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

            env = {
                "current": str(current_file),
                "history": str(history_dir),
            }
            with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", env["current"]), patch(
                "app.DEFAULT_HISTORY_REGLEMENTS_DIR", env["history"]
            ):
                response = asyncio.run(
                    get_dashboard_for_range(start_date="2026-04-25", end_date="2026-05-25")
                )

        payload = json.loads(response.body)
        self.assertEqual(payload["mode"], "date_range")
        self.assertEqual(payload["grand_count"], 3)
        self.assertAlmostEqual(payload["grand_total"], 576.89, places=3)
        self.assertEqual(payload["date_range"], {"start": "2026-04-25", "end": "2026-05-25"})
        self.assertEqual(len(payload["source_files"]), 3)

    def test_default_dashboard_returns_warning_when_file_is_missing(self):
        missing = Path("/tmp/does-not-exist/REGLEMENT.txt")
        with patch("app.DEFAULT_CURRENT_REGLEMENT_FILE", str(missing)):
            response = asyncio.run(get_default_dashboard())

        payload = json.loads(response.body)
        self.assertEqual(payload["grand_count"], 0)
        self.assertTrue(payload["warnings"])
        self.assertIn("Fichier introuvable", payload["warnings"][0])

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


if __name__ == "__main__":
    unittest.main()
