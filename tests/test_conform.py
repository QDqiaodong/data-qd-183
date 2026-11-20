import json
import unittest
from pathlib import Path
from subtitle_conform.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class SubtitleTests(unittest.TestCase):
    def test_clips_split_trim_and_speed_with_readability(self):
        result = run(fixture())
        self.assertEqual(result["output_duration_ms"], "4000")
        self.assertEqual([(r["start_ms"], r["end_ms"]) for r in result["fragments"]],
                         [("0", "1000"), ("2000", "3000"), ("3000", "3500")])
        self.assertEqual(result["summary"], {"clips": 2, "output_fragments": 3, "srt_cues": 3,
                                            "overlap_pairs": 0, "warning_fragments": 1})
        self.assertEqual(result["fragments"][-1]["issues"], ["short_duration", "reading_speed"])
        self.assertIn("00:00:03,000 --> 00:00:03,500", result["srt_text"])

    def test_reused_clip_keeps_separate_fragment_identity(self):
        data = fixture()
        data["clips"][1] = dict(data["clips"][0], id="repeat")
        rows = run(data)["fragments"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["fragment_id"], ["cut-1", "caption-1"])
        self.assertEqual(rows[2]["fragment_id"], ["repeat", "caption-1"])

    def test_exact_fraction_and_zero_quantized_duration_are_audited(self):
        data = fixture()
        data["clips"] = [dict(data["clips"][0], in_ms=0, out_ms=2, speed_num=3)]
        data["cues"] = [dict(data["cues"][0], start_ms=0, end_ms=1)]
        result = run(data)
        self.assertEqual(result["fragments"][0]["end_ms"], "1/3")
        self.assertEqual(result["srt_cues"], 0)
        self.assertEqual(result["srt_dropped_fragments"], [["cut-1", "caption-1"]])

    def test_overlaps_and_removed_cues_are_visible(self):
        data = fixture()
        data["cues"].append(dict(data["cues"][0], id="overlap", start_ms=2500, end_ms=3500))
        data["cues"].append(dict(data["cues"][0], id="omitted", start_ms=5000, end_ms=6000))
        result = run(data)
        self.assertEqual(result["overlap_count"], 1)
        self.assertEqual(result["omitted_cue_ids"], ["omitted"])

    def test_unknown_source_and_zero_speed_reject(self):
        data = fixture()
        data["clips"][0]["source_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown"):
            run(data)
        data = fixture()
        data["clips"][0]["speed_num"] = 0
        with self.assertRaisesRegex(ValueError, "speed_num"):
            run(data)
