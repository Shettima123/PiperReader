"""Render structured lessons with Piper Reader's free Microsoft online voices.

``voice_map`` maps Narrator, Host, or Guest to an exact ``ONLINE_VOICES``
display label or its ``(voice_id, language)`` pair. Only mapped speakers used
in the lesson are required. Call this function on a worker thread: synthesis
sends the spoken text to Microsoft's service and requires internet access.

Progress callbacks receive ``(completed_chunks, total_chunks, speaker)``. The
initial callback uses an empty speaker. Output is a mono PCM16, 24 kHz WAV,
published atomically only after all chunks and pauses succeed. Returned segment
times exclude their trailing pause; the total duration includes every pause.
"""

import math
import os
import re
import tempfile
import wave
from collections.abc import Mapping

from edge_engine import EdgeEngine, ONLINE_VOICES, SpeechCancelled


SAMPLE_RATE = 24000
CHUNK_CHARACTERS = 700
_SPEAKERS = frozenset(("Narrator", "Host", "Guest"))
_COPY_FRAMES = 8192


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SpeechCancelled("Lesson audio stopped")


def split_speech_text(text, max_characters=CHUNK_CHARACTERS):
    """Split at paragraphs, sentences, then words without dropping words.

    Whitespace is normalized. A single word longer than the requested size
    stays intact, so a URL or unusual identifier is never silently truncated.
    """
    if not isinstance(text, str):
        raise ValueError("Lesson text must be a string")
    if not isinstance(max_characters, int) or max_characters < 1:
        raise ValueError("The speech chunk size must be a positive integer")
    chunks = []
    for paragraph in re.split(r"\n\s*\n", text):
        remainder = " ".join(paragraph.split())
        while len(remainder) > max_characters:
            boundary = remainder.rfind(" ", 0, max_characters + 1)
            if boundary <= 0:
                boundary = remainder.find(" ")
                if boundary < 0:
                    break
            # Prefer a complete sentence when doing so still fills at least
            # half the request, keeping both phrasing and request count useful.
            sentences = list(re.finditer(r"[.!?][\"'\u201d\u2019)]*\s+",
                                         remainder[:boundary + 1]))
            if sentences and sentences[-1].end() >= max_characters // 2:
                boundary = sentences[-1].end()
            chunks.append(remainder[:boundary].strip())
            remainder = remainder[boundary:].strip()
        if remainder:
            chunks.append(remainder)
    return chunks


def _pause_seconds(value):
    if isinstance(value, bool):
        value = 0.4
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        value = 0.4
    if not math.isfinite(value):
        value = 0.4
    return max(0.0, min(10.0, value))


def _resolve_voice(value):
    if isinstance(value, str):
        try:
            return ONLINE_VOICES[value]
        except KeyError:
            raise ValueError("Choose an available Microsoft online voice") from None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        candidate = tuple(value)
        if candidate in ONLINE_VOICES.values():
            return candidate
    raise ValueError("Choose an available Microsoft online voice")


def _prepare_segments(segments, voice_map, cancel_event):
    if not isinstance(voice_map, Mapping):
        raise ValueError("A voice mapping is required for the lesson")
    if isinstance(segments, (str, bytes, Mapping)):
        raise ValueError("The lesson must contain a sequence of speech segments")
    try:
        segments = iter(segments)
    except TypeError:
        raise ValueError("The lesson must contain a sequence of speech segments") from None
    plans = []
    for segment in segments:
        _check_cancel(cancel_event)
        if not isinstance(segment, Mapping):
            raise ValueError("Each lesson segment must contain a speaker and text")
        speaker = segment.get("speaker")
        if not isinstance(speaker, str) or speaker not in _SPEAKERS:
            raise ValueError("Lesson speakers must be Narrator, Host, or Guest")
        chunks = split_speech_text(segment.get("text"))
        if not chunks:
            raise ValueError("Lesson segments cannot contain empty text")
        if speaker not in voice_map:
            raise ValueError("Choose a voice for " + speaker)
        voice_id, language = _resolve_voice(voice_map[speaker])
        plans.append({
            "speaker": speaker,
            "text": segment["text"],
            "chunks": chunks,
            "voice_id": voice_id,
            "language": language,
            "pause_after": _pause_seconds(segment.get("pause_after", 0.4)),
        })
    if not plans:
        raise ValueError("There is no lesson text to read")
    return plans


def _copy_speech(source_path, destination, cancel_event):
    """Validate and copy one response in small PCM blocks."""
    with wave.open(source_path, "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(),
                source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
            raise ValueError("Lesson speech must be mono PCM16 audio at 24 kHz")
        frame_count = source.getnframes()
        if frame_count <= 0:
            raise ValueError("The speech service returned empty audio")
        remaining = frame_count
        while remaining:
            _check_cancel(cancel_event)
            count = min(remaining, _COPY_FRAMES)
            pcm = source.readframes(count)
            if len(pcm) != count * 2:
                raise ValueError("The speech service returned incomplete audio")
            destination.writeframesraw(pcm)
            remaining -= count
        return frame_count


def _write_pause(destination, frames, cancel_event):
    silence = b"\0\0" * _COPY_FRAMES
    while frames:
        _check_cancel(cancel_event)
        count = min(frames, _COPY_FRAMES)
        destination.writeframesraw(silence[:count * 2])
        frames -= count


def render_lesson_audio(segments, output_path, voice_map, speed=1.0,
                        volume=1.0, cancel_event=None, progress=None, engine=None):
    """Synthesize a lesson, preserving an existing output on any failure.

    No account, OpenAI API, or new dependencies are used. ``engine`` is an
    optional EdgeEngine-compatible adapter, primarily for offline tests.
    Cancellation raises ``edge_engine.SpeechCancelled``. Audio is never kept
    in memory for more than one short speech response and a small copy block.
    """
    _check_cancel(cancel_event)
    plans = _prepare_segments(segments, voice_map, cancel_event)
    total_chunks = sum(len(plan["chunks"]) for plan in plans)
    destination_path = os.path.abspath(os.fspath(output_path))
    destination_directory = os.path.dirname(destination_path)
    if not os.path.isdir(destination_directory):
        raise ValueError("The audio output folder does not exist")
    if engine is None:
        engine = EdgeEngine(lambda _message: None)
    if progress is not None:
        progress(0, total_chunks, "")

    completed = 0
    total_frames = 0
    metadata = []
    temporary_output = None
    try:
        descriptor, temporary_output = tempfile.mkstemp(
            prefix="lesson-", suffix=".wav", dir=destination_directory)
        os.close(descriptor)
        # Temporary response files live beside the destination and are removed
        # even when synthesis fails halfway through a request.
        with tempfile.TemporaryDirectory(prefix="lesson-chunks-",
                                         dir=destination_directory) as scratch:
            chunk_path = os.path.join(scratch, "speech.wav")
            with wave.open(temporary_output, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(SAMPLE_RATE)
                for plan in plans:
                    start_frame = total_frames
                    for chunk in plan["chunks"]:
                        _check_cancel(cancel_event)
                        engine.synth_to_wav(
                            chunk, plan["voice_id"], plan["language"], speed,
                            volume, chunk_path, cancel_event=cancel_event)
                        _check_cancel(cancel_event)
                        total_frames += _copy_speech(chunk_path, output, cancel_event)
                        os.remove(chunk_path)
                        completed += 1
                        if progress is not None:
                            progress(completed, total_chunks, plan["speaker"])
                    end_frame = total_frames
                    pause_frames = round(plan["pause_after"] * SAMPLE_RATE)
                    _write_pause(output, pause_frames, cancel_event)
                    total_frames += pause_frames
                    metadata.append({
                        "speaker": plan["speaker"],
                        "text": plan["text"],
                        "voice_id": plan["voice_id"],
                        "start_seconds": start_frame / SAMPLE_RATE,
                        "end_seconds": end_frame / SAMPLE_RATE,
                        "pause_after": pause_frames / SAMPLE_RATE,
                        "chunks": len(plan["chunks"]),
                    })
        _check_cancel(cancel_event)
        os.replace(temporary_output, destination_path)
        temporary_output = None
        return {"duration_seconds": total_frames / SAMPLE_RATE,
                "sample_rate": SAMPLE_RATE, "segments": metadata}
    finally:
        if temporary_output:
            try:
                os.remove(temporary_output)
            except OSError:
                pass
