"""subtitle-playback-qc 离线复盘质检回归测试。"""
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from playback_qc.qc import run_playback_qc


def cue(cid, start, end, text="台词", source_id="src"):
    return {"id": cid, "source_id": source_id, "start_ms": start,
            "end_ms": end, "text": text}


def conform_input(cues, clip_out=1000, clip_in=0, speed=(1, 1),
                  duration=10000, min_duration=0, max_cps=1000):
    return {
        "sources": [{"id": "src", "duration_ms": duration}],
        "clips": [{"id": "cut", "source_id": "src", "in_ms": clip_in,
                   "out_ms": clip_out, "speed_num": speed[0],
                   "speed_den": speed[1]}],
        "cues": cues,
        "min_duration_ms": min_duration,
        "max_chars_per_second": max_cps,
    }


def clock_row(start, end, program, num, den=1):
    return {"start_ms": start, "end_ms": end, "program_start_ms": program,
            "rate_num": num, "rate_den": den}


def snap(seq, at, active):
    return {"seq": seq, "at_ms": at, "op": "snapshot", "active": list(active)}


def show(seq, at, line):
    return {"seq": seq, "at_ms": at, "op": "show", "cue": line}


def hide(seq, at, line):
    return {"seq": seq, "at_ms": at, "op": "hide", "cue": line}


def make_input(batches, conform=None, capture_end=1000, last_seq=2,
               clock=None):
    return {
        "conform": conform if conform is not None
        else conform_input([cue("c1", 0, 1000)]),
        "capture_end_ms": capture_end,
        "last_seq": last_seq,
        "clock": clock if clock is not None else [clock_row(0, 1000, 0, 1)],
        "batches": batches,
    }


def unknown(start, end):
    return {"kind": "unknown", "start_ms": str(start), "end_ms": str(end)}


def mismatch(start, end, missing=(), extra=()):
    return {"kind": "mismatch", "start_ms": str(start), "end_ms": str(end),
            "missing": list(missing), "extra": list(extra)}


def replay_matches(report):
    """只从初始视图逐批应用更正，必须得到每批及最终的规范结果。"""
    alerts = [dict(row) for row in report["initial_view"]["alerts"]]
    totals = dict(report["initial_view"]["measurements"])

    def key(row):
        return (Fraction(row["start_ms"]), Fraction(row["end_ms"]),
                row["kind"], tuple(row.get("missing", ())),
                tuple(row.get("extra", ())))

    for batch in report["batches"]:
        corrections = batch["corrections"]
        for row in corrections["withdraw"]:
            alerts.remove(row)
        alerts.extend(corrections["add"])
        alerts.sort(key=key)
        for name, delta in corrections["delta"].items():
            totals[name] = str(Fraction(totals[name]) + Fraction(delta))
        if alerts != batch["alerts"] or totals != batch["measurements"]:
            return False
    return alerts == report["alerts"] and totals == report["measurements"]


class AcceptanceFixtureTests(unittest.TestCase):
    """验收一：缺第 3 条时 [200,800) 只能记未知，补包后更正为多显。"""

    @classmethod
    def setUpClass(cls):
        raw = json.loads(Path("fixtures/playback-qc.json").read_text())
        cls.report = run_playback_qc(raw)

    def test_line_map_and_durations(self):
        report = self.report
        self.assertEqual(report["capture_duration_ms"], "1200")
        self.assertEqual(report["program_duration_ms"], "600")
        self.assertEqual(
            [(row["line"], row["fragment_id"], row["srt_start_ms"],
              row["srt_end_ms"]) for row in report["line_map"]],
            [(1, ["take-1", "line-a"], 0, 300),
             (2, ["take-1", "line-b"], 300, 600)])

    def test_first_batch_keeps_unknown_instead_of_missing(self):
        batch = self.report["batches"][0]
        self.assertEqual(batch["alerts"], [unknown(200, 800)])
        self.assertEqual(batch["measurements"],
                         {"unknown_duration_ms": "600",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "0"})
        self.assertEqual(batch["corrections"]["withdraw"], [unknown(0, 1200)])
        self.assertEqual(batch["corrections"]["add"], [unknown(200, 800)])
        self.assertEqual(batch["corrections"]["delta"],
                         {"unknown_duration_ms": "-600",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "0"})

    def test_second_batch_corrects_to_extra_line(self):
        batch = self.report["batches"][1]
        self.assertEqual(batch["corrections"]["withdraw"], [unknown(200, 800)])
        self.assertEqual(batch["corrections"]["add"],
                         [mismatch(400, 800, extra=[1])])
        self.assertEqual(batch["corrections"]["delta"],
                         {"unknown_duration_ms": "-600",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "400"})

    def test_retransmission_produces_no_correction(self):
        batch = self.report["batches"][2]
        self.assertEqual(batch["new_records"], 0)
        self.assertEqual(batch["corrections"]["withdraw"], [])
        self.assertEqual(batch["corrections"]["add"], [])
        self.assertEqual(batch["corrections"]["delta"],
                         {"unknown_duration_ms": "0",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "0"})

    def test_final_and_replay(self):
        self.assertEqual(self.report["alerts"], [mismatch(400, 800, extra=[1])])
        self.assertEqual(self.report["measurements"],
                         {"unknown_duration_ms": "0",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "400"})
        self.assertTrue(replay_matches(self.report))


class TrustRuleTests(unittest.TestCase):
    """可信状态规则：首快照前、序号缺口、缺日志尾部、末端动作。"""

    def test_state_before_first_snapshot_is_unknown(self):
        data = make_input([[snap(2, 400, [1]), hide(3, 700, 1)]], last_seq=3)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"],
                         [unknown(0, 400), mismatch(700, 1000, missing=[1])])
        self.assertEqual(report["measurements"],
                         {"unknown_duration_ms": "400",
                          "missing_duration_ms": "300",
                          "extra_duration_ms": "0"})
        self.assertTrue(replay_matches(report))

    def test_missing_tail_marks_unknown_from_last_record(self):
        data = make_input([[snap(1, 0, [1]), hide(2, 300, 1)]], last_seq=5)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [unknown(300, 1000)])
        self.assertEqual(report["measurements"]["unknown_duration_ms"], "700")
        self.assertTrue(replay_matches(report))

    def test_no_records_keeps_whole_capture_unknown(self):
        data = make_input([[], []], last_seq=5)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [unknown(0, 1000)])
        self.assertEqual(report["measurements"],
                         {"unknown_duration_ms": "1000",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "0"})
        for batch in report["batches"]:
            self.assertEqual(batch["corrections"]["withdraw"], [])
            self.assertEqual(batch["corrections"]["add"], [])
        self.assertTrue(replay_matches(report))

    def test_increment_after_gap_cannot_restore_trust(self):
        data = make_input(
            [[snap(1, 0, [1]), hide(2, 100, 1), show(4, 200, 1)]], last_seq=4)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [unknown(100, 1000)])
        self.assertTrue(replay_matches(report))

    def test_snapshot_after_gap_at_same_time_restores_trust(self):
        # 缺口 2->4 与 seq 4 快照同一时刻：快照序号在缺口之后，当场重建
        data = make_input(
            [[snap(1, 0, [1]), hide(2, 500, 1), snap(4, 500, [])]], last_seq=4)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [mismatch(500, 1000, missing=[1])])
        self.assertEqual(report["measurements"]["unknown_duration_ms"], "0")
        self.assertTrue(replay_matches(report))

    def test_snapshot_before_gap_at_same_time_loses_trust(self):
        # seq 2 快照与缺口起点同一时刻，但缺口序号在其后：信任仍被破坏
        data = make_input(
            [[snap(1, 0, [1]), snap(2, 500, []), show(4, 500, 1)]],
            last_seq=4)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [unknown(500, 1000)])
        self.assertTrue(replay_matches(report))

    def test_gap_between_same_time_records_breaks_until_next_snapshot(self):
        data = make_input(
            [[snap(1, 0, [1]), snap(2, 500, []), show(4, 500, 1),
              snap(5, 700, [1])]], last_seq=5)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [unknown(500, 700)])
        self.assertTrue(replay_matches(report))

    def test_record_at_capture_end_confirms_completeness_only(self):
        # 末端动作确认收齐但不产生显示时长；屏上全程为空，漏显整段
        data = make_input([[snap(1, 0, []), show(2, 1000, 1)]], last_seq=2)
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [mismatch(0, 1000, missing=[1])])
        self.assertEqual(report["measurements"]["unknown_duration_ms"], "0")
        self.assertTrue(replay_matches(report))

    def test_same_batch_duplicate_and_snapshot_order_dedup(self):
        data = make_input(
            [[snap(1, 0, [1]), snap(1, 0, [1]), hide(2, 500, 1)],
             [snap(1, 0, [1])]], last_seq=2)
        report = run_playback_qc(data)
        self.assertEqual(report["batches"][0]["new_records"], 2)
        self.assertEqual(report["batches"][1]["new_records"], 0)
        self.assertEqual(report["batches"][1]["corrections"]["add"], [])
        self.assertTrue(replay_matches(report))


class TwoGapTests(unittest.TestCase):
    """验收二：两个缺口先补靠后的，前方未知区不得变成已知。"""

    def _data(self):
        return make_input(
            [[snap(1, 0, [1]), hide(2, 100, 1), snap(5, 500, [1]),
              show(8, 800, 1), snap(9, 900, [])],
             [hide(6, 600, 1), show(7, 700, 1)],
             [show(3, 200, 1), hide(4, 300, 1)]],
            last_seq=9)

    def test_first_batch_merges_unknown_across_gaps(self):
        report = run_playback_qc(self._data())
        batch = report["batches"][0]
        self.assertEqual(batch["alerts"],
                         [unknown(100, 900), mismatch(900, 1000, missing=[1])])
        self.assertEqual(batch["measurements"],
                         {"unknown_duration_ms": "800",
                          "missing_duration_ms": "100",
                          "extra_duration_ms": "0"})

    def test_later_gap_fill_keeps_front_unknown(self):
        report = run_playback_qc(self._data())
        batch = report["batches"][1]
        self.assertEqual(batch["alerts"],
                         [unknown(100, 500), mismatch(600, 700, missing=[1]),
                          mismatch(900, 1000, missing=[1])])
        corrections = batch["corrections"]
        self.assertEqual(corrections["withdraw"], [unknown(100, 900)])
        self.assertEqual(corrections["add"],
                         [unknown(100, 500), mismatch(600, 700, missing=[1])])
        self.assertEqual(corrections["delta"],
                         {"unknown_duration_ms": "-400",
                          "missing_duration_ms": "100",
                          "extra_duration_ms": "0"})

    def test_front_gap_fill_withdraws_and_splits_rows(self):
        report = run_playback_qc(self._data())
        batch = report["batches"][2]
        self.assertEqual(batch["alerts"],
                         [mismatch(100, 200, missing=[1]),
                          mismatch(300, 500, missing=[1]),
                          mismatch(600, 700, missing=[1]),
                          mismatch(900, 1000, missing=[1])])
        corrections = batch["corrections"]
        self.assertEqual(corrections["withdraw"], [unknown(100, 500)])
        self.assertEqual(corrections["add"],
                         [mismatch(100, 200, missing=[1]),
                          mismatch(300, 500, missing=[1])])
        self.assertEqual(corrections["delta"],
                         {"unknown_duration_ms": "-400",
                          "missing_duration_ms": "300",
                          "extra_duration_ms": "0"})
        self.assertEqual(report["measurements"]["missing_duration_ms"], "500")
        self.assertTrue(replay_matches(report))


class CoordinateTests(unittest.TestCase):
    """验收三：非整数边界、丢弃碎片、重叠行、暂停/回拖/快跳坐标。"""

    def test_fractional_capture_boundaries_stay_exact(self):
        conform = conform_input([cue("a", 0, 250, "甲"), cue("b", 250, 600, "乙")],
                                clip_out=600)
        data = make_input([[snap(1, 0, [])]], conform=conform, capture_end=400,
                          last_seq=1, clock=[clock_row(0, 400, 0, 3, 2)])
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"],
                         [mismatch(0, "500/3", missing=[1]),
                          mismatch("500/3", 400, missing=[2])])
        self.assertEqual(report["measurements"]["missing_duration_ms"], "400")
        self.assertTrue(replay_matches(report))

    def test_dropped_fragment_has_no_line_and_no_expectation(self):
        conform = conform_input(
            [cue("c1", 0, 1200, "甲"), cue("c2", 1500, 1501, "乙"),
             cue("c3", 1800, 3000, "丙")],
            clip_out=3000, speed=(3, 1))
        data = make_input([[snap(1, 0, [])]], conform=conform, last_seq=1)
        report = run_playback_qc(data)
        # c2 精确区间 [500,500+1/3) 舍入后零长被 SRT 丢弃，不占行号
        self.assertEqual(
            [(row["line"], row["fragment_id"], row["srt_start_ms"],
              row["srt_end_ms"]) for row in report["line_map"]],
            [(1, ["cut", "c1"], 0, 400), (2, ["cut", "c3"], 600, 1000)])
        self.assertEqual(report["alerts"],
                         [mismatch(0, 400, missing=[1]),
                          mismatch(600, 1000, missing=[2])])
        self.assertTrue(replay_matches(report))

    def test_overlapping_lines_expect_all_hits(self):
        conform = conform_input([cue("a", 0, 400, "甲"), cue("b", 200, 600, "乙")],
                                clip_out=600)
        data = make_input([[snap(1, 0, [2]), snap(2, 300, [1])]],
                          conform=conform, capture_end=600, last_seq=2,
                          clock=[clock_row(0, 600, 0, 1)])
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"],
                         [mismatch(0, 200, missing=[1], extra=[2]),
                          mismatch(200, 300, missing=[1]),
                          mismatch(300, 400, missing=[2]),
                          mismatch(400, 600, missing=[2], extra=[1])])
        # 同一毫秒可同时计入漏显与多显
        self.assertEqual(report["measurements"],
                         {"unknown_duration_ms": "0",
                          "missing_duration_ms": "600",
                          "extra_duration_ms": "400"})
        self.assertTrue(replay_matches(report))

    def test_pause_at_program_end_expects_nothing(self):
        conform = conform_input([cue("a", 0, 300, "甲"), cue("b", 500, 800, "乙")],
                                clip_out=800)
        data = make_input([[snap(1, 0, [])]], conform=conform, capture_end=400,
                          last_seq=1,
                          clock=[clock_row(0, 200, 0, 4),
                                 clock_row(200, 400, 800, 0)])
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"],
                         [mismatch(0, 75, missing=[1]),
                          mismatch(125, 200, missing=[2])])
        self.assertEqual(report["measurements"]["missing_duration_ms"], "150")
        self.assertTrue(replay_matches(report))

    def test_skip_forward_never_times_skipped_program(self):
        conform = conform_input([cue("a", 0, 300, "甲"), cue("b", 500, 800, "乙")],
                                clip_out=800)
        data = make_input([[snap(1, 0, [1]), snap(2, 200, [2])]],
                          conform=conform, capture_end=400, last_seq=2,
                          clock=[clock_row(0, 200, 0, 1),
                                 clock_row(200, 400, 500, 1)])
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"], [])
        self.assertEqual(report["measurements"],
                         {"unknown_duration_ms": "0",
                          "missing_duration_ms": "0",
                          "extra_duration_ms": "0"})
        self.assertTrue(replay_matches(report))

    def test_rewind_rewatches_same_program_as_new_exposure(self):
        # 同一节目段看两次：采集时间 [0,100) 与 [200,300) 都对应行 1
        conform = conform_input([cue("a", 0, 300, "甲")], clip_out=300)
        data = make_input(
            [[snap(1, 0, []), snap(2, 300, [1])]], conform=conform,
            capture_end=300, last_seq=2,
            clock=[clock_row(0, 100, 0, 1), clock_row(100, 200, 300, 0),
                   clock_row(200, 300, 0, 1)])
        report = run_playback_qc(data)
        self.assertEqual(report["alerts"],
                         [mismatch(0, 100, missing=[1]),
                          mismatch(200, 300, missing=[1])])
        self.assertEqual(report["measurements"]["missing_duration_ms"], "200")
        self.assertTrue(replay_matches(report))


class ValidationTests(unittest.TestCase):
    def _good(self):
        return make_input([[snap(1, 0, [1]), hide(2, 500, 1)]])

    def _expect(self, mutator, needle):
        data = self._good()
        mutator(data)
        with self.assertRaisesRegex(ValueError, needle):
            run_playback_qc(data)

    def test_root_fields_are_strict(self):
        self._expect(lambda d: d.__setitem__("extra", 1), "unknown")
        self._expect(lambda d: d.__delitem__("clock"), "missing")

    def test_capture_end_and_last_seq_bounds(self):
        self._expect(lambda d: d.__setitem__("capture_end_ms", 0),
                     "capture_end_ms")
        self._expect(lambda d: d.__setitem__("capture_end_ms", 86400001),
                     "capture_end_ms")
        self._expect(lambda d: d.__setitem__("capture_end_ms", True),
                     "capture_end_ms")
        self._expect(lambda d: d.__setitem__("last_seq", 0), "last_seq")
        self._expect(lambda d: d.__setitem__("last_seq", 2001), "last_seq")
        self._expect(lambda d: d.__setitem__("last_seq", False), "last_seq")

    def test_clock_coverage_is_checked(self):
        self._expect(lambda d: d.__setitem__("clock", []), "clock")
        self._expect(lambda d: d.__setitem__(
            "clock", [clock_row(0, 500, 0, 1)]), "capture_end_ms")
        self._expect(lambda d: d.__setitem__(
            "clock", [clock_row(100, 1000, 0, 1)]), "without gaps")
        self._expect(lambda d: d.__setitem__(
            "clock", [clock_row(0, 500, 0, 1), clock_row(600, 1000, 0, 1)]),
            "without gaps")
        self._expect(lambda d: d.__setitem__(
            "clock", [clock_row(0, 0, 0, 1), clock_row(0, 1000, 0, 1)]),
            "positive duration")

    def test_clock_rates_and_program_range(self):
        self._expect(lambda d: d["clock"][0].__setitem__("rate_num", 1001),
                     "rate_num")
        self._expect(lambda d: d["clock"][0].__setitem__("rate_den", 0),
                     "rate_den")
        self._expect(lambda d: d["clock"][0].__setitem__("rate_num", True),
                     "rate_num")
        # 节目位置越出普通对版精确节目范围
        self._expect(lambda d: d["clock"][0].__setitem__(
            "program_start_ms", 1001), "program range")
        self._expect(lambda d: d["clock"][0].__setitem__("rate_num", 2),
                     "program range")

    def test_batches_structure(self):
        self._expect(lambda d: d.__setitem__("batches", []), "batches")
        self._expect(lambda d: d.__setitem__("batches", [[]] * 13), "batches")
        self._expect(lambda d: d.__setitem__("batches", [{}]), "batch")

    def test_record_structure_and_fields(self):
        self._expect(lambda d: d["batches"][0].append(
            {"seq": 3, "at_ms": 0, "op": "peek"}), "unknown log op")
        self._expect(lambda d: d["batches"][0].append(
            {"seq": 3, "at_ms": 0, "op": "snapshot"}), "missing")
        self._expect(lambda d: d["batches"][0].append(
            {"seq": 3, "at_ms": 0, "op": "show"}), "missing")
        self._expect(lambda d: d["batches"][0].append(
            {"seq": 3, "at_ms": 0, "op": "show", "cue": 1, "active": []}),
            "unknown")
        self._expect(lambda d: d["batches"][0].append(
            {"seq": 3, "at_ms": 0, "op": "snapshot", "active": [], "cue": 1}),
            "unknown")
        self._expect(lambda d: d["batches"][0].append("junk"), "object")

    def test_record_values(self):
        self._expect(lambda d: d["batches"][0].append(show(0, 0, 1)), "seq")
        self._expect(lambda d: d["batches"][0].append(show(3, 0, 1)),
                     "seq")  # last_seq=2
        self._expect(lambda d: d["batches"][0].append(show(2, 1001, 1)),
                     "at_ms")
        self._expect(lambda d: d["batches"][0].append(
            {"seq": 2, "at_ms": True, "op": "show", "cue": 1}), "at_ms")
        self._expect(lambda d: d["batches"][0].append(show(2, 0, 0)), "cue")
        self._expect(lambda d: d["batches"][0].append(show(2, 0, 2)), "cue")
        self._expect(lambda d: d["batches"][0].append(snap(2, 0, [2])),
                     "active line")

    def test_snapshot_duplicate_lines_rejected(self):
        def mutate(d):
            d["conform"]["cues"].append(cue("c2", 0, 1000, "乙"))
            d["batches"][0].append(snap(2, 0, [1, 1]))
        self._expect(mutate, "duplicate line")

    def test_first_record_rule(self):
        self._expect(lambda d: d["batches"][0].__setitem__(
            0, snap(1, 5, [1])), "seq 1")
        self._expect(lambda d: d["batches"][0].__setitem__(
            0, show(1, 0, 1)), "seq 1")

    def test_time_must_not_go_backward(self):
        def mutate(d):
            d["last_seq"] = 3
            d["batches"] = [[snap(1, 0, [1]), snap(2, 600, [])],
                            [snap(3, 500, [])]]
        self._expect(mutate, "must not decrease")

    def test_conflicting_retransmission_rejected(self):
        self._expect(lambda d: d["batches"].append([hide(2, 600, 1)]),
                     "conflicting")
        self._expect(lambda d: d["batches"].append([snap(1, 0, [])]),
                     "conflicting")

    def test_same_content_retransmission_accepted(self):
        data = self._good()
        data["batches"].append([snap(1, 0, [1]), hide(2, 500, 1)])
        report = run_playback_qc(data)
        self.assertEqual(report["batches"][1]["new_records"], 0)
        self.assertEqual(report["batches"][1]["corrections"]["add"], [])

    def test_conform_input_still_strict(self):
        self._expect(lambda d: d["conform"].__setitem__("junk", 1), "unknown")
        self._expect(lambda d: d.__setitem__("conform", []), "object")


class CliTests(unittest.TestCase):
    def _run_cli(self, source, output):
        env = dict(__import__("os").environ,
                   PYTHONPATH=str(Path("src").resolve()))
        return subprocess.run(
            [sys.executable, "-m", "playback_qc.cli", "--source", str(source),
             "--output", str(output)],
            env=env, capture_output=True, text=True)

    def test_cli_fixture_summary_and_atomic_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            completed = self._run_cli("fixtures/playback-qc.json", target)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('"alert_rows": 1', completed.stdout)
            self.assertIn('"extra_duration_ms": "400"', completed.stdout)
            report = json.loads(target.read_text())
            self.assertEqual(report["mode"], "subtitle-playback-qc")
            self.assertTrue(replay_matches(report))
            marker = b'{"keep": true}'
            target.write_bytes(marker)
            failed = self._run_cli("fixtures/playback-qc.json", target)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(target.read_bytes(), marker)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_cli_conflict_in_last_batch_leaves_no_output(self):
        data = make_input([[snap(1, 0, [1]), hide(2, 500, 1)],
                           [hide(2, 600, 1)]])
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.json"
            source.write_text(json.dumps(data))
            target = Path(tmp) / "report.json"
            failed = self._run_cli(source, target)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("conflicting", failed.stderr)
            self.assertFalse(target.exists())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             ["input.json"])

    def test_cli_invalid_input_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.json"
            source.write_text('{"conform": {}, "capture_end_ms": 1000, '
                              '"last_seq": 1, "clock": [], "batches": [], '
                              '"ghost": 1}')
            target = Path(tmp) / "report.json"
            failed = self._run_cli(source, target)
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
