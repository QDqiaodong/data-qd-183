"""subtitle-cost 跨节目译制费分账回归测试。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from subtitle_cost.cost import (
    run, run_collect, run_merge, run_settle, union)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def conform_input(clips, cues=None, sources=None,
                  min_duration=0, max_cps=1000):
    return {
        "sources": sources if sources is not None
        else [{"id": "s", "duration_ms": 100000}],
        "clips": clips,
        "cues": cues if cues is not None else [
            {"id": "c", "source_id": "s", "start_ms": 0, "end_ms": 100000,
             "text": "台词"}],
        "min_duration_ms": min_duration,
        "max_chars_per_second": max_cps,
    }


def clip(cid, start, end, num=1, den=1, source_id="s"):
    return {"id": cid, "source_id": source_id, "in_ms": start,
            "out_ms": end, "speed_num": num, "speed_den": den}


def cue(cid, start, end, source_id="s", text="台词"):
    return {"id": cid, "source_id": source_id, "start_ms": start,
            "end_ms": end, "text": text}


def usage_row(source_id="s", cue_id="c", bounds=(0, 100), intervals=()):
    return {"source_id": source_id, "cue_id": cue_id,
            "cue_start_ms": bounds[0], "cue_end_ms": bounds[1],
            "intervals": [list(span) for span in intervals]}


def program(pid, center, rows=None, withdrawn=False):
    if rows is None:
        rows = [usage_row(intervals=[(0, 10)])]
    return {"program_id": pid, "cost_center": center,
            "withdrawn": withdrawn, "usage": rows}


def bundle(programs):
    return {"schema_version": 1, "mode": "subtitle-cost", "op": "bundle",
            "programs": programs}


def merge_input(bundles, withdraw=()):
    return {"op": "merge", "bundles": bundles,
            "withdraw": list(withdraw)}


def collect_input(pid, center, conform, op="collect"):
    return {"op": op, "program_id": pid, "cost_center": center,
            "conform": conform}


def settle_input(bundle_obj, fees):
    return {"op": "settle", "bundle": bundle_obj, "fees": fees}


def fee(source_id="s", cue_id="c", bounds=(0, 100), fen=101):
    return {"source_id": source_id, "cue_id": cue_id,
            "cue_start_ms": bounds[0], "cue_end_ms": bounds[1],
            "fee_fen": fen}


def intervals_of(bundle_obj, pid, cue_key):
    prog = next(p for p in bundle_obj["programs"]
                if p["program_id"] == pid)
    row = next(r for r in prog["usage"]
               if (r["source_id"], r["cue_id"]) == cue_key)
    return [tuple(span) for span in row["intervals"]]


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

class CollectTests(unittest.TestCase):
    def test_union_of_repeated_cuts_counts_coverage_once(self):
        data = collect_input("P", "A", conform_input([
            clip("cut-1", 10, 60), clip("cut-2", 40, 90)]))
        result = run_collect(data)
        self.assertEqual(result["op"], "bundle")
        self.assertEqual(intervals_of(result, "P", ("s", "c")),
                         [(10, 90)])
        self.assertEqual(len(result["programs"]), 1)
        self.assertFalse(result["programs"][0]["withdrawn"])

    def test_disjoint_cuts_keep_separate_intervals(self):
        data = collect_input("P", "A", conform_input([
            clip("cut-1", 0, 30), clip("cut-2", 60, 90)]))
        result = run_collect(data)
        self.assertEqual(intervals_of(result, "P", ("s", "c")),
                         [(0, 30), (60, 90)])

    def test_speed_change_does_not_change_source_usage(self):
        slow = collect_input("P", "A", conform_input(
            [clip("cut-1", 100, 400, num=1, den=1)]))
        fast = collect_input("P", "A", conform_input(
            [clip("cut-1", 100, 400, num=3, den=1)]))
        self.assertEqual(
            intervals_of(run_collect(slow), "P", ("s", "c")),
            intervals_of(run_collect(fast), "P", ("s", "c")))
        self.assertEqual(
            intervals_of(run_collect(fast), "P", ("s", "c")), [(100, 400)])

    def test_srt_dropped_short_fragment_is_still_counted(self):
        conform = conform_input(
            [clip("cut-1", 200, 201, num=3, den=1)],
            cues=[cue("c", 199, 250)], min_duration=600)
        # 普通对版 SRT 确实舍掉该碎片
        from subtitle_conform.service import run as run_conform
        conform_report = run_conform(conform)
        self.assertEqual(conform_report["srt_cues"], 0)
        self.assertEqual(conform_report["srt_dropped_fragments"],
                         [["cut-1", "c"]])
        result = run_collect(collect_input("P", "A", conform))
        self.assertEqual(intervals_of(result, "P", ("s", "c")),
                         [(200, 201)])

    def test_records_original_cue_bounds_and_per_cue_union(self):
        conform = conform_input(
            [clip("cut-1", 0, 1000), clip("cut-2", 3000, 4000)],
            cues=[cue("c1", 0, 2000), cue("c2", 2500, 5000)])
        result = run_collect(collect_input("P", "A", conform))
        rows = {(r["source_id"], r["cue_id"]): r
                for r in result["programs"][0]["usage"]}
        self.assertEqual((rows[("s", "c1")]["cue_start_ms"],
                          rows[("s", "c1")]["cue_end_ms"]), (0, 2000))
        self.assertEqual(rows[("s", "c1")]["intervals"], [[0, 1000]])
        self.assertEqual(rows[("s", "c2")]["intervals"], [[3000, 4000]])
        # 稳定顺序：按 (source_id, cue_id)
        self.assertEqual([(r["source_id"], r["cue_id"])
                          for r in result["programs"][0]["usage"]],
                         [("s", "c1"), ("s", "c2")])

    def test_unused_cue_is_omitted_but_no_text_in_bundle(self):
        conform = conform_input(
            [clip("cut-1", 0, 100)],
            cues=[cue("c1", 0, 200), cue("c2", 500, 600)])
        result = run_collect(collect_input("P", "A", conform))
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("台词", encoded)
        self.assertEqual([r["cue_id"]
                          for r in result["programs"][0]["usage"]], ["c1"])

    def test_touching_intervals_merge(self):
        self.assertEqual(union([(0, 50), (50, 100), (30, 60)]),
                         [[0, 100]])


# ---------------------------------------------------------------------------
# merge：归并、去重、撤回
# ---------------------------------------------------------------------------

class MergeTests(unittest.TestCase):
    def _pr(self):
        return (bundle([program("P", "A", [usage_row(
                    intervals=[(0, 60)])])]),
                bundle([program("R", "B", [usage_row(
                    intervals=[(20, 80)])])]))

    def test_dedup_identical_program_across_bundles(self):
        p_bundle = bundle([program("P", "A")])
        result = run_merge(merge_input([p_bundle, bundle([
            program("P", "A")])]))  # 同 ID 同中心同规范用量
        self.assertEqual([p["program_id"] for p in result["programs"]],
                         ["P"])

    def test_conflicting_content_rejected_even_when_withdrawn(self):
        p_bundle = bundle([program("P", "A")])
        withdrawn = run_merge(merge_input([p_bundle], withdraw=["P"]))
        # 即使 P 已撤回，同 ID 不同成本中心仍须拒绝
        with self.assertRaisesRegex(ValueError, "conflicting program"):
            run_merge(merge_input([withdrawn, bundle([
                program("P", "Z")])]))
        # 同 ID 不同用量同样拒绝
        with self.assertRaisesRegex(ValueError, "conflicting program"):
            run_merge(merge_input([withdrawn, bundle([
                program("P", "A", [usage_row(intervals=[(1, 9)])])])]))

    def test_withdrawal_precedes_bundle_arrival(self):
        # 撤回先于用量到达：输出无内容空墓碑
        first = run_merge(merge_input([], withdraw=["X"]))
        self.assertEqual(first["programs"], [
            {"program_id": "X", "withdrawn": True}])
        # 后到的同 ID 用量不能恢复它，墓碑吸收该内容但保持撤回
        again = run_merge(merge_input(
            [first, bundle([program("X", "A")])]))
        self.assertEqual(len(again["programs"]), 1)
        entry = again["programs"][0]
        self.assertTrue(entry["withdrawn"])
        self.assertEqual(entry["cost_center"], "A")
        self.assertEqual(entry["usage"][0]["intervals"], [[0, 10]])
        # 再来一个不同内容的同 ID 包仍须拒绝（墓碑已带内容，可做冲突检测）
        with self.assertRaisesRegex(ValueError, "conflicting program"):
            run_merge(merge_input([
                again, bundle([program("X", "Z")])]))

    def test_old_bundle_resend_cannot_restore_withdrawn_program(self):
        p_bundle, r_bundle = self._pr()
        merged = run_merge(merge_input([p_bundle, r_bundle]))
        withdrawn = run_merge(merge_input([merged], withdraw=["P"]))
        # 旧包重送
        resent = run_merge(merge_input([withdrawn, p_bundle]))
        statuses = {p["program_id"]: p["withdrawn"]
                    for p in resent["programs"]}
        self.assertEqual(statuses, {"P": True, "R": False})

    def test_nested_merge_tree_flattens_and_preserves_overlap(self):
        p_bundle, r_bundle = self._pr()
        inner = {"op": "merge", "bundles": [p_bundle, r_bundle],
                 "withdraw": []}
        outer = run_merge(merge_input([inner, bundle([
            program("Q", "A", [usage_row(intervals=[(40, 100)])])])]))
        self.assertEqual(
            sorted(p["program_id"] for p in outer["programs"]),
            ["P", "Q", "R"])
        # 不折算成中心总量：中心 A 下仍可分别撤回 P 与 Q
        centers = {p["program_id"]: p["cost_center"]
                   for p in outer["programs"]}
        self.assertEqual(centers["P"], "A")
        self.assertEqual(centers["Q"], "A")

    def test_withdraw_keeps_center_overlap_of_other_programs(self):
        p_bundle, r_bundle = self._pr()
        merged = run_merge(merge_input([
            p_bundle, r_bundle,
            bundle([program("Q", "A", [usage_row(
                intervals=[(40, 100)])])])]))
        withdrawn = run_merge(merge_input([merged], withdraw=["P"]))
        q = next(p for p in withdrawn["programs"]
                 if p["program_id"] == "Q")
        # 撤回 P 后 Q 的 [40,60) 覆盖完整保留
        self.assertEqual(q["usage"][0]["intervals"], [[40, 100]])

    def test_remerge_earliest_bundle_is_idempotent(self):
        p_bundle, r_bundle = self._pr()
        first = run_merge(merge_input([p_bundle, r_bundle]))
        with_q = run_merge(merge_input([first, bundle([
            program("Q", "A", [usage_row(intervals=[(40, 100)])])])]))
        withdrawn = run_merge(merge_input([with_q], withdraw=["P"]))
        # 再并回最早的 P/R 包，撤回状态与用量不变
        again = run_merge(merge_input([withdrawn, first]))
        self.assertEqual(
            json.dumps(again, sort_keys=True),
            json.dumps(withdrawn, sort_keys=True))

    def test_withdraw_unknown_id_creates_tombstone(self):
        result = run_merge(merge_input(
            [bundle([program("P", "A")])], withdraw=["ghost"]))
        ids = {p["program_id"]: p["withdrawn"]
               for p in result["programs"]}
        self.assertEqual(ids, {"P": False, "ghost": True})

    def test_withdraw_inside_nested_node_is_applied(self):
        inner = {"op": "merge",
                 "bundles": [bundle([program("P", "A")]),
                             bundle([program("R", "B")])],
                 "withdraw": ["P"]}  # 撤回挂在内层节点
        result = run_merge(merge_input([inner]))
        statuses = {p["program_id"]: p["withdrawn"]
                    for p in result["programs"]}
        self.assertEqual(statuses, {"P": True, "R": False})

    def test_same_withdraw_id_at_multiple_levels_is_idempotent(self):
        inner = {"op": "merge",
                 "bundles": [bundle([program("P", "A")])],
                 "withdraw": ["P"]}
        result = run_merge(merge_input([inner], withdraw=["P"]))
        self.assertEqual(
            [p["program_id"] for p in result["programs"]], ["P"])
        self.assertTrue(result["programs"][0]["withdrawn"])

    def test_output_sorted_by_program_id_unicode(self):
        result = run_merge(merge_input([
            bundle([program("节目乙", "A")]),
            bundle([program("节目甲", "A")])]))
        self.assertEqual([p["program_id"] for p in result["programs"]],
                         sorted(["节目乙", "节目甲"]))


# ---------------------------------------------------------------------------
# settle：权重、最大余数、待摊、对平
# ---------------------------------------------------------------------------

class SettleTests(unittest.TestCase):
    def _three_programs(self):
        return bundle([
            program("P", "A", [usage_row(intervals=[(0, 60)])]),
            program("Q", "A", [usage_row(intervals=[(40, 100)])]),
            program("R", "B", [usage_row(intervals=[(20, 80)])]),
        ])

    def test_mandatory_weights_and_allocation_71_30(self):
        report = run_settle(settle_input(
            self._three_programs(), [fee(fen=101)]))
        cue = report["cues"][0]
        by_center = {c["cost_center"]: c for c in cue["centers"]}
        self.assertEqual(by_center["A"]["weight_ms"], "70")
        self.assertEqual(by_center["B"]["weight_ms"], "30")
        self.assertEqual(by_center["A"]["allocated_fen"], 71)
        self.assertEqual(by_center["B"]["allocated_fen"], 30)
        self.assertEqual(report["center_totals"], [
            {"cost_center": "A", "total_fen": 71},
            {"cost_center": "B", "total_fen": 30}])

    def test_withdraw_p_leaves_equal_weights_51_50(self):
        merged = run_merge(merge_input(
            [self._three_programs()], withdraw=["P"]))
        report = run_settle(settle_input(merged, [fee(fen=101)]))
        by_center = {c["cost_center"]: c
                     for c in report["cues"][0]["centers"]}
        self.assertEqual(by_center["A"]["weight_ms"], "40")
        self.assertEqual(by_center["B"]["weight_ms"], "40")
        # 平局余分按中心 ID Unicode 码点序，A 在 B 前
        self.assertEqual(by_center["A"]["allocated_fen"], 51)
        self.assertEqual(by_center["B"]["allocated_fen"], 50)
        self.assertEqual(
            report["summary"]["withdrawn_program_count"], 1)

    def test_segment_split_equally_among_covering_centers(self):
        # [0,30) 仅 A；[30,60) A+B 各分一半；[60,90) 仅 B
        b = bundle([
            program("P", "A", [usage_row(intervals=[(0, 60)])]),
            program("R", "B", [usage_row(intervals=[(30, 90)])])])
        report = run_settle(settle_input(b, [fee(fen=90)]))
        weights = {c["cost_center"]: Fraction(c["weight_ms"])
                   for c in report["cues"][0]["centers"]}
        self.assertEqual(weights, {"A": Fraction(45), "B": Fraction(45)})

    def test_same_center_multiple_programs_do_not_add_share(self):
        # 同中心两个节目覆盖完全相同的区间，份额不翻倍
        b = bundle([
            program("P", "A", [usage_row(intervals=[(0, 100)])]),
            program("Q", "A", [usage_row(intervals=[(0, 100)])]),
            program("R", "B", [usage_row(intervals=[(0, 100)])])])
        report = run_settle(settle_input(b, [fee(fen=100)]))
        alloc = {c["cost_center"]: c["allocated_fen"]
                 for c in report["cues"][0]["centers"]}
        self.assertEqual(alloc, {"A": 50, "B": 50})

    def test_exact_fractional_weights(self):
        # 单点等分产生分数权重：[0,30) A 独占 30；[30,60) A/B 各 15；
        # [60,90) A/B/C 各 10 -> A=55, B=25, C=10，总 90
        b = bundle([
            program("P", "A", [usage_row(intervals=[(0, 90)])]),
            program("R", "B", [usage_row(intervals=[(30, 90)])]),
            program("S", "C", [usage_row(intervals=[(60, 90)])])])
        report = run_settle(settle_input(b, [fee(fen=90)]))
        weights = {c["cost_center"]: c["weight_ms"]
                   for c in report["cues"][0]["centers"]}
        self.assertEqual(weights, {"A": "55", "B": "25", "C": "10"})

    def test_largest_remainder_distribution(self):
        # 权重 1:1:1，费用 100 -> 各 33，余 1 分给码点序最前的中心
        b = bundle([
            program("P", "A", [usage_row(intervals=[(0, 10)])]),
            program("Q", "B", [usage_row(intervals=[(0, 10)])]),
            program("R", "C", [usage_row(intervals=[(0, 10)])])])
        report = run_settle(settle_input(b, [fee(fen=100)]))
        alloc = {c["cost_center"]: c["allocated_fen"]
                 for c in report["cues"][0]["centers"]}
        self.assertEqual(alloc, {"A": 34, "B": 33, "C": 33})

    def test_remainder_order_uses_fraction_size_not_center_order(self):
        # 独占段构造精确权重 1:2:3；费用 10 -> 精确 5/3,10/3,5，
        # 向下取整 1,3,5 合计 9，余 1 归小数余数最大的 A（2,3,5）
        b = bundle([
            program("P", "A", [usage_row(bounds=(0, 6),
                                         intervals=[(0, 1)])]),
            program("Q", "B", [usage_row(bounds=(0, 6),
                                         intervals=[(1, 3)])]),
            program("R", "C", [usage_row(bounds=(0, 6),
                                         intervals=[(3, 6)])])])
        report = run_settle(settle_input(b, [fee(bounds=(0, 6), fen=10)]))
        alloc = {c["cost_center"]: c["allocated_fen"]
                 for c in report["cues"][0]["centers"]}
        self.assertEqual(alloc, {"A": 2, "B": 3, "C": 5})

    def test_unicode_tie_break_order(self):
        b = bundle([
            program("P", "中心乙", [usage_row(intervals=[(0, 10)])]),
            program("Q", "中心甲", [usage_row(intervals=[(0, 10)])])])
        report = run_settle(settle_input(b, [fee(fen=1)]))
        centers = [c["cost_center"] for c in report["cues"][0]["centers"]]
        self.assertEqual(centers, ["中心乙", "中心甲"])
        alloc = {c["cost_center"]: c["allocated_fen"]
                 for c in report["cues"][0]["centers"]}
        self.assertEqual(alloc["中心乙"], 1)
        self.assertEqual(alloc["中心甲"], 0)

    def test_all_withdrawn_moves_fee_to_deferred(self):
        b = bundle([
            program("P", "A", [usage_row(intervals=[(0, 60)])],
                    withdrawn=True),
            program("R", "B", [usage_row(intervals=[(20, 80)])],
                    withdrawn=True)])
        report = run_settle(settle_input(b, [fee(fen=101), fee(
            cue_id="idle", bounds=(0, 10), fen=7)]))
        self.assertEqual(report["cues"], [])
        self.assertEqual(report["center_totals"], [])
        self.assertEqual(report["summary"]["allocated_total_fen"], 0)
        self.assertEqual(report["summary"]["deferred_total_fen"], 108)
        self.assertEqual(
            [f["cue_id"] for f in report["deferred_fees"]],
            ["c", "idle"])

    def test_deferred_fees_listed_and_accounts_balance(self):
        b = bundle([program("P", "A", [usage_row(
            intervals=[(0, 60)])])])
        report = run_settle(settle_input(b, [
            fee(fen=101),
            fee(cue_id="idle", bounds=(0, 10), fen=37)]))
        self.assertEqual([f["cue_id"]
                          for f in report["deferred_fees"]], ["idle"])
        total_fees = 101 + 37
        self.assertEqual(
            report["summary"]["allocated_total_fen"]
            + report["summary"]["deferred_total_fen"], total_fees)
        allocated = sum(c["allocated_fen"]
                        for cue in report["cues"] for c in cue["centers"])
        self.assertEqual(
            allocated + sum(f["fee_fen"]
                            for f in report["deferred_fees"]), total_fees)

    def test_zero_fee_allocates_zero(self):
        b = bundle([
            program("P", "A", [usage_row(intervals=[(0, 60)])]),
            program("R", "B", [usage_row(intervals=[(20, 80)])])])
        report = run_settle(settle_input(b, [fee(fen=0)]))
        self.assertEqual(
            [c["allocated_fen"] for c in report["cues"][0]["centers"]],
            [0, 0])
        self.assertEqual(report["summary"]["allocated_total_fen"], 0)

    def test_missing_quote_for_in_use_cue_rejected(self):
        b = bundle([program("P", "A")])
        with self.assertRaisesRegex(ValueError, "missing fee quote"):
            run_settle(settle_input(b, []))

    def test_quote_bounds_mismatch_rejected(self):
        b = bundle([program("P", "A", [usage_row(
            bounds=(0, 100), intervals=[(0, 60)])])])
        with self.assertRaisesRegex(ValueError, "bounds mismatch"):
            run_settle(settle_input(b, [fee(bounds=(0, 99), fen=10)]))

    def test_inconsistent_cue_bounds_across_programs_rejected(self):
        b = bundle([
            program("P", "A", [usage_row(
                bounds=(0, 100), intervals=[(0, 60)])]),
            program("R", "B", [usage_row(
                bounds=(0, 90), intervals=[(20, 80)])])])
        with self.assertRaisesRegex(ValueError, "inconsistent original cue"):
            run_settle(settle_input(b, [fee(fen=10)]))

    def test_duplicate_fee_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate fee key"):
            run_settle(settle_input(
                bundle([program("P", "A")]), [fee(fen=1), fee(fen=2)]))

    def test_multi_cue_reports_sorted_and_totals_aggregated(self):
        rows_a = [
            usage_row("s", "c1", (0, 100), [(0, 100)]),
            usage_row("s", "c2", (0, 50), [(0, 50)])]
        b = bundle([
            program("P", "A", rows_a),
            program("R", "B", [usage_row("s", "c1", (0, 100),
                                         [(0, 100)])])])
        report = run_settle(settle_input(b, [
            fee("s", "c1", (0, 100), 100), fee("s", "c2", (0, 50), 40)]))
        self.assertEqual([(c["source_id"], c["cue_id"])
                          for c in report["cues"]],
                         [("s", "c1"), ("s", "c2")])
        totals = {c["cost_center"]: c["total_fen"]
                  for c in report["center_totals"]}
        # c1 对半 50/50；c2 仅 A 拿 40
        self.assertEqual(totals, {"A": 90, "B": 50})
        self.assertEqual(
            report["summary"]["allocated_total_fen"], 140)


# ---------------------------------------------------------------------------
# 端到端必测流程
# ---------------------------------------------------------------------------

class MandatoryFlowTests(unittest.TestCase):
    def test_full_mandatory_sequence(self):
        def b(pid, center, spans):
            return bundle([program(pid, center, [
                usage_row(intervals=spans)])])

        fees = [fee(fen=101)]
        # 先归并 P、R，再收入 Q -> A 71, B 30
        pr = run_merge(merge_input([
            b("P", "A", [(0, 60)]), b("R", "B", [(20, 80)])]))
        full = run_merge(merge_input([
            pr, b("Q", "A", [(40, 100)])]))
        first = run_settle(settle_input(full, fees))
        alloc = {c["cost_center"]: c["allocated_fen"]
                 for c in first["cues"][0]["centers"]}
        self.assertEqual(alloc, {"A": 71, "B": 30})

        # 撤回 P -> A 的 [40,60) 仍由 Q 覆盖，权重均 40 -> 51/50
        withdrawn = run_merge(merge_input([full], withdraw=["P"]))
        second = run_settle(settle_input(withdrawn, fees))
        alloc = {c["cost_center"]: c["allocated_fen"]
                 for c in second["cues"][0]["centers"]}
        self.assertEqual(alloc, {"A": 51, "B": 50})

        # 再并回最早的 P/R 包，结果不变
        remerged = run_merge(merge_input([withdrawn, pr]))
        third = run_settle(settle_input(remerged, fees))
        self.assertEqual(
            json.dumps(third, sort_keys=True),
            json.dumps(second, sort_keys=True))

        # 换成成本中心不同的同 ID P 包则须拒绝
        with self.assertRaisesRegex(ValueError, "conflicting program"):
            run_merge(merge_input(
                [withdrawn, b("P", "Z", [(0, 60)])]))


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_unknown_op_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown op"):
            run({"op": "nope"})

    def test_strict_root_fields(self):
        good = collect_input("P", "A", conform_input([clip("c", 0, 10)]))
        bad = dict(good, extra=1)
        with self.assertRaisesRegex(ValueError, "unknown"):
            run(bad)
        bad = dict(good)
        del bad["cost_center"]
        with self.assertRaisesRegex(ValueError, "missing"):
            run(bad)

    def test_bad_intervals_rejected(self):
        cases = [
            [[]],                                   # 空区间数组
            [[10]],                                 # 长度不为 2
            [[100, 100]],                            # 零长
            [[200, 100]],                            # 倒序
            [[-1, 100]],                             # 负数
            [[0, 101]],                              # 越出 cue 边界
        ]
        for bad_intervals in cases:
            with self.assertRaises(ValueError):
                run_merge(merge_input([bundle([program(
                    "P", "A", [usage_row(
                        bounds=(0, 100),
                        intervals=[tuple(x) for x in bad_intervals])])])]))

    def test_overlapping_input_intervals_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            run_merge(merge_input([bundle([program("P", "A", [
                usage_row(intervals=[(0, 60), (50, 100)])])])]))

    def test_boolean_rejected_as_fee_integer(self):
        with self.assertRaisesRegex(ValueError, "fee_fen"):
            run_settle(settle_input(
                bundle([program("P", "A")]), [fee(fen=True)]))

    def test_negative_fee_rejected(self):
        with self.assertRaisesRegex(ValueError, "fee_fen"):
            run_settle(settle_input(
                bundle([program("P", "A")]), [fee(fen=-1)]))

    def test_duplicate_program_in_one_bundle_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate program_id"):
            run_merge(merge_input([bundle([
                program("P", "A"), program("P", "A")])]))

    def test_duplicate_usage_key_in_program_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            run_merge(merge_input([bundle([program("P", "A", [
                usage_row(intervals=[(0, 10)]),
                usage_row(intervals=[(20, 30)])])])]))

    def test_duplicate_withdraw_id_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate program id"):
            run_merge(merge_input([], withdraw=["P", "P"]))

    def test_nested_empty_bundles_rejected_but_top_level_allows_withdraw(self):
        # 顶层空 bundles 合法：撤回先于用量到达
        result = run_merge(merge_input([], withdraw=["P"]))
        self.assertTrue(result["programs"][0]["withdrawn"])
        # 嵌套归并节点不允许空 bundles
        with self.assertRaisesRegex(ValueError, "bundles"):
            run_merge(merge_input([
                {"op": "merge", "bundles": [], "withdraw": []}]))

    def test_bad_schema_version_and_mode_rejected(self):
        bad = bundle([program("P", "A")])
        bad["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "schema_version"):
            run_merge(merge_input([bad]))
        bad = bundle([program("P", "A")])
        bad["mode"] = "other"
        with self.assertRaisesRegex(ValueError, "mode"):
            run_merge(merge_input([bad]))

    def test_collect_reuses_conform_validation(self):
        # 缺少 conform 必填字段
        bad_conform = conform_input([clip("c", 0, 10)])
        del bad_conform["sources"]
        with self.assertRaises(ValueError):
            run_collect(collect_input("P", "A", bad_conform))

    def test_tombstone_requires_boolean_withdrawn(self):
        bad = bundle([program("P", "A")])
        bad["programs"][0]["withdrawn"] = "yes"
        with self.assertRaisesRegex(ValueError, "withdrawn requires"):
            run_merge(merge_input([bad]))

    def test_partial_tombstone_fields_rejected(self):
        # 撤回墓碑要么只有两键（空墓碑），要么四键齐全
        partial = {"schema_version": 1, "mode": "subtitle-cost",
                   "op": "bundle",
                   "programs": [{"program_id": "P", "withdrawn": True,
                                 "cost_center": "A"}]}
        with self.assertRaises(ValueError):
            run_merge(merge_input([partial]))
        # 合法空墓碑被接受
        empty = {"schema_version": 1, "mode": "subtitle-cost",
                 "op": "bundle",
                 "programs": [{"program_id": "P", "withdrawn": True}]}
        result = run_merge(merge_input([empty]))
        self.assertEqual(result["programs"],
                         [{"program_id": "P", "withdrawn": True}])


# ---------------------------------------------------------------------------
# CLI 与原子写
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    ENV = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    BASE = [sys.executable, "-m", "subtitle_cost.cli"]

    def _run(self, source, output):
        return subprocess.run(
            self.BASE + ["--source", str(source), "--output", str(output)],
            env=self.ENV, capture_output=True, text=True)

    def test_fixture_chain_collect_merge_settle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            c_out = tmp / "collect.json"
            r = self._run("fixtures/subtitle-cost-collect.json", c_out)
            self.assertEqual(r.returncode, 0, r.stderr)
            collect = json.loads(c_out.read_text())
            self.assertEqual(
                collect["programs"][0]["usage"][1]["intervals"],
                [[4000, 5000], [7000, 8000]])

            m_out = tmp / "merge.json"
            r = self._run("fixtures/subtitle-cost-merge.json", m_out)
            self.assertEqual(r.returncode, 0, r.stderr)
            merged = json.loads(m_out.read_text())
            self.assertTrue(merged["programs"][0]["withdrawn"])

            s_out = tmp / "settle.json"
            r = self._run("fixtures/subtitle-cost-settle.json", s_out)
            self.assertEqual(r.returncode, 0, r.stderr)
            settled = json.loads(s_out.read_text())
            alloc = {c["cost_center"]: c["allocated_fen"]
                     for c in settled["cues"][0]["centers"]}
            self.assertEqual(alloc, {"center-a": 51, "center-b": 50})
            self.assertEqual(
                settled["summary"]["deferred_total_fen"], 37)

    def test_existing_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            marker = b'{"keep": true}'
            target.write_bytes(marker)
            result = self._run(
                "fixtures/subtitle-cost-collect.json", target)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(target.read_bytes(), marker)
            # 无临时残留
            self.assertEqual(list(Path(tmp).iterdir()), [target])

    def test_missing_source_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(
                Path(tmp) / "missing.json", Path(tmp) / "o.json")
            self.assertEqual(result.returncode, 2)
            self.assertFalse((Path(tmp) / "o.json").exists())

    def test_oversized_duplicate_keys_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            oversized = tmp / "big.json"
            oversized.write_text(
                '{"op": "merge", "bundles": [], "withdraw": [], "pad": "'
                + "x" * (8 * 1024 * 1024) + '"}')
            r = self._run(oversized, tmp / "o1.json")
            self.assertEqual(r.returncode, 2)
            self.assertIn("8 MB", r.stderr)

            dup = tmp / "dup.json"
            dup.write_text(
                '{"op": "merge", "bundles": [], "bundles": [], '
                '"withdraw": []}')
            r = self._run(dup, tmp / "o2.json")
            self.assertEqual(r.returncode, 2)
            self.assertIn("duplicate JSON key", r.stderr)

            nan = tmp / "nan.json"
            nan.write_text(
                '{"op": "merge", "bundles": [], "withdraw": [], "x": NaN}')
            r = self._run(nan, tmp / "o3.json")
            self.assertEqual(r.returncode, 2)

            self.assertEqual(
                sorted(p.name for p in tmp.iterdir()),
                ["big.json", "dup.json", "nan.json"])

    def test_invalid_input_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.json"
            source.write_text('{"op": "settle", "bundle": {}, "fees": []}')
            target = Path(tmp) / "out.json"
            r = self._run(source, target)
            self.assertEqual(r.returncode, 2)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
