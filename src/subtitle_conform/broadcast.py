"""播出端单轨字幕对版（broadcast 模式）。

复用原校验与精确有理数对版，在全部精确碎片上枚举合法播出条目，
用两级扫描 DP 选出唯一方案，并为未入选可选碎片计算全局反事实分数。
仅依赖标准库，不引入外部求解器。
"""
from bisect import bisect_left, bisect_right
from fractions import Fraction
from math import ceil

from .io import fields, integer
from .model import validate
from .render import timestamp
from .timeline import conform

FRAME_MS = 40


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _validate_broadcast(data, clips, fragments, duration):
    broadcast = data["broadcast"]
    max_shift = integer(broadcast["max_shift_ms"], "max_shift_ms", 0, 200)
    min_gap = integer(broadcast["min_gap_frames"], "min_gap_frames", 0, 5)

    protected_raw = broadcast["protected"]
    if not isinstance(protected_raw, list) or len(protected_raw) > 200:
        raise ValueError("protected requires an array of at most 200 rows")
    protected = []
    for row in protected_raw:
        fields(row, ("start_ms", "end_ms"))
        start = integer(row["start_ms"], "protected.start_ms", 0, 10**12)
        end = integer(row["end_ms"], "protected.end_ms", 0, 10**12)
        if start >= end:
            raise ValueError("protected interval must have positive duration")
        if Fraction(end) > duration:
            raise ValueError("protected interval outside program duration")
        protected.append((start, end))

    clip_order = list(clips)
    adjacent_cuts = {tuple(pair) for pair in zip(clip_order, clip_order[1:])}
    join_raw = broadcast["join_cuts"]
    if not isinstance(join_raw, list):
        raise ValueError("join_cuts requires an array")
    join_cuts = set()
    for row in join_raw:
        if not isinstance(row, list) or len(row) != 2 \
                or not all(isinstance(value, str) for value in row):
            raise ValueError("join_cuts row must be [left_clip_id, right_clip_id]")
        pair = tuple(row)
        if pair[0] not in clips or pair[1] not in clips:
            raise ValueError("join_cuts references unknown clip")
        if pair not in adjacent_cuts:
            raise ValueError("join_cuts must be adjacent clips in EDL order")
        if pair in join_cuts:
            raise ValueError("duplicate join_cuts declaration")
        join_cuts.add(pair)

    identities = {tuple(row["fragment_id"]) for row in fragments}
    rules_raw = broadcast["fragment_rules"]
    if not isinstance(rules_raw, list):
        raise ValueError("fragment_rules requires an array")
    rules = {}
    for row in rules_raw:
        fields(row, ("fragment_id", "must_keep", "priority"))
        fragment_id = row["fragment_id"]
        if not isinstance(fragment_id, list) or len(fragment_id) != 2 \
                or not all(isinstance(value, str) for value in fragment_id):
            raise ValueError("fragment_id must be [clip_id, cue_id]")
        identity = tuple(fragment_id)
        if identity not in identities:
            raise ValueError("fragment_rules references unknown fragment identity")
        if identity in rules:
            raise ValueError("duplicate fragment_rules declaration")
        if type(row["must_keep"]) is not bool:
            raise ValueError("must_keep requires a boolean")
        priority = integer(row["priority"], "priority", 1, 1000)
        rules[identity] = (row["must_keep"], priority)

    return {
        "max_shift_ms": max_shift,
        "gap_ms": min_gap * FRAME_MS,
        "protected": _merge_protected(protected),
        "join_cuts": join_cuts,
        "rules": rules,
    }


def _merge_protected(segments):
    merged = []
    for start, end in sorted(segments):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


# ---------------------------------------------------------------------------
#  Fenwick 树（前缀最大值，自定义优劣关系）
# ---------------------------------------------------------------------------

class FenwickMax:
    def __init__(self, coordinates):
        self.coordinates = sorted(set(coordinates))
        self.tree = [None] * (len(self.coordinates) + 1)

    def update(self, key, value, better):
        index = bisect_left(self.coordinates, key) + 1
        while index < len(self.tree):
            current = self.tree[index]
            if current is None or better(value, current):
                self.tree[index] = value
            index += index & -index

    def prefix_query(self, key, better):
        """返回坐标 <= key 的最优记录，没有则 None。"""
        index = bisect_right(self.coordinates, key)
        best = None
        while index:
            current = self.tree[index]
            if current is not None and (best is None or better(current, best)):
                best = current
            index -= index & -index
        return best


class ReverseFenwickMax:
    """坐标 >= key 的后缀最大值。"""

    def __init__(self, coordinates):
        self.coordinates = sorted(set(coordinates))
        self.size = len(self.coordinates)
        self.tree = [None] * (self.size + 1)

    def update(self, key, value, better):
        index = self.size - bisect_left(self.coordinates, key)
        while index < len(self.tree):
            current = self.tree[index]
            if current is None or better(value, current):
                self.tree[index] = value
            index += index & -index

    def suffix_query(self, key, better):
        """返回坐标 >= key 的最优记录，没有则 None。"""
        index = self.size - bisect_left(self.coordinates, key)
        best = None
        while index:
            current = self.tree[index]
            if current is not None and (best is None or better(current, best)):
                best = current
            index -= index & -index
        return best


# ---------------------------------------------------------------------------
# 合法条目枚举
# ---------------------------------------------------------------------------

def _grid_points(low, high):
    """[low, high] 内 40ms 整数倍网格点。"""
    first = ceil(Fraction(low) / FRAME_MS) * FRAME_MS
    value = first
    while value <= high:
        yield Fraction(value)
        value += FRAME_MS


def _joinable(left, right, join_cuts):
    return (left["cue_id"] == right["cue_id"]
            and (left["clip_id"], right["clip_id"]) in join_cuts
            and left["source_end_ms"] == right["source_start_ms"]
            and left["end"] == right["start"])


def _has_protected_overlap(protected, starts, start, end):
    if not protected:
        return False
    index = bisect_right(starts, start) - 1
    if index >= 0 and protected[index][1] > start:
        return True
    next_index = index + 1
    if next_index < len(protected) and protected[next_index][0] < end:
        return True
    return False


def _enumerate_placements(fragments, spans, config, conform_input):
    """返回 (groups, all_starts, all_ends, touched_indices)。

    group = (i, j, placements)；placement 为 dict。
    """
    n = len(fragments)
    shift = config["max_shift_ms"]
    protected = config["protected"]
    protected_starts = [row[0] for row in protected]
    rules = config["rules"]
    minimum_ms = conform_input["min_duration_ms"]
    max_cps = conform_input["max_chars_per_second"]
    span_by_clip = {row["clip_id"]: (row["output_start_ms"], row["output_end_ms"])
                    for row in spans}

    joinable = [False] * max(n - 1, 0)
    for index in range(n - 1):
        joinable[index] = _joinable(fragments[index], fragments[index + 1],
                                    config["join_cuts"])

    groups = []
    groups_by_start = [[] for _ in range(n)]
    groups_by_end = [[] for _ in range(n)]
    all_starts, all_ends, touched = [], [], set()

    def build_group(i, j):
        first, last = fragments[i], fragments[j]
        anchor_start, anchor_end = first["start"], last["end"]
        span_low = Fraction(span_by_clip[first["clip_id"]][0])
        span_high = Fraction(span_by_clip[last["clip_id"]][1])
        text = first["text"]
        characters = sum(not char.isspace() for char in text)
        identity_keys = tuple(tuple(fragments[k]["fragment_id"]) for k in range(i, j + 1))

        must_count = 0
        optional_sum = 0
        for k in range(i, j + 1):
            identity = tuple(fragments[k]["fragment_id"])
            is_must, priority = rules.get(identity, (False, 1))
            if is_must:
                must_count += 1
            else:
                optional_sum += priority

        start_low = max(span_low, anchor_start - shift, Fraction(0))
        start_high = anchor_start + shift
        # 起止只需与首/末碎片正长相交，允许越过锚点（在 shift 范围内）
        end_low = anchor_end - shift
        end_high = min(span_high, anchor_end + shift)

        placements = []
        for start in _grid_points(start_low, start_high):
            if start >= first["end"]:
                continue
            for end in _grid_points(end_low, end_high):
                if end <= last["start"] or end <= start:
                    continue
                duration = end - start
                if duration < minimum_ms:
                    continue
                if characters * 1000 > max_cps * duration:
                    continue
                if _has_protected_overlap(protected, protected_starts, start, end):
                    continue
                offset = abs(start - anchor_start) + abs(end - anchor_end)
                placements.append({
                    "i": i, "j": j, "start": start, "end": end,
                    "must": must_count, "opt": optional_sum,
                    "offset": offset,
                    "key": (start, end, identity_keys),
                    "text": text, "ids": identity_keys,
                })
                all_starts.append(start)
                all_ends.append(end)
        if not placements:
            return
        group = (i, j, placements)
        groups.append(group)
        groups_by_start[i].append(group)
        groups_by_end[j].append(group)
        for k in range(i, j + 1):
            touched.add(k)

    for i in range(n):
        # 单碎片组
        build_group(i, i)
        # 以 i 为起点的连读组
        j = i
        while j + 1 < n and joinable[j]:
            j += 1
            build_group(i, j)

    return groups, groups_by_start, groups_by_end, all_starts, all_ends, touched


# ---------------------------------------------------------------------------
# 方案记录与五级优劣
# ---------------------------------------------------------------------------

# record = (must, opt, count, offset, seq, parent, entry, end)
SENTINEL = (0, 0, 0, Fraction(0), (), None, None, None)


def _better(left, right):
    if left[0] != right[0]:
        return left[0] > right[0]
    if left[1] != right[1]:
        return left[1] > right[1]
    if left[2] != right[2]:
        return left[2] < right[2]
    if left[3] != right[3]:
        return left[3] < right[3]
    return left[4] < right[4]


def _extend(record, placement):
    entry = (placement["start"], placement["end"], placement["ids"], placement["text"])
    return (record[0] + placement["must"],
            record[1] + placement["opt"],
            record[2] + 1,
            record[3] + placement["offset"],
            record[4] + (placement["key"],),
            record, entry, placement["end"])


def _solve(fragments, config, groups_by_start, groups_by_end,
           all_starts, all_ends):
    n = len(fragments)
    gap = config["gap_ms"]
    forward = FenwickMax(all_ends or [Fraction(0)])
    release_buckets = [[] for _ in range(n)]

    # 前向扫描：每个 placement 缓存最优前缀记录
    for i in range(n):
        if i > 0:
            for record in release_buckets[i - 1]:
                forward.update(record[7], record, _better)
        for group in groups_by_start[i]:
            j = group[1]
            for placement in group[2]:
                predecessor = forward.prefix_query(placement["start"] - gap, _better)
                if predecessor is None:
                    predecessor = SENTINEL
                placement["prefix"] = predecessor
                record = _extend(predecessor, placement)
                release_buckets[j].append(record)

    best = SENTINEL
    for bucket in release_buckets:
        for record in bucket:
            if _better(record, best):
                best = record

    # 反向扫描：每个 placement 缓存 (must, opt) 最优后缀
    backward = ReverseFenwickMax(all_starts or [Fraction(0)])

    def tuple_better(left, right):
        return left > right

    for frontier in range(n - 1, -1, -1):
        # 先计算在 frontier 结束的组（结构中只含起点 > frontier 的后缀）
        for group in groups_by_end[frontier]:
            for placement in group[2]:
                suffix = backward.suffix_query(placement["end"] + gap, tuple_better)
                placement["suffix"] = suffix if suffix is not None else (0, 0)
        # 再释放起点恰为 frontier 的组
        for group in groups_by_start[frontier]:
            for placement in group[2]:
                suffix = placement["suffix"]
                value = (placement["must"] + suffix[0], placement["opt"] + suffix[1])
                backward.update(placement["start"], value, tuple_better)

    return best


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------

def _srt_text(entries):
    lines = []
    for index, entry in enumerate(entries, 1):
        lines.append(
            f"{index}\n{timestamp(entry['start_ms'])} --> "
            f"{timestamp(entry['end_ms'])}\n{entry['text']}\n")
    return "\n".join(lines)


def _counterfactuals(fragments, config, placements, coverage,
                     selected_optional_sum, total_must):
    """对每条未入选的可选碎片计算强制纳入后的全局最优可选总分。"""
    rules = config["rules"]
    containing = {}
    for placement in placements:
        for index in range(placement["i"], placement["j"] + 1):
            containing.setdefault(index, []).append(placement)

    results = []
    for index, row in enumerate(fragments):
        identity = tuple(row["fragment_id"])
        is_must, _priority = rules.get(identity, (False, 1))
        if is_must or identity in coverage:
            continue
        candidates = containing.get(index, ())
        any_entry = bool(candidates)
        best_optional = None
        for placement in candidates:
            prefix = placement["prefix"]
            suffix = placement["suffix"]
            combined_must = prefix[0] + placement["must"] + suffix[0]
            if combined_must != total_must:
                continue
            combined_opt = prefix[1] + placement["opt"] + suffix[1]
            if best_optional is None or combined_opt > best_optional:
                best_optional = combined_opt
        if not any_entry:
            reason = "no_legal_entry"
        elif best_optional is None:
            reason = "cannot_satisfy_all_must_keep"
        elif best_optional < selected_optional_sum:
            reason = "optional_score_reduced"
        else:
            reason = "same_optional_score_decided_by_later_rules"
        results.append({
            "fragment_id": list(identity),
            "has_legal_entry": any_entry,
            "max_optional_priority_sum": best_optional,
            "reason": reason,
        })
    return results


def run_broadcast(data):
    if not isinstance(data, dict):
        raise ValueError("broadcast input requires an object")
    fields(data, ("conform", "broadcast"))
    if not isinstance(data["broadcast"], dict):
        raise ValueError("broadcast must be an object")
    fields(data["broadcast"], ("max_shift_ms", "min_gap_frames", "protected",
                               "join_cuts", "fragment_rules"))
    conform_input = data["conform"]
    if not isinstance(conform_input, dict):
        raise ValueError("conform must be an object")
    _, clips, cues = validate(conform_input)
    fragments, spans, duration, _omitted = conform(clips, cues)
    config = _validate_broadcast(data, clips, fragments, duration)
    (groups, groups_by_start, groups_by_end,
     all_starts, all_ends, touched) = _enumerate_placements(
        fragments, spans, config, conform_input)

    best = _solve(fragments, config, groups_by_start, groups_by_end,
                  all_starts, all_ends)

    all_placements = [placement for group in groups for placement in group[2]]

    rules = config["rules"]
    total_must = sum(1 for row in fragments
                     if rules.get(tuple(row["fragment_id"]), (False, 1))[0])
    selected_must = best[0]
    status = "ready" if selected_must == total_must else "blocked"

    # 重建选中条目
    selected_entries = []
    record = best
    while record[6] is not None:
        start, end, ids, text = record[6]
        selected_entries.append({
            "start_ms": int(start), "end_ms": int(end), "text": text,
            "fragment_ids": [list(identity) for identity in ids],
        })
        record = record[5]
    selected_entries.reverse()

    coverage = {}
    for entry_index, entry in enumerate(selected_entries, 1):
        for identity in entry["fragment_ids"]:
            coverage[tuple(identity)] = entry_index

    audit = []
    for index, row in enumerate(fragments):
        identity = tuple(row["fragment_id"])
        is_must, priority = rules.get(identity, (False, 1))
        audit.append({
            "fragment_id": list(identity),
            "source_start_ms": row["source_start_ms"],
            "source_end_ms": row["source_end_ms"],
            "start_ms": str(row["start"]),
            "end_ms": str(row["end"]),
            "must_keep": is_must,
            "priority": priority,
            "has_legal_entry": index in touched,
            "covered_by_entry": coverage.get(identity),
        })

    report = {
        "schema_version": 2,
        "mode": "broadcast",
        "status": status,
        "output_duration_ms": str(duration),
        "scores": {
            "must_keep_fragments_total": total_must,
            "must_keep_fragments_covered": selected_must,
            "optional_priority_sum": best[1],
            "entry_count": best[2],
            "total_abs_offset_ms": str(best[3]),
        },
        "entries": [dict({"index": index}, **entry)
                    for index, entry in enumerate(selected_entries, 1)],
        "fragment_audit": audit,
    }

    if status == "ready":
        report["counterfactuals"] = _counterfactuals(
            fragments, config, all_placements, coverage,
            best[1], total_must)
        report["srt_text"] = _srt_text(selected_entries)
    else:
        report["srt_text"] = None
    return report
