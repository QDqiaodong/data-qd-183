"""cut-clearance 授权核验回归测试。"""
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from cut_clearance.clearance import (
    merge, run_clearance, subtract)
from cut_clearance.model import parse_time


def source(sid="s", duration=100000):
    return {"id": sid, "duration_ms": duration}


def clip(cid, in_ms, out_ms, num=1, den=1, source_id="s"):
    return {"id": cid, "source_id": source_id, "in_ms": in_ms,
            "out_ms": out_ms, "speed_num": num, "speed_den": den}


def delivery(did="d", channel="web", territory="CN",
             release="2027-01-01T00:00:00Z"):
    return {"id": did, "channel": channel, "territory": territory,
            "release_at": release}


def grant(gid, start, end, channels=("web",), territories=("CN",),
          source_id="s", valid_from="2026-01-01T00:00:00Z",
          valid_until="2028-01-01T00:00:00Z"):
    return {"id": gid, "source_id": source_id, "start_ms": start,
            "end_ms": end, "channels": list(channels),
            "territories": list(territories),
            "valid_from": valid_from, "valid_until": valid_until}


def exception(eid, did, cid, start, end,
              approved_at="2026-12-01T00:00:00Z", approved_by="legal"):
    return {"id": eid, "delivery_id": did, "clip_id": cid,
            "start_ms": start, "end_ms": end,
            "approved_by": approved_by, "approved_at": approved_at}


def make_input(sources=None, clips=None, deliveries=None, grants=None,
               exceptions=None):
    return {
        "sources": [source()] if sources is None else sources,
        "clips": [clip("c1", 0, 1000)] if clips is None else clips,
        "deliveries": [delivery()] if deliveries is None else deliveries,
        "grants": [] if grants is None else grants,
        "exceptions": [] if exceptions is None else exceptions,
    }


def use_for(report, did, cid):
    target = next(d for d in report["deliveries"] if d["delivery_id"] == did)
    return next(u for u in target["uses"] if u["clip_id"] == cid)


class IntervalHelperTests(unittest.TestCase):
    def test_merge_joins_touching_intervals(self):
        self.assertEqual(merge([(0, 100), (100, 200), (150, 300)]),
                         [(0, 300)])

    def test_subtract_keeps_positive_gaps_and_drops_contact(self):
        self.assertEqual(subtract((0, 300), [(0, 100), (100, 200)]),
                         [(200, 300)])
        self.assertEqual(subtract((0, 100), [(0, 100)]), [])


class GrantCoverageTests(unittest.TestCase):
    """验收：相接授权完整覆盖；存在正长缺口则阻断。"""

    def test_two_touching_grants_clear_the_use(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 0, 500), grant("g2", 500, 1000)])
        report = run_clearance(data)
        use = report["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "cleared")
        self.assertEqual(use["grant_ids"], ["g1", "g2"])
        self.assertEqual(use["remaining_gaps"], [])
        self.assertEqual(report["deliveries"][0]["status"], "cleared")

    def test_positive_gap_blocks_even_when_endpoints_touch(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 0, 400), grant("g2", 600, 1000)])
        report = run_clearance(data)
        use = report["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "blocked")
        self.assertEqual(use["remaining_gaps"][0]["source_start_ms"], 400)
        self.assertEqual(use["remaining_gaps"][0]["source_end_ms"], 600)
        self.assertEqual(report["summary"]["gap_count"], 1)


class TargetMatchingTests(unittest.TestCase):
    """验收：渠道/地区/时间不匹配的授权不得跨目标拼接。"""

    def _two_targets(self):
        return make_input(
            clips=[clip("c1", 0, 1000)],
            deliveries=[delivery("d-cn", "web", "CN"),
                        delivery("d-us", "web", "US")],
            grants=[
                grant("g-cn", 0, 600, territories=("CN",)),
                grant("g-us", 400, 1000, territories=("US",)),
            ])

    def test_grants_never_stitch_across_territories(self):
        report = run_clearance(self._two_targets())
        cn = use_for(report, "d-cn", "c1")
        us = use_for(report, "d-us", "c1")
        self.assertEqual(cn["grant_ids"], ["g-cn"])
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in cn["remaining_gaps"]], [(600, 1000)])
        self.assertEqual(us["grant_ids"], ["g-us"])
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in us["remaining_gaps"]], [(0, 400)])

    def test_multiple_grants_valid_at_release_jointly_cover(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[
                grant("g1", 0, 500,
                      valid_from="2025-01-01T00:00:00Z",
                      valid_until="2027-06-01T00:00:00Z"),
                grant("g2", 500, 1000,
                      valid_from="2026-12-01T00:00:00Z",
                      valid_until="2029-01-01T00:00:00Z"),
            ])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "cleared")
        self.assertEqual(use["grant_ids"], ["g1", "g2"])

    def test_grant_expiring_exactly_at_release_does_not_apply(self):
        data = make_input(
            deliveries=[delivery("d", release="2028-01-01T00:00:00Z")],
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 0, 1000,
                          valid_until="2028-01-01T00:00:00Z")])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "blocked")
        self.assertEqual(use["grant_ids"], [])

    def test_channel_mismatch_blocks(self):
        data = make_input(
            deliveries=[delivery("d", channel="tv")],
            grants=[grant("g1", 0, 1000, channels=("web",))])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "blocked")

    def test_open_ended_grant_never_expires(self):
        data = make_input(
            deliveries=[delivery("d", release="2099-01-01T00:00:00Z")],
            grants=[grant("g1", 0, 1000, valid_until=None)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "cleared")


class ReusedClipExceptionTests(unittest.TestCase):
    """验收：例外绑定具体剪辑实例，不能放行另一个实例。"""

    def _data(self):
        return make_input(
            clips=[clip("c1", 0, 1000), clip("c2", 0, 1000)],
            grants=[grant("g1", 0, 400)])

    def test_exception_for_one_instance_does_not_clear_other(self):
        data = self._data()
        data["exceptions"] = [
            exception("ex-c1", "d", "c1", 400, 1000)]
        report = run_clearance(data)
        first = use_for(report, "d", "c1")
        second = use_for(report, "d", "c2")
        self.assertEqual(first["status"], "cleared")
        self.assertEqual(first["exception_ids"], ["ex-c1"])
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["exception_ids"], [])
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in second["remaining_gaps"]],
                         [(400, 1000)])

    def test_partial_exception_only_shrinks_gap(self):
        data = self._data()
        data["exceptions"] = [exception("ex-1", "d", "c1", 400, 700)]
        use = use_for(run_clearance(data), "d", "c1")
        self.assertEqual(use["status"], "blocked")
        self.assertEqual(use["exception_ids"], ["ex-1"])
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in use["remaining_gaps"]], [(700, 1000)])

    def test_exception_clipped_to_gap_intersection(self):
        # 例外起点伸入已授权区域，只把与缺口正长相交的部分计入
        data = self._data()
        data["exceptions"] = [exception("ex-1", "d", "c1", 200, 1000)]
        use = use_for(run_clearance(data), "d", "c1")
        self.assertEqual(use["status"], "cleared")
        self.assertEqual(use["exception_ids"], ["ex-1"])

    def test_endpoint_contact_exception_cannot_fill(self):
        data = self._data()
        # 缺口 [400,1000)，例外 [0,400) 仅在 400 端点接触
        data["exceptions"] = [exception("ex-1", "d", "c1", 0, 400)]
        use = use_for(run_clearance(data), "d", "c1")
        self.assertEqual(use["exception_ids"], [])
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in use["remaining_gaps"]], [(400, 1000)])

    def test_exception_bound_to_other_delivery_ignored(self):
        data = self._data()
        data["deliveries"] = [delivery("d1"), delivery("d2")]
        data["exceptions"] = [exception("ex-1", "d1", "c1", 400, 1000)]
        self.assertEqual(use_for(run_clearance(data), "d2", "c1")["status"],
                         "blocked")


class SpeedMappingTests(unittest.TestCase):
    """验收：非整数节目时间精确换算，缺口映射保持精确有理数。"""

    def test_fractional_program_intervals_are_exact(self):
        data = make_input(
            clips=[clip("c1", 0, 300, num=3, den=1)],
            grants=[grant("g1", 0, 100)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        gap = use["remaining_gaps"][0]
        # 节目起点 = (100-0)/3 = 100/3，终点 = 300/3 = 100
        self.assertEqual(gap["program_start_ms"], "100/3")
        self.assertEqual(gap["program_end_ms"], "100")
        self.assertEqual(Fraction(gap["program_start_ms"]), Fraction(100, 3))
        self.assertEqual(use["program_interval_ms"], ["0", "100"])

    def test_speed_den_over_num_mapping(self):
        data = make_input(
            clips=[clip("c1", 100, 400, num=3, den=2)],
            grants=[grant("g1", 100, 250)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        gap = use["remaining_gaps"][0]
        # scale = 2/3，起点 (250-100)*2/3 = 100，终点 (400-100)*2/3 = 200
        self.assertEqual((gap["source_start_ms"], gap["source_end_ms"]),
                         (250, 400))
        self.assertEqual((gap["program_start_ms"], gap["program_end_ms"]),
                         ("100", "200"))
        self.assertEqual(use["program_interval_ms"], ["0", "200"])

    def test_exception_partial_fill_keeps_exact_fraction_gap(self):
        data = make_input(
            clips=[clip("c1", 0, 300, num=3, den=1)],
            grants=[grant("g1", 0, 100)],
            exceptions=[exception("ex-1", "d", "c1", 100, 200)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        gap = use["remaining_gaps"][0]
        self.assertEqual((gap["source_start_ms"], gap["source_end_ms"]),
                         (200, 300))
        self.assertEqual((gap["program_start_ms"], gap["program_end_ms"]),
                         ("200/3", "100"))


class DeliveryStatusAndUnusedTests(unittest.TestCase):
    """验收：目标级阻断独立判定；未引用授权保留清单。"""

    def test_one_blocked_use_blocks_delivery_but_not_other_target(self):
        data = make_input(
            clips=[clip("c1", 0, 1000), clip("c2", 0, 1000)],
            deliveries=[delivery("d1", territory="CN"),
                        delivery("d2", territory="US")],
            grants=[
                grant("g-cn", 0, 1000, territories=("CN",)),
                grant("g-us", 0, 500, territories=("US",)),
            ])
        report = run_clearance(data)
        statuses = {d["delivery_id"]: d["status"]
                    for d in report["deliveries"]}
        self.assertEqual(statuses, {"d1": "cleared", "d2": "blocked"})
        self.assertEqual(report["summary"],
                         {"delivery_count": 2, "cleared_count": 1,
                          "blocked_count": 1, "gap_count": 2})

    def test_unreferenced_grant_listed_but_changes_nothing(self):
        data = make_input(
            sources=[source("s"), source("s2", 5000)],
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g-used", 0, 1000),
                    grant("g-idle", 0, 5000, source_id="s2")])
        report = run_clearance(data)
        self.assertEqual(report["unused_grant_ids"], ["g-idle"])
        self.assertEqual(report["deliveries"][0]["status"], "cleared")

    def test_grant_on_unreferenced_source_still_listed_unicode_sorted(self):
        data = make_input(
            sources=[source("s"), source("s2", 5000)],
            clips=[clip("c1", 0, 1000)],
            grants=[
                grant("g-z", 0, 10, source_id="s2"),
                grant("g-a", 0, 20, source_id="s2"),
            ])
        self.assertEqual(run_clearance(data)["unused_grant_ids"],
                         ["g-a", "g-z"])


class OrderingTests(unittest.TestCase):
    def test_delivery_clip_and_interval_order_is_stable(self):
        data = make_input(
            clips=[clip("c2", 0, 1000), clip("c1", 0, 1000)],
            deliveries=[delivery("d2"), delivery("d1")],
            grants=[grant("g1", 0, 100)])
        report = run_clearance(data)
        self.assertEqual([d["delivery_id"] for d in report["deliveries"]],
                         ["d2", "d1"])
        self.assertEqual([u["clip_id"]
                          for u in report["deliveries"][0]["uses"]],
                         ["c2", "c1"])
        gaps = use_for(report, "d2", "c2")["remaining_gaps"]
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in gaps], [(100, 1000)])

    def test_grant_and_exception_ids_unicode_sorted(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g-z", 0, 500), grant("g-a", 0, 500),
                    grant("g-m", 500, 750)],
            exceptions=[exception("ex-z", "d", "c1", 750, 900),
                        exception("ex-a", "d", "c1", 900, 1000)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["grant_ids"], ["g-a", "g-m", "g-z"])
        self.assertEqual(use["exception_ids"], ["ex-a", "ex-z"])
        self.assertEqual(use["status"], "cleared")


class ValidationTests(unittest.TestCase):
    def _good(self):
        return make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 0, 1000)],
            exceptions=[exception("ex-1", "d", "c1", 0, 1000)])

    def _expect(self, mutator, needle):
        data = self._good()
        mutator(data)
        with self.assertRaisesRegex(ValueError, needle):
            run_clearance(data)

    def test_unknown_and_missing_root_fields(self):
        data = self._good()
        data["extra"] = 1
        with self.assertRaisesRegex(ValueError, "unknown"):
            run_clearance(data)
        data = self._good()
        del data["grants"]
        with self.assertRaisesRegex(ValueError, "missing"):
            run_clearance(data)

    def test_duplicate_ids_rejected(self):
        self._expect(lambda d: d["clips"].append(clip("c1", 0, 10)),
                     "duplicate clip ID")
        self._expect(lambda d: d["grants"].append(
            dict(d["grants"][0], start_ms=0, end_ms=10)), "duplicate grant")

    def test_unknown_references_rejected(self):
        self._expect(lambda d: d["clips"][0].__setitem__(
            "source_id", "ghost"), "unknown source")
        self._expect(lambda d: d["exceptions"][0].__setitem__(
            "delivery_id", "ghost"), "unknown delivery")
        self._expect(lambda d: d["exceptions"][0].__setitem__(
            "clip_id", "ghost"), "unknown clip")
        self._expect(lambda d: d["grants"][0].__setitem__(
            "source_id", "ghost"), "unknown source")

    def test_non_positive_intervals_rejected(self):
        self._expect(lambda d: d["clips"][0].__setitem__("out_ms", 0),
                     "positive-length")
        self._expect(lambda d: d["grants"][0].__setitem__("end_ms", 0),
                     "positive-length")
        self._expect(lambda d: d["exceptions"][0].__setitem__(
            "end_ms", 0), "positive-length")

    def test_boolean_rejected_as_integer(self):
        self._expect(lambda d: d["sources"][0].__setitem__(
            "duration_ms", True), "duration_ms")
        self._expect(lambda d: d["clips"][0].__setitem__(
            "speed_num", True), "speed_num")

    def test_exception_outside_clip_range_rejected(self):
        def shift_left(d):
            # 起点越出剪辑左边界（in_ms=0），区间仍为正长
            d["exceptions"][0]["start_ms"] = 0
            d["exceptions"][0]["end_ms"] = 500
            d["clips"][0]["in_ms"] = 200
        self._expect(shift_left, "within clip")
        data = self._good()
        data["exceptions"][0]["start_ms"] = 900
        data["exceptions"][0]["end_ms"] = 1001
        with self.assertRaisesRegex(ValueError, "within clip"):
            run_clearance(data)

    def test_approval_after_release_rejected(self):
        self._expect(lambda d: d["exceptions"][0].__setitem__(
            "approved_at", "2027-02-01T00:00:00Z"), "release_at")

    def test_approval_equal_to_release_accepted(self):
        data = self._good()
        data["exceptions"][0]["approved_at"] = "2027-01-01T00:00:00Z"
        self.assertEqual(run_clearance(data)["deliveries"][0]["status"],
                         "cleared")

    def test_string_sets_must_be_nonempty_and_unique(self):
        self._expect(lambda d: d["grants"][0].__setitem__(
            "channels", []), "nonempty")
        self._expect(lambda d: d["grants"][0].__setitem__(
            "territories", ["CN", "CN"]), "duplicate")

    def test_bad_rfc3339_forms_rejected(self):
        for bad in ("2027-01-01T00:00:00", "2027-01-01 00:00:00Z",
                    "2027-13-01T00:00:00Z", "2027-01-01T25:00:00Z",
                    "not-a-time", 123):
            with self.assertRaises(ValueError):
                parse_time(bad, "t")

    def test_fractional_seconds_parsed_exactly(self):
        self.assertEqual(
            parse_time("2027-01-01T00:00:00.250Z", "t"),
            parse_time("2027-01-01T00:00:00Z", "t") + Fraction(1, 4))

    def test_validity_window_must_be_positive(self):
        self._expect(lambda d: d["grants"][0].__setitem__(
            "valid_until", "2026-01-01T00:00:00Z"), "positive length")

    def test_empty_clips_or_deliveries_rejected(self):
        data = self._good()
        data["clips"] = []
        with self.assertRaisesRegex(ValueError, "clip"):
            run_clearance(data)
        data = self._good()
        data["deliveries"] = []
        with self.assertRaisesRegex(ValueError, "delivery"):
            run_clearance(data)


class MoreGapAndBoundaryTests(unittest.TestCase):
    def test_multiple_gaps_sorted_by_source_start(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 100, 200), grant("g2", 400, 500),
                    grant("g3", 800, 900)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual([(g["source_start_ms"], g["source_end_ms"])
                          for g in use["remaining_gaps"]],
                         [(0, 100), (200, 400), (500, 800), (900, 1000)])
        self.assertEqual(use["status"], "blocked")

    def test_overlapping_exceptions_merge_and_fully_fill_gap(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 0, 400)],
            exceptions=[exception("ex-a", "d", "c1", 400, 750),
                        exception("ex-b", "d", "c1", 600, 1000)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "cleared")
        self.assertEqual(use["exception_ids"], ["ex-a", "ex-b"])
        self.assertEqual(use["remaining_gaps"], [])

    def test_exception_partly_outside_gap_only_counts_intersection(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g1", 0, 400), grant("g2", 700, 1000)],
            exceptions=[exception("ex-1", "d", "c1", 200, 800)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        # 缺口 [400,700) 被例外的 [400,700) 相交部分补足；伸入授权区的部分不计
        self.assertEqual(use["status"], "cleared")
        self.assertEqual(use["exception_ids"], ["ex-1"])

    def test_grant_covering_more_than_clip_leaves_no_gap(self):
        data = make_input(
            clips=[clip("c1", 200, 800)],
            grants=[grant("g1", 0, 1000)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["status"], "cleared")
        self.assertEqual(use["source_interval_ms"], [200, 800])

    def test_unicode_id_ordering_uses_code_points(self):
        ids = ["授-乙", "授-甲"]
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant(ids[0], 0, 500), grant(ids[1], 500, 1000)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["grant_ids"], sorted(ids))
        self.assertEqual(use["status"], "cleared")

    def test_release_at_validity_lower_boundary_is_inclusive(self):
        data = make_input(
            deliveries=[delivery("d", release="2026-01-01T00:00:00Z")],
            grants=[grant("g1", 0, 1000,
                          valid_from="2026-01-01T00:00:00Z")])
        self.assertEqual(
            run_clearance(data)["deliveries"][0]["uses"][0]["status"],
            "cleared")

    def test_gap_count_accumulates_across_targets_and_uses(self):
        data = make_input(
            clips=[clip("c1", 0, 1000), clip("c2", 0, 1000)],
            deliveries=[delivery("d1"), delivery("d2", territory="US")],
            grants=[grant("g1", 0, 250, territories=("CN", "US"))])
        report = run_clearance(data)
        # 每个目标每个使用各 1 个缺口 -> 4
        self.assertEqual(report["summary"]["gap_count"], 4)
        self.assertEqual(report["summary"]["blocked_count"], 2)

    def test_contained_applicable_grant_still_listed(self):
        data = make_input(
            clips=[clip("c1", 0, 1000)],
            grants=[grant("g-outer", 0, 1000),
                    grant("g-inner", 200, 300)])
        use = run_clearance(data)["deliveries"][0]["uses"][0]
        self.assertEqual(use["grant_ids"], ["g-inner", "g-outer"])
        self.assertEqual(use["status"], "cleared")

    def test_fractional_release_at_boundary_uses_exact_time(self):
        data = make_input(
            deliveries=[delivery("d", release="2028-01-01T00:00:00.500Z")],
            grants=[grant("g1", 0, 1000,
                          valid_until="2028-01-01T00:00:01Z")])
        # 半开上界 01.000，00.500 仍在有效期内
        self.assertEqual(
            run_clearance(data)["deliveries"][0]["uses"][0]["status"],
            "cleared")
        data["grants"][0]["valid_until"] = "2028-01-01T00:00:00.500Z"
        self.assertEqual(
            run_clearance(data)["deliveries"][0]["uses"][0]["status"],
            "blocked")

    def test_non_object_root_and_missing_file_are_input_errors(self):
        from subtitle_conform.io import load
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "array.json"
            path.write_text("[]")
            with self.assertRaisesRegex(ValueError, "root must be an object"):
                load(path)
            with self.assertRaises(OSError):
                load(Path(tmp) / "missing.json")


class FixtureCliTests(unittest.TestCase):
    def test_example_fixture_report(self):
        raw = json.loads(Path("fixtures/clearance.json").read_text())
        report = run_clearance(raw)
        statuses = {d["delivery_id"]: d["status"]
                    for d in report["deliveries"]}
        self.assertEqual(statuses, {"dl-web-cn": "blocked",
                                    "dl-tv-us": "cleared",
                                    "dl-web-eu": "blocked"})
        self.assertEqual(report["unused_grant_ids"], ["g-c-unused"])
        # cut-3 的剩余缺口为精确非整数节目时间 17500/3
        cut3 = use_for(report, "dl-web-cn", "cut-3")
        self.assertEqual(cut3["remaining_gaps"][0]["program_end_ms"],
                         "17500/3")
        # 只给 cut-3 的例外没有放行重复实例 cut-4
        self.assertEqual(use_for(report, "dl-web-cn", "cut-4")
                         ["exception_ids"], [])
        # 端点接触的 ex-3 不计入补缺
        self.assertNotIn("ex-3", cut3["exception_ids"])
        # EU 目标两条授权共同覆盖同一来源区间
        eu_cut3 = use_for(report, "dl-web-eu", "cut-3")
        self.assertEqual(eu_cut3["status"], "cleared")
        self.assertEqual(eu_cut3["grant_ids"],
                         ["g-b-web-eu-rest", "g-b-web-partial"])

    def test_cli_atomic_create_only_and_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            env = dict(__import__("os").environ,
                       PYTHONPATH=str(Path("src").resolve()))
            cmd = [sys.executable, "-m", "cut_clearance.cli",
                   "--source", "fixtures/clearance.json",
                   "--output", str(target)]
            completed = subprocess.run(cmd, env=env, capture_output=True,
                                       text=True, check=True)
            self.assertIn('"blocked_count": 2', completed.stdout)
            self.assertTrue(target.exists())
            marker = b'{"keep": true}'
            target.write_bytes(marker)
            failed = subprocess.run(cmd, env=env, capture_output=True,
                                    text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(target.read_bytes(), marker)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_cli_rejects_oversized_duplicate_keys_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(__import__("os").environ,
                       PYTHONPATH=str(Path("src").resolve()))
            base = [sys.executable, "-m", "cut_clearance.cli"]
            oversized = Path(tmp) / "big.json"
            oversized.write_text(
                '{"sources": [], "clips": [], "deliveries": [], '
                '"grants": [], "exceptions": [], "pad": "'
                + "x" * (8 * 1024 * 1024) + '"}')
            result = subprocess.run(
                base + ["--source", str(oversized),
                        "--output", str(Path(tmp) / "o1.json")],
                env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("8 MB", result.stderr)

            dup = Path(tmp) / "dup.json"
            dup.write_text('{"sources": [], "sources": [], "clips": [], '
                           '"deliveries": [], "grants": [], '
                           '"exceptions": []}')
            result = subprocess.run(
                base + ["--source", str(dup),
                        "--output", str(Path(tmp) / "o2.json")],
                env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("duplicate JSON key", result.stderr)

            nan = Path(tmp) / "nan.json"
            nan.write_text('{"sources": [], "clips": [], "deliveries": [], '
                           '"grants": [], "exceptions": [], "x": NaN}')
            result = subprocess.run(
                base + ["--source", str(nan),
                        "--output", str(Path(tmp) / "o3.json")],
                env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            # 所有失败路径都不得新建报告，目录只剩三个非法输入文件
            self.assertEqual(
                sorted(p.name for p in Path(tmp).iterdir()),
                ["big.json", "dup.json", "nan.json"])


if __name__ == "__main__":
    unittest.main()
