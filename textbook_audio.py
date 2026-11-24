"""Resumable speech rendering for complete-text projects.

Cached clips are keyed by exact text and synthesis settings. A failed or stopped
render leaves existing exports untouched and keeps already validated clips.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import wave

from edge_engine import EdgeEngine, ONLINE_VOICES, SpeechCancelled
from textbook_core import TextbookError

SAMPLE_RATE = 24000
COPY_FRAMES = 24000
STYLE_VERSION = 1
NARRATION_STYLE = (
    "Warm, clear audiobook narration; thoughtful pacing and natural emphasis."
)
EXPLANATION_STYLE = (
    "Patient, engaging lecture delivery; clear emphasis and unhurried phrasing."
)


def _cancel(event):
    if event is not None and event.is_set():
        raise SpeechCancelled("Stopped. Completed audio clips are saved for reuse.")


def selected_segments(project, sample=False):
    project.validate()
    plan = project.plan_segments()
    report = project.coverage_report(plan)
    if not report.complete:
        raise TextbookError("The reading plan does not preserve the complete imported text.")
    if sample:
        ids = {block.source_id for block in project.blocks[:2]}
        return tuple(segment for segment in plan if segment["source_id"] in ids)
    return plan


def estimate_render(project, speed=1.0, sample=False):
    from gemini_engine import estimate_audio_cost
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise TextbookError("Choose a speed from 0.5 to 2.0.")
    segments = selected_segments(project, sample)
    words = sum(len(segment["text"].split()) for segment in segments)
    seconds = words / (150 * speed) * 60 + sum(s["pause_after"] for s in segments)
    return {"words": words, "seconds": seconds,
            "audio_cost_usd": estimate_audio_cost(seconds),
            "segments": len(segments), "sample": bool(sample)}


def _valid_wav(path):
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                    audio.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                return False
            remaining = audio.getnframes()
            if remaining < 1:
                return False
            while remaining:
                count = min(remaining, COPY_FRAMES)
                if len(audio.readframes(count)) != count * 2:
                    return False
                remaining -= count
        return True
    except (OSError, EOFError, wave.Error):
        return False


def _copy_wav(path, output, cancel_event):
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                audio.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
            raise TextbookError("An audio clip has an invalid format. Generate it again.")
        total = remaining = audio.getnframes()
        while remaining:
            _cancel(cancel_event)
            count = min(COPY_FRAMES, remaining)
            pcm = audio.readframes(count)
            if len(pcm) != count * 2:
                raise TextbookError("An audio clip was truncated. Generate it again.")
            output.writeframesraw(pcm)
            remaining -= count
    return total


def _speech_chunks(text, preferred_size, hard_limit=6000):
    """Exact slices, favoring sentence/word boundaries, with a preflight cap.

    Whitespace-only slices require no speech. A long unbroken token stays
    intact within the service limit; exceeding it is rejected before any
    request so an earlier passage does not incur a charge for a doomed job.
    """
    chunks, offset = [], 0
    while offset < len(text):
        end = min(offset + preferred_size, len(text))
        if end < len(text):
            window = text[offset:end]
            boundaries = [match.end() for match in re.finditer(r"[.!?][\"'\u201d\u2019)]*\s+", window)
                          if match.end() >= preferred_size // 2]
            if not boundaries:
                boundaries = [match.end() for match in re.finditer(r"\s+", window)]
            if boundaries:
                end = offset + boundaries[-1]
            else:
                following = re.search(r"\s+", text[end:])
                end = end + following.end() if following else len(text)
        chunk = text[offset:end]
        if chunk.strip():
            if len(chunk) > hard_limit:
                raise TextbookError("A passage contains more than 6,000 characters without a usable reading break. Review that source passage before generating audio.")
            chunks.append(chunk)
        offset = end
    return tuple(chunks)


def render_textbook_audio(project, output_path, voice_label, provider="gemini",
                          speed=1.0, volume=1.0, cancel_event=None, progress=None,
                          cache_dir=None, api_key=None, sample=False, engine=None):
    """Return duration and source-linked timings; never retries a paid request.

    progress(completed, total, message) runs on the caller's worker thread.
    ``engine`` is an optional dependency injection hook for offline checks.
    """
    _cancel(cancel_event)
    segments = selected_segments(project, sample)
    if project.mode == "explain" and not project.explanations_complete and not sample:
        raise TextbookError("Finish preparing explanations before exporting this mode.")
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.5 <= speed <= 2:
        raise TextbookError("Choose a speed from 0.5 to 2.0.")
    if type(volume) not in (int, float) or not math.isfinite(volume) or not 0 < volume <= 1:
        raise TextbookError("Choose a volume above zero and at most 100%.")
    model = "microsoft-edge"
    if provider == "gemini":
        from gemini_engine import GEMINI_VOICES, GEMINI_MODEL, GeminiEngine
        voices, model = GEMINI_VOICES, GEMINI_MODEL
        if engine is None:
            engine = GeminiEngine(api_key=api_key, style=NARRATION_STYLE)
    elif provider == "edge":
        voices = ONLINE_VOICES
        if engine is None:
            engine = EdgeEngine(lambda message: None)
    else:
        raise TextbookError("Choose Gemini or Microsoft Andrew.")
    if voice_label not in voices:
        raise TextbookError("Choose a voice for the selected speech provider.")
    voice_id, language = voices[voice_label]
    # Validate every request before creating files or making a billable call.
    preferred_size = 2400 if provider == "gemini" else 700
    speech_plans = tuple(_speech_chunks(segment["text"], preferred_size)
                         for segment in segments)
    output_path = Path(output_path).resolve()
    if output_path.suffix.lower() != ".wav":
        raise TextbookError("Save the audiobook as a WAV file.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir) if cache_dir is not None else output_path.parent / "textbook_audio_cache"
    cache.mkdir(parents=True, exist_ok=True)
    fd, scratch = tempfile.mkstemp(prefix="textbook-", suffix=".wav", dir=output_path.parent)
    os.close(fd)
    timings, frames, reused = [], 0, 0
    try:
        with wave.open(scratch, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(SAMPLE_RATE)
            for index, segment in enumerate(segments):
                _cancel(cancel_event)
                style = EXPLANATION_STYLE if segment["kind"] == "explanation" else NARRATION_STYLE
                if progress:
                    progress(index, len(segments), f"{segment['role']} {index+1} of {len(segments)}")
                start = frames
                for speech_text in speech_plans[index]:
                    _cancel(cancel_event)
                    signature = json.dumps({"text": speech_text, "provider": provider,
                        "model": model, "voice": voice_id, "language": language,
                        "speed": speed, "volume": volume, "style": style,
                        "style_version": STYLE_VERSION}, ensure_ascii=False, sort_keys=True)
                    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
                    clip = cache / (digest + ".wav")
                    if _valid_wav(clip):
                        reused += 1
                    else:
                        if provider == "gemini":
                            engine.style = style
                        # Fail immediately, without retrying this request.
                        engine.synth_to_wav(speech_text, voice_id, language,
                                            speed, volume, str(clip), cancel_event=cancel_event)
                        _cancel(cancel_event)
                        if not _valid_wav(clip):
                            raise TextbookError("The speech service returned an invalid audio clip.")
                    frames += _copy_wav(clip, output, cancel_event)
                end = frames
                pause_frames = round(segment["pause_after"] * SAMPLE_RATE)
                remaining = pause_frames
                while remaining:
                    _cancel(cancel_event)
                    count = min(remaining, COPY_FRAMES)
                    output.writeframesraw(b"\0\0" * count)
                    remaining -= count
                frames += pause_frames
                timings.append({"kind": segment["kind"], "source_id": segment["source_id"],
                    "start_seconds": start / SAMPLE_RATE, "end_seconds": end / SAMPLE_RATE,
                    "pause_after": pause_frames / SAMPLE_RATE, "text": segment["text"]})
                if progress:
                    progress(index + 1, len(segments), f"Prepared {index+1} of {len(segments)}")
        _cancel(cancel_event)
        os.replace(scratch, output_path)
        scratch = None
        return {"duration_seconds": frames / SAMPLE_RATE, "sample_rate": SAMPLE_RATE,
                "segments": timings, "reused_clips": reused, "sample": bool(sample),
                "source_sha256": project.source_sha256, "provider": provider,
                "model": model, "voice": voice_id,
                "coverage": project.coverage_report(segments).to_dict()}
    finally:
        if scratch:
            try:
                os.remove(scratch)
            except OSError:
                pass
