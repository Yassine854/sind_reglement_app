import unittest

from app import build_upload_payload, parse_lines


class SiteAggregationTests(unittest.TestCase):
    def test_site_only_rows_are_counted_in_site_summary(self):
        text = "\n".join(
            [
                "CTRT-26-03-0000020;;20260313;20260420;NAB;TRT;CLN02868;;UIB;;0.01",
                "CTRT-26-03-0000002;;20260304;20260525;SFX;TRT;CLS06386;;ATB;ACF-SFX-26-00003;188.249",
                "CTRT-26-03-0000015;;20260310;20260325;SFX;TRT;CLF02238;;AMEN B;ACF-SFX-26-00004;184.44",
                "CTRT-26-03-0000025;;20260312;20260312;NAB;TRT;CLN02835;;UBCI;UIBDT-D-26-03-0004;1272.097",
            ]
        )

        rows = parse_lines(text)
        payload = build_upload_payload(rows, "sample.txt")

        self.assertEqual(payload["grand_count"], 4)
        self.assertEqual(payload["active_cams"], 0)
        self.assertEqual(payload["rows_without_cam_with_site"], 4)
        self.assertEqual(payload["rows"], [])
        self.assertIn("NAB", payload["sites_summary"])
        self.assertIn("SFX", payload["sites_summary"])
        self.assertEqual(payload["sites_summary"]["NAB"]["count"], 2)
        self.assertEqual(payload["sites_summary"]["NAB"]["site_only_count"], 2)
        self.assertEqual(payload["sites_summary"]["NAB"]["cam_count"], 0)

    def test_cam_rows_and_site_only_rows_accumulate_together_per_site(self):
        text = "\n".join(
            [
                "CTRT-26-03-0000002;26-99-CAM50-00159;20260304;20260525;SFX;TRT;CLS06386;;ATB;ACF-SFX-26-00003;188.249",
                "CTRT-26-03-0000025;;20260312;20260312;NAB;TRT;CLN02835;;UBCI;UIBDT-D-26-03-0004;1272.097",
            ]
        )

        rows = parse_lines(text)
        payload = build_upload_payload(rows, "sample.txt")

        self.assertEqual(payload["active_cams"], 1)
        self.assertEqual(payload["sites_summary"]["NAB"]["cam_count"], 1)
        self.assertEqual(payload["sites_summary"]["NAB"]["site_only_count"], 1)
        self.assertEqual(payload["sites_summary"]["NAB"]["count"], 2)
        self.assertAlmostEqual(payload["sites_summary"]["NAB"]["amount"], 1460.346, places=3)


if __name__ == "__main__":
    unittest.main()
