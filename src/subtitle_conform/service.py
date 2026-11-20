from .model import validate
from .timeline import conform
from .render import render


def run(data):
    _, clips, cues = validate(data)
    fragments, spans, duration, omitted = conform(clips, cues)
    output = render(fragments, data["min_duration_ms"], data["max_chars_per_second"])
    return {"schema_version": 1, "output_duration_ms": str(duration), "clips": spans,
            "omitted_cue_ids": omitted, **output,
            "summary": {"clips": len(clips), "output_fragments": len(fragments),
                        "srt_cues": output["srt_cues"], "overlap_pairs": output["overlap_count"],
                        "warning_fragments": output["warning_fragments"]}}
