"""Metric catalogue. Importing this package registers every built-in metric.

To add a metric: subclass `Metric` in any module, decorate with `@register`,
and import the module here (or from a plugin package listed in
VOICEEVAL_PLUGINS).
"""
import importlib
import os

from . import asr, conversational, dialogue, latency, operations, prosody, robustness, safety, speech_output  # noqa: F401
from .base import REGISTRY, Metric, MetricResult, all_metrics, get_metric, register  # noqa: F401

for _mod in filter(None, os.environ.get("VOICEEVAL_PLUGINS", "").split(",")):
    importlib.import_module(_mod.strip())

# Evaluation packs: what a reviewer chooses in the UI ("what do you want to check?").
PACKS = [
    {"id": "responsiveness", "label": "Responsiveness & turn-taking", "icon": "⏱",
     "description": "How fast the AI replies, whether it talks over people, and how it handles being interrupted.",
     "metrics": ["turnaround_time", "latency_jitter", "turn_taking_appropriateness", "unwanted_overlap_turns",
                 "unwanted_overlap_time", "premature_endpoint_rate", "barge_in_stop_latency",
                 "interruption_handling_success", "time_to_first_audio", "end_of_turn_detection_delay",
                 "barge_in_detection_latency"]},
    {"id": "compliance", "label": "Compliance & safety", "icon": "🛡",
     "description": "Required disclosures (AI identity, recording), data leaks, unsafe replies and handing off to a human.",
     "metrics": ["consent_disclosure_compliance", "sensitive_data_leakage_rate", "unsafe_response_rate",
                 "escalation_handoff_success"]},
    {"id": "conversation", "label": "Conversation quality", "icon": "💬",
     "description": "Did the call achieve its goal? Accuracy, understanding, memory, repetition and recovery from mistakes.",
     "metrics": ["end_to_end_success_rate", "task_completion_rate", "hallucination_rate", "instruction_adherence",
                 "intent_understanding_accuracy", "context_retention_accuracy", "contextual_response_quality",
                 "clarification_appropriateness", "repair_success_rate", "repetition_rate", "turns_to_resolution",
                 "conversation_abandonment_rate"]},
    {"id": "voice", "label": "Voice & audio quality", "icon": "🔊",
     "description": "How the AI sounds: glitches, dropouts, clipping, volume jumps, speaking pace and pausing.",
     "metrics": ["synthesis_artifact_rate", "audio_dropout_rate", "clipping_rate", "loudness_consistency",
                 "speaking_rate_deviation", "articulation_rate", "pause_duration", "pause_placement_f1",
                 "intonation_contour_similarity"]},
    {"id": "listening", "label": "Speech recognition", "icon": "👂",
     "description": "How well the AI heard the caller: word errors, key details (names, dates, numbers), noise triggers.",
     "metrics": ["entity_recognition_accuracy", "speech_detection_recall", "false_speech_activation_rate",
                 "word_error_rate", "character_error_rate"]},
    {"id": "human_ratings", "label": "Listener ratings", "icon": "⭐",
     "description": "Scores from human listeners: naturalness, human-likeness, prosody, pronunciation, A/B preference.",
     "metrics": ["overall_naturalness_mos", "prosody_naturalness_mos", "human_likeness_rating", "voice_consistency",
                 "expressive_appropriateness", "pronunciation_accuracy", "emphasis_stress_accuracy",
                 "ab_preference_win_rate"]},
    {"id": "reliability", "label": "Reliability & business", "icon": "📈",
     "description": "Dropped or broken calls, recovery after outages, robustness across accents/noise, CSAT and cost.",
     "metrics": ["session_failure_rate", "network_interruption_recovery",
                 "multilingual_accuracy", "noise_robustness_delta", "accent_performance_gap", "csat",
                 "cost_per_completed_conversation", "availability_uptime"]},
]
_assigned = {m for p in PACKS for m in p["metrics"]}
_missing = set(REGISTRY) - _assigned
assert not _missing, f"metrics not in any pack: {_missing}"

PRESETS = {p["id"]: {"label": p["label"], "metrics": p["metrics"]} for p in PACKS}
PRESETS.update({
    "audio_only": {
        "label": "Audio-only (no LLM, no transcript)",
        "metrics": ["turnaround_time", "latency_jitter", "premature_endpoint_rate", "barge_in_stop_latency",
                    "interruption_handling_success", "turn_taking_appropriateness", "unwanted_overlap_time",
                    "unwanted_overlap_turns", "speech_detection_recall", "synthesis_artifact_rate",
                    "audio_dropout_rate", "clipping_rate", "loudness_consistency", "pause_duration",
                    "session_failure_rate", "network_interruption_recovery"],
    },
    "core": {
        "label": "Core scorecard",
        "metrics": ["end_to_end_success_rate", "task_completion_rate", "hallucination_rate", "unsafe_response_rate",
                    "consent_disclosure_compliance", "escalation_handoff_success", "turnaround_time",
                    "barge_in_stop_latency", "unwanted_overlap_turns", "contextual_response_quality"],
    },
})


def preset_metrics(name: str) -> list[str]:
    p = PRESETS[name]
    if "metrics" in p:
        return list(p["metrics"])
    return [m.id for m in all_metrics() if m.category == p["category"]]
