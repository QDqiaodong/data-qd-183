"""subtitle-cost 输入校验：严格字段、区间并集与归并树结构。

所有时间为源时间轴上的整数毫秒，区间一律半开 [start, end)。
本模块只做结构与范围校验；跨包冲突与撤回压过的业务规则在 cost.py。
"""
from subtitle_conform.io import fields, integer, label, rows

MAX_PROGRAMS = 1000
MAX_USAGE_ROWS = 2000
MAX_INTERVALS = 2000
MAX_MERGE_NODES = 100
MAX_MERGE_DEPTH = 100
MAX_WITHDRAWN = 1000
MAX_FEES = 2000
MAX_BOUND_MS = 10**12
MAX_FEE_FEN = 10**12


def interval(value, name):
    """校验 [start_ms, end_ms) 半开正长整数区间，返回整数二元组。"""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} requires a [start_ms, end_ms) array")
    start = integer(value[0], f"{name}.start_ms", 0, MAX_BOUND_MS)
    end = integer(value[1], f"{name}.end_ms", 0, MAX_BOUND_MS)
    if start >= end:
        raise ValueError(f"{name} must have positive length")
    return start, end


def merged_intervals(value, name):
    """校验区间数组：每项正长，整体相接合并后互不重叠，返回并集。"""
    rows(value, name, 1, MAX_INTERVALS)
    spans = [interval(item, f"{name}[]") for item in value]
    ordered = sorted(spans)
    for (_, previous_end), (next_start, _) in zip(ordered, ordered[1:]):
        if next_start < previous_end:
            raise ValueError(f"{name} intervals must not overlap")
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def usage_row(value, name="usage"):
    """校验一条 [source_id, cue_id] 用量行，含原 cue 起止与并集区间。"""
    if not isinstance(value, dict):
        raise ValueError(f"{name} row must be an object")
    fields(value, ("source_id", "cue_id", "cue_start_ms", "cue_end_ms",
                   "intervals"))
    source_id = label(value["source_id"], f"{name}.source_id")
    cue_id = label(value["cue_id"], f"{name}.cue_id")
    cue_start = integer(value["cue_start_ms"], f"{name}.cue_start_ms",
                        0, MAX_BOUND_MS)
    cue_end = integer(value["cue_end_ms"], f"{name}.cue_end_ms",
                      0, MAX_BOUND_MS)
    if cue_start >= cue_end:
        raise ValueError(f"{name} cue interval must have positive length")
    spans = merged_intervals(value["intervals"], f"{name}.intervals")
    for start, end in spans:
        if start < cue_start or end > cue_end:
            raise ValueError(
                f"{name} intervals must stay within the original cue bounds")
    return {"source_id": source_id, "cue_id": cue_id,
            "cue_start_ms": cue_start, "cue_end_ms": cue_end,
            "intervals": spans}


def program(value, name="program"):
    """校验用量包中的单个节目条目（含撤回墓碑）。

    先于用量到达的撤回没有任何内容可保留，墓碑只有 program_id 与
    withdrawn=true；一旦同 ID 用量到达，墓碑会带上该节目的规范内容。
    """
    if not isinstance(value, dict):
        raise ValueError(f"{name} row must be an object")
    program_id = label(value["program_id"], f"{name}.program_id")
    if type(value["withdrawn"]) is not bool:
        raise ValueError(f"{name}.withdrawn requires a boolean")
    withdrawn = value["withdrawn"]
    # 先于用量到达的撤回墓碑只有两个键；带内容的墓碑与有效节目四键齐全
    if withdrawn and set(value) == {"program_id", "withdrawn"}:
        return {"program_id": program_id, "cost_center": None,
                "withdrawn": True, "usage": []}
    fields(value, ("program_id", "cost_center", "withdrawn", "usage"))
    cost_center = label(value["cost_center"], f"{name}.cost_center")
    usage_rows = rows(value["usage"], f"{name}.usage", 0, MAX_USAGE_ROWS)
    seen = set()
    normalized = []
    for row in usage_rows:
        checked = usage_row(row, f"{name}.usage")
        key = (checked["source_id"], checked["cue_id"])
        if key in seen:
            raise ValueError(
                f"{name} usage contains duplicate [source_id, cue_id]: {key}")
        seen.add(key)
        normalized.append(checked)
    normalized.sort(key=lambda row: (row["source_id"], row["cue_id"]))
    return {"program_id": program_id, "cost_center": cost_center,
            "withdrawn": withdrawn, "usage": normalized}


def bundle(value, name="bundle"):
    """校验一个可继续归并的用量包，program_id 全库唯一。"""
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    fields(value, ("schema_version", "mode", "op", "programs"))
    if value["schema_version"] != 1:
        raise ValueError(f"{name}.schema_version must be 1")
    if value["mode"] != "subtitle-cost":
        raise ValueError(f"{name}.mode must be \"subtitle-cost\"")
    if value["op"] != "bundle":
        raise ValueError(f"{name}.op must be \"bundle\"")
    program_rows = rows(value["programs"], f"{name}.programs",
                        0, MAX_PROGRAMS)
    seen = set()
    programs = []
    for row in program_rows:
        checked = program(row, name)
        if checked["program_id"] in seen:
            raise ValueError(
                f"{name} contains duplicate program_id: "
                f"{checked['program_id']}")
        seen.add(checked["program_id"])
        programs.append(checked)
    programs.sort(key=lambda row: row["program_id"])
    return programs


def _withdraw_list(value, name):
    rows(value, name, 0, MAX_WITHDRAWN)
    result = []
    seen = set()
    for item in value:
        program_id = label(item, f"{name}[]")
        if program_id in seen:
            raise ValueError(f"{name} contains duplicate program id: "
                             f"{program_id}")
        seen.add(program_id)
        result.append(program_id)
    return result


def merge_node(value, depth=1, name="merge node"):
    """递归校验任意层级的归并树，返回 (bundle节目列表, 本层及下层撤回ID)。"""
    if depth > MAX_MERGE_DEPTH:
        raise ValueError("merge nesting exceeds maximum depth")
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if value.get("op") == "bundle":
        return bundle(value, name), []
    fields(value, ("op", "bundles", "withdraw"))
    if value["op"] != "merge":
        raise ValueError(f"{name}.op must be \"merge\" or \"bundle\"")
    children = rows(value["bundles"], f"{name}.bundles",
                    1, MAX_MERGE_NODES)
    collected = []
    withdrawn = []
    for child in children:
        child_programs, child_withdrawn = merge_node(
            child, depth + 1, f"{name}.bundles[]")
        collected.extend(child_programs)
        withdrawn.extend(child_withdrawn)
    withdrawn.extend(
        _withdraw_list(value["withdraw"], f"{name}.withdraw"))
    return collected, withdrawn


def fee_row(value, name="fee"):
    """校验一条译制费报价，键为 [source_id, cue_id]，金额为非负整数分。"""
    if not isinstance(value, dict):
        raise ValueError(f"{name} row must be an object")
    fields(value, ("source_id", "cue_id", "cue_start_ms", "cue_end_ms",
                   "fee_fen"))
    source_id = label(value["source_id"], f"{name}.source_id")
    cue_id = label(value["cue_id"], f"{name}.cue_id")
    cue_start = integer(value["cue_start_ms"], f"{name}.cue_start_ms",
                        0, MAX_BOUND_MS)
    cue_end = integer(value["cue_end_ms"], f"{name}.cue_end_ms",
                      0, MAX_BOUND_MS)
    if cue_start >= cue_end:
        raise ValueError(f"{name} cue interval must have positive length")
    fee_fen = integer(value["fee_fen"], f"{name}.fee_fen",
                      0, MAX_FEE_FEN)
    return {"source_id": source_id, "cue_id": cue_id,
            "cue_start_ms": cue_start, "cue_end_ms": cue_end,
            "fee_fen": fee_fen}


def validate_settle(data):
    fields(data, ("op", "bundle", "fees"))
    if data["op"] != "settle":
        raise ValueError("settle input requires op \"settle\"")
    programs = bundle(data["bundle"], "bundle")
    fee_rows = rows(data["fees"], "fees", 0, MAX_FEES)
    seen = set()
    fees = {}
    for row in fee_rows:
        checked = fee_row(row)
        key = (checked["source_id"], checked["cue_id"])
        if key in seen:
            raise ValueError(f"duplicate fee key: {list(key)}")
        seen.add(key)
        fees[key] = checked
    return programs, fees


def validate_collect(data):
    fields(data, ("op", "program_id", "cost_center", "conform"))
    if data["op"] != "collect":
        raise ValueError("collect input requires op \"collect\"")
    program_id = label(data["program_id"], "program_id")
    cost_center = label(data["cost_center"], "cost_center")
    if not isinstance(data["conform"], dict):
        raise ValueError("conform must be an object")
    return program_id, cost_center, data["conform"]


def validate_merge(data):
    fields(data, ("op", "bundles", "withdraw"))
    if data["op"] != "merge":
        raise ValueError("merge input requires op \"merge\"")
    # 顶层允许空 bundles：撤回可以先于任何用量包到达
    children = rows(data["bundles"], "bundles", 0, MAX_MERGE_NODES)
    programs = []
    nested_withdrawn = []
    for child in children:
        child_programs, child_withdrawn = merge_node(
            child, 1, "bundles[]")
        programs.extend(child_programs)
        nested_withdrawn.extend(child_withdrawn)
    withdrawn = _withdraw_list(data["withdraw"], "withdraw")
    # 同一撤回 ID 在不同层级重复出现是幂等的，去重后统一应用
    seen_withdrawn = set(withdrawn)
    for pid in nested_withdrawn:
        if pid not in seen_withdrawn:
            seen_withdrawn.add(pid)
            withdrawn.append(pid)
    return programs, withdrawn
