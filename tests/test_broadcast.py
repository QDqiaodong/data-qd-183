"""播出端单轨字幕对版（broadcast 模式）回归测试。"""
import json
import unittest
from fractions import Fraction
from pathlib import Path

from subtitle_conform.broadcast import run_broadcast
from subtitle_conform.service import run


def source(duration=100000):
    return [{"id": "s", "duration_ms": duration}]


def clip(cid, in_ms, out_ms, num=1, den=1, source_id="s"):
    return {"id": cid, "source_id": source_id, "in_ms": in_ms,
            "out_ms": out_ms, "speed_num": num, "speed_den": den}


def cue(qid, start, end, text="A", source_id="s"):
    return {"id": qid, "source_id": source_id, "start_ms": start,
            "end_ms": end, "text": text}


def rule(identity, must_keep=False, priority=1):
    return {"fragment_id": identity, "must_keep": must_keep,
            "priority": priority}


def make_input(clips, cues, rules, joins=None, protected=None,
               shift=40, gap=0, min_duration=40, cps=1000,
               source_duration=100000):
    return {
        "conform": {
            "sources": source(source_duration),
            "clips": clips,
            "cues": cues,
            "min_duration_ms": min_duration,
            "max_chars_per_second": cps,
        },
        "broadcast": {
            "max_shift_ms": shift,
            "min_gap_frames": gap,
            "protected": [] if protected is None else protected,
            "join_cuts": [] if joins is None else joins,
            "fragment_rules": rules,
        },
    }


class JoinAcrossCutsTests(unittest.TestCase):
    """验收：同一句对白跨连续剪缝只读一次。"""

    def _two_fragments(self):
        # cut-1 源[0,100) 以 10:4 变速 -> 节目[0,40)
        # cut-2 源[100,200) 以 10:12 变速 -> 节目[40,160)
        clips = [clip("cut-1", 0, 100, 10, 4), clip("cut-2", 100, 200, 10, 12)]
        cues = [cue("line-1", 0, 200, "ABCD")]
        rules = [rule(["cut-1", "line-1"], True), rule(["cut-2", "line-1"], True)]
        return clips, cues, rules

    def test_approved_join_delivers_single_entry(self):
        clips, cues, rules = self._two_fragments()
        data = make_input(clips, cues, rules, joins=[["cut-1", "cut-2"]],
                          shift=0, min_duration=120, cps=30)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(len(report["entries"]), 1)
        entry = report["entries"][0]
        self.assertEqual((entry["start_ms"], entry["end_ms"]), (0, 160))
        self.assertEqual(entry["fragment_ids"],
                         [["cut-1", "line-1"], ["cut-2", "line-1"]])
        self.assertEqual(entry["text"], "ABCD")
        self.assertEqual([row["covered_by_entry"] for row in report["fragment_audit"]],
                         [1, 1])
        self.assertIn("00:00:00,000 --> 00:00:00,160", report["srt_text"])

    def test_without_approval_cannot_join(self):
        clips, cues, rules = self._two_fragments()
        data = make_input(clips, cues, rules, joins=[], shift=0,
                          min_duration=120, cps=30)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["entries"], [])
        self.assertIsNone(report["srt_text"])

    def test_protected_positive_overlap_blocks_join(self):
        clips, cues, rules = self._two_fragments()
        data = make_input(clips, cues, rules, joins=[["cut-1", "cut-2"]],
                          protected=[{"start_ms": 80, "end_ms": 120}],
                          shift=0, min_duration=120, cps=30)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "blocked")

    def test_protection_endpoint_contact_is_legal(self):
        # 单碎片可移动场景：保护段与条目仅端点接触
        clips = [clip("c", 0, 300, num=3, den=1)]
        cues = [cue("q", 0, 1)]  # 节目碎片精确 [0, 1/3)
        data = make_input(clips, cues, [rule(["c", "q"], True)],
                          protected=[{"start_ms": 40, "end_ms": 80}],
                          shift=40)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual((report["entries"][0]["start_ms"],
                          report["entries"][0]["end_ms"]), (0, 40))
        self.assertEqual(report["fragment_audit"][0]["end_ms"], "1/3")

    def test_same_text_different_cue_cannot_join(self):
        clips = [clip("cut-1", 0, 100, 10, 4), clip("cut-2", 100, 200, 10, 12)]
        cues = [cue("q1", 0, 100, "ABCD"), cue("q2", 100, 200, "ABCD")]
        rules = [rule(["cut-1", "q1"], True), rule(["cut-2", "q2"], True)]
        data = make_input(clips, cues, rules, joins=[["cut-1", "cut-2"]],
                          shift=0, min_duration=120, cps=30)
        self.assertEqual(run_broadcast(data)["status"], "blocked")

    def test_replayed_source_interval_cannot_join(self):
        clips = [clip("cut-1", 0, 100, 10, 4), clip("cut-2", 0, 100, 10, 12)]
        cues = [cue("line-1", 0, 200, "ABCD")]
        rules = [rule(["cut-1", "line-1"], True), rule(["cut-2", "line-1"], True)]
        data = make_input(clips, cues, rules, joins=[["cut-1", "cut-2"]],
                          shift=0, min_duration=120, cps=30)
        self.assertEqual(run_broadcast(data)["status"], "blocked")


class TinyFragmentTests(unittest.TestCase):
    """验收：旧 SRT 会舍掉的极短精确碎片仍是审核单位。"""

    def _data(self, shift):
        clips = [clip("c", 0, 300, num=3, den=1)]
        cues = [cue("q", 0, 1)]  # 精确节目区间 [0, 1/3)
        return make_input(clips, cues, [rule(["c", "q"], True)], shift=shift)

    def test_shift_40_delivers_grid_entry(self):
        report = run_broadcast(self._data(40))
        self.assertEqual(report["status"], "ready")
        self.assertEqual((report["entries"][0]["start_ms"],
                          report["entries"][0]["end_ms"]), (0, 40))
        audit = report["fragment_audit"][0]
        self.assertEqual((audit["start_ms"], audit["end_ms"]), ("0", "1/3"))
        self.assertEqual(report["scores"]["total_abs_offset_ms"], "119/3")

    def test_shift_39_has_no_legal_entry(self):
        report = run_broadcast(self._data(39))
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["fragment_audit"][0]["has_legal_entry"])
        self.assertIsNone(report["srt_text"])


class GlobalSelectionTests(unittest.TestCase):
    """验收：全局取舍、最小间隔、平局。"""

    def test_slot_choice_preserves_later_must_keep(self):
        # 两个必保碎片，gap=1 帧时早条目选择合适时槽后二者可同排
        clips = [clip("c", 0, 1000)]
        cues = [cue("m1", 0, 100), cue("m2", 120, 220)]
        rules = [rule(["c", "m1"], True), rule(["c", "m2"], True)]
        data = make_input(clips, cues, rules, shift=20, gap=1, min_duration=80)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual([(e["start_ms"], e["end_ms"]) for e in report["entries"]],
                         [(0, 80), (120, 200)])

    def test_two_legal_must_keep_can_be_jointly_impossible(self):
        clips = [clip("c", 0, 1000)]
        cues = [cue("m1", 0, 100), cue("m2", 120, 220)]
        rules = [rule(["c", "m1"], True), rule(["c", "m2"], True)]
        data = make_input(clips, cues, rules, shift=20, gap=2, min_duration=80)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["scores"]["must_keep_fragments_covered"], 1)
        # 平局规则取排序在前的 m1
        self.assertEqual(report["entries"][0]["fragment_ids"], [["c", "m1"]])
        self.assertIsNone(report["srt_text"])

    def test_min_gap_frames_enforced_between_entries(self):
        clips = [clip("c", 0, 1000)]
        cues = [cue("q1", 0, 80), cue("q2", 200, 280)]
        rules = [rule(["c", "q1"], True), rule(["c", "q2"], True)]
        data = make_input(clips, cues, rules, shift=0, gap=2)
        report = run_broadcast(data)
        first, second = report["entries"]
        self.assertGreaterEqual(second["start_ms"] - first["end_ms"], 80)

    def test_offset_tie_broken_by_lexicographic_sequence(self):
        clips = [clip("c", 0, 200)]
        cues = [cue("q", 40, 120, "T")]
        data = make_input(clips, cues, [rule(["c", "q"], True)],
                          shift=40, min_duration=120)
        report = run_broadcast(data)
        # [0,120) 与 [40,160) 等偏移 40，字典序取 start 更小者
        self.assertEqual((report["entries"][0]["start_ms"],
                          report["entries"][0]["end_ms"]), (0, 120))
        self.assertEqual(report["scores"]["total_abs_offset_ms"], "40")

    def test_identity_tie_uses_unicode_order(self):
        clips = [clip("c", 0, 400)]
        cues = [cue("zebra", 0, 80), cue("apple", 0, 80)]
        rules = [rule(["c", "zebra"], False, 7), rule(["c", "apple"], False, 7)]
        data = make_input(clips, cues, rules, shift=0)
        report = run_broadcast(data)
        self.assertEqual([e["text"] for e in report["entries"]], ["A"])

    def test_reading_speed_uses_exact_duration(self):
        clips = [clip("c", 0, 400)]
        cues = [cue("q", 0, 120, "A B C D")]  # 4 非空白代码点
        blocked = make_input(clips, cues, [rule(["c", "q"], True)],
                             shift=0, cps=30)
        self.assertEqual(run_broadcast(blocked)["status"], "blocked")
        ready = make_input(clips, cues, [rule(["c", "q"], True)],
                           shift=40, cps=30)
        report = run_broadcast(ready)
        self.assertEqual(report["status"], "ready")
        self.assertEqual((report["entries"][0]["start_ms"],
                          report["entries"][0]["end_ms"]), (0, 160))

    def test_multi_cut_variable_speed_chain(self):
        clips = [clip("c1", 0, 80),
                 clip("c2", 80, 160, num=2, den=1),
                 clip("c3", 160, 320, num=1, den=2)]
        cues = [cue("q", 0, 320, "HI")]
        rules = [rule(["c1", "q"], True), rule(["c2", "q"], True),
                 rule(["c3", "q"], True)]
        data = make_input(clips, cues, rules,
                          joins=[["c1", "c2"], ["c2", "c3"]], shift=0)
        report = run_broadcast(data)
        self.assertEqual(len(report["entries"]), 1)
        self.assertEqual((report["entries"][0]["start_ms"],
                          report["entries"][0]["end_ms"]), (0, 440))
        self.assertEqual(report["entries"][0]["fragment_ids"],
                         [["c1", "q"], ["c2", "q"], ["c3", "q"]])

    def test_groups_never_skip_intermediate_fragment(self):
        from subtitle_conform.broadcast import (
            _validate_broadcast, _enumerate_placements)
        from subtitle_conform.model import validate as v
        from subtitle_conform.timeline import conform
        clips = [clip("c1", 0, 80), clip("c2", 80, 160), clip("c3", 160, 240)]
        cues = [cue("q", 0, 240, "ABC")]
        rules = [rule(["c1", "q"]), rule(["c2", "q"]), rule(["c3", "q"])]
        data = make_input(clips, cues, rules,
                          joins=[["c1", "c2"], ["c2", "c3"]], shift=0)
        _, vclips, vcues = v(data["conform"])
        frags, spans, dur, _ = conform(vclips, vcues)
        cfg = _validate_broadcast(data, vclips, frags, dur)
        groups, *_ = _enumerate_placements(frags, spans, cfg, data["conform"])
        self.assertEqual({(g[0], g[1]) for g in groups},
                         {(0, 0), (1, 1), (2, 2), (0, 1), (1, 2), (0, 2)})


class CounterfactualTests(unittest.TestCase):
    """验收：未入选可选碎片的四类全局反事实。"""

    def test_no_legal_entry(self):
        clips = [clip("c", 0, 200)]
        cues = [cue("x", 0, 80)]
        data = make_input(clips, cues, [rule(["c", "x"], False, 2)],
                          shift=40, protected=[{"start_ms": 0, "end_ms": 120}])
        report = run_broadcast(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["entries"], [])
        cf = report["counterfactuals"][0]
        self.assertEqual(cf["reason"], "no_legal_entry")
        self.assertIsNone(cf["max_optional_priority_sum"])
        self.assertFalse(cf["has_legal_entry"])

    def test_cannot_satisfy_all_must_keep(self):
        clips = [clip("c", 0, 1000)]
        cues = [cue("m", 0, 80), cue("x", 40, 120)]
        data = make_input(clips, cues,
                          [rule(["c", "m"], True), rule(["c", "x"], False, 7)],
                          shift=0)
        report = run_broadcast(data)
        cf = {tuple(c["fragment_id"]): c for c in report["counterfactuals"]}
        self.assertEqual(cf[("c", "x")]["reason"],
                         "cannot_satisfy_all_must_keep")
        self.assertIsNone(cf[("c", "x")]["max_optional_priority_sum"])
        self.assertTrue(cf[("c", "x")]["has_legal_entry"])

    def test_optional_score_reduced_independently_checkable(self):
        clips = [clip("c", 0, 1000)]
        cues = [cue("x", 0, 80), cue("y", 40, 120), cue("z", 400, 480)]
        data = make_input(clips, cues,
                          [rule(["c", "x"], False, 9), rule(["c", "y"], False, 5),
                           rule(["c", "z"], False, 2)], shift=0)
        report = run_broadcast(data)
        self.assertEqual(report["scores"]["optional_priority_sum"], 11)
        cf = {tuple(c["fragment_id"]): c for c in report["counterfactuals"]}
        self.assertEqual(cf[("c", "y")]["reason"], "optional_score_reduced")
        # 强制 y：y(5) + z(2) = 7，x 被挤出
        self.assertEqual(cf[("c", "y")]["max_optional_priority_sum"], 7)

    def test_same_score_decided_by_later_rules(self):
        clips = [clip("c", 0, 1000)]
        cues = [cue("x", 0, 80), cue("y", 40, 120)]
        data = make_input(clips, cues,
                          [rule(["c", "x"], False, 3), rule(["c", "y"], False, 3)],
                          shift=0)
        report = run_broadcast(data)
        cf = {tuple(c["fragment_id"]): c for c in report["counterfactuals"]}
        self.assertEqual(cf[("c", "y")]["reason"],
                         "same_optional_score_decided_by_later_rules")
        self.assertEqual(cf[("c", "y")]["max_optional_priority_sum"], 3)

    def test_counterfactual_achieved_only_by_group_split(self):
        # 连读组（10 分）与后续碎片 fx 在 gap=1 帧下互斥；
        # 强制 fx 必须拆掉连读组并放弃 f2，总分降到 8
        clips = [clip("c1", 0, 80), clip("c2", 80, 160), clip("c3", 160, 240)]
        cues = [cue("q", 0, 160, "AB"), cue("x", 160, 240)]
        rules = [rule(["c1", "q"], False, 5), rule(["c2", "q"], False, 5),
                 rule(["c3", "x"], False, 3)]
        data = make_input(clips, cues, rules,
                          joins=[["c1", "c2"], ["c2", "c3"]], shift=0, gap=1)
        report = run_broadcast(data)
        self.assertEqual(report["scores"]["optional_priority_sum"], 10)
        self.assertEqual(report["entries"][0]["fragment_ids"],
                         [["c1", "q"], ["c2", "q"]])
        cf = {tuple(c["fragment_id"]): c for c in report["counterfactuals"]}
        self.assertEqual(cf[("c3", "x")]["reason"], "optional_score_reduced")
        self.assertEqual(cf[("c3", "x")]["max_optional_priority_sum"], 8)

    def test_blocked_emits_no_counterfactuals(self):
        clips = [clip("c", 0, 1000)]
        cues = [cue("m1", 0, 100), cue("m2", 120, 220)]
        data = make_input(clips, cues,
                          [rule(["c", "m1"], True), rule(["c", "m2"], True)],
                          shift=20, gap=2, min_duration=80)
        report = run_broadcast(data)
        self.assertEqual(report["status"], "blocked")
        self.assertNotIn("counterfactuals", report)
        self.assertIsNone(report["srt_text"])


class ValidationTests(unittest.TestCase):
    def _good(self):
        clips = [clip("c1", 0, 80), clip("c2", 80, 160)]
        cues = [cue("q", 0, 160, "AB")]
        rules = [rule(["c1", "q"], True), rule(["c2", "q"], True)]
        return make_input(clips, cues, rules, joins=[["c1", "c2"]], shift=0)

    def _expect(self, mutator, needle):
        data = self._good()
        mutator(data)
        with self.assertRaisesRegex(ValueError, needle):
            run_broadcast(data)

    def test_booleans_rejected_as_integers(self):
        self._expect(lambda d: d["broadcast"].__setitem__("max_shift_ms", True),
                     "max_shift_ms")
        self._expect(lambda d: d["broadcast"].__setitem__("min_gap_frames", False),
                     "min_gap_frames")

    def test_ranges_enforced(self):
        self._expect(lambda d: d["broadcast"].__setitem__("max_shift_ms", 201),
                     "max_shift_ms")
        self._expect(lambda d: d["broadcast"].__setitem__("min_gap_frames", 6),
                     "min_gap_frames")
        self._expect(lambda d: d["broadcast"]["fragment_rules"][0].__setitem__(
            "priority", 1001), "priority")

    def test_unknown_and_missing_fields(self):
        self._expect(lambda d: d.__setitem__("extra", 1), "unknown")
        self._expect(lambda d: d["broadcast"].__setitem__("extra", 1), "unknown")
        self._expect(lambda d: d["broadcast"]["fragment_rules"][0].__setitem__(
            "x", 1), "unknown")
        self._expect(lambda d: d["broadcast"].pop("protected"), "missing")

    def test_protected_validation(self):
        self._expect(lambda d: d["broadcast"].__setitem__(
            "protected", [{"start_ms": 40, "end_ms": 40}]), "positive duration")
        self._expect(lambda d: d["broadcast"].__setitem__(
            "protected", [{"start_ms": 0, "end_ms": 999}]), "outside program")
        self._expect(lambda d: d["broadcast"].__setitem__(
            "protected", [{"start_ms": True, "end_ms": 1}]), "start_ms")

    def test_join_cut_validation(self):
        self._expect(lambda d: d["broadcast"].__setitem__(
            "join_cuts", [["c2", "c1"]]), "adjacent")
        self._expect(lambda d: d["broadcast"].__setitem__(
            "join_cuts", [["c1", "nope"]]), "unknown clip")
        self._expect(lambda d: d["broadcast"]["join_cuts"].append(
            ["c1", "c2"]), "duplicate")

    def test_fragment_rule_validation(self):
        self._expect(lambda d: d["broadcast"]["fragment_rules"].append(
            rule(["c1", "ghost"])), "unknown fragment")
        self._expect(lambda d: d["broadcast"]["fragment_rules"].append(
            rule(["c1", "q"])), "duplicate fragment_rules")
        self._expect(lambda d: d["broadcast"]["fragment_rules"][0].__setitem__(
            "must_keep", 1), "must_keep")

    def test_legal_clip_cue_pair_without_intersection_is_unknown(self):
        data = self._good()
        data["conform"]["cues"] = [cue("q", 500, 600, "Z")]
        with self.assertRaisesRegex(ValueError, "unknown fragment"):
            run_broadcast(data)

    def test_protected_limit(self):
        data = self._good()
        data["broadcast"]["protected"] = [
            {"start_ms": 0, "end_ms": 1}] * 201
        with self.assertRaisesRegex(ValueError, "protected"):
            run_broadcast(data)


class BruteForceCrossCheckTests(unittest.TestCase):
    """独立暴力枚举参考实现，交叉验证 DP 的五级选解与反事实分数。"""

    @staticmethod
    def _brute_force(data):
        from subtitle_conform.broadcast import (
            _validate_broadcast, _enumerate_placements)
        from subtitle_conform.model import validate as v
        from subtitle_conform.timeline import conform
        _, vclips, vcues = v(data["conform"])
        frags, spans, dur, _ = conform(vclips, vcues)
        cfg = _validate_broadcast(data, vclips, frags, dur)
        groups, *_ = _enumerate_placements(frags, spans, cfg, data["conform"])
        gap = cfg["gap_ms"]
        placements = [p for g in groups for p in g[2]]

        plans = [[]]
        for placement in placements:
            used = set(range(placement["i"], placement["j"] + 1))
            updated = []
            for plan in plans:
                updated.append(plan)
                if all(not (used & set(range(q["i"], q["j"] + 1)))
                       and placement["start"] >= q["end"] + gap
                       for q in plan):
                    updated.append(plan + [placement])
            plans = updated
        unique = {frozenset(id(p) for p in plan): plan for plan in plans}
        scored = []
        for plan in unique.values():
            plan.sort(key=lambda p: p["i"])
            scored.append((
                sum(p["must"] for p in plan),
                sum(p["opt"] for p in plan),
                len(plan),
                sum(p["offset"] for p in plan),
                tuple(p["key"] for p in plan),
                plan))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2], t[3], t[4]))
        total_must = sum(
            1 for r in frags
            if cfg["rules"].get(tuple(r["fragment_id"]), (False, 1))[0])
        best = scored[0]
        covered = set()
        for p in best[5]:
            covered.update(range(p["i"], p["j"] + 1))
        counterfactual = {}
        for idx, row in enumerate(frags):
            identity = tuple(row["fragment_id"])
            is_must = cfg["rules"].get(identity, (False, 1))[0]
            if is_must or idx in covered:
                continue
            any_entry = any(p["i"] <= idx <= p["j"] for p in placements)
            best_opt = None
            for candidate in scored:
                if candidate[0] != total_must:
                    continue
                if any(p["i"] <= idx <= p["j"] for p in candidate[5]):
                    best_opt = candidate[1]
                    break
            counterfactual[identity] = (any_entry, best_opt)
        return best, counterfactual, total_must

    def _random_data(self, seed):
        import random
        rng = random.Random(seed)
        n_clips = rng.randint(1, 3)
        clips = [clip(f"c{i}", i * 120, (i + 1) * 120,
                      num=rng.choice([1, 2])) for i in range(n_clips)]
        src_len = n_clips * 120
        cues = []
        for k in range(rng.randint(1, 4)):
            start = rng.randint(0, src_len - 20)
            cues.append(cue(f"q{k}", start,
                            min(start + rng.randint(8, 80), src_len),
                            rng.choice(["A", "AB", "XYZ"])))
        base_clips = clips
        from subtitle_conform.model import validate as v
        from subtitle_conform.timeline import conform
        base = {"sources": source(src_len + 1), "clips": base_clips,
                "cues": cues, "min_duration_ms": 40,
                "max_chars_per_second": 1000}
        _, vclips, vcues = v(base)
        frags, _spans, program_dur, _ = conform(vclips, vcues)
        rules = [rule(fr["fragment_id"], rng.random() < 0.3,
                      rng.randint(1, 5)) for fr in frags]
        joins = [[f"c{i}", f"c{i+1}"] for i in range(n_clips - 1)
                 if rng.random() < 0.5]
        protected = []
        if rng.random() < 0.3 and program_dur > 40:
            start = rng.randint(0, max(0, float(program_dur) - 40))
            protected = [{"start_ms": start,
                          "end_ms": min(float(program_dur),
                                        start + rng.randint(20, 60))}]
        return {"conform": base,
                "broadcast": {"max_shift_ms": rng.choice([0, 40, 80]),
                              "min_gap_frames": rng.randint(0, 2),
                              "protected": protected, "join_cuts": joins,
                              "fragment_rules": rules}}

    def test_dp_matches_brute_force_on_random_cases(self):
        categories = set()
        checked = 0
        for seed in range(120):
            data = self._random_data(seed)
            try:
                report = run_broadcast(data)
            except ValueError:
                continue
            checked += 1
            best, brute_cf, _total_must = self._brute_force(data)
            scores = report["scores"]
            self.assertEqual(
                (scores["must_keep_fragments_covered"],
                 scores["optional_priority_sum"], scores["entry_count"]),
                (best[0], best[1], best[2]))
            self.assertEqual(Fraction(scores["total_abs_offset_ms"]), best[3])
            dp_sequence = tuple(
                (Fraction(e["start_ms"]), Fraction(e["end_ms"]),
                 tuple(tuple(identity) for identity in e["fragment_ids"]))
                for e in report["entries"])
            self.assertEqual(
                dp_sequence,
                tuple((p["start"], p["end"], p["ids"]) for p in best[5]))
            if report["status"] == "ready":
                for item in report["counterfactuals"]:
                    identity = tuple(item["fragment_id"])
                    self.assertEqual(
                        (item["has_legal_entry"],
                         item["max_optional_priority_sum"]),
                        brute_cf[identity])
                    categories.add(item["reason"])
        self.assertGreater(checked, 80)
        # 随机集应覆盖到至少三类反事实（no_legal_entry 由专门单测保证）
        self.assertIn("optional_score_reduced", categories)
        self.assertIn("same_optional_score_decided_by_later_rules", categories)


class EdgeAndCompatibilityTests(unittest.TestCase):
    def test_zero_fragments_is_ready_with_empty_srt(self):
        clips = [clip("c", 0, 100)]
        cues = [cue("q", 500, 600)]
        data = make_input(clips, cues, [])
        report = run_broadcast(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["entries"], [])
        self.assertEqual(report["srt_text"], "")
        self.assertEqual(report["fragment_audit"], [])
        self.assertEqual(report["counterfactuals"], [])

    def test_default_optional_priority_is_one(self):
        clips = [clip("c", 0, 200)]
        cues = [cue("q", 0, 80)]
        data = make_input(clips, cues, [])  # 未声明 -> 可选，优先值 1
        report = run_broadcast(data)
        self.assertEqual(report["fragment_audit"][0]["priority"], 1)
        self.assertFalse(report["fragment_audit"][0]["must_keep"])
        self.assertEqual(report["scores"]["optional_priority_sum"], 1)

    def test_legacy_run_unchanged(self):
        legacy = json.loads(Path("fixtures/input.json").read_text())
        result = run(legacy)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["summary"]["srt_cues"], 3)
        self.assertNotIn("mode", result)

    def test_fixture_runs_through_cli_and_keeps_create_only_target(self):
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            env = dict(__import__("os").environ,
                       PYTHONPATH=str(Path("src").resolve()))
            cmd = [sys.executable, "-m", "subtitle_conform.cli",
                   "--source", "fixtures/broadcast.json",
                   "--output", str(target), "--broadcast"]
            completed = subprocess.run(cmd, env=env, capture_output=True,
                                       text=True, check=True)
            self.assertIn('"status": "ready"', completed.stdout)
            self.assertTrue(target.exists())
            # 目标已存在：退出 2，旧文件保留，不产生第二个 SRT 文件
            marker = b'{"keep": true}'
            target.write_bytes(marker)
            failed = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(target.read_bytes(), marker)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_fixture_runs_and_audits_all_exact_fragments(self):
        data = json.loads(Path("fixtures/broadcast.json").read_text())
        report = run_broadcast(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(len(report["fragment_audit"]), 2)
        self.assertEqual(report["output_duration_ms"], "160")


if __name__ == "__main__":
    unittest.main()
