from fractions import Fraction


def conform(clips, cues):
    target_cursor = Fraction(0)
    result, spans = [], []
    used = set()
    for clip_id, clip in clips.items():  # input clip order is the EDL order
        scale = Fraction(clip["speed_den"], clip["speed_num"])
        duration = (clip["out_ms"] - clip["in_ms"]) * scale
        spans.append({"clip_id": clip_id, "output_start_ms": str(target_cursor),
                      "output_end_ms": str(target_cursor + duration)})
        for cue_id, cue in sorted(cues.items()):
            if cue["source_id"] != clip["source_id"]:
                continue
            start = max(cue["start_ms"], clip["in_ms"])
            end = min(cue["end_ms"], clip["out_ms"])
            if start >= end:
                continue
            if len(result) >= 2000:
                raise ValueError("conform exceeds 2000 output fragments")
            used.add(cue_id)
            result.append({"cue_id": cue_id, "clip_id": clip_id,
                           "fragment_id": [clip_id, cue_id],
                           "source_start_ms": start, "source_end_ms": end,
                           "start": target_cursor + (start - clip["in_ms"]) * scale,
                           "end": target_cursor + (end - clip["in_ms"]) * scale,
                           "text": cue["text"]})
        target_cursor += duration
    result.sort(key=lambda row: (row["start"], row["end"], row["clip_id"], row["cue_id"]))
    return result, spans, target_cursor, sorted(set(cues) - used)
