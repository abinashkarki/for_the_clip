"""Editorial clip selection from a timestamped word transcript.

The remote selector only proposes transcript segment IDs. All timestamps,
word IDs, duration checks, overlap checks, and final clip IDs are assigned here.
"""

from __future__ import annotations

import math
import json
import re
from collections import Counter
from typing import Any

import httpx


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_PAUSE_BOUNDARY = 0.8
_WINDOW_WORDS = 1600
_STOP_WORDS = frozenset(
    "a an and are as at be been but by for from had has have he her hers him his i if in into is it its of on or our she that the their them then there they this to was we were what when where which who will with would you your".split()
)


class _InvalidProviderResponse(ValueError):
    def __init__(self, message: str, raw: Any):
        super().__init__(message)
        self.raw = raw


def _word_id(word: dict[str, Any]) -> int:
    value = word.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Each word must have an integer id.")
    return value


def _word_times(word: dict[str, Any]) -> tuple[float, float]:
    try:
        start, end = float(word["start"]), float(word["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Each word must have numeric start and end times.") from exc
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("Word times must be finite, non-negative, and end after start.")
    return start, end


def _validated_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(words, list):
        raise ValueError("words must be a list.")
    output = []
    prior_start = -1.0
    ids: set[int] = set()
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            raise ValueError("Each word must be an object with text, id, start, and end.")
        word_id = _word_id(word)
        start, end = _word_times(word)
        if word_id in ids:
            raise ValueError(f"Word IDs must be unique; duplicate ID {word_id}.")
        if start < prior_start:
            raise ValueError("Words must be ordered by start time.")
        ids.add(word_id)
        prior_start = start
        output.append({**word, "id": word_id, "start": start, "end": end})
    return output


def build_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group words at sentence endings and meaningful pauses.

    Segments remain small enough to be useful as model-selectable units. Long
    unpunctuated passages are split after roughly 18 seconds.
    """
    checked = _validated_words(words)
    if not checked:
        return []
    segments: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []

    def flush() -> None:
        if not group:
            return
        segments.append({
            "id": len(segments),
            "text": " ".join(w["text"].strip() for w in group).strip(),
            "start": group[0]["start"],
            "end": group[-1]["end"],
            "start_word_id": group[0]["id"],
            "end_word_id": group[-1]["id"],
        })
        group.clear()

    for word in checked:
        if group:
            gap = word["start"] - group[-1]["end"]
            elapsed = group[-1]["end"] - group[0]["start"]
            if gap >= _PAUSE_BOUNDARY or elapsed >= 18.0:
                flush()
        group.append(word)
        if re.search(r"[.!?][\"’')\]]*$", word["text"].strip()):
            flush()
    flush()
    return segments


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w'-]+", text.casefold()) if len(t) > 2 and t not in _STOP_WORDS]


def _windowed_segments(
    segments: list[dict[str, Any]], max_duration: float = 60.0, max_words: int = _WINDOW_WORDS,
) -> list[list[dict[str, Any]]]:
    """Build bounded windows with enough temporal overlap for a max-length clip."""
    if not segments:
        return []
    windows: list[list[dict[str, Any]]] = []
    start = 0
    while start < len(segments):
        end = start
        word_count = 0
        while end < len(segments):
            size = max(1, len(segments[end]["text"].split()))
            if end > start and word_count + size > max_words:
                break
            word_count += size
            end += 1
        windows.append(segments[start:end])
        if end >= len(segments):
            break
        next_start = end - 1
        while next_start > start and segments[end - 1]["end"] - segments[next_start]["start"] < max_duration:
            next_start -= 1
        # Always make forward progress, even with unusually long segments.
        start = max(start + 1, next_start)
    return windows


def _parse_model_clips(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("clips"), list):
        raise ValueError("DeepSeek response must be a JSON object containing a clips array.")
    clips = payload["clips"]
    if any(not isinstance(item, dict) for item in clips):
        raise ValueError("Every proposed clip must be a JSON object.")
    return clips


def _post_window(
    window: list[dict[str, Any]], brief: str, api_key: str, model: str,
    min_duration: float, max_duration: float, max_clips: int, correction: str | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    compact = json.dumps([{
        "id": s["id"], "text": s["text"], "start": s["start"], "end": s["end"],
        "duration": s["end"] - s["start"],
    } for s in window], ensure_ascii=False)
    prompt = (
        f"Select up to {max_clips} coherent short-form video excerpts relevant to the user's brief. "
        "This is editorial selection, not a prediction of virality. Return only JSON: "
        '{"clips":[{"start_segment_id":0,"end_segment_id":2,"title":"...",'
        '"reason":"...","score":0.0}]}. Use only segment IDs supplied, with inclusive endpoints. '
        "Choose contiguous segments. Never return timestamps; local code maps IDs to word times. "
        f"Each excerpt must be {min_duration:g} to {max_duration:g} seconds inclusive. "
        "Use the first selected segment's start and last selected segment's end to estimate duration. "
        "Favor a clear hook, enough context to understand the point, and a satisfying payoff or complete thought. "
        "Avoid abrupt cutoffs, repetition, and excerpts that require missing context. "
        "Score 0 to 1 with this rubric: brief relevance (0.0-0.5), clarity/context (0.0-0.25), "
        "and payoff/completeness (0.0-0.25). These are editorial estimates, not virality predictions.\n\n"
        f"Brief: {brief}\nSegments: {compact}"
    )
    if correction:
        prompt += f"\n\nYour previous proposal had these validation issues. Return corrected valid proposals only:\n{correction}"
    response = httpx.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a careful transcript editor. Never invent transcript content or timestamps."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "reasoning_effort": "none",
            "max_tokens": max(1200, max_clips * 260),
        },
        timeout=45.0,
    )
    response.raise_for_status()
    try:
        envelope = response.json()
    except ValueError as exc:
        raise _InvalidProviderResponse("DeepSeek response body was not valid JSON.", getattr(response, "text", None)) from exc
    try:
        content = envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _InvalidProviderResponse("DeepSeek response did not contain a completion message.", envelope) from exc
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise _InvalidProviderResponse("DeepSeek returned invalid JSON content.", envelope) from exc
    else:
        parsed = content
    try:
        return _parse_model_clips(parsed), envelope
    except ValueError as exc:
        raise _InvalidProviderResponse(str(exc), envelope) from exc


def _make_clip(
    candidate: dict[str, Any], segments: list[dict[str, Any]], words_by_id: dict[int, dict[str, Any]],
    min_duration: float, max_duration: float,
) -> dict[str, Any]:
    start_id, end_id = candidate.get("start_segment_id"), candidate.get("end_segment_id")
    if isinstance(start_id, bool) or not isinstance(start_id, int) or isinstance(end_id, bool) or not isinstance(end_id, int):
        raise ValueError("segment IDs must be integers.")
    index = {segment["id"]: i for i, segment in enumerate(segments)}
    if start_id not in index or end_id not in index or index[start_id] > index[end_id]:
        raise ValueError("segment IDs are out of range or reversed.")
    selected = segments[index[start_id]:index[end_id] + 1]
    start_word_id, end_word_id = selected[0]["start_word_id"], selected[-1]["end_word_id"]
    if start_word_id not in words_by_id or end_word_id not in words_by_id:
        raise ValueError("segment references unavailable word IDs.")
    start, end = words_by_id[start_word_id]["start"], words_by_id[end_word_id]["end"]
    duration = end - start
    if duration + 1e-6 < min_duration or duration - 1e-6 > max_duration:
        raise ValueError(f"selected duration {duration:.2f}s is outside {min_duration:g}-{max_duration:g}s.")
    title = candidate.get("title")
    reason = candidate.get("reason")
    score = candidate.get("score")
    if not isinstance(title, str) or not title.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("title and reason must be non-empty strings.")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
        raise ValueError("score must be a finite number between 0 and 1.")
    return {
        "title": title.strip()[:140], "reason": reason.strip()[:600], "score": float(score),
        "start": start, "end": end, "start_word_id": start_word_id, "end_word_id": end_word_id,
    }


def _heuristic_candidates(
    words: list[dict[str, Any]], segments: list[dict[str, Any]], brief: str,
    min_duration: float, max_duration: float,
) -> list[dict[str, Any]]:
    brief_terms = set(_tokens(brief))
    candidates = []
    for i, first in enumerate(segments):
        excerpt_segments: list[str] = []
        excerpt_tokens: list[str] = []
        for j in range(i, len(segments)):
            last = segments[j]
            duration = last["end"] - first["start"]
            if duration > max_duration + 1e-6:
                break
            excerpt_segments.append(last["text"])
            excerpt_tokens.extend(_tokens(last["text"]))
            if duration + 1e-6 < min_duration:
                continue
            text = " ".join(excerpt_segments)
            if not excerpt_tokens:
                continue
            overlap = set(excerpt_tokens) & brief_terms
            relevance = len(overlap) / max(1, len(brief_terms)) if brief_terms else 0.0
            # Baseline measures brief-word overlap and transcript completeness only.
            score = min(0.75, 0.15 + relevance * 0.6 + min(len(excerpt_tokens), 80) / 80 * 0.12)
            reason = (
                f"Heuristic baseline: {len(overlap)} brief keyword(s) overlap the excerpt."
                if brief_terms else "Heuristic baseline: no brief keywords were available; ranked by transcript coverage."
            )
            candidates.append({
                "title": " ".join(text.split()[:8]).strip(" ,.!?") or "Transcript excerpt",
                "reason": reason, "score": round(score, 4), "start": first["start"], "end": last["end"],
                "start_word_id": first["start_word_id"], "end_word_id": last["end_word_id"],
            })
    candidates.sort(key=lambda c: (c["score"], c["end"] - c["start"]), reverse=True)
    return candidates


def _ranked_nonoverlap(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for candidate in sorted(candidates, key=lambda c: (-c["score"], c["start"])):
        key = (candidate["start_word_id"], candidate["end_word_id"])
        if key in seen:
            continue
        if any(candidate["start"] < old["end"] and old["start"] < candidate["end"] for old in result):
            continue
        seen.add(key)
        result.append(candidate)
        if len(result) >= limit:
            break
    for i, candidate in enumerate(result, start=1):
        candidate["id"] = f"clip-{i}"
        candidate["review"] = "unreviewed"
        candidate["feedback"] = ""
    return result


def select_clips(
    words: list[dict[str, Any]], brief: str, provider: str = "heuristic", min_duration: float = 25,
    max_duration: float = 60, max_clips: int = 5, api_key: str | None = None,
    model: str = "deepseek-flash",
) -> dict[str, Any]:
    """Select clips using a deterministic baseline or DeepSeek proposals.

    Remote-provider failures are reported and never trigger a heuristic fallback.
    """
    checked = _validated_words(words)
    if not isinstance(brief, str):
        raise ValueError("brief must be a string.")
    if isinstance(min_duration, bool) or isinstance(max_duration, bool) or not isinstance(min_duration, (int, float)) or not isinstance(max_duration, (int, float)):
        raise ValueError("duration limits must be numbers.")
    min_duration, max_duration = float(min_duration), float(max_duration)
    if not math.isfinite(min_duration) or not math.isfinite(max_duration) or min_duration <= 0 or max_duration < min_duration:
        raise ValueError("duration limits must be finite, positive, and max_duration >= min_duration.")
    if isinstance(max_clips, bool) or not isinstance(max_clips, int) or max_clips < 1:
        raise ValueError("max_clips must be a positive integer.")
    if provider not in ("heuristic", "deepseek"):
        raise ValueError("provider must be 'heuristic' or 'deepseek'.")
    segments = build_segments(checked)
    warnings: list[str] = []
    raw: list[Any] = []
    if provider == "heuristic":
        candidates = _heuristic_candidates(checked, segments, brief, min_duration, max_duration)
        label = "heuristic baseline"
        warnings.append("Heuristic baseline uses brief-word overlap and transcript coverage; scores are editorial estimates, not virality predictions.")
    else:
        if not api_key:
            raise ValueError("DeepSeek selection requires an API key.")
        windows = _windowed_segments(segments)
        candidates = []
        errors: list[str] = []
        request_count = prompt_tokens = completion_tokens = total_tokens = 0
        for window_number, window in enumerate(windows, start=1):
            correction = None
            for attempt in range(2):
                request_count += 1
                try:
                    proposals, response_raw = _post_window(
                        window, brief, api_key, model, min_duration, max_duration, max_clips, correction,
                    )
                    raw.append({"window": window_number, "attempt": attempt + 1, "response": response_raw})
                    usage = response_raw.get("usage", {}) if isinstance(response_raw, dict) else {}
                    prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                    completion_tokens += int(usage.get("completion_tokens", 0) or 0)
                    total_tokens += int(usage.get("total_tokens", 0) or 0)
                except httpx.TimeoutException:
                    errors.append(f"DeepSeek request timed out in context window {window_number}.")
                    break
                except httpx.RequestError:
                    errors.append(f"DeepSeek request failed in context window {window_number}; check network connectivity.")
                    break
                except httpx.HTTPStatusError as exc:
                    errors.append(f"DeepSeek returned HTTP {exc.response.status_code} in context window {window_number}.")
                    break
                except _InvalidProviderResponse as exc:
                    raw.append({"window": window_number, "attempt": attempt + 1, "response": exc.raw})
                    if attempt == 0:
                        correction = "The previous response was invalid JSON or did not contain a clips array. Return the required JSON object."
                        continue
                    errors.append(f"DeepSeek returned an invalid response in context window {window_number} after one correction retry.")
                    break
                except (ValueError, KeyError, IndexError, TypeError):
                    errors.append(f"DeepSeek returned an invalid response in context window {window_number}.")
                    break
                window_ids = {s["id"] for s in window}
                rejected: list[str] = []
                for proposal in proposals:
                    try:
                        if proposal.get("start_segment_id") not in window_ids or proposal.get("end_segment_id") not in window_ids:
                            raise ValueError("segment IDs must belong to the supplied context window.")
                        candidates.append(_make_clip(proposal, segments, {w["id"]: w for w in checked}, min_duration, max_duration))
                    except (ValueError, TypeError) as exc:
                        rejected.append(str(exc))
                if rejected and attempt == 0:
                    correction = "\n".join(f"- {reason}" for reason in rejected)
                    continue
                if rejected:
                    warnings.extend(f"Rejected DeepSeek proposal in window {window_number}: {reason}" for reason in rejected)
                break
        if errors:
            warnings.extend(errors)
            if not candidates:
                raise RuntimeError(" ".join(errors))
        label = f"DeepSeek ({model})"
    clips = _ranked_nonoverlap(candidates, max_clips)
    if not clips:
        warnings.append("No excerpts met the requested duration and selection constraints.")
    return {
        "clips": clips,
        "metrics": {
            "provider": label,
            "score_label": "editorial estimate, not virality",
            "segments": len(segments),
            "context_windows": len(windows) if provider == "deepseek" else 0,
            "candidates_considered": len(candidates),
            "provider_requests": request_count if provider == "deepseek" else 0,
            "prompt_tokens": prompt_tokens if provider == "deepseek" else 0,
            "completion_tokens": completion_tokens if provider == "deepseek" else 0,
            "total_tokens": total_tokens if provider == "deepseek" else 0,
        },
        "warnings": warnings,
        "raw": raw,
    }

