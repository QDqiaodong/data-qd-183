"""cut-clearance 输入校验：严格字段、唯一引用与 UTC RFC 3339 时间。"""
import calendar
import re
from datetime import datetime, timezone
from fractions import Fraction

from subtitle_conform.io import fields, integer, label, records

MAX_SOURCES = 50
MAX_CLIPS = 100
MAX_DELIVERIES = 100
MAX_GRANTS = 1000
MAX_EXCEPTIONS = 1000
MAX_SET_ITEMS = 100

_RFC3339_Z = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$")


def parse_time(value, name):
    """解析带 Z 的 UTC RFC 3339 时间，返回自 epoch 的精确 Fraction 秒。"""
    if not isinstance(value, str):
        raise ValueError(f"{name} requires an RFC 3339 UTC string")
    match = _RFC3339_Z.match(value)
    if not match:
        raise ValueError(f"{name} requires RFC 3339 UTC form ending in Z")
    year, month, day, hour, minute, second, fraction = match.groups()
    # datetime 负责校验历法与时分秒范围（拒绝闰年以外的非法日期、闰秒等）
    moment = datetime(int(year), int(month), int(day), int(hour),
                      int(minute), int(second), tzinfo=timezone.utc)
    result = Fraction(calendar.timegm(moment.timetuple()))
    if fraction:
        result += Fraction(int(fraction), 10 ** len(fraction))
    return result


def string_set(value, name):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SET_ITEMS:
        raise ValueError(f"{name} requires a nonempty array of <= {MAX_SET_ITEMS} strings")
    result = []
    seen = set()
    for item in value:
        label(item, f"{name}[]")
        if item in seen:
            raise ValueError(f"{name} contains duplicate item: {item}")
        seen.add(item)
        result.append(item)
    return result


def _interval(row, duration, start, end):
    a = integer(row[start], start, 0, duration)
    b = integer(row[end], end, 0, duration)
    if a >= b:
        raise ValueError(f"{start}/{end} must define a positive-length interval")
    return a, b


def validate(data):
    fields(data, ("sources", "clips", "deliveries", "grants", "exceptions"))

    source_rows = records(data["sources"], "source", 1, MAX_SOURCES)
    sources = {}
    for sid, row in source_rows.items():
        fields(row, ("id", "duration_ms"))
        sources[sid] = integer(row["duration_ms"], "duration_ms", 1, 86_400_000)

    clip_rows = records(data["clips"], "clip", 1, MAX_CLIPS)
    clips = {}
    clip_order = []
    for cid, row in clip_rows.items():
        fields(row, ("id", "source_id", "in_ms", "out_ms",
                     "speed_num", "speed_den"))
        source_id = row["source_id"]
        if not isinstance(source_id, str) or source_id not in sources:
            raise ValueError(f"clip {cid} references unknown source")
        in_ms, out_ms = _interval(row, sources[source_id], "in_ms", "out_ms")
        speed_num = integer(row["speed_num"], "speed_num", 1, 1000)
        speed_den = integer(row["speed_den"], "speed_den", 1, 1000)
        clips[cid] = {"id": cid, "source_id": source_id, "in_ms": in_ms,
                      "out_ms": out_ms, "speed_num": speed_num,
                      "speed_den": speed_den}
        clip_order.append(cid)

    delivery_rows = records(data["deliveries"], "delivery", 1, MAX_DELIVERIES)
    deliveries = {}
    delivery_order = []
    for did, row in delivery_rows.items():
        fields(row, ("id", "channel", "territory", "release_at"))
        label(row["channel"], "channel")
        label(row["territory"], "territory")
        release_at = parse_time(row["release_at"], "release_at")
        deliveries[did] = {"id": did, "channel": row["channel"],
                           "territory": row["territory"],
                           "release_at": release_at,
                           "release_at_text": row["release_at"]}
        delivery_order.append(did)

    grant_rows = records(data["grants"], "grant", 0, MAX_GRANTS)
    grants = []
    for gid, row in grant_rows.items():
        fields(row, ("id", "source_id", "start_ms", "end_ms", "channels",
                     "territories", "valid_from", "valid_until"))
        source_id = row["source_id"]
        if not isinstance(source_id, str) or source_id not in sources:
            raise ValueError(f"grant {gid} references unknown source")
        start_ms, end_ms = _interval(row, sources[source_id],
                                     "start_ms", "end_ms")
        channels = string_set(row["channels"], "channels")
        territories = string_set(row["territories"], "territories")
        valid_from = parse_time(row["valid_from"], "valid_from")
        if row["valid_until"] is None:
            valid_until = None
        else:
            valid_until = parse_time(row["valid_until"], "valid_until")
            if not valid_from < valid_until:
                raise ValueError("grant validity period must have positive length")
        grants.append({"id": gid, "source_id": source_id,
                       "start_ms": start_ms, "end_ms": end_ms,
                       "channels": channels, "territories": territories,
                       "valid_from": valid_from, "valid_until": valid_until})

    exception_rows = records(data["exceptions"], "exception", 0, MAX_EXCEPTIONS)
    exceptions = []
    for eid, row in exception_rows.items():
        fields(row, ("id", "delivery_id", "clip_id", "start_ms", "end_ms",
                     "approved_by", "approved_at"))
        delivery_id = row["delivery_id"]
        if not isinstance(delivery_id, str) or delivery_id not in deliveries:
            raise ValueError(f"exception {eid} references unknown delivery")
        clip_id = row["clip_id"]
        if not isinstance(clip_id, str) or clip_id not in clips:
            raise ValueError(f"exception {eid} references unknown clip")
        label(row["approved_by"], "approved_by")
        clip = clips[clip_id]
        start_ms, end_ms = _interval(
            row, sources[clip["source_id"]], "start_ms", "end_ms")
        if start_ms < clip["in_ms"] or end_ms > clip["out_ms"]:
            raise ValueError(
                f"exception {eid} range must stay within clip {clip_id} source use")
        approved_at = parse_time(row["approved_at"], "approved_at")
        if approved_at > deliveries[delivery_id]["release_at"]:
            raise ValueError(
                f"exception {eid} approved_at must not be later than release_at")
        exceptions.append({"id": eid, "delivery_id": delivery_id,
                           "clip_id": clip_id, "start_ms": start_ms,
                           "end_ms": end_ms, "approved_by": row["approved_by"],
                           "approved_at": approved_at})

    return {"sources": sources, "clips": clips, "clip_order": clip_order,
            "deliveries": deliveries, "delivery_order": delivery_order,
            "grants": grants, "exceptions": exceptions}
