"""重新剪辑后的逐次字幕审校迁移（carry-review 模式）。

输入给出旧版/新版两份普通对版输入、剪辑部提供的来源对应关系（links）
以及字幕组在旧剪辑实例上批准的改词/隐藏决定（edits）。以新版普通对版
产生的每个精确碎片为范围，只在关系区间、新碎片、同 cue 旧碎片三者
正长重叠处采信旧决定；正文已更正或候选结论不一致的区间标记重审并
阻断交付。可交付时按迁移后的实显正文重新计算质检与 SRT。
仅依赖标准库，不读取媒体。
"""
from fractions import Fraction

from .io import fields, integer, records
from .model import validate
from .render import render
from .timeline import conform

MAX_LINKS = 2000
MAX_EDITS = 2000


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _edit_text(text):
    """替换正文采用普通 cue 的正文规则。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError("edit text requires 1..2000 characters")
    if "\r" in text or any(ord(c) < 32 and c != "\n" for c in text):
        raise ValueError("edit text contains unsupported control characters")
    if "\n\n" in text or text.startswith("\n") or text.endswith("\n"):
        raise ValueError("edit text must not contain empty SRT blocks")


def _positive_interval(row):
    start = integer(row["start_ms"], "start_ms")
    end = integer(row["end_ms"], "end_ms")
    if start >= end:
        raise ValueError("source interval must have positive duration")
    return start, end


def _reject_overlap(pairs, what):
    """同一键下的区间可以相接，不能重叠。"""
    ordered = sorted(pairs)
    for (_a_start, a_end), (b_start, _b_end) in zip(ordered, ordered[1:]):
        if b_start < a_end:
            raise ValueError(f"{what} intervals overlap")


def _validate_links(value, previous_clips, current_clips):
    links = {}
    for row in records(value, "link", 0, MAX_LINKS).values():
        fields(row, ("id", "old_clip_id", "new_clip_id", "start_ms", "end_ms"))
        old_clip_id = row["old_clip_id"]
        new_clip_id = row["new_clip_id"]
        if not isinstance(old_clip_id, str) or old_clip_id not in previous_clips:
            raise ValueError(f"link {row['id']} references unknown old clip")
        if not isinstance(new_clip_id, str) or new_clip_id not in current_clips:
            raise ValueError(f"link {row['id']} references unknown new clip")
        old_clip = previous_clips[old_clip_id]
        new_clip = current_clips[new_clip_id]
        if old_clip["source_id"] != new_clip["source_id"]:
            raise ValueError(f"link {row['id']} requires both clips on the same source")
        start, end = _positive_interval(row)
        if start < old_clip["in_ms"] or end > old_clip["out_ms"] \
                or start < new_clip["in_ms"] or end > new_clip["out_ms"]:
            raise ValueError(f"link {row['id']} interval outside clip source use")
        links[row["id"]] = {"id": row["id"], "old_clip_id": old_clip_id,
                            "new_clip_id": new_clip_id,
                            "start_ms": start, "end_ms": end}
    by_pair = {}
    for link in links.values():
        by_pair.setdefault((link["old_clip_id"], link["new_clip_id"]), []).append(
            (link["start_ms"], link["end_ms"]))
    for pair, spans in by_pair.items():
        _reject_overlap(spans, f"links on clip pair {pair}")
    return links


def _validate_edits(value, previous_fragments):
    edits = {}
    for row in records(value, "edit", 0, MAX_EDITS).values():
        fields(row, ("id", "clip_id", "cue_id", "start_ms", "end_ms", "action"),
               optional=("text",))
        clip_id = row["clip_id"]
        cue_id = row["cue_id"]
        identity = (clip_id, cue_id)
        if not isinstance(clip_id, str) or not isinstance(cue_id, str) \
                or identity not in previous_fragments:
            raise ValueError(f"edit {row['id']} references unknown previous fragment")
        fragment = previous_fragments[identity]
        start, end = _positive_interval(row)
        if start < fragment["source_start_ms"] or end > fragment["source_end_ms"]:
            raise ValueError(f"edit {row['id']} interval outside fragment source range")
        action = row["action"]
        if action == "replace":
            if "text" not in row:
                raise ValueError(f"edit {row['id']} replace requires text")
            _edit_text(row["text"])
            text = row["text"]
        elif action == "hide":
            if "text" in row:
                raise ValueError(f"edit {row['id']} hide must not carry text")
            text = None
        else:
            raise ValueError(f"edit {row['id']} has unknown action")
        edits[row["id"]] = {"id": row["id"], "clip_id": clip_id, "cue_id": cue_id,
                            "start_ms": start, "end_ms": end,
                            "action": action, "text": text}
    by_fragment = {}
    for edit in edits.values():
        by_fragment.setdefault((edit["clip_id"], edit["cue_id"]), []).append(
            (edit["start_ms"], edit["end_ms"]))
    for identity, spans in by_fragment.items():
        _reject_overlap(spans, f"edits on fragment {identity}")
    return edits


# ---------------------------------------------------------------------------
# 区间工具（源时间轴上的并集与补集）
# ---------------------------------------------------------------------------

def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _subtract(span, covered):
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


# ---------------------------------------------------------------------------
# 逐段判定
# ---------------------------------------------------------------------------

def _judge_fragment(fragment, bases, edits_by_fragment, previous_cues, new_text):
    """把一个新精确碎片划分为判定一致的审校段。

    返回段列表 [(start, end, verdict, text, basis)]，basis 为按关系 ID
    排序的候选依据元组 ((link_id, old_clip_id, edit_id, claim), ...)，
    claim 为 ("visible", text)、("hidden",) 或 ("stale",)。
    """
    source_start = fragment["source_start_ms"]
    source_end = fragment["source_end_ms"]
    points = {source_start, source_end}
    for _link, _old, start, end in bases:
        points.add(start)
        points.add(end)
    involved = {(old["clip_id"], old["cue_id"]) for _l, old, _s, _e in bases}
    for identity in involved:
        for edit in edits_by_fragment.get(identity, ()):
            points.add(min(max(edit["start_ms"], source_start), source_end))
            points.add(min(max(edit["end_ms"], source_start), source_end))
    ordered = sorted(points)

    elementary = []
    for start, end in zip(ordered, ordered[1:]):
        candidates = []
        stale = False
        claims = []
        for link, old_fragment, basis_start, basis_end in bases:
            if not (basis_start <= start and basis_end >= end):
                continue
            identity = (old_fragment["clip_id"], old_fragment["cue_id"])
            edit = next(
                (row for row in edits_by_fragment.get(identity, ())
                 if row["start_ms"] <= start and row["end_ms"] >= end),
                None)
            if edit is None:
                claim = ("visible", new_text)
                edit_id = None
            elif previous_cues[fragment["cue_id"]]["text"] != new_text:
                # 原始正文已改变：这条依据失效，整段必须重审
                claim = ("stale",)
                edit_id = edit["id"]
                stale = True
            elif edit["action"] == "hide":
                claim = ("hidden",)
                edit_id = edit["id"]
            else:
                claim = ("visible", edit["text"])
                edit_id = edit["id"]
            candidates.append((link["id"], link["old_clip_id"], edit_id, claim))
            claims.append(claim)
        candidates.sort(key=lambda row: row[0])
        if stale:
            verdict, text = "stale_review", None
        elif not candidates:
            verdict, text = "visible", new_text
        elif all(claim == claims[0] for claim in claims):
            verdict = claims[0][0]
            text = claims[0][1] if verdict == "visible" else None
        else:
            verdict, text = "conflict", None
        elementary.append((start, end, verdict, text, tuple(candidates)))

    merged = []
    for start, end, verdict, text, candidates in elementary:
        if merged and merged[-1][2] == verdict and merged[-1][3] == text \
                and merged[-1][4] == candidates and merged[-1][1] == start:
            merged[-1][1] = end
        else:
            merged.append([start, end, verdict, text, candidates])
    return [tuple(row) for row in merged]


def _basis_rows(candidates):
    return [{"link_id": link_id, "old_clip_id": old_clip_id, "edit_id": edit_id,
             "claim": claim[0],
             "text": claim[1] if claim[0] == "visible" else None}
            for link_id, old_clip_id, edit_id, claim in candidates]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_carry_review(data):
    fields(data, ("previous", "current", "links", "edits"))
    if not isinstance(data["previous"], dict):
        raise ValueError("previous must be an object")
    if not isinstance(data["current"], dict):
        raise ValueError("current must be an object")
    _prev_sources, previous_clips, previous_cues = validate(data["previous"])
    _curr_sources, current_clips, current_cues = validate(data["current"])
    previous_fragments, _prev_spans, _prev_duration, _prev_omitted = conform(
        previous_clips, previous_cues)
    current_fragments, _curr_spans, current_duration, _curr_omitted = conform(
        current_clips, current_cues)
    previous_by_identity = {
        (row["clip_id"], row["cue_id"]): row for row in previous_fragments}
    links = _validate_links(data["links"], previous_clips, current_clips)
    edits = _validate_edits(data["edits"], previous_by_identity)

    links_by_new_clip = {}
    for link in links.values():
        links_by_new_clip.setdefault(link["new_clip_id"], []).append(link)
    edits_by_fragment = {}
    for edit in edits.values():
        edits_by_fragment.setdefault((edit["clip_id"], edit["cue_id"]), []).append(edit)

    adopted = {edit_id: [] for edit_id in edits}
    review_segments = []
    delivery_lines = []

    for fragment in current_fragments:
        clip_id = fragment["clip_id"]
        cue_id = fragment["cue_id"]
        source_start = fragment["source_start_ms"]
        clip = current_clips[clip_id]
        scale = Fraction(clip["speed_den"], clip["speed_num"])
        new_text = current_cues[cue_id]["text"]

        # 一条关系只在它的区间、新版碎片、对应旧剪辑上同素材同 cue 的
        # 旧碎片三者正长重叠处提供依据；其余情形（换 ID、换素材、无旧
        # 碎片、仅端点接触）一律不提供依据。
        bases = []
        for link in links_by_new_clip.get(clip_id, ()):
            old_fragment = previous_by_identity.get((link["old_clip_id"], cue_id))
            if old_fragment is None:
                continue
            start = max(link["start_ms"], source_start,
                        old_fragment["source_start_ms"])
            end = min(link["end_ms"], fragment["source_end_ms"],
                      old_fragment["source_end_ms"])
            if start < end:
                bases.append((link, old_fragment, start, end))

        segments = _judge_fragment(fragment, bases, edits_by_fragment,
                                   previous_cues, new_text)

        def program(at, _fragment=fragment, _scale=scale, _start=source_start):
            return _fragment["start"] + (at - _start) * _scale

        run = None  # [source_start, source_end, text] 交付行累积
        for start, end, verdict, text, candidates in segments:
            if verdict in ("visible", "hidden"):
                for _link_id, _old_clip_id, edit_id, _claim in candidates:
                    if edit_id is not None:
                        adopted[edit_id].append((start, end))
            review_segments.append({
                "fragment_id": [clip_id, cue_id],
                "source_start_ms": start,
                "source_end_ms": end,
                "start_ms": str(program(start)),
                "end_ms": str(program(end)),
                "verdict": verdict,
                "text": text,
                "basis": _basis_rows(candidates),
            })
            # 同一剪辑实例同一 cue 内，相邻且实显正文相同的可见段并为
            # 一条交付字幕；隐藏或待重审段打断合并。
            if verdict == "visible" and run is not None \
                    and run[1] == start and run[2] == text:
                run[1] = end
            else:
                if run is not None:
                    delivery_lines.append({
                        "line_id": [clip_id, cue_id, run[0], run[1]],
                        "source_start_ms": run[0],
                        "source_end_ms": run[1],
                        "start_ms": str(program(run[0])),
                        "end_ms": str(program(run[1])),
                        "text": run[2],
                    })
                run = [start, end, text] if verdict == "visible" else None
        if run is not None:
            delivery_lines.append({
                "line_id": [clip_id, cue_id, run[0], run[1]],
                "source_start_ms": run[0],
                "source_end_ms": run[1],
                "start_ms": str(program(run[0])),
                "end_ms": str(program(run[1])),
                "text": run[2],
            })

    review_segments.sort(key=lambda row: (
        Fraction(row["start_ms"]), Fraction(row["end_ms"]),
        row["fragment_id"][0], row["fragment_id"][1],
        row["source_start_ms"], row["source_end_ms"]))
    delivery_lines.sort(key=lambda row: (
        Fraction(row["start_ms"]), Fraction(row["end_ms"]),
        row["line_id"][0], row["line_id"][1],
        row["source_start_ms"], row["source_end_ms"]))

    edit_dispositions = []
    for edit_id in sorted(edits):
        edit = edits[edit_id]
        taken = _merge(adopted[edit_id])
        edit_dispositions.append({
            "edit_id": edit_id,
            "old_fragment_id": [edit["clip_id"], edit["cue_id"]],
            "source_start_ms": edit["start_ms"],
            "source_end_ms": edit["end_ms"],
            "adopted": [{"start_ms": start, "end_ms": end}
                        for start, end in taken],
            "not_adopted": [{"start_ms": start, "end_ms": end}
                            for start, end in _subtract(
                                (edit["start_ms"], edit["end_ms"]), taken)],
        })

    blocked = any(row["verdict"] in ("stale_review", "conflict")
                  for row in review_segments)
    status = "blocked" if blocked else "ready"

    quality = None
    srt_text = None
    delivered = None
    if status == "ready":
        delivered = delivery_lines
        # 以交付行重新计算普通模式质检与 SRT，阈值取 current
        render_rows = [{"fragment_id": line["line_id"],
                        "start": Fraction(line["start_ms"]),
                        "end": Fraction(line["end_ms"]),
                        "text": line["text"]}
                       for line in delivery_lines]
        output = render(render_rows, data["current"]["min_duration_ms"],
                        data["current"]["max_chars_per_second"])
        quality = {
            "lines": [{"line_id": row["fragment_id"],
                       "start_ms": row["start_ms"],
                       "end_ms": row["end_ms"],
                       "text": row["text"],
                       "chars_per_second": row["chars_per_second"],
                       "issues": row["issues"],
                       "srt_start_ms": row["srt_start_ms"],
                       "srt_end_ms": row["srt_end_ms"]}
                      for row in output["fragments"]],
            "overlap_count": output["overlap_count"],
            "overlap_examples": output["overlap_examples"],
            "warning_lines": output["warning_fragments"],
            "srt_cues": output["srt_cues"],
            "srt_dropped_line_ids": output["srt_dropped_fragments"],
        }
        srt_text = output["srt_text"]

    stale_count = sum(1 for row in review_segments
                      if row["verdict"] == "stale_review")
    conflict_count = sum(1 for row in review_segments
                         if row["verdict"] == "conflict")
    return {
        "schema_version": 3,
        "mode": "carry-review",
        "status": status,
        "output_duration_ms": str(current_duration),
        "review_segments": review_segments,
        "edit_dispositions": edit_dispositions,
        "delivery_lines": delivered,
        "quality": quality,
        "srt_text": srt_text,
        "summary": {
            "status": status,
            "review_segments": len(review_segments),
            "stale_review_segments": stale_count,
            "conflict_segments": conflict_count,
            "edit_decisions": len(edits),
            "edits_carried": sum(1 for row in edit_dispositions
                                 if row["adopted"]),
            "delivery_lines": len(delivered) if delivered is not None else None,
            "srt_cues": quality["srt_cues"] if quality is not None else None,
        },
    }
