"""重新剪辑审校迁移（carry-review 模式）回归测试。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from subtitle_conform.carry import run_carry_review


def source(duration=1000, sid="s"):
    return {"id": sid, "duration_ms": duration}


def clip(cid, in_ms, out_ms, num=1, den=1, source_id="s"):
    return {"id": cid, "source_id": source_id, "in_ms": in_ms,
            "out_ms": out_ms, "speed_num": num, "speed_den": den}


def cue(qid, start, end, text="orig", source_id="s"):
    return {"id": qid, "source_id": source_id, "start_ms": start,
            "end_ms": end, "text": text}


def conform_input(clips, cues, sources=None, min_duration=0, cps=1000):
    return {"sources": [source()] if sources is None else sources,
            "clips": clips, "cues": cues,
            "min_duration_ms": min_duration, "max_chars_per_second": cps}


def link(lid, old, new, start, end):
    return {"id": lid, "old_clip_id": old, "new_clip_id": new,
            "start_ms": start, "end_ms": end}


def edit(eid, cid, qid, start, end, action="hide", text=None):
    row = {"id": eid, "clip_id": cid, "cue_id": qid,
           "start_ms": start, "end_ms": end, "action": action}
    if text is not None:
        row["text"] = text
    return row


def make_input(previous, current, links=None, edits=None):
    return {"previous": previous, "current": current,
            "links": [] if links is None else links,
            "edits": [] if edits is None else edits}


def segments_of(report, clip_id, cue_id="q"):
    return [row for row in report["review_segments"]
            if row["fragment_id"] == [clip_id, cue_id]]


def disposition(report, edit_id):
    return next(row for row in report["edit_dispositions"]
                if row["edit_id"] == edit_id)


class SharedSourceReuseTests(unittest.TestCase):
    """验收：旧剪辑 A、B 复用同一素材，只有 A 的 [10,70) 批准改词。"""

    def _abx(self, links):
        previous = conform_input(
            [clip("A", 0, 100), clip("B", 0, 100)],
            [cue("q", 0, 100, "orig")])
        current = conform_input(
            [clip("X", 0, 100), clip("Y", 0, 100)],
            [cue("q", 0, 100, "orig")])
        edits = [edit("e1", "A", "q", 10, 70, "replace", "new words")]
        return make_input(previous, current, links, edits)

    def test_conflicting_candidates_block_delivery(self):
        data = self._abx([link("l-a", "A", "X", 0, 80),
                          link("l-b", "B", "X", 40, 90)])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["srt_text"])
        self.assertIsNone(report["delivery_lines"])
        self.assertIsNone(report["quality"])
        rows = [(s["source_start_ms"], s["source_end_ms"], s["verdict"], s["text"])
                for s in segments_of(report, "X")]
        self.assertEqual(rows, [(0, 10, "visible", "orig"),
                                (10, 40, "visible", "new words"),
                                (40, 70, "conflict", None),
                                (70, 80, "visible", "orig"),
                                (80, 90, "visible", "orig"),
                                (90, 100, "visible", "orig")])
        conflict = segments_of(report, "X")[2]
        self.assertEqual([b["link_id"] for b in conflict["basis"]],
                         ["l-a", "l-b"])
        self.assertEqual(conflict["basis"][0]["edit_id"], "e1")
        self.assertEqual(conflict["basis"][0]["claim"], "visible")
        self.assertEqual(conflict["basis"][0]["text"], "new words")
        self.assertIsNone(conflict["basis"][1]["edit_id"])
        self.assertEqual(conflict["basis"][1]["text"], "orig")
        # 阻断仍交付完整清单与旧决定去向
        disp = disposition(report, "e1")
        self.assertEqual(disp["adopted"], [{"start_ms": 10, "end_ms": 40}])
        self.assertEqual(disp["not_adopted"], [{"start_ms": 40, "end_ms": 70}])
        # 没有关系的重复使用实例 Y 不得继承这次改词
        y = segments_of(report, "Y")
        self.assertEqual(len(y), 1)
        self.assertEqual((y[0]["verdict"], y[0]["text"]), ("visible", "orig"))
        self.assertEqual(y[0]["basis"], [])

    def test_single_link_inherits_fully_and_delivers(self):
        data = self._abx([link("l-a", "A", "X", 0, 80)])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        rows = [(s["source_start_ms"], s["source_end_ms"], s["text"])
                for s in segments_of(report, "X")]
        self.assertEqual(rows, [(0, 10, "orig"), (10, 70, "new words"),
                                (70, 80, "orig"), (80, 100, "orig")])
        disp = disposition(report, "e1")
        self.assertEqual(disp["adopted"], [{"start_ms": 10, "end_ms": 70}])
        self.assertEqual(disp["not_adopted"], [])
        # [70,80) 与 [80,100) 依据不同但实显正文相同，交付并为一条
        lines = [l for l in report["delivery_lines"] if l["line_id"][0] == "X"]
        self.assertEqual(
            [(l["source_start_ms"], l["source_end_ms"], l["text"]) for l in lines],
            [(0, 10, "orig"), (10, 70, "new words"), (70, 100, "orig")])
        self.assertEqual(len(report["delivery_lines"]), 4)  # 含 Y 的一条
        self.assertIn("new words", report["srt_text"])
        self.assertEqual(report["quality"]["srt_cues"], 4)


class RecutGeometryTests(unittest.TestCase):
    """验收：裁剪、变速、重复使用后新版位置仍精确。"""

    def test_trimmed_new_clip_positions_exact(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "line")])
        current = conform_input([clip("X", 40, 90)], [cue("q", 0, 100, "line")])
        data = make_input(previous, current,
                          [link("l", "A", "X", 40, 90)],
                          [edit("e", "A", "q", 10, 70, "replace", "new")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        rows = [(s["source_start_ms"], s["source_end_ms"],
                 s["start_ms"], s["end_ms"], s["text"])
                for s in report["review_segments"]]
        self.assertEqual(rows, [(40, 70, "0", "30", "new"),
                                (70, 90, "30", "50", "line")])
        # 源起止变动不使重叠部分失效；未覆盖部分进入补集
        disp = disposition(report, "e")
        self.assertEqual(disp["adopted"], [{"start_ms": 40, "end_ms": 70}])
        self.assertEqual(disp["not_adopted"], [{"start_ms": 10, "end_ms": 40}])

    def test_speed_change_maps_program_exactly(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "line")])
        current = conform_input([clip("X", 0, 100, num=2)],
                                [cue("q", 0, 100, "line")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)],
                          [edit("e", "A", "q", 10, 70, "replace", "new")])
        rows = [(s["start_ms"], s["end_ms"], s["text"])
                for s in run_carry_review(data)["review_segments"]]
        self.assertEqual(rows, [("0", "5", "line"), ("5", "35", "new"),
                                ("35", "50", "line")])

    def test_fractional_speed_keeps_rational_strings(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "line")])
        current = conform_input([clip("X", 0, 100, num=3)],
                                [cue("q", 0, 100, "line")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)],
                          [edit("e", "A", "q", 10, 70, "replace", "new")])
        middle = run_carry_review(data)["review_segments"][1]
        self.assertEqual((middle["start_ms"], middle["end_ms"]),
                         ("10/3", "70/3"))

    def test_one_old_clip_can_feed_multiple_new_instances(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "orig")])
        current = conform_input([clip("X", 0, 100), clip("Y", 0, 100)],
                                [cue("q", 0, 100, "orig")])
        data = make_input(previous, current,
                          [link("lx", "A", "X", 0, 100),
                           link("ly", "A", "Y", 0, 100)],
                          [edit("e", "A", "q", 10, 70, "replace", "new")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        for cid in ("X", "Y"):
            rows = segments_of(report, cid)
            self.assertEqual(rows[1]["source_start_ms"], 10)
            self.assertEqual(rows[1]["text"], "new")
        # 相同源区间在多个新实例被采用，并集只计一次
        disp = disposition(report, "e")
        self.assertEqual(disp["adopted"], [{"start_ms": 10, "end_ms": 70}])
        self.assertEqual(disp["not_adopted"], [])

    def test_edit_on_tiny_old_fragment_is_valid(self):
        # 旧碎片短到不会出现在旧 SRT 中，仍可有审校决定
        previous = conform_input([clip("A", 0, 3, num=3)], [cue("q", 0, 3, "x")],
                                 sources=[source(100)])
        current = conform_input([clip("X", 0, 3)], [cue("q", 0, 3, "x")],
                                sources=[source(100)])
        data = make_input(previous, current, [link("l", "A", "X", 0, 3)],
                          [edit("e", "A", "q", 0, 3, "replace", "y")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["review_segments"][0]["text"], "y")


class TextCorrectionTests(unittest.TestCase):
    """验收：原始正文更正只在有旧决定的有效对应区间触发重审。"""

    def test_correction_stales_only_edit_region(self):
        previous = conform_input([clip("A", 0, 100)],
                                 [cue("q", 0, 100, "orignal")])
        current = conform_input([clip("X", 0, 100)],
                                [cue("q", 0, 100, "original")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)],
                          [edit("e", "A", "q", 10, 20, "replace", "fixed")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "blocked")
        rows = [(s["source_start_ms"], s["source_end_ms"], s["verdict"], s["text"])
                for s in report["review_segments"]]
        self.assertEqual(rows, [(0, 10, "visible", "original"),
                                (10, 20, "stale_review", None),
                                (20, 100, "visible", "original")])
        stale = report["review_segments"][1]
        self.assertEqual(stale["basis"][0]["claim"], "stale")
        self.assertEqual(stale["basis"][0]["edit_id"], "e")
        disp = disposition(report, "e")
        self.assertEqual(disp["adopted"], [])
        self.assertEqual(disp["not_adopted"], [{"start_ms": 10, "end_ms": 20}])

    def test_correction_without_edit_does_not_stale(self):
        # 正文更正但对应区间没有旧决定：候选主张新版原文，不重审
        previous = conform_input([clip("A", 0, 100)],
                                 [cue("q", 0, 100, "orignal")])
        current = conform_input([clip("X", 0, 100)],
                                [cue("q", 0, 100, "original")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)], [])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["review_segments"][0]["text"], "original")

    def test_edit_without_new_counterpart_not_carried_and_not_blocking(self):
        previous = conform_input([clip("A", 0, 100)],
                                 [cue("q", 0, 100, "orig"),
                                  cue("gone", 0, 100, "old")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "orig")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)],
                          [edit("e-gone", "A", "gone", 10, 20, "replace", "new")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        disp = disposition(report, "e-gone")
        self.assertEqual(disp["adopted"], [])
        self.assertEqual(disp["not_adopted"], [{"start_ms": 10, "end_ms": 20}])


class CandidateCombinationTests(unittest.TestCase):
    """验收：相接关系、相同结论的多个候选、等于新版原文的替换。"""

    def test_touching_links_and_same_text_merge_delivery(self):
        previous = conform_input([clip("A", 0, 100), clip("B", 0, 100)],
                                 [cue("q", 0, 100, "same")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "same")])
        data = make_input(previous, current,
                          [link("l-1", "A", "X", 0, 50),
                           link("l-2", "B", "X", 50, 100)], [])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        # 审校清单保留依据分界，交付按一条字幕算
        self.assertEqual(len(report["review_segments"]), 2)
        self.assertEqual([s["basis"][0]["link_id"]
                          for s in report["review_segments"]], ["l-1", "l-2"])
        self.assertEqual(len(report["delivery_lines"]), 1)
        line = report["delivery_lines"][0]
        self.assertEqual(line["line_id"], ["X", "q", 0, 100])
        self.assertEqual((line["start_ms"], line["end_ms"]), ("0", "100"))

    def test_agreeing_candidates_both_recorded(self):
        previous = conform_input([clip("A", 0, 100), clip("B", 0, 100)],
                                 [cue("q", 0, 100, "orig")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "orig")])
        data = make_input(previous, current,
                          [link("la", "A", "X", 0, 100),
                           link("lb", "B", "X", 0, 100)],
                          [edit("ea", "A", "q", 10, 20, "replace", "new"),
                           edit("eb", "B", "q", 10, 20, "replace", "new")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        middle = segments_of(report, "X")[1]
        self.assertEqual((middle["source_start_ms"], middle["source_end_ms"]),
                         (10, 20))
        self.assertEqual((middle["verdict"], middle["text"]), ("visible", "new"))
        self.assertEqual([b["edit_id"] for b in middle["basis"]], ["ea", "eb"])
        for eid in ("ea", "eb"):
            self.assertEqual(disposition(report, eid)["adopted"],
                             [{"start_ms": 10, "end_ms": 20}])

    def test_replace_matching_new_text_is_not_conflict_but_traced(self):
        previous = conform_input([clip("A", 0, 100), clip("B", 0, 100)],
                                 [cue("q", 0, 100, "orig")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "orig")])
        data = make_input(previous, current,
                          [link("la", "A", "X", 0, 100),
                           link("lb", "B", "X", 0, 100)],
                          [edit("ea", "A", "q", 10, 20, "replace", "orig")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(disposition(report, "ea")["adopted"],
                         [{"start_ms": 10, "end_ms": 20}])

    def test_hidden_and_text_never_agree(self):
        previous = conform_input([clip("A", 0, 100), clip("B", 0, 100)],
                                 [cue("q", 0, 100, "orig")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "orig")])
        data = make_input(previous, current,
                          [link("la", "A", "X", 0, 100),
                           link("lb", "B", "X", 0, 100)],
                          [edit("ea", "A", "q", 10, 20, "hide"),
                           edit("eb", "B", "q", 10, 20, "replace", "orig")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "blocked")
        middle = segments_of(report, "X")[1]
        self.assertEqual(middle["verdict"], "conflict")


class DeliveryAndQualityTests(unittest.TestCase):
    """验收：交付行、隐藏缺口、质检与 SRT 随新交付变化。"""

    def test_hidden_gap_splits_delivery_lines(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "same")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "same")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)],
                          [edit("h", "A", "q", 40, 60, "hide")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        rows = [(s["source_start_ms"], s["source_end_ms"], s["verdict"])
                for s in report["review_segments"]]
        self.assertEqual(rows, [(0, 40, "visible"), (40, 60, "hidden"),
                                (60, 100, "visible")])
        self.assertEqual([(l["source_start_ms"], l["source_end_ms"])
                          for l in report["delivery_lines"]],
                         [(0, 40), (60, 100)])
        self.assertEqual(report["quality"]["srt_cues"], 2)

    def test_longer_replacement_updates_quality_and_srt(self):
        previous = conform_input([clip("A", 0, 1000)],
                                 [cue("q", 0, 1000, "short")])
        current = conform_input([clip("X", 0, 1000)],
                                [cue("q", 0, 1000, "short")], cps=10)
        long_text = "a much longer replacement line"
        data = make_input(previous, current, [link("l", "A", "X", 0, 1000)],
                          [edit("e", "A", "q", 0, 1000, "replace", long_text)])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        line = report["quality"]["lines"][0]
        self.assertIn("reading_speed", line["issues"])
        self.assertEqual(line["text"], long_text)
        self.assertIn(long_text, report["srt_text"])
        self.assertNotIn("short", report["srt_text"])

    def test_overlaps_counted_on_delivery_lines(self):
        previous = conform_input([clip("A", 0, 100)],
                                 [cue("p", 0, 100, "P"), cue("q", 0, 100, "Q")])
        current = conform_input([clip("X", 0, 100)],
                                [cue("p", 0, 100, "P"), cue("q", 0, 100, "Q")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)], [])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["quality"]["overlap_count"], 1)
        self.assertEqual(report["quality"]["overlap_examples"],
                         [[["X", "p", 0, 100], ["X", "q", 0, 100]]])

    def test_visible_line_rounding_to_zero_stays_audited(self):
        previous = conform_input([clip("A", 0, 10)], [cue("q", 0, 10, "hello")],
                                 sources=[source(100)])
        current = conform_input([clip("X", 0, 1, num=3)],
                                [cue("q", 0, 10, "hello")], sources=[source(100)])
        data = make_input(previous, current, [link("l", "A", "X", 0, 1)], [])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(len(report["delivery_lines"]), 1)
        line = report["delivery_lines"][0]
        self.assertEqual(line["line_id"], ["X", "q", 0, 1])
        self.assertEqual((line["start_ms"], line["end_ms"]), ("0", "1/3"))
        # 舍入后消失：SRT 为空，但精确交付行与质检仍在
        self.assertEqual(report["srt_text"], "")
        self.assertEqual(report["quality"]["srt_cues"], 0)
        self.assertEqual(report["quality"]["srt_dropped_line_ids"],
                         [["X", "q", 0, 1]])
        self.assertEqual(len(report["quality"]["lines"]), 1)
        self.assertIn("reading_speed", report["quality"]["lines"][0]["issues"])

    def test_all_hidden_report_is_ready_with_empty_delivery(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "x")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "x")])
        data = make_input(previous, current, [link("l", "A", "X", 0, 100)],
                          [edit("h", "A", "q", 0, 100, "hide")])
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["review_segments"][0]["verdict"], "hidden")
        self.assertEqual(report["delivery_lines"], [])
        self.assertEqual(report["srt_text"], "")
        self.assertEqual(report["quality"]["lines"], [])
        self.assertEqual(report["quality"]["overlap_count"], 0)
        self.assertEqual(report["quality"]["srt_cues"], 0)
        self.assertEqual(disposition(report, "h")["adopted"],
                         [{"start_ms": 0, "end_ms": 100}])

    def test_merged_line_duration_drives_quality(self):
        # 两段各 400ms 的相同正文跨依据分界合并为 800ms，按合并后时长质检
        previous = conform_input([clip("A", 0, 800), clip("B", 0, 800)],
                                 [cue("q", 0, 800, "same")])
        current = conform_input([clip("X", 0, 800)], [cue("q", 0, 800, "same")],
                                min_duration=600)
        data = make_input(previous, current,
                          [link("l-1", "A", "X", 0, 400),
                           link("l-2", "B", "X", 400, 800)], [])
        report = run_carry_review(data)
        self.assertEqual(len(report["review_segments"]), 2)
        self.assertEqual(len(report["delivery_lines"]), 1)
        self.assertEqual(report["delivery_lines"][0]["end_ms"], "800")
        self.assertEqual(report["quality"]["lines"][0]["issues"], [])
        self.assertEqual(report["quality"]["warning_lines"], 0)


class ValidationTests(unittest.TestCase):
    def _good(self):
        previous = conform_input([clip("A", 0, 100)], [cue("q", 0, 100, "orig")])
        current = conform_input([clip("X", 0, 100)], [cue("q", 0, 100, "orig")])
        return make_input(previous, current,
                          [link("l", "A", "X", 0, 100)],
                          [edit("e", "A", "q", 10, 20, "replace", "new")])

    def _expect(self, mutator, needle):
        data = self._good()
        mutator(data)
        with self.assertRaisesRegex(ValueError, needle):
            run_carry_review(data)

    def test_root_fields_strict(self):
        self._expect(lambda d: d.__setitem__("extra", 1), "unknown")
        self._expect(lambda d: d.pop("links"), "missing")
        self._expect(lambda d: d.pop("edits"), "missing")
        self._expect(lambda d: d.__setitem__("previous", []), "object")

    def test_links_and_edits_must_be_arrays_within_limit(self):
        self._expect(lambda d: d.__setitem__("links", None), "array")
        self._expect(lambda d: d.__setitem__("edits", {}), "array")
        self._expect(lambda d: d.__setitem__(
            "links", [link(f"l{i}", "A", "X", 0, 1) for i in range(2001)]),
            "2000")

    def test_link_reference_and_source_checks(self):
        self._expect(lambda d: d["links"].append(link("l2", "ghost", "X", 0, 1)),
                     "unknown old clip")
        self._expect(lambda d: d["links"].append(link("l2", "A", "ghost", 0, 1)),
                     "unknown new clip")

        def other_source(d):
            d["current"]["sources"] = [source(1000, "s2")]
            d["current"]["clips"][0]["source_id"] = "s2"
            d["current"]["cues"][0]["source_id"] = "s2"
        self._expect(other_source, "same source")

    def test_link_interval_checks(self):
        self._expect(lambda d: d["links"][0].__setitem__("end_ms", 200),
                     "outside clip source use")
        self._expect(lambda d: d["links"][0].__setitem__("start_ms", 100),
                     "positive duration")
        self._expect(lambda d: d["links"][0].__setitem__("start_ms", True),
                     "start_ms")
        self._expect(lambda d: d["links"][0].__setitem__("note", 1), "unknown")
        self._expect(lambda d: d["links"].append(link("l", "A", "X", 0, 1)),
                     "duplicate link ID")

    def test_overlapping_links_same_pair_rejected(self):
        self._expect(lambda d: d["links"].append(link("l2", "A", "X", 50, 100)),
                     "overlap")

    def test_touching_links_accepted(self):
        data = self._good()
        data["links"] = [link("l1", "A", "X", 0, 50),
                         link("l2", "A", "X", 50, 100)]
        self.assertEqual(run_carry_review(data)["status"], "ready")

    def test_edit_reference_and_interval_checks(self):
        self._expect(lambda d: d["edits"][0].__setitem__("cue_id", "ghost"),
                     "unknown previous fragment")
        self._expect(lambda d: d["edits"][0].__setitem__("end_ms", 200),
                     "outside fragment")
        self._expect(lambda d: d["edits"][0].__setitem__("start_ms", 20),
                     "positive duration")
        self._expect(lambda d: d["edits"][0].__setitem__("start_ms", False),
                     "start_ms")
        self._expect(lambda d: d["edits"][0].__setitem__("note", 1), "unknown")
        self._expect(lambda d: d["edits"].append(
            edit("e", "A", "q", 30, 40, "hide")), "duplicate edit ID")

    def test_edit_without_produced_fragment_rejected(self):
        # clip/cue 各自合法但无正长交集，不算是实际产生的碎片
        data = self._good()
        data["previous"]["sources"] = [source(1000)]
        data["previous"]["cues"] = [cue("q", 200, 300, "orig")]
        with self.assertRaisesRegex(ValueError, "unknown previous fragment"):
            run_carry_review(data)

    def test_edit_action_checks(self):
        self._expect(lambda d: d["edits"][0].__setitem__("action", "hide"),
                     "must not carry text")
        self._expect(lambda d: d["edits"][0].pop("text"),
                     "replace requires text")
        self._expect(lambda d: d["edits"][0].__setitem__("action", "mute"),
                     "unknown action")
        self._expect(lambda d: d["edits"][0].__setitem__("text", ""),
                     "1..2000")
        self._expect(lambda d: d["edits"][0].__setitem__("text", "a\rb"),
                     "control")

    def test_overlapping_edits_same_fragment_rejected(self):
        self._expect(lambda d: d["edits"].append(
            edit("e2", "A", "q", 15, 25, "hide")), "overlap")

    def test_touching_edits_accepted(self):
        data = self._good()
        data["edits"].append(edit("e2", "A", "q", 20, 30, "hide"))
        report = run_carry_review(data)
        self.assertEqual(report["status"], "ready")
        rows = [(s["source_start_ms"], s["source_end_ms"], s["verdict"])
                for s in report["review_segments"]]
        self.assertEqual(rows, [(0, 10, "visible"), (10, 20, "visible"),
                                (20, 30, "hidden"), (30, 100, "visible")])

    def test_inner_inputs_keep_normal_validation(self):
        self._expect(lambda d: d["previous"]["clips"][0].__setitem__(
            "source_id", "ghost"), "unknown clip source")
        self._expect(lambda d: d["current"].__setitem__("extra", 1), "unknown")


class FixtureAndCliTests(unittest.TestCase):
    def _env(self):
        return dict(os.environ, PYTHONPATH=str(Path("src").resolve()))

    def test_fixture_report(self):
        data = json.loads(Path("fixtures/carry-review.json").read_text())
        report = run_carry_review(data)
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["mode"], "carry-review")
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["output_duration_ms"], "3800")
        self.assertEqual(len(report["review_segments"]), 6)
        self.assertEqual(len(report["delivery_lines"]), 5)
        self.assertEqual(report["quality"]["srt_cues"], 5)
        self.assertEqual(report["quality"]["warning_lines"], 4)
        hidden = [s for s in report["review_segments"]
                  if s["verdict"] == "hidden"]
        self.assertEqual(len(hidden), 1)
        self.assertEqual((hidden[0]["start_ms"], hidden[0]["end_ms"]),
                         ("2800", "3300"))
        self.assertEqual(disposition(report, "fix-1")["adopted"],
                         [{"start_ms": 2000, "end_ms": 3000}])
        self.assertEqual(disposition(report, "hide-1")["adopted"],
                         [{"start_ms": 7000, "end_ms": 8000}])
        self.assertEqual(disposition(report, "fix-1")["not_adopted"], [])
        self.assertIn("First line, revised.", report["srt_text"])
        self.assertNotIn("Second line.\n\n3", report["srt_text"])

    def test_cli_ready_create_only_and_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            cmd = [sys.executable, "-m", "subtitle_conform.cli",
                   "--source", "fixtures/carry-review.json",
                   "--output", str(target), "--carry-review"]
            completed = subprocess.run(cmd, env=self._env(), capture_output=True,
                                       text=True, check=True)
            self.assertIn('"status": "ready"', completed.stdout)
            self.assertTrue(target.exists())
            # 目标已存在：退出 2，旧文件保留
            marker = b'{"keep": true}'
            target.write_bytes(marker)
            failed = subprocess.run(cmd, env=self._env(), capture_output=True,
                                    text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(target.read_bytes(), marker)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)
            # 与 --broadcast 互斥
            both = subprocess.run(
                [sys.executable, "-m", "subtitle_conform.cli",
                 "--source", "fixtures/carry-review.json",
                 "--output", str(Path(tmp) / "other.json"),
                 "--broadcast", "--carry-review"],
                env=self._env(), capture_output=True, text=True)
            self.assertEqual(both.returncode, 2)
            self.assertFalse((Path(tmp) / "other.json").exists())

    def test_cli_invalid_input_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            data = json.loads(Path("fixtures/carry-review.json").read_text())
            data["links"][0]["old_clip_id"] = "ghost"
            bad.write_text(json.dumps(data))
            target = Path(tmp) / "report.json"
            failed = subprocess.run(
                [sys.executable, "-m", "subtitle_conform.cli",
                 "--source", str(bad), "--output", str(target),
                 "--carry-review"],
                env=self._env(), capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("unknown old clip", failed.stderr)
            self.assertFalse(target.exists())

    def test_cli_blocked_still_writes_report_with_null_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "blocked.json"
            previous = conform_input([clip("A", 0, 100), clip("B", 0, 100)],
                                     [cue("q", 0, 100, "orig")])
            current = conform_input([clip("X", 0, 100)],
                                    [cue("q", 0, 100, "orig")])
            data = make_input(previous, current,
                              [link("la", "A", "X", 0, 100),
                               link("lb", "B", "X", 0, 100)],
                              [edit("e", "A", "q", 10, 20, "replace", "new")])
            src.write_text(json.dumps(data))
            target = Path(tmp) / "report.json"
            completed = subprocess.run(
                [sys.executable, "-m", "subtitle_conform.cli",
                 "--source", str(src), "--output", str(target),
                 "--carry-review"],
                env=self._env(), capture_output=True, text=True, check=True)
            self.assertIn('"status": "blocked"', completed.stdout)
            report = json.loads(target.read_text())
            self.assertIsNone(report["srt_text"])
            self.assertIsNone(report["delivery_lines"])
            self.assertIsNone(report["quality"])
            self.assertEqual(len(report["review_segments"]), 3)
            self.assertEqual(len(report["edit_dispositions"]), 1)


if __name__ == "__main__":
    unittest.main()
