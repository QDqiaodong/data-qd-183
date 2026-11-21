"""授权核验引擎：区间并集、缺口、例外补缺与精确节目时间映射。"""
from fractions import Fraction

from .model import validate


def merge(intervals):
    """把半开整数区间合并为互不重叠、按起点排序的并集；区间相接即合并。"""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def subtract(span, covered):
    """返回半开区间 span 相对已合并区间 covered 的正长缺口。"""
    start, end = span
    gaps = []
    cursor = start
    for cstart, cend in covered:
        if cend <= cursor:
            continue
        if cstart >= end:
            break
        if cstart > cursor:
            gaps.append((cursor, min(cstart, end)))
        cursor = max(cursor, cend)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def intersect(a, b):
    """两个半开区间的正长交集，无正长交集时返回 None。"""
    start, end = max(a[0], b[0]), min(a[1], b[1])
    return (start, end) if start < end else None


def _program_intervals(gaps, clip, cursor):
    """把源时间缺口按该剪辑速度比换算为节目时间，速度比取自剪辑声明。"""
    scale = Fraction(clip["speed_den"], clip["speed_num"])
    return [
        {
            "source_start_ms": start,
            "source_end_ms": end,
            "program_start_ms": str(
                cursor + (start - clip["in_ms"]) * scale),
            "program_end_ms": str(
                cursor + (end - clip["in_ms"]) * scale),
        }
        for start, end in gaps
    ]


def run_clearance(data):
    model = validate(data)
    clips = model["clips"]
    clip_order = model["clip_order"]
    deliveries = model["deliveries"]
    delivery_order = model["delivery_order"]
    grants = model["grants"]
    exceptions = model["exceptions"]

    referenced_sources = {clip["source_id"] for clip in clips.values()}

    # 按 (发布目标, 剪辑) 预筛同时匹配渠道、地区且在 release_at 有效的授权
    applicable = {}
    for did in delivery_order:
        delivery = deliveries[did]
        release_at = delivery["release_at"]
        for cid in clip_order:
            clip = clips[cid]
            matched = []
            matched_ids = []
            for grant in grants:
                if grant["source_id"] != clip["source_id"]:
                    continue
                if delivery["channel"] not in grant["channels"]:
                    continue
                if delivery["territory"] not in grant["territories"]:
                    continue
                # 有效期半开 [valid_from, valid_until)，
                # release_at 恰在结束端点时授权不适用
                if release_at < grant["valid_from"]:
                    continue
                if grant["valid_until"] is not None and \
                        release_at >= grant["valid_until"]:
                    continue
                matched.append((grant["start_ms"], grant["end_ms"]))
                matched_ids.append(grant["id"])
            applicable[(did, cid)] = (merge(matched),
                                      sorted(matched_ids))

    # 例外严格绑定发布目标与剪辑实例，不能跨实例使用
    exception_index = {}
    for exception in exceptions:
        exception_index.setdefault(
            (exception["delivery_id"], exception["clip_id"]), []).append(
                exception)

    delivery_reports = []
    total_gap_count = 0

    for did in delivery_order:
        delivery = deliveries[did]
        uses = []
        target_blocked = False
        cursor = Fraction(0)

        for cid in clip_order:
            clip = clips[cid]
            scale = Fraction(clip["speed_den"], clip["speed_num"])
            span = (clip["in_ms"], clip["out_ms"])
            covered, grant_ids = applicable[(did, cid)]
            gaps = subtract(span, covered)

            used_exception_ids = []
            exception_cuts = []
            for exception in exception_index.get((did, cid), ()):
                exception_span = (exception["start_ms"], exception["end_ms"])
                pieces = [piece for gap in gaps
                          if (piece := intersect(gap, exception_span))
                          is not None]
                if not pieces:
                    # 仅端点接触或落在已授权范围：无正长交集，不得补缺
                    continue
                used_exception_ids.append(exception["id"])
                exception_cuts.extend(pieces)

            remaining_gaps = subtract(span, merge(
                covered + merge(exception_cuts))) if exception_cuts else gaps
            remaining = _program_intervals(remaining_gaps, clip, cursor)
            total_gap_count += len(remaining_gaps)
            status = "cleared" if not remaining_gaps else "blocked"
            target_blocked = target_blocked or bool(remaining_gaps)

            uses.append({
                "clip_id": cid,
                "source_id": clip["source_id"],
                "source_interval_ms": [clip["in_ms"], clip["out_ms"]],
                "program_interval_ms": [str(cursor),
                                        str(cursor + (
                                            clip["out_ms"] - clip["in_ms"])
                                            * scale)],
                "grant_ids": grant_ids,
                "exception_ids": sorted(set(used_exception_ids)),
                "remaining_gaps": remaining,
                "status": status,
            })
            cursor += (clip["out_ms"] - clip["in_ms"]) * scale

        delivery_reports.append({
            "delivery_id": did,
            "channel": delivery["channel"],
            "territory": delivery["territory"],
            "release_at": delivery["release_at_text"],
            "status": "blocked" if target_blocked else "cleared",
            "uses": uses,
        })

    cleared_count = sum(1 for report in delivery_reports
                        if report["status"] == "cleared")
    unused_grant_ids = sorted(
        grant["id"] for grant in grants
        if grant["source_id"] not in referenced_sources)

    return {
        "schema_version": 1,
        "mode": "cut-clearance",
        "deliveries": delivery_reports,
        "summary": {
            "delivery_count": len(delivery_reports),
            "cleared_count": cleared_count,
            "blocked_count": len(delivery_reports) - cleared_count,
            "gap_count": total_gap_count,
        },
        "unused_grant_ids": unused_grant_ids,
    }
