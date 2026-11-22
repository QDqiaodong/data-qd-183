"""subtitle-playback-qc 输入校验：严格字段、时钟覆盖与日志记录结构。"""
from subtitle_conform.io import fields, integer, rows

MAX_CAPTURE_END_MS = 86400000
MAX_LAST_SEQ = 2000
MAX_CLOCK_ROWS = 20
MAX_BATCHES = 12
MAX_BATCH_RECORDS = 10000
MAX_PROGRAM_MS = 10**12


def validate_clock(value, capture_end_ms):
    """校验时钟表：1..20 段，半开相接，严密覆盖 [0, capture_end_ms]。"""
    rows(value, "clock", 1, MAX_CLOCK_ROWS)
    clock = []
    cursor = 0
    for row in value:
        fields(row, ("start_ms", "end_ms", "program_start_ms",
                     "rate_num", "rate_den"))
        start = integer(row["start_ms"], "clock.start_ms", 0, capture_end_ms)
        end = integer(row["end_ms"], "clock.end_ms", 0, capture_end_ms)
        if start >= end:
            raise ValueError("clock segment must have positive duration")
        if start != cursor:
            raise ValueError(
                "clock segments must cover the capture interval without gaps")
        program_start = integer(row["program_start_ms"],
                                "clock.program_start_ms", 0, MAX_PROGRAM_MS)
        rate_num = integer(row["rate_num"], "clock.rate_num", 0, 1000)
        rate_den = integer(row["rate_den"], "clock.rate_den", 1, 1000)
        clock.append({"start_ms": start, "end_ms": end,
                      "program_start_ms": program_start,
                      "rate_num": rate_num, "rate_den": rate_den})
        cursor = end
    if cursor != capture_end_ms:
        raise ValueError("clock must end exactly at capture_end_ms")
    return clock


def validate_batches(value):
    rows(value, "batches", 1, MAX_BATCHES)
    batches = []
    for batch in value:
        rows(batch, "batch", 0, MAX_BATCH_RECORDS)
        batches.append(batch)
    return batches


def validate_root(data):
    fields(data, ("conform", "capture_end_ms", "last_seq", "clock", "batches"))
    capture_end_ms = integer(data["capture_end_ms"], "capture_end_ms",
                             1, MAX_CAPTURE_END_MS)
    last_seq = integer(data["last_seq"], "last_seq", 1, MAX_LAST_SEQ)
    if not isinstance(data["conform"], dict):
        raise ValueError("conform must be an object")
    clock = validate_clock(data["clock"], capture_end_ms)
    batches = validate_batches(data["batches"])
    return {"conform": data["conform"], "capture_end_ms": capture_end_ms,
            "last_seq": last_seq, "clock": clock, "batches": batches}


def validate_record(row, last_seq, capture_end_ms, line_count):
    """校验单条日志记录，返回带规范化内容（用于重发去重）的记录。"""
    if not isinstance(row, dict):
        raise ValueError("log record must be an object")
    op = row.get("op")
    if op == "snapshot":
        fields(row, ("seq", "at_ms", "op", "active"))
    elif op in ("show", "hide"):
        fields(row, ("seq", "at_ms", "op", "cue"))
    else:
        raise ValueError(f"unknown log op: {op!r}")
    seq = integer(row["seq"], "seq", 1, last_seq)
    at_ms = integer(row["at_ms"], "at_ms", 0, capture_end_ms)
    if op == "snapshot":
        active = row["active"]
        if not isinstance(active, list) or len(active) > line_count:
            raise ValueError(
                f"snapshot active requires an array of at most {line_count} lines")
        seen = set()
        for item in active:
            line = integer(item, "active line", 1, line_count)
            if line in seen:
                raise ValueError("snapshot active contains duplicate line")
            seen.add(line)
        # 快照行号顺序不影响相同判断，规范化时排序
        return {"seq": seq, "at_ms": at_ms, "op": op, "active": frozenset(seen),
                "canonical": ("snapshot", at_ms, tuple(sorted(seen)))}
    cue = integer(row["cue"], "cue", 1, line_count)
    return {"seq": seq, "at_ms": at_ms, "op": op, "cue": cue,
            "canonical": (op, at_ms, cue)}
