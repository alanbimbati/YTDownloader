from faster_whisper import WhisperModel

from . import config

_model = None


def transcribe(audio_path: str) -> tuple[str, str]:
    """Returns (text, detected language)."""
    global _model
    if _model is None:
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=3)
    segments, info = _model.transcribe(audio_path, vad_filter=True)
    return " ".join(s.text.strip() for s in segments), info.language
