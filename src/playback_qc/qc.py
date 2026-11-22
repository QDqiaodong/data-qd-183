"""离线复盘播放器字幕质检引擎。

把播放器采集时钟与显示日志同普通模式实际交付的 SRT 对账：按批到达顺序
逐步重建可信显示状态，在已知区间内比较应显示与实显示的行号集合，输出
每批的告警更正与三项时长差值。补齐缺包后按同一套规则从头重判，不把
迟到的动作简单追加到当前屏幕状态上。仅依赖标准库，不读取媒体。
"""
from fractions import Fraction

from subtitle_conform.model import validate as validate_conform
from subtitle_conform.render import render
from subtitle_conform.timeline import conform

from .model import validate_record, validate_root

MEASURE_KEYS = ("unknown", "missing", "extra")


# ---------------------------------------------------------------------------
# 基准字幕：普通对版 + 普通 SRT 行号
# ---------------------------------------------------------------------------

def _program_and_lines(conform_input):
    """返回 (精确节目时长, 交付行列表)。

    行号即普通模式 SRT 中实际交付的条目号（从 1 开始）；舍入后被丢弃的
    碎片没有行号。每行保留普通对版碎片身份与 SRT 整数起止时间。
    """
    _sources, clips, cues = validate_conform(conform_input)
    fragments, _spans, duration, _omitted = conform(clips, cues)
    output = render(fragments, conform_input["min_duration_ms"],
                    conform_input["max_chars_per_second"])
    dropped = {tuple(item) for item in output["srt_dropped_fragments"]}
    lines = []
    for row in output["fragments"]:
        if tuple(row["fragment_id"]) in dropped:
            continue
        lines.append({"fragment_id": row["fragment_id"],
                      "srt_start_ms": row["srt_start_ms"],
                      "srt_end_ms": row["srt_end_ms"]})
    return duration, lines


def _enrich_clock(clock, duration):
    """校验每段节目位置落在精确节目范围内，并预计算精确倍速。"""
    segments = []
    for row in clock:
        rate = Fraction(row["rate_num"], row["rate_den"])
        start_pos = Fraction(row["program_start_ms"])
        end_pos = start_pos + (row["end_ms"] - row["start_ms"]) * rate
        if not (Fraction(0) <= start_pos <= duration) \
                or not (Fraction(0) <= end_pos <= duration):
            raise ValueError(
                "clock program position outside conformed program range")
        segments.append({**row, "rate": rate})
    return segments


# ---------------------------------------------------------------------------
# 应显示集合：节目位置 -> 行号集合，再换算回采集时间
# ---------------------------------------------------------------------------

def _lines_at(bounds, position, duration):
    """节目位置 position 上应显示的行号集合；节目末端没有应显示字幕。"""
    if not position < duration:
        return frozenset()
    return frozenset(index for index, (start, end) in enumerate(bounds, 1)
                     if start <= position < end)


def _expected_intervals(segments, bounds, duration):
    """把采集区间划分为应显示集合恒定的正长半开区间。

    SRT 整数区间按半开使用；从节目位置换回采集时间用精确有理数，
    不再舍入。暂停段按所在位置取应显示集合；段间回拖/快跳不为跳过
    的位置记时。
    """
    intervals = []
    for segment in segments:
        start = Fraction(segment["start_ms"])
        end = Fraction(segment["end_ms"])
        p0 = Fraction(segment["program_start_ms"])
        rate = segment["rate"]
        if rate == 0:
            intervals.append((start, end, _lines_at(bounds, p0, duration)))
            continue
        p1 = p0 + (end - start) * rate
        on_events = {}
        off_events = {}
        current = _lines_at(bounds, p0, duration)
        for index, (line_start, line_end) in enumerate(bounds, 1):
            low, high = Fraction(line_start), Fraction(line_end)
            if high <= p0 or low >= p1:
                continue
            if low > p0:
                on_events.setdefault(start + (low - p0) / rate, set()).add(index)
            if p0 < high < p1:
                off_events.setdefault(start + (high - p0) / rate, set()).add(index)
        cursor = start
        for point in sorted(set(on_events) | set(off_events)):
            if cursor < point:
                intervals.append((cursor, point, current))
            current = frozenset((current - off_events.get(point, set()))
                                | on_events.get(point, set()))
            cursor = point
        intervals.append((cursor, end, current))
    return intervals


# ---------------------------------------------------------------------------
# 可信显示状态：按已收记录重建
# ---------------------------------------------------------------------------

def _trust_intervals(ordered, last_seq, capture_end_ms):
    """把采集区间划分为 (是否可信, 实显示集合) 恒定的正长半开区间。

    ordered 为按序号排列的已收记录。首次完整快照前不可信；序号缺口从
    前一条记录的时刻起破坏信任，只有完整快照能重建；最后已收序号小于
    last_seq 时从最后记录时刻起不可信。同一时刻的动作按序号应用，不
    产生正长中间状态。
    """
    events = []  # (at_ms, seq, rank, record)；rank 1 为缺口失信事件
    for record in ordered:
        events.append((record["at_ms"], record["seq"], 0, record))
    for previous, nxt in zip(ordered, ordered[1:]):
        if nxt["seq"] > previous["seq"] + 1:
            events.append((previous["at_ms"], previous["seq"], 1, None))
    if ordered and ordered[-1]["seq"] < last_seq:
        events.append((ordered[-1]["at_ms"], ordered[-1]["seq"], 1, None))
    events.sort(key=lambda item: (item[0], item[1], item[2]))

    intervals = []
    trusted = False
    state = frozenset()
    cursor = 0
    for at_ms, _seq, _rank, record in events:
        if cursor < at_ms:
            intervals.append((Fraction(cursor), Fraction(at_ms), trusted, state))
        cursor = at_ms
        if record is None:
            trusted = False
            state = frozenset()
        elif record["op"] == "snapshot":
            trusted = True
            state = record["active"]
        elif trusted:
            if record["op"] == "show":
                state = state | {record["cue"]}
            else:
                state = state - {record["cue"]}
    if cursor < capture_end_ms:
        intervals.append((Fraction(cursor), Fraction(capture_end_ms),
                          trusted, state))
    return intervals


# ---------------------------------------------------------------------------
# 对账：告警行与三项计量
# ---------------------------------------------------------------------------

def _reconcile(trust, expected, capture_end_ms):
    """合并可信状态与应显示集合，输出规范告警行与三项时长。

    告警行：("unknown",) 或 ("mismatch", missing, extra)，附起止；相邻
    同类同内容合并，完全匹配不产生告警，只输出正长区间。计量未知总
    时长、有任一漏显的已知时长、有任一多显的已知时长。
    """
    points = set()
    for start, end, *_rest in trust:
        points.add(start)
        points.add(end)
    for start, end, _wanted in expected:
        points.add(start)
        points.add(end)
    ordered = sorted(points)

    trust_index = 0
    expected_index = 0
    rows = []
    totals = {key: Fraction(0) for key in MEASURE_KEYS}
    for begin, end in zip(ordered, ordered[1:]):
        while trust[trust_index][1] <= begin:
            trust_index += 1
        while expected[expected_index][1] <= begin:
            expected_index += 1
        _t_start, _t_end, trusted, state = trust[trust_index]
        wanted = expected[expected_index][2]
        if not trusted:
            content = ("unknown",)
            totals["unknown"] += end - begin
        else:
            missing = tuple(sorted(wanted - state))
            extra = tuple(sorted(state - wanted))
            if not missing and not extra:
                content = None
            else:
                content = ("mismatch", missing, extra)
                if missing:
                    totals["missing"] += end - begin
                if extra:
                    totals["extra"] += end - begin
        if content is None:
            continue
        if rows and rows[-1][0] == content and rows[-1][2] == begin:
            rows[-1] = (content, rows[-1][1], end)
        else:
            rows.append((content, begin, end))
    return rows, totals


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------

def _row_json(row):
    content, start, end = row
    result = {"kind": content[0], "start_ms": str(start), "end_ms": str(end)}
    if content[0] == "mismatch":
        result["missing"] = list(content[1])
        result["extra"] = list(content[2])
    return result


def _row_sort_key(row):
    content, start, end = row
    detail = (content[1], content[2]) if content[0] == "mismatch" else ((), ())
    return (start, end, content[0], detail)


def _measurements_json(totals):
    return {"unknown_duration_ms": str(totals["unknown"]),
            "missing_duration_ms": str(totals["missing"]),
            "extra_duration_ms": str(totals["extra"])}


def _delta_json(previous, current):
    return {"unknown_duration_ms": str(current["unknown"] - previous["unknown"]),
            "missing_duration_ms": str(current["missing"] - previous["missing"]),
            "extra_duration_ms": str(current["extra"] - previous["extra"])}


def _diff(previous_rows, current_rows):
    """相对紧邻前一批的更正：完全相同的行不撤回也不加入。"""
    previous_set = set(previous_rows)
    current_set = set(current_rows)
    withdraw = sorted((row for row in previous_rows if row not in current_set),
                      key=_row_sort_key)
    add = sorted((row for row in current_rows if row not in previous_set),
                 key=_row_sort_key)
    return withdraw, add


def _check_sequence_rules(ordered):
    for previous, nxt in zip(ordered, ordered[1:]):
        if nxt["at_ms"] < previous["at_ms"]:
            raise ValueError("log at_ms must not decrease with seq")
    if ordered and ordered[0]["seq"] == 1:
        first = ordered[0]
        if first["op"] != "snapshot" or first["at_ms"] != 0:
            raise ValueError("seq 1 must be a full snapshot at time 0")


def run_playback_qc(data):
    spec = validate_root(data)
    capture_end_ms = spec["capture_end_ms"]
    last_seq = spec["last_seq"]
    duration, lines = _program_and_lines(spec["conform"])
    segments = _enrich_clock(spec["clock"], duration)
    bounds = [(line["srt_start_ms"], line["srt_end_ms"]) for line in lines]
    expected = _expected_intervals(segments, bounds, duration)

    line_map = [{"line": index,
                 "fragment_id": line["fragment_id"],
                 "srt_start_ms": line["srt_start_ms"],
                 "srt_end_ms": line["srt_end_ms"]}
                for index, line in enumerate(lines, 1)]

    # 初始视图：整段未知，未知时长等于采集时长，另外两项为零
    previous_rows = [(("unknown",), Fraction(0), Fraction(capture_end_ms))]
    previous_totals = {"unknown": Fraction(capture_end_ms),
                       "missing": Fraction(0), "extra": Fraction(0)}
    initial_view = {
        "alerts": [_row_json(previous_rows[0])],
        "measurements": _measurements_json(previous_totals),
    }

    received = {}
    batch_reports = []
    for index, batch in enumerate(spec["batches"], 1):
        new_records = 0
        for raw in batch:
            record = validate_record(raw, last_seq, capture_end_ms, len(lines))
            existing = received.get(record["seq"])
            if existing is not None:
                if existing["canonical"] != record["canonical"]:
                    raise ValueError(
                        f"seq {record['seq']} has conflicting retransmission")
                continue
            received[record["seq"]] = record
            new_records += 1
        ordered = [received[seq] for seq in sorted(received)]
        _check_sequence_rules(ordered)

        trust = _trust_intervals(ordered, last_seq, capture_end_ms)
        rows, totals = _reconcile(trust, expected, capture_end_ms)
        withdraw, add = _diff(previous_rows, rows)
        batch_reports.append({
            "batch": index,
            "new_records": new_records,
            "received_records": len(received),
            "corrections": {
                "withdraw": [_row_json(row) for row in withdraw],
                "add": [_row_json(row) for row in add],
                "delta": _delta_json(previous_totals, totals),
            },
            "alerts": [_row_json(row) for row in rows],
            "measurements": _measurements_json(totals),
        })
        previous_rows, previous_totals = rows, totals

    final_alerts = [_row_json(row) for row in previous_rows]
    final_measurements = _measurements_json(previous_totals)
    return {
        "schema_version": 1,
        "mode": "subtitle-playback-qc",
        "capture_duration_ms": str(Fraction(capture_end_ms)),
        "program_duration_ms": str(duration),
        "last_seq": last_seq,
        "line_map": line_map,
        "initial_view": initial_view,
        "batches": batch_reports,
        "alerts": final_alerts,
        "measurements": final_measurements,
        "summary": {
            "batches": len(batch_reports),
            "received_records": len(received),
            "alert_rows": len(final_alerts),
            **final_measurements,
        },
    }
