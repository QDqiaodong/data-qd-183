from fractions import Fraction


def nearest_ms(value):
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def timestamp(ms):
    hours, remainder = divmod(ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{fraction:03d}"


def render(rows, minimum_ms, max_cps):
    fragments, srt, dropped = [], [], []
    overlap_count = 0
    overlap_examples = []
    active = []
    warnings = 0
    for row in rows:
        duration = row["end"] - row["start"]
        characters = sum(not c.isspace() for c in row["text"])
        cps = Fraction(characters * 1000, 1) / duration
        issues = []
        if duration < minimum_ms:
            issues.append("short_duration")
        if cps > max_cps:
            issues.append("reading_speed")
        active = [previous for previous in active if previous["end"] > row["start"]]
        for previous in active:
            overlap_count += 1
            if len(overlap_examples) < 100:
                overlap_examples.append([previous["fragment_id"], row["fragment_id"]])
        active.append(row)
        a, b = nearest_ms(row["start"]), nearest_ms(row["end"])
        warnings += bool(issues)
        fragments.append({key: value for key, value in row.items() if key not in ("start", "end")} |
                         {"start_ms": str(row["start"]), "end_ms": str(row["end"]),
                          "chars_per_second": str(cps), "issues": issues,
                          "srt_start_ms": a, "srt_end_ms": b})
        if a == b:
            dropped.append(row["fragment_id"])
        else:
            srt.append(f"{len(srt) + 1}\n{timestamp(a)} --> {timestamp(b)}\n{row['text']}\n")
    return {"fragments": fragments, "srt_text": "\n".join(srt),
            "srt_dropped_fragments": dropped, "overlap_count": overlap_count,
            "overlap_examples": overlap_examples, "warning_fragments": warnings,
            "srt_cues": len(srt)}
