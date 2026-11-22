"""subtitle-cost 引擎：用量统计、多级归并撤回与按覆盖权重分账。

区间全部为源时间轴上的半开整数毫秒区间；费用权重使用 fractions.Fraction
精确计算，逐 cue 向下取整后按最大余数法补齐，全程不做浮点运算。
"""
from fractions import Fraction

from subtitle_conform.model import validate as validate_conform
from subtitle_conform.timeline import conform

from .model import validate_collect, validate_merge, validate_settle

SCHEMA_VERSION = 1
MODE = "subtitle-cost"


# ---------------------------------------------------------------------------
# 区间工具
# ---------------------------------------------------------------------------

def union(spans):
    """把半开整数区间合并为互不重叠、按起点排序的并集；相接即合并。"""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def _serialize(program):
    """空墓碑只输出 program_id/withdrawn；其余条目输出完整规范内容。"""
    if program["cost_center"] is None:
        return {"program_id": program["program_id"],
                "withdrawn": True}
    return program


def _bundle(programs):
    """构造规范用量包，节目按 program_id 的 Unicode 码点序排列。"""
    ordered = sorted(programs, key=lambda row: row["program_id"])
    return {"schema_version": SCHEMA_VERSION, "mode": MODE, "op": "bundle",
            "programs": [_serialize(row) for row in ordered]}


def _content_signature(program):
    """同 ID 节目内容相同的判据：成本中心与全部规范用量逐字节一致。

    先于用量到达的空墓碑没有内容，不参与内容比较（任何后到包都会被
    撤回状态直接压过）。
    """
    if program["cost_center"] is None:
        return None
    return (program["cost_center"],
            tuple((row["source_id"], row["cue_id"],
                   row["cue_start_ms"], row["cue_end_ms"],
                   tuple(tuple(span) for span in row["intervals"]))
                  for row in program["usage"]))


# ---------------------------------------------------------------------------
# collect：从普通对版输入统计源区间并集
# ---------------------------------------------------------------------------

def run_collect(data):
    program_id, cost_center, conform_data = validate_collect(data)
    _, clips, cues = validate_conform(conform_data)
    # 复用普通对版：以实际产生的精确碎片为准，SRT 舍掉的短碎片仍在其中；
    # 速度只改变成片时长，碎片始终带源时间，源用量与变速无关。
    fragments, _, _, _ = conform(clips, cues)

    collected = {}
    for fragment in fragments:
        cue = cues[fragment["cue_id"]]
        key = (cue["source_id"], fragment["cue_id"])
        if key not in collected:
            collected[key] = {
                "source_id": cue["source_id"],
                "cue_id": fragment["cue_id"],
                "cue_start_ms": cue["start_ms"],
                "cue_end_ms": cue["end_ms"],
                "spans": [],
            }
        # 同节目重复剪入同一源位置：区间求并集后只计覆盖一次
        collected[key]["spans"].append(
            (fragment["source_start_ms"], fragment["source_end_ms"]))

    usage = []
    for (source_id, cue_id) in sorted(collected):
        row = collected[(source_id, cue_id)]
        usage.append({
            "source_id": source_id,
            "cue_id": cue_id,
            "cue_start_ms": row["cue_start_ms"],
            "cue_end_ms": row["cue_end_ms"],
            "intervals": union(row["spans"]),
        })

    return _bundle([{
        "program_id": program_id,
        "cost_center": cost_center,
        "withdrawn": False,
        "usage": usage,
    }])


# ---------------------------------------------------------------------------
# merge：多级归并与永久撤回
# ---------------------------------------------------------------------------

def _upsert(store, incoming):
    """把一个节目条目并入以 program_id 为键的节目表，处理冲突与撤回。"""
    pid = incoming["program_id"]
    if pid not in store:
        store[pid] = incoming
        return
    existing = store[pid]
    existing_sig = _content_signature(existing)
    incoming_sig = _content_signature(incoming)
    if existing_sig is not None and incoming_sig is not None \
            and existing_sig != incoming_sig:
        # 同 ID 内容不同一律报错，即使该节目已经撤回
        raise ValueError(f"conflicting program content for program_id: {pid}")
    # 内容相同或一方为先到空墓碑：有效条目只计一次；撤回永久压过同 ID
    # 用量，旧包重送不能恢复它。空墓碑被后到内容填充后仍保持撤回。
    withdrawn = existing["withdrawn"] or incoming["withdrawn"]
    if existing_sig is None:
        merged = dict(incoming, withdrawn=True)
    else:
        merged = dict(existing, withdrawn=withdrawn)
    store[pid] = merged


def run_merge(data):
    programs, withdrawn_ids = validate_merge(data)
    store = {}
    for row in programs:
        _upsert(store, row)
    for pid in withdrawn_ids:
        if pid in store:
            if not store[pid]["withdrawn"]:
                store[pid] = dict(store[pid], withdrawn=True)
        else:
            # 撤回可先于用量到达：建立无内容空墓碑，永久压过后到同 ID 包
            store[pid] = {"program_id": pid, "cost_center": None,
                          "withdrawn": True, "usage": []}
    return _bundle(list(store.values()))


# ---------------------------------------------------------------------------
# settle：按成本中心覆盖权重分配逐 cue 费用
# ---------------------------------------------------------------------------

def _center_union(active_programs):
    """返回 {(source_id, cue_id): {cost_center: 合并区间并集}} 及 cue 边界。"""
    per_cue = {}
    for program in active_programs:
        center = program["cost_center"]
        for row in program["usage"]:
            key = (row["source_id"], row["cue_id"])
            entry = per_cue.setdefault(
                key, {"bounds": (row["cue_start_ms"], row["cue_end_ms"]),
                      "centers": {}})
            if entry["bounds"] != (row["cue_start_ms"], row["cue_end_ms"]):
                raise ValueError(
                    f"inconsistent original cue bounds for {list(key)}")
            entry["centers"].setdefault(center, []).extend(
                tuple(span) for span in row["intervals"])
    return per_cue


def _segment_weights(spans_by_center):
    """扫描事件点，按每段覆盖的不同成本中心等分长度，累计各中心精确权重。"""
    events = []
    for center, spans in spans_by_center.items():
        for start, end in spans:
            events.append((start, 1, center))
            events.append((end, -1, center))
    # 同一时刻先结束后开始：半开区间端点相接不产生零长中间段
    events.sort(key=lambda event: (event[0], event[1]))
    weights = {center: Fraction(0) for center in spans_by_center}
    active = set()
    cursor = None
    for position, delta, center in events:
        if cursor is not None and active and position > cursor:
            share = Fraction(position - cursor, len(active))
            for member in active:
                weights[member] += share
        if delta == 1:
            active.add(center)
        else:
            active.discard(center)
        cursor = position
    return weights


def _largest_remainder(weights, fee_fen):
    """整笔费用按权重比例分配：逐中心向下取整，余分按最大余数补齐。"""
    total_weight = sum(weights.values(), Fraction(0))
    exact = {center: Fraction(fee_fen) * weight / total_weight
             for center, weight in weights.items()}
    floors = {center: exact[center].numerator // exact[center].denominator
              for center in exact}
    remainder = fee_fen - sum(floors.values())
    # 余数从大到小；平局按中心 ID 的 Unicode 码点序（Python 字符串默认序）
    order = sorted(weights, key=lambda center: (
        -(exact[center] - floors[center]), center))
    allocated = dict(floors)
    for center in order[:remainder]:
        allocated[center] += 1
    assert sum(allocated.values()) == fee_fen
    return exact, allocated


def run_settle(data):
    programs, fees = validate_settle(data)
    active = [row for row in programs if not row["withdrawn"]]
    per_cue = _center_union(active)

    cue_reports = []
    center_totals = {}
    allocated_total = 0

    for key in sorted(per_cue):
        source_id, cue_id = key
        entry = per_cue[key]
        if key not in fees:
            raise ValueError(f"missing fee quote for in-use cue: {list(key)}")
        quote = fees[key]
        if (quote["cue_start_ms"], quote["cue_end_ms"]) != entry["bounds"]:
            raise ValueError(
                f"fee quote bounds mismatch for cue: {list(key)}")

        spans_by_center = {
            center: union(spans)
            for center, spans in entry["centers"].items()}
        weights = _segment_weights(spans_by_center)
        exact, allocated = _largest_remainder(weights, quote["fee_fen"])

        centers_report = []
        for center in sorted(weights):
            center_totals.setdefault(center, 0)
            center_totals[center] += allocated[center]
            centers_report.append({
                "cost_center": center,
                "coverage_intervals": spans_by_center[center],
                "weight_ms": str(weights[center]),
                "allocated_fen": allocated[center],
            })

        allocated_total += quote["fee_fen"]
        cue_reports.append({
            "source_id": source_id,
            "cue_id": cue_id,
            "cue_start_ms": entry["bounds"][0],
            "cue_end_ms": entry["bounds"][1],
            "fee_fen": quote["fee_fen"],
            "centers": centers_report,
        })

    used_keys = set(per_cue)
    deferred = [fees[key] for key in sorted(fees) if key not in used_keys]
    deferred_total = sum(row["fee_fen"] for row in deferred)

    # 分配与待摊必须对平全部报价
    assert allocated_total + deferred_total == sum(
        row["fee_fen"] for row in fees.values())

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "op": "settle",
        "cues": cue_reports,
        "deferred_fees": deferred,
        "center_totals": [
            {"cost_center": center, "total_fen": center_totals[center]}
            for center in sorted(center_totals)],
        "summary": {
            "program_count": len(programs),
            "active_program_count": len(active),
            "withdrawn_program_count": len(programs) - len(active),
            "settled_cue_count": len(cue_reports),
            "deferred_fee_count": len(deferred),
            "allocated_total_fen": allocated_total,
            "deferred_total_fen": deferred_total,
        },
    }


def run(data):
    op = data.get("op")
    if op == "collect":
        return run_collect(data)
    if op == "merge":
        return run_merge(data)
    if op == "settle":
        return run_settle(data)
    raise ValueError(f"unknown op: {op!r}")
