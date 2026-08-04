"""Optional on-device speech-to-text, for when browser recognition isn't wanted.

The browser's Web Speech API is the default because it needs no install and no
model download — but it ships your audio to the browser vendor. This module is
the opt-in alternative: `pip install -e ".[local-stt]"` and audio never leaves
the machine.

Note what this deliberately *doesn't* reuse from freeflow: the streaming stack.
freeflow needs `HypothesisBuffer` and `StreamingTranscriber` because it types
words into a live text field, so partial results must be stabilized and never
revised. casecraft displays nothing while you talk — the answer is only needed
once, when you finish. So it transcribes the whole utterance in one pass, which
is both simpler and *more accurate*, because the model sees the full context
instead of a sliding window.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

_MODEL = None
_LOCK = threading.Lock()

MODEL_NAME = os.environ.get("CASECRAFT_WHISPER_MODEL", "base.en")


def available() -> bool:
    """True if local transcription can run. Cheap — no model load."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _model():
    """Load once, on first use. The first call pays ~10s; later ones don't."""
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel

            _MODEL = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        return _MODEL


# Whisper was trained on subtitles, so on silence or room noise it emits
# subtitle furniture — "[BLANK_AUDIO]", "(music)", "♪", bare punctuation — at a
# high rate. During a case that means phantom words appearing while the
# candidate is simply thinking.
_NONSPEECH = re.compile(r"[\[\]()♪*]")
_PHANTOM = {"you", "thank you", "thanks for watching", "bye", "so", "."}


def _clean(text: str) -> str:
    # Drop bracketed annotations AND standalone punctuation tokens. The final
    # pass runs without VAD (so thinking pauses aren't clipped), which means it
    # emits trailing ". . . . ." over silence — that inflates the word count and
    # skews the delivery analysis.
    kept = [w for w in text.split()
            if not _NONSPEECH.search(w) and re.search(r"\w", w)]
    out = " ".join(kept).strip()
    bare = out.lower().strip(" .,!?-—…")
    if not bare or not re.search(r"\w", out):      # pure punctuation
        return ""
    return "" if bare in _PHANTOM else out


def transcribe(audio: bytes, suffix: str = ".webm", *, partial: bool = False) -> str:
    """Transcribe one utterance.

    `partial=True` is the live pass shown while the candidate is still talking:
    greedy decoding for speed, and Silero VAD on — which is the strongest guard
    against Whisper inventing words during a thinking pause. The final pass uses
    a beam and leaves VAD off, because an interview answer is one continuous
    utterance and clipping its pauses would lose real speech.
    """
    if not available():
        raise RuntimeError("faster-whisper not installed")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(audio)
        path = Path(fh.name)
    try:
        segments, _info = _model().transcribe(
            str(path),
            language="en",
            beam_size=1 if partial else 5,
            vad_filter=partial,
            condition_on_previous_text=False,
        )
        return _clean(" ".join(seg.text.strip() for seg in segments))
    finally:
        path.unlink(missing_ok=True)
