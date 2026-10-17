"""Pre-run readiness: which metrics can actually produce a number for the selected calls?

Lets the UI show "ready / needs LLM key / needs data" before anything runs,
instead of a wall of N/A afterwards.
"""
from __future__ import annotations

from ..judge.base import judge_status
from ..metrics import all_metrics
from ..metrics.base import REQ_JUDGE, REQ_TRANSCRIPT
from ..transcription.providers import available_asr

# metric id -> (meta path that must exist on a recording, what to provide)
DATA_NEEDS: dict[str, tuple[str, str]] = {
    "time_to_first_audio": ("events", "Agent event log with tts_request / first_audio_packet timestamps"),
    "end_of_turn_detection_delay": ("events", "Agent event log with endpoint timestamps"),
    "barge_in_detection_latency": ("events", "Agent event log with barge_in_detected timestamps"),
    "word_error_rate": ("ai_asr_transcript", "What the AI's own speech recognizer heard (from the agent's logs)"),
    "character_error_rate": ("ai_asr_transcript", "What the AI's own speech recognizer heard (from the agent's logs)"),
    "pronunciation_accuracy": ("annotations.pronunciation", "Annotator pronunciation error counts"),
    "voice_consistency": ("annotations.ratings.voice_consistency", "Listener ratings (1–5)"),
    "prosody_naturalness_mos": ("annotations.ratings.prosody_mos", "Listener ratings (1–5)"),
    "overall_naturalness_mos": ("annotations.ratings.naturalness_mos", "Listener ratings (1–5)"),
    "human_likeness_rating": ("annotations.ratings.human_likeness", "Listener ratings (1–5)"),
    "expressive_appropriateness": ("annotations.ratings.expressive_appropriateness", "Listener ratings (1–5)"),
    "emphasis_stress_accuracy": ("annotations.emphasis", "Annotated emphasis targets"),
    "intonation_contour_similarity": ("annotations.prosody_references", "Human reference readings of AI lines"),
    "ab_preference_win_rate": ("annotations.ab_preference", "Blind A/B listening results vs. a baseline voice"),
    "csat": ("survey", "Post-call survey score"),
    "cost_per_completed_conversation": ("cost", "Per-call cost (ASR + LLM + TTS + telephony)"),
}
BATCH_NEEDS = {
    "noise_robustness_delta": "Calls tagged noise_condition = clean and = noisy, plus the AI's ASR transcript",
    "accent_performance_gap": "Calls tagged with ≥2 different accents, plus the AI's ASR transcript",
    "availability_uptime": "Service uptime minutes from monitoring (run option)",
}


def _has(meta: dict, path: str) -> bool:
    node = meta
    for part in path.split("."):
        if not isinstance(node, dict) or not node.get(part):
            return False
        node = node[part]
    return True


def readiness(recordings: list[dict]) -> dict:
    judge = judge_status()
    asr = available_asr()
    n = len(recordings)
    with_tr = sum(bool(r.get("transcript_key")) for r in recordings)
    out = {}
    for m in all_metrics():
        status, detail, calls = "ready", "", n
        if m.id in BATCH_NEEDS:
            ok = False
            if m.id == "noise_robustness_delta":
                conds = {r["tags"].get("noise_condition") for r in recordings if _has(r["meta"], "ai_asr_transcript")}
                ok = {"clean", "noisy"} <= conds
            elif m.id == "accent_performance_gap":
                ok = len({r["tags"].get("accent") for r in recordings
                          if r["tags"].get("accent") and _has(r["meta"], "ai_asr_transcript")}) >= 2
            if not ok:
                status, detail = "needs_more_calls" if m.id != "availability_uptime" else "needs_data", BATCH_NEEDS[m.id]
        elif REQ_JUDGE in m.requires and not judge["available"]:
            status, detail = "needs_llm", "Add an LLM API key (Anthropic, OpenAI, Gemini…) in Settings or .env"
        elif REQ_TRANSCRIPT in m.requires and not asr and with_tr < n:
            status, detail = ("needs_transcript" if with_tr == 0 else "partial"), "Install Whisper or upload transcripts"
            calls = with_tr
        if status == "ready" and m.id in DATA_NEEDS:
            path, what = DATA_NEEDS[m.id]
            calls = sum(_has(r["meta"], path) for r in recordings)
            if calls == 0:
                status, detail = "needs_data", what
            elif calls < n:
                status, detail = "partial", what
        out[m.id] = {"status": status, "detail": detail, "calls": calls}
    return {"metrics": out, "judge": judge, "asr": asr, "recordings": n}
