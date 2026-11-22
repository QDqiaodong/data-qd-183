"""subtitle-review-queue 离线复核工单账本回归测试。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from subtitle_review_queue import engine, store
from subtitle_review_queue.queue import build_groups


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def conform_input(clips, cues, sources=None, min_duration=0, max_cps=1000):
    return {
        "sources": sources if sources is not None
        else [{"id": "s", "duration_ms": 100000}],
        "clips": clips,
        "cues": cues,
        "min_duration_ms": min_duration,
        "max_chars_per_second": max_cps,
    }


def clip(cid, start, end, num=1, den=1, source_id="s"):
    return {"id": cid, "source_id": source_id, "in_ms": start,
            "out_ms": end, "speed_num": num, "speed_den": den}


def cue(cid, start, end, text="台词", source_id="s"):
    return {"id": cid, "source_id": source_id, "start_ms": start,
            "end_ms": end, "text": text}


def acceptance_conform():
    """验收素材：X 上 A/B/C 与 Y 上 A 传递成一组，变速 Z 上的 D 独立成组。"""
    return {
        "sources": [{"id": "S", "duration_ms": 1000},
                    {"id": "T", "duration_ms": 1000}],
        "clips": [
            clip("X", 0, 100, source_id="S"),
            clip("Y", 0, 40, source_id="S"),
            clip("Z", 0, 1, num=3, den=1, source_id="T"),
        ],
        "cues": [
            cue("A", 0, 45, "甲", "S"),
            cue("B", 40, 80, "乙", "S"),
            cue("C", 70, 90, "丙", "S"),
            cue("D", 0, 1, "丁", "T"),
        ],
        "min_duration_ms": 0,
        "max_chars_per_second": 1000,
    }


def member(fragment_id, start, end):
    return {"fragment_id": list(fragment_id), "start_ms": str(start),
            "end_ms": str(end)}


def issue(request_id, reviewer="alice", capacity=2000):
    return {"op": "issue", "request_id": request_id, "reviewer": reviewer,
            "max_fragments": capacity}


def resolve(request_id, ticket_id, action):
    return {"op": "resolve", "request_id": request_id,
            "ticket_id": ticket_id, "action": action}


def by_fragment(group_receipt):
    return [(row["group_id"],
             [m["fragment_id"] for m in row["members"]])
            for row in group_receipt["groups"]]


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = self.root / "state.json"
        self.input = self.root / "input.json"

    def register(self, conform=None, state=None):
        result, _ = engine.run(
            {"op": "register", "conform": conform or acceptance_conform()},
            state or self.state)
        return result

    def call(self, request, state=None):
        return engine.run(request, state or self.state)

    def status(self, state=None):
        report, committed = self.call({"op": "status"}, state)
        self.assertFalse(committed)
        return report


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------

class GroupingTests(LedgerTestCase):
    def test_acceptance_groups_both_rules_transitively(self):
        _, groups = build_groups(acceptance_conform())
        self.assertEqual(
            [[member["fragment_id"] for member in group["members"]]
             for group in groups],
            [[["X", "A"], ["X", "B"], ["X", "C"], ["Y", "A"]],
             [["Z", "D"]]])
        self.assertEqual([group["group_id"] for group in groups],
                         [["X", "A"], ["Z", "D"]])

    def test_member_details_are_exact_and_complete(self):
        members, groups = build_groups(acceptance_conform())
        first = groups[0]["members"]
        self.assertEqual(first[0], {
            "fragment_id": ["X", "A"],
            "source_id": "S",
            "source_start_ms": 0,
            "source_end_ms": 45,
            "start_ms": "0",
            "end_ms": "45",
            "text": "甲",
            "chars_per_second": "200/9",
            "issues": [],
            "srt_dropped": False,
        })
        dropped = groups[1]["members"][0]
        self.assertTrue(dropped["srt_dropped"])
        self.assertEqual(dropped["issues"], ["reading_speed"])
        self.assertEqual(dropped["start_ms"], "140")
        self.assertEqual(dropped["end_ms"], "421/3")
        self.assertEqual(dropped["chars_per_second"], "3000")
        # 身份是精确碎片身份，不是 SRT 行号
        self.assertNotIn("line", dropped)

    def test_middle_member_without_hint_does_not_break_closure(self):
        # B 自身没有提示，A/C 也没有提示；仅靠重叠把 A-B-C 连成一组
        conform = conform_input(
            [clip("X", 0, 100)],
            [cue("A", 0, 10), cue("B", 8, 20), cue("C", 18, 30)])
        _, groups = build_groups(conform)
        self.assertEqual(
            [[m["fragment_id"] for m in group["members"]] for group in groups],
            [[["X", "A"], ["X", "B"], ["X", "C"]]])

    def test_same_source_cue_grouped_across_occurrences(self):
        # 同源同 cue 的两次出现由规则一直接成组（Y 上太短另有提示）
        conform = conform_input(
            [clip("X", 0, 20), clip("Y", 0, 1)], [cue("p", 0, 20)],
            min_duration=10)
        _, groups = build_groups(conform)
        self.assertEqual(
            [[m["fragment_id"] for m in group["members"]] for group in groups],
            [[["X", "p"], ["Y", "p"]]])

    def test_hintless_duplicate_group_not_dispatched(self):
        # 同一源 cue 重复出现但组内无提示、无舍入丢弃、无重叠：不派单
        conform = conform_input(
            [clip("X", 0, 20), clip("Y", 0, 20)], [cue("p", 0, 20)])
        _, groups = build_groups(conform)
        self.assertEqual(groups, [])

    def test_isolated_hintless_duplicate_not_dispatched(self):
        conform = conform_input(
            [clip("X", 0, 50), clip("Y", 0, 20)], [cue("p", 0, 50)])
        _, groups = build_groups(conform)
        self.assertEqual(groups, [])

    def test_endpoint_contact_is_not_overlap(self):
        conform = conform_input(
            [clip("X", 0, 100)], [cue("p", 0, 50), cue("q", 50, 100)])
        _, groups = build_groups(conform)
        self.assertEqual(groups, [])

    def test_no_review_rows_yields_no_groups(self):
        conform = conform_input(
            [clip("X", 0, 100)],
            [cue("p", 0, 100)],
            min_duration=10, max_cps=100)
        _, groups = build_groups(conform)
        self.assertEqual(groups, [])

    def test_hint_anywhere_keeps_hintless_members(self):
        # 只有 Y/A 太短有提示，整组仍含 X 上无提示的 A/B/C
        conform = conform_input(
            [clip("X", 0, 100), clip("Y", 0, 1)],
            [cue("A", 0, 45), cue("B", 40, 80), cue("C", 70, 90)],
            min_duration=10)
        _, groups = build_groups(conform)
        self.assertEqual(
            [(group["group_id"],
              [m["fragment_id"] for m in group["members"]]) for group in groups],
            [(['X', 'A'], [['X', 'A'], ['X', 'B'], ['X', 'C'], ['Y', 'A']])])

    def test_overlap_example_truncation_does_not_hide_relations(self):
        # 101 组独立重叠对：报告只保留前 100 个示例，最后一对仍须成组
        cues = []
        for index in range(1, 102):
            base = index * 10
            cues.append(cue(f"a{index:03d}", base, base + 5))
            cues.append(cue(f"b{index:03d}", base + 2, base + 7))
        conform = conform_input([clip("X", 0, 1100)], cues)
        members, groups = build_groups(conform)
        self.assertEqual(len(members), 202)
        self.assertEqual(len(groups), 101)
        self.assertEqual(groups[-1]["group_id"], ["X", "a101"])
        from subtitle_conform.service import run as run_conform
        report = run_conform(conform)
        self.assertEqual(report["overlap_count"], 101)
        self.assertEqual(len(report["overlap_examples"]), 100)
        sample_ids = {tuple(item) for pair in report["overlap_examples"]
                      for item in pair}
        self.assertNotIn(("X", "a101"), sample_ids)

    def test_long_interval_overlapping_many_shorts_is_one_group(self):
        # A 跨过 B、C、D 三个连续短行；只靠端点相接不重叠的假设会漏掉
        conform = conform_input(
            [clip("X", 0, 100)],
            [cue("A", 0, 100), cue("B", 10, 20), cue("C", 30, 40),
             cue("D", 50, 60)])
        _, groups = build_groups(conform)
        self.assertEqual(
            [[m["fragment_id"] for m in group["members"]] for group in groups],
            [[["X", "A"], ["X", "B"], ["X", "C"], ["X", "D"]]])

    def test_group_order_follows_first_member_in_conform_order(self):
        members, groups = build_groups(acceptance_conform())
        positions = {tuple(row["fragment_id"]): index
                     for index, row in enumerate(members)}
        starts = [positions[tuple(group["group_id"])] for group in groups]
        self.assertEqual(starts, sorted(starts))


# ---------------------------------------------------------------------------
# 登记与账本
# ---------------------------------------------------------------------------

class RegisterTests(LedgerTestCase):
    def test_register_receipt_lists_groups_and_members(self):
        receipt = self.register()
        self.assertEqual(receipt["op"], "register")
        self.assertEqual(receipt["group_count"], 2)
        self.assertEqual(by_fragment(receipt), [
            (["X", "A"],
             [["X", "A"], ["X", "B"], ["X", "C"], ["Y", "A"]]),
            (["Z", "D"], [["Z", "D"]])])
        self.assertEqual([row["member_count"] for row in receipt["groups"]],
                         [4, 1])

    def test_register_rejects_second_different_content(self):
        self.register()
        other = acceptance_conform()
        other["min_duration_ms"] = 1
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.register(other)
        # 账本内容与回执都不被替换
        status = self.status()
        self.assertEqual(status["group_count"], 2)

    def test_register_replay_same_content_returns_same_receipt(self):
        first = self.register()
        second, committed = self.call(
            {"op": "register", "conform": acceptance_conform()})
        self.assertFalse(committed)
        self.assertEqual(first, second)

    def test_commands_work_after_registration_file_is_removed(self):
        self.input.write_text(json.dumps(
            {"op": "register", "conform": acceptance_conform()}))
        self.register()
        self.input.unlink()
        receipt, _ = self.call(issue("R", capacity=4))
        self.assertEqual(receipt["groups"][0]["group_id"], ["X", "A"])
        self.assertFalse(self.input.exists())

    def test_status_does_not_change_ledger(self):
        self.register()
        before = self.state.read_bytes()
        status = self.status()
        self.assertEqual(after := self.state.read_bytes(), before)
        self.assertEqual(status["group_count"], 2)
        self.assertEqual([row["state"] for row in status["groups"]],
                         ["pending", "pending"])
        self.assertEqual(after, before)

    def test_missing_ledger_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.call({"op": "status"})
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.call(issue("R"))

    def test_register_input_validation_is_strict(self):
        with self.assertRaises(ValueError):
            self.call({"op": "register",
                       "conform": acceptance_conform(), "ghost": 1})
        bad = acceptance_conform()
        bad["max_chars_per_second"] = True
        with self.assertRaises(ValueError):
            self.call({"op": "register", "conform": bad})
        with self.assertRaises(ValueError):
            self.call({"op": "register", "conform": []})


# ---------------------------------------------------------------------------
# 领取
# ---------------------------------------------------------------------------

class IssueTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.register()

    def test_capacity_one_skips_four_member_group_and_claims_second(self):
        receipt, committed = self.call(issue("R", capacity=1))
        self.assertTrue(committed)
        self.assertEqual(receipt["ticket_id"], "R")
        self.assertEqual(receipt["reviewer"], "alice")
        self.assertEqual(receipt["fragment_count"], 1)
        self.assertEqual(receipt["groups"],
                         [{"group_id": ["Z", "D"], "member_count": 1,
                           "members": receipt["groups"][0]["members"]}])
        self.assertEqual(receipt["groups"][0]["members"][0]["fragment_id"],
                         ["Z", "D"])

    def test_group_never_split(self):
        receipt, _ = self.call(issue("R", capacity=3))
        self.assertEqual(receipt["groups"][0]["group_id"], ["Z", "D"])
        status = self.status()
        self.assertEqual([row["state"] for row in status["groups"]],
                         ["pending", "assigned"])

    def test_one_ticket_can_hold_multiple_groups_in_order(self):
        receipt, _ = self.call(issue("R", capacity=5))
        self.assertEqual([row["group_id"] for row in receipt["groups"]],
                         [["X", "A"], ["Z", "D"]])
        self.assertEqual(receipt["fragment_count"], 5)
        status = self.status()
        self.assertEqual([(row["state"], row["ticket_id"], row["reviewer"])
                          for row in status["groups"]],
                         [("assigned", "R", "alice"),
                          ("assigned", "R", "alice")])

    def test_no_claimable_group_returns_and_remembers_empty_receipt(self):
        self.call(issue("R1", capacity=5))
        empty, committed = self.call(issue("R2", "bob", capacity=5))
        self.assertTrue(committed)  # 空回执本身要入账以便重取
        self.assertTrue(empty["empty"])
        self.assertIsNone(empty["ticket_id"])
        self.assertEqual(empty["groups"], [])
        # 不产生可处置工单
        with self.assertRaisesRegex(ValueError, "unknown or closed"):
            self.call(resolve("R3", "R2", "complete"))
        # 有新可领组后，原空请求仍原样重取空回执
        self.call(resolve("R4", "R1", "return"))
        replay, committed = self.call(issue("R2", "bob", capacity=5))
        self.assertFalse(committed)
        self.assertEqual(replay, empty)

    def test_capacity_bounds_and_types(self):
        for bad in (0, 2001, True, "4", 4.0):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.call(issue("R", capacity=bad))

    def test_ids_and_reviewer_are_nonempty_strings(self):
        for bad in ("", "   ", 7, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.call(issue(bad))
                with self.assertRaises(ValueError):
                    self.call(issue("R", reviewer=bad))

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            self.call({"op": "issue", "request_id": "R", "reviewer": "a",
                       "max_fragments": 1, "ghost": True})


# ---------------------------------------------------------------------------
# 处置与重取
# ---------------------------------------------------------------------------

class ResolveTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.register()

    def test_complete_closes_ticket_and_completes_groups(self):
        self.call(issue("R", capacity=5))
        receipt, committed = self.call(resolve("D1", "R", "complete"))
        self.assertTrue(committed)
        self.assertEqual(receipt["ticket_id"], "R")
        self.assertEqual(receipt["action"], "complete")
        self.assertEqual([row["group_id"] for row in receipt["groups"]],
                         [["X", "A"], ["Z", "D"]])
        status = self.status()
        self.assertEqual([row["state"] for row in status["groups"]],
                         ["completed", "completed"])
        self.assertEqual([row["ticket_id"] for row in status["groups"]],
                         [None, None])
        with self.assertRaisesRegex(ValueError, "unknown or closed"):
            self.call(resolve("D2", "R", "complete"))

    def test_return_makes_groups_pending_for_new_ticket(self):
        self.call(issue("R", capacity=5))
        receipt, _ = self.call(resolve("D1", "R", "return"))
        self.assertEqual(receipt["action"], "return")
        self.assertEqual([row["state"] for row in self.status()["groups"]],
                         ["pending", "pending"])
        again, _ = self.call(issue("R2", "bob", capacity=5))
        self.assertEqual([row["group_id"] for row in again["groups"]],
                         [["X", "A"], ["Z", "D"]])
        status = self.status()
        self.assertEqual([row["ticket_id"] for row in status["groups"]],
                         ["R2", "R2"])

    def test_old_ticket_cannot_resolve_regrouped_ownership(self):
        self.call(issue("R", capacity=5))
        self.call(resolve("D1", "R", "return"))
        self.call(issue("R2", "bob", capacity=5))
        # R 已关闭；即使用新请求 ID 也不能处置 Bob 的新归属
        with self.assertRaisesRegex(ValueError, "unknown or closed"):
            self.call(resolve("D2", "R", "complete"))
        status = self.status()
        self.assertEqual([row["ticket_id"] for row in status["groups"]],
                         ["R2", "R2"])

    def test_resolve_requires_valid_action(self):
        self.call(issue("R", capacity=5))
        for bad in ("done", "", None, 1):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.call(resolve("D1", "R", bad))
        # 失败不占用请求 ID：换动作后同一 ID 可以提交
        receipt, _ = self.call(resolve("D1", "R", "return"))
        self.assertEqual(receipt["action"], "return")

    def test_unknown_ticket_rejected_and_receipt_unchanged(self):
        with self.assertRaisesRegex(ValueError, "unknown or closed"):
            self.call(resolve("D1", "nope", "complete"))
        self.assertEqual(self.status()["groups"][0]["state"], "pending")


class ReplayTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.register()

    def test_same_request_returns_original_receipt_without_redoing(self):
        first, _ = self.call(issue("R", capacity=1))
        # 账本状态随后改变；重取仍返回最初回执
        self.call(resolve("D1", "R", "complete"))
        second, committed = self.call(issue("R", capacity=1))
        self.assertFalse(committed)
        self.assertEqual(first, second)
        self.assertEqual(second["groups"][0]["group_id"], ["Z", "D"])

    def test_key_order_does_not_change_request_identity(self):
        first, _ = self.call({"op": "issue", "request_id": "R",
                              "reviewer": "alice", "max_fragments": 1})
        second, committed = self.call({"max_fragments": 1, "reviewer": "alice",
                                       "request_id": "R", "op": "issue"})
        self.assertFalse(committed)
        self.assertEqual(first, second)

    def test_same_request_id_different_request_rejected(self):
        self.call(issue("R", capacity=1))
        with self.assertRaisesRegex(ValueError, "already used"):
            self.call(issue("R", capacity=2))
        with self.assertRaisesRegex(ValueError, "already used"):
            self.call(issue("R", "bob", capacity=1))
        with self.assertRaisesRegex(ValueError, "already used"):
            self.call(resolve("R", "R", "return"))

    def test_replay_of_resolve_returns_original_receipt(self):
        self.call(issue("R", capacity=5))
        first, _ = self.call(resolve("D1", "R", "return"))
        second, committed = self.call(resolve("D1", "R", "return"))
        self.assertFalse(committed)
        self.assertEqual(first, second)

    def test_failed_request_does_not_occupy_id(self):
        with self.assertRaises(ValueError):
            self.call(issue("R", capacity=0))
        receipt, _ = self.call(issue("R", capacity=1))
        self.assertEqual(receipt["ticket_id"], "R")


# ---------------------------------------------------------------------------
# 跨进程、丢回复与提交边界
# ---------------------------------------------------------------------------

class ProcessTestCase(LedgerTestCase):
    ENV = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))

    def cli(self, request, output, state=None):
        source = self.root / f"req-{abs(hash(json.dumps(request, sort_keys=True)))}.json"
        source.write_text(json.dumps(request, ensure_ascii=False))
        return subprocess.run(
            [sys.executable, "-m", "subtitle_review_queue.cli",
             "--state", str(state or self.state),
             "--source", str(source), "--output", str(output)],
            env=self.ENV, capture_output=True, text=True)

    def write_register_input(self):
        self.input.write_text(json.dumps(
            {"op": "register", "conform": acceptance_conform()}))
        return self.input


class LostReplyTests(ProcessTestCase):
    def test_cross_process_handoff_after_lost_reply(self):
        register_input = self.write_register_input()
        done = self.cli({"op": "register",
                         "conform": acceptance_conform()},
                        self.root / "register-report.json")
        self.assertEqual(done.returncode, 0, done.stderr)

        # Alice 以 R 占有四成员组；提交后、写报告前失败（目标已存在）
        blocked = self.root / "blocked.json"
        blocked.write_bytes(b'{"occupied": true}')
        lost = self.cli(issue("R", "alice", 4), blocked)
        self.assertEqual(lost.returncode, 3, lost.stderr)
        self.assertEqual(blocked.read_bytes(), b'{"occupied": true}')
        # 报告没写出来，但账本已提交
        self.assertIn("committed", lost.stderr)

        register_input.unlink()  # 后续命令不依赖登记输入文件

        # 新进程重新打开账本，靠 status 找到 R
        status_path = self.root / "status.json"
        status = self.cli({"op": "status"}, status_path)
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status_path.read_text())
        self.assertEqual([(row["group_id"], row["state"], row["ticket_id"],
                           row["reviewer"]) for row in report["groups"]],
                         [(["X", "A"], "assigned", "R", "alice"),
                          (["Z", "D"], "pending", None, None)])

        # 对照一：不归还时，Bob 容量 4 领不到该组（这里 D 已可领，仅验证 A/B/C 组）
        no_return = self.root / "bob-no-return.json"
        bob_early = self.cli(issue("BOB-NO-RETURN", "bob", 4), no_return)
        self.assertEqual(bob_early.returncode, 0, bob_early.stderr)
        early = json.loads(no_return.read_text())
        self.assertEqual([row["group_id"] for row in early["groups"]],
                         [["Z", "D"]])

        # 主管归还 R
        returned = self.root / "return.json"
        result = self.cli(resolve("D-R", "R", "return"), returned)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(returned.read_text())["action"], "return")

        # 对照二：归还后 Bob 以新请求、同样容量 4 领取该组
        after_return = self.root / "bob-after-return.json"
        bob = self.cli(issue("BOB-AFTER", "bob", 4), after_return)
        self.assertEqual(bob.returncode, 0, bob.stderr)
        claimed = json.loads(after_return.read_text())
        self.assertEqual([row["group_id"] for row in claimed["groups"]],
                         [["X", "A"]])
        self.assertEqual(
            [member["fragment_id"] for member in claimed["groups"][0]["members"]],
            [["X", "A"], ["X", "B"], ["X", "C"], ["Y", "A"]])

        # 用新输出路径重取 R：得到原 Alice 回执（同一身份、组与成员详情）
        replay_path = self.root / "alice-replay.json"
        replay = self.cli(issue("R", "alice", 4), replay_path)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        recovered = json.loads(replay_path.read_text())
        self.assertEqual(recovered["ticket_id"], "R")
        self.assertEqual(recovered["reviewer"], "alice")
        self.assertEqual(
            [row["group_id"] for row in recovered["groups"]], [["X", "A"]])
        self.assertEqual(
            [member["fragment_id"] for member in recovered["groups"][0]["members"]],
            [["X", "A"], ["X", "B"], ["X", "C"], ["Y", "A"]])
        self.assertEqual(
            recovered["groups"][0]["members"],
            claimed["groups"][0]["members"])

        # 重取不恢复工单效力：status 仍归 Bob；用 R 完成该组被拒绝
        status2 = self.root / "status2.json"
        self.assertEqual(self.cli({"op": "status"}, status2).returncode, 0)
        current = json.loads(status2.read_text())
        self.assertEqual((current["groups"][0]["ticket_id"],
                          current["groups"][0]["reviewer"]), ("BOB-AFTER", "bob"))
        refused = self.cli(resolve("D-R2", "R", "complete"),
                           self.root / "refused.json")
        self.assertEqual(refused.returncode, 2)
        self.assertFalse((self.root / "refused.json").exists())

        # 同一 R 改容量也拒绝
        changed = self.cli(issue("R", "alice", 5),
                           self.root / "changed.json")
        self.assertEqual(changed.returncode, 2)
        self.assertIn("already used", changed.stderr)

        # Bob 可以完成自己领取的组
        completed = self.cli(resolve("D-BOB", "BOB-AFTER", "complete"),
                             self.root / "bob-complete.json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status3 = self.root / "status3.json"
        self.assertEqual(self.cli({"op": "status"}, status3).returncode, 0)
        final = json.loads(status3.read_text())
        self.assertEqual([row["state"] for row in final["groups"]],
                         ["completed", "assigned"])

    def test_empty_receipt_still_replays_after_groups_become_claimable(self):
        self.cli({"op": "register", "conform": acceptance_conform()},
                 self.root / "r.json")
        self.cli(issue("R1", "alice", 5), self.root / "issue1.json")
        first = self.root / "empty.json"
        self.assertEqual(self.cli(issue("R2", "bob", 5), first).returncode, 0)
        empty = json.loads(first.read_text())
        self.assertTrue(empty["empty"])
        self.cli(resolve("D1", "R1", "return"), self.root / "return.json")
        again = self.root / "empty-again.json"
        self.assertEqual(self.cli(issue("R2", "bob", 5), again).returncode, 0)
        self.assertEqual(json.loads(again.read_text()), empty)


class ConcurrencyTests(ProcessTestCase):
    def test_two_processes_racing_for_one_group(self):
        self.cli({"op": "register", "conform": acceptance_conform()},
                 self.root / "r.json")
        # 先占走四成员组，只留下 [Z,D] 供争抢
        self.cli(issue("OWNER", "alice", 4), self.root / "owner.json")
        barrier = threading.Barrier(2)
        results = {}

        def race(name):
            barrier.wait()
            output = self.root / f"{name}.json"
            completed = self.cli(issue(name, name, 1), output)
            results[name] = (completed.returncode, output)

        threads = [threading.Thread(target=race, args=(name,))
                   for name in ("PA", "PB")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = []
        for name, (code, output) in results.items():
            self.assertEqual(code, 0)
            receipt = json.loads(output.read_text())
            if receipt["groups"]:
                winners.append(name)
                self.assertEqual(receipt["groups"][0]["group_id"], ["Z", "D"])
            else:
                self.assertTrue(receipt["empty"])
        self.assertEqual(len(winners), 1)
        status = self.root / "status.json"
        self.assertEqual(self.cli({"op": "status"}, status).returncode, 0)
        assigned = [row for row in json.loads(status.read_text())["groups"]
                    if row["group_id"] == ["Z", "D"]]
        self.assertEqual(assigned[0]["ticket_id"], winners[0])


class CommitBoundaryTests(LedgerTestCase):
    def test_write_failure_before_commit_leaves_no_partial_change(self):
        self.register()
        before = self.state.read_bytes()
        with mock.patch.object(store, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.call(issue("R", capacity=5))
        self.assertEqual(self.state.read_bytes(), before)
        # 没有部分领单，请求 ID 也未占用：同一 ID 可以重新提交
        self.assertEqual([row["state"] for row in self.status()["groups"]],
                         ["pending", "pending"])
        receipt, _ = self.call(issue("R", capacity=5))
        self.assertEqual(len(receipt["groups"]), 2)
        self.assertEqual(self.status()["groups"][0]["ticket_id"], "R")

    def test_write_failure_after_resolve_leaves_no_partial_change(self):
        self.register()
        self.call(issue("R", capacity=5))
        before = self.state.read_bytes()
        with mock.patch.object(store, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.call(resolve("D1", "R", "complete"))
        self.assertEqual(self.state.read_bytes(), before)
        status = self.status()
        self.assertEqual([row["state"] for row in status["groups"]],
                         ["assigned", "assigned"])
        # 失败未提交的处置请求不占用 ID
        receipt, _ = self.call(resolve("D1", "R", "return"))
        self.assertEqual(receipt["action"], "return")

    def test_cli_report_failure_after_commit_exits_three_and_recovers(self):
        self.input.write_text(json.dumps(
            {"op": "register", "conform": acceptance_conform()}))
        env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
        subprocess.run([sys.executable, "-m", "subtitle_review_queue.cli",
                        "--state", str(self.state), "--source", str(self.input),
                        "--output", str(self.root / "register.json")],
                       env=env, check=True, capture_output=True)
        source = self.root / "issue.json"
        source.write_text(json.dumps(issue("R", "alice", 4)))
        target = self.root / "exists.json"
        target.write_bytes(b"old")
        failed = subprocess.run(
            [sys.executable, "-m", "subtitle_review_queue.cli",
             "--state", str(self.state), "--source", str(source),
             "--output", str(target)],
            env=env, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 3)
        self.assertIn("committed", failed.stderr)
        self.assertEqual(target.read_bytes(), b"old")
        # 账本已经提交
        status = subprocess.run(
            [sys.executable, "-m", "subtitle_review_queue.cli",
             "--state", str(self.state), "--source", str(self._status_source()),
             "--output", str(self.root / "status.json")],
            env=env, capture_output=True, text=True)
        self.assertEqual(status.returncode, 0)
        self.assertEqual(
            json.loads((self.root / "status.json").read_text())["groups"][0]
            ["ticket_id"], "R")
        # 同请求、新输出路径重取原回执
        recovered = self.root / "recovered.json"
        retry = subprocess.run(
            [sys.executable, "-m", "subtitle_review_queue.cli",
             "--state", str(self.state), "--source", str(source),
             "--output", str(recovered)],
            env=env, capture_output=True, text=True)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        receipt = json.loads(recovered.read_text())
        self.assertEqual(receipt["ticket_id"], "R")
        self.assertEqual([row["group_id"] for row in receipt["groups"]],
                         [["X", "A"]])

    def _status_source(self):
        path = self.root / "status-request.json"
        path.write_text('{"op": "status"}')
        return path


# ---------------------------------------------------------------------------
# CLI 报告
# ---------------------------------------------------------------------------

class CliTests(ProcessTestCase):
    def test_cli_invalid_input_exits_two_and_leaves_no_report(self):
        self.cli({"op": "register", "conform": acceptance_conform()},
                 self.root / "r.json")
        target = self.root / "bad-report.json"
        completed = self.cli(issue("R", capacity=0), target)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(target.exists())

    def test_cli_report_is_create_only_and_atomic(self):
        self.cli({"op": "register", "conform": acceptance_conform()},
                 self.root / "r.json")
        target = self.root / "report.json"
        completed = self.cli(issue("R", "alice", 2), target)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(target.read_text())
        self.assertEqual(report["mode"], "subtitle-review-queue")
        self.assertEqual(report["op"], "issue")
        self.assertIn('"ticket_id": "R"', completed.stdout)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()
                                if path.name.startswith(".report-")), [])
        marker = b"keep"
        target.write_bytes(marker)
        again = self.cli(issue("R", "alice", 2), target)
        # 重取未提交新变更，报告写失败按普通失败退出 2
        self.assertEqual(again.returncode, 2)
        self.assertEqual(target.read_bytes(), marker)


class FixtureTests(unittest.TestCase):
    def test_register_fixture_matches_contract(self):
        raw = json.loads(
            Path("fixtures/review-queue-register.json").read_text())
        members, groups = build_groups(raw["conform"])
        self.assertEqual(
            [[m["fragment_id"] for m in g["members"]] for g in groups],
            [[["X", "A"], ["X", "B"], ["X", "C"], ["Y", "A"]],
             [["Z", "D"]]])
        self.assertEqual(groups[1]["members"][0]["start_ms"], "140")
        self.assertEqual(groups[1]["members"][0]["end_ms"], "421/3")
        self.assertTrue(groups[1]["members"][0]["srt_dropped"])


if __name__ == "__main__":
    unittest.main()
