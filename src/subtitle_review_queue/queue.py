"""待复核组：从普通对版精确碎片构造传递闭包并登记快照。

两条合并规则各自传递闭合，并取二者的并集闭包：

1. 同一 ``[source_id, cue_id]`` 的所有实际出现一起复核；
2. 精确节目区间正长相交的两行一起复核。端点接触不是重叠。

闭包推导不依赖某一行自身是否有阅读提示。整组是否派单看组内是否有任何
一条时长/阅读速度提示、有被 SRT 舍掉的行、或有任意两行精确重叠。成员按
普通对版的精确排序（起点、终点、clip_id、cue_id）排列，组顺序按首成员在
该排序里的位置。组 ID 就是首成员的碎片身份 ``[clip_id, cue_id]``。
"""
from fractions import Fraction
from subtitle_conform.model import validate as validate_conform
from subtitle_conform.render import render
from subtitle_conform.timeline import conform

SCHEMA_VERSION = 1
MODE = "subtitle-review-queue"


class _UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def member_order(row):
    return (Fraction(row["start_ms"]), Fraction(row["end_ms"]),
            row["fragment_id"][0], row["fragment_id"][1])


def _member_rows(fragments, cues, report):
    """把普通对版的精确碎片、提示与舍入丢弃标记合并成登记成员。"""
    hints = {tuple(row["fragment_id"]): row
             for row in report["fragments"]}
    dropped = {tuple(identity) for identity in report["srt_dropped_fragments"]}
    rows = []
    for fragment in fragments:
        identity = (fragment["clip_id"], fragment["cue_id"])
        rendered = hints[identity]
        rows.append({
            "fragment_id": [fragment["clip_id"], fragment["cue_id"]],
            "source_id": cues[fragment["cue_id"]]["source_id"],
            "source_start_ms": fragment["source_start_ms"],
            "source_end_ms": fragment["source_end_ms"],
            "start_ms": str(fragment["start"]),
            "end_ms": str(fragment["end"]),
            "text": fragment["text"],
            "chars_per_second": rendered["chars_per_second"],
            "issues": list(rendered["issues"]),
            "srt_dropped": identity in dropped,
        })
    return rows


def _needs_review(row):
    return bool(row["issues"]) or row["srt_dropped"]


def build_groups(conform_data):
    """校验并计算普通对版，返回 (完整成员快照, 待复核组)。"""
    _sources, clips, cues = validate_conform(conform_data)
    fragments, _spans, _duration, _omitted = conform(clips, cues)
    report = render(fragments, conform_data["min_duration_ms"],
                    conform_data["max_chars_per_second"])
    rows = _member_rows(fragments, cues, report)

    union = _UnionFind(len(rows))
    # 规则一：同一素材上同一 cue 的全部实际出现。
    first_seen = {}
    for index, row in enumerate(rows):
        key = (row["source_id"], row["fragment_id"][1])
        if key in first_seen:
            union.union(first_seen[key], index)
        else:
            first_seen[key] = index
    # 规则二：精确节目区间正长相交。rows 已按普通对版排序，扫描活动集，
    # 端点接触（前一行 end == 本行 start）不进入活动集。
    active = []
    for index, row in enumerate(rows):
        start = Fraction(row["start_ms"])
        active = [other for other in active
                  if Fraction(rows[other]["end_ms"]) > start]
        for other in active:
            union.union(other, index)
        active.append(index)

    components = {}
    for index in range(len(rows)):
        components.setdefault(union.find(index), []).append(index)

    groups = []
    for indexes in components.values():
        ordered = sorted((rows[index] for index in indexes), key=member_order)
        has_hint = any(_needs_review(row) for row in ordered)
        # 任意两行精确重叠：按起点扫描并维护已见最大终点，长区间跨过多个
        # 短区间也能发现；不依赖报告里截断到 100 条的重叠示例。
        overlaps = False
        furthest = None
        for row in ordered:
            start = Fraction(row["start_ms"])
            end = Fraction(row["end_ms"])
            if furthest is not None and start < furthest:
                overlaps = True
                break
            furthest = end if furthest is None else max(furthest, end)
        if not (has_hint or overlaps):
            continue
        groups.append({"group_id": list(ordered[0]["fragment_id"]),
                       "members": ordered})

    groups.sort(key=lambda group: member_order(group["members"][0]))
    return rows, groups


def registration_state(conform_data):
    """登记用的完整初始账本；普通对版只在这里计算一次。"""
    members, groups = build_groups(conform_data)
    index_of = {tuple(member["fragment_id"]): index
                for index, member in enumerate(members)}
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "members": members,
        "groups": [
            {"group_id": group["group_id"],
             "member_indexes": [index_of[tuple(member["fragment_id"])]
                                for member in group["members"]],
             "state": "pending",
             "assignment": None,
             "generation": 0}
            for group in groups
        ],
        "tickets": {},
        "requests": {},
        "request_ids": {},
        "register_key": None,
        "register_receipt": None,
    }
