from .io import fields, integer, label, records


def interval(row, duration, start, end):
    a = integer(row[start], start, 0, duration)
    b = integer(row[end], end, 0, duration)
    if a >= b:
        raise ValueError("source interval must have positive duration")
    return a, b


def validate(data):
    fields(data, ("sources", "clips", "cues", "min_duration_ms", "max_chars_per_second"))
    sources = records(data["sources"], "source", 1, 50)
    for source in sources.values():
        fields(source, ("id", "duration_ms"))
        integer(source["duration_ms"], "duration_ms", 1, 86400000)
    clips = records(data["clips"], "clip", 1, 100)
    for clip in clips.values():
        fields(clip, ("id", "source_id", "in_ms", "out_ms", "speed_num", "speed_den"))
        if not isinstance(clip["source_id"], str) or clip["source_id"] not in sources:
            raise ValueError("unknown clip source")
        interval(clip, sources[clip["source_id"]]["duration_ms"], "in_ms", "out_ms")
        integer(clip["speed_num"], "speed_num", 1, 1000)
        integer(clip["speed_den"], "speed_den", 1, 1000)
    cues = records(data["cues"], "cue", 0, 500)
    for cue in cues.values():
        fields(cue, ("id", "source_id", "start_ms", "end_ms", "text"))
        if not isinstance(cue["source_id"], str) or cue["source_id"] not in sources:
            raise ValueError("unknown cue source")
        interval(cue, sources[cue["source_id"]]["duration_ms"], "start_ms", "end_ms")
        text = cue["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("cue text requires 1..2000 characters")
        if "\r" in text or any(ord(c) < 32 and c != "\n" for c in text):
            raise ValueError("cue text contains unsupported control characters")
        if "\n\n" in text or text.startswith("\n") or text.endswith("\n"):
            raise ValueError("cue text must not contain empty SRT blocks")
    integer(data["min_duration_ms"], "min_duration_ms", 0, 100000)
    integer(data["max_chars_per_second"], "max_chars_per_second", 1, 1000)
    return sources, clips, cues
