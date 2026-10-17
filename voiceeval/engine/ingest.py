"""Recording ingest: store original, normalise to canonical stereo WAV, register in DB."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from ..audio.loader import diarize_mono, merge_mono_pair, normalize_stereo, probe
from ..db import Recording, session_scope
from ..storage import get_storage
from ..transcription.base import parse_sidecar
from .service import recording_to_dict

CHANNELS = {"left": 0, "right": 1, "0": 0, "1": 1}


def _channel(v) -> int:
    key = str(v).strip().lower()
    if key not in CHANNELS:
        raise ValueError("human_channel must be left/right (or 0/1)")
    return CHANNELS[key]


def ingest_recording(*, name: str, stereo_path: Path | None = None, human_path: Path | None = None,
                     ai_path: Path | None = None, human_channel="left", transcript: dict | list | None = None,
                     tags: dict | None = None, meta: dict | None = None, original_filename: str = "") -> dict:
    storage = get_storage()
    with session_scope() as s:
        rec = Recording(name=name, original_filename=original_filename, audio_key="", tags=tags or {}, meta=meta or {})
        s.add(rec)
        s.flush()
        rid = rec.id

    try:
        audio_key = f"recordings/{rid}/audio.wav"
        dest = storage.local_path(audio_key)
        extra_meta = {}
        if stereo_path is not None and probe(stereo_path)["channels"] == 1:
            hc = 0  # virtual stereo is always [human, ai]
            info, diar = diarize_mono(stereo_path, dest)
            extra_meta = {"channel_source": "mono_diarized", "diarization": diar}
            orig_key = f"recordings/{rid}/original{stereo_path.suffix.lower()}"
            shutil.copyfile(stereo_path, storage.local_path(orig_key))
        elif stereo_path is not None:
            hc = _channel(human_channel)
            info = normalize_stereo(stereo_path, dest)
            orig_key = f"recordings/{rid}/original{stereo_path.suffix.lower()}"
            shutil.copyfile(stereo_path, storage.local_path(orig_key))
        elif human_path is not None and ai_path is not None:
            hc = 0  # merged file is always [human, ai]
            info = merge_mono_pair(human_path, ai_path, dest)
        else:
            raise ValueError("Provide a stereo file, or both a human-channel and an AI-channel file")

        transcript_key = None
        if transcript is not None:
            parse_sidecar(transcript)  # validate early
            transcript_key = f"recordings/{rid}/transcript.json"
            storage.put_json(transcript_key, transcript)

        with session_scope() as s:
            rec = s.get(Recording, rid)
            rec.audio_key = audio_key
            rec.transcript_key = transcript_key
            rec.duration_s = info["duration"]
            rec.sample_rate = info["sample_rate"]
            rec.human_channel, rec.ai_channel = hc, 1 - hc
            if extra_meta:
                rec.meta = {**(rec.meta or {}), **extra_meta}
            return recording_to_dict(rec)
    except Exception:
        with session_scope() as s:
            rec = s.get(Recording, rid)
            if rec:
                s.delete(rec)
        storage.delete_prefix(f"recordings/{rid}")
        raise


def ingest_file(path: str | Path, *, name: str | None = None, human_channel="left",
                transcript_path: str | Path | None = None, meta_path: str | Path | None = None) -> dict:
    """CLI helper. A `<name>.meta.json` next to the audio may hold {tags, meta, human_channel}."""
    path = Path(path)
    sidecar_meta = {}
    meta_file = Path(meta_path) if meta_path else path.with_suffix(".meta.json")
    if meta_file.exists():
        sidecar_meta = json.loads(meta_file.read_text())
    tr_file = Path(transcript_path) if transcript_path else path.with_suffix(".transcript.json")
    transcript = json.loads(tr_file.read_text()) if tr_file.exists() else None
    return ingest_recording(
        name=name or sidecar_meta.get("name") or path.stem, stereo_path=path,
        human_channel=sidecar_meta.get("human_channel", human_channel), transcript=transcript,
        tags=sidecar_meta.get("tags"), meta=sidecar_meta.get("meta"), original_filename=path.name,
    )


def save_upload(data: bytes, filename: str) -> Path:
    suffix = Path(filename or "audio.wav").suffix or ".wav"
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(data)
    f.close()
    return Path(f.name)
