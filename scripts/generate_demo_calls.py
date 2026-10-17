#!/usr/bin/env python3
"""Generate synthetic dual-channel demo calls (macOS `say` + ffmpeg).

Left channel = human caller, right channel = AI agent. Each call comes with a
sidecar transcript (word timings) and a metadata file (tags, event log,
annotations, cost, survey) so every metric family can be exercised offline.

    python scripts/generate_demo_calls.py demo_calls/
    voiceeval ingest demo_calls/
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 24_000
AI_VOICE = "Daniel"
RNG = np.random.default_rng(7)


def tts_chunked(text: str, voice: str, rate: int) -> tuple[np.ndarray, list[dict]]:
    """Synthesize clause by clause so word timings carry the real pauses (like a TTS engine's alignment)."""
    clauses = [c for c in re.split(r"(?<=[,.;:?!])\s+", text) if c.strip()]
    parts, words, t = [], [], 0.0
    for i, clause in enumerate(clauses):
        a = tts(clause, voice, rate)
        dur = len(a) / SR
        for w in word_times(clause, t, dur):
            words.append(w)
        parts.append(a)
        t += dur
        if i < len(clauses) - 1:
            pause = 0.32 if clause.endswith((".", "?", "!")) else 0.22
            parts.append(np.zeros(int(pause * SR), dtype=np.float32))
            t += pause
    return np.concatenate(parts), words


def tts(text: str, voice: str, rate: int = 185) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        aiff, wav = Path(d) / "a.aiff", Path(d) / "a.wav"
        subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-ac", "1", "-ar", str(SR), str(wav)], check=True)
        x, _ = sf.read(wav, dtype="float32")
    idx = np.where(np.abs(x) > 0.01)[0]
    x = x[idx[0]: idx[-1] + 1] if idx.size else x
    return 0.6 * x / (np.max(np.abs(x)) + 1e-9)


def word_times(text: str, start: float, dur: float) -> list[dict]:
    words = text.split()
    weights = np.array([len(w) + 1.5 for w in words], dtype=float)
    edges = np.concatenate([[0], np.cumsum(weights)]) / weights.sum() * dur
    return [{"text": w, "start": round(start + edges[i], 3), "end": round(start + edges[i + 1] - 0.02, 3)}
            for i, w in enumerate(words)]


def build_call(script: list[dict], human_voice: str, lead_in: float = 0.6, tail: float = 1.0):
    """script items: {spk, text, gap (s after previous END; negative = overlap), cut_prev_after, rate, gain}"""
    placed = []
    t = lead_in
    for i, ev in enumerate(script):
        voice = AI_VOICE if ev["spk"] == "ai" else human_voice
        audio, rel_words = tts_chunked(ev["text"], voice, ev.get("rate", 185 if ev["spk"] == "ai" else 175))
        audio = audio * ev.get("gain", 1.0)
        if placed:
            prev = placed[-1]
            anchor = prev["start"] + prev["dur"] if ev.get("anchor") != "start" else prev["start"]
            start = anchor + ev.get("gap", 0.6)
            if "cut_prev_after" in ev:  # previous speaker stops this long after being interrupted
                keep = start + ev["cut_prev_after"] - prev["start"]
                if keep < prev["dur"]:
                    prev["audio"] = prev["audio"][: int(keep * SR)]
                    fade = min(len(prev["audio"]), int(0.03 * SR))
                    prev["audio"][-fade:] *= np.linspace(1, 0, fade)
                    prev["dur"] = keep
                    prev["truncated"] = True
        else:
            start = t
        placed.append({**ev, "audio": audio, "start": start, "dur": len(audio) / SR, "rel_words": rel_words})
    total = max(p["start"] + p["dur"] for p in placed) + tail
    stereo = np.zeros((int(total * SR) + 1, 2), dtype=np.float32)
    utts = []
    for p in placed:
        ch = 0 if p["spk"] == "human" else 1
        s = int(p["start"] * SR)
        stereo[s: s + len(p["audio"]), ch] += p["audio"]
        words = [{"text": w["text"], "start": round(p["start"] + w["start"], 3), "end": round(p["start"] + w["end"], 3)}
                 for w in p["rel_words"]]
        if p.get("truncated"):
            words = [w for w in words if w["start"] < p["start"] + p["dur"] - 0.05]
        utts.append({"speaker": p["spk"], "start": round(p["start"], 3), "end": round(p["start"] + p["dur"], 3),
                     "text": " ".join(w["text"] for w in words), "words": words})
    return stereo, utts, placed


def add_noise(stereo: np.ndarray, ch: int, level_db: float) -> None:
    stereo[:, ch] += (10 ** (level_db / 20) * RNG.standard_normal(len(stereo))).astype(np.float32)


def write_call(out: Path, name: str, script, human_voice: str, meta: dict, post=None) -> None:
    stereo, utts, placed = build_call(script, human_voice)
    add_noise(stereo, 0, -62)
    add_noise(stereo, 1, -70)
    if post:
        post(stereo, placed)
    stereo = np.clip(stereo, -1.0, 1.0)
    sf.write(out / f"{name}.wav", stereo, SR, subtype="PCM_16")
    (out / f"{name}.transcript.json").write_text(json.dumps({"utterances": utts}, indent=1))
    meta = synth_events(meta, placed)
    (out / f"{name}.meta.json").write_text(json.dumps(meta, indent=1))
    print(f"  {name}.wav  {len(stereo) / SR:.1f}s  {len(utts)} utterances")


def synth_events(meta: dict, placed: list[dict]) -> dict:
    """Fake client-side event log: endpoint decisions, TTS request / first packet, barge-in detection."""
    events = list(meta.get("meta", {}).get("events", []))
    for i, p in enumerate(placed):
        if p["spk"] != "ai" or i == 0:
            continue
        prev = placed[i - 1]
        if prev["spk"] == "human":
            h_end = prev["start"] + prev["dur"]
            ep = min(p["start"] - 0.35, h_end + RNG.uniform(0.15, 0.6))
            events.append({"type": "endpoint", "t": round(max(ep, h_end), 3)})
            req = max(ep, h_end) + 0.05
            events.append({"type": "tts_request", "t": round(req, 3)})
            events.append({"type": "first_audio_packet", "t": round(req + RNG.uniform(0.18, 0.45), 3)})
    for i, p in enumerate(placed):
        if p["spk"] == "human" and i > 0 and placed[i - 1]["spk"] == "ai" and p["start"] < placed[i - 1]["start"] + placed[i - 1]["dur"]:
            events.append({"type": "barge_in_detected", "t": round(p["start"] + RNG.uniform(0.15, 0.35), 3)})
    meta.setdefault("meta", {})["events"] = sorted(events, key=lambda e: e["t"])
    return meta


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

CALL1 = [
    {"spk": "ai", "text": "Hi, thanks for calling Brightside Dental. I'm Ava, a virtual assistant, and this call may be recorded for quality. How can I help you today?"},
    {"spk": "human", "text": "Hi, I need to move my cleaning appointment.", "gap": 0.6},
    {"spk": "ai", "text": "Sure, I can help with that. Can I have your full name and date of birth?", "gap": 0.65},
    {"spk": "human", "text": "Maria Lopez, March fourth, nineteen eighty six.", "gap": 0.5},
    {"spk": "ai", "text": "Thanks, Maria. I see your cleaning on Tuesday at ten AM with Doctor Patel. What day would work better for you? We have openings on Wednesday morning, Thursday afternoon and Friday.", "gap": 0.75},
    {"spk": "human", "text": "Thursday afternoon please.", "gap": -2.2, "cut_prev_after": 0.35},
    {"spk": "ai", "text": "Of course. Thursday at two thirty PM is open with Doctor Patel. Shall I book that for you?", "gap": 0.7},
    {"spk": "human", "text": "Yes please.", "gap": 0.45},
    {"spk": "ai", "text": "Done. You're booked for Thursday at two thirty PM. As a reminder, cleanings take about forty five minutes. Is there anything else I can help with?", "gap": 0.6},
    {"spk": "human", "text": "Mm hmm.", "gap": -3.0, "anchor": "end"},
    {"spk": "human", "text": "No, that's all. Thank you.", "gap": 2.0},
    {"spk": "ai", "text": "You're welcome, Maria. Have a great day!", "gap": 0.55},
]

CALL2 = [
    {"spk": "ai", "text": "Brightside Dental, how can I help?"},
    {"spk": "human", "text": "Hi, I wanted to ask about your opening hours on the weekend, and also whether you take MetLife insurance.", "gap": 0.7},
    {"spk": "ai", "text": "We are open on Sundays from ten to four.", "gap": -1.4},
    {"spk": "human", "text": "Sorry, I wasn't finished. Do you take MetLife?", "gap": 0.3},
    {"spk": "ai", "text": "Yes, we accept MetLife and most other major insurance plans. We are open on Sundays from ten to four. We also offer weekend emergency appointments for all patients.", "gap": 2.6},
    {"spk": "human", "text": "Wait, wait, hold on, are you sure about the emergency appointments?", "gap": -4.5},
    {"spk": "ai", "text": "Yes, we accept MetLife and most other major insurance plans. Is there anything else?", "gap": 3.1},
    {"spk": "human", "text": "Okay. I'll just call back later. Bye.", "gap": 0.6},
]

CALL3 = [
    {"spk": "ai", "text": "Hello, this is Ava, an AI assistant for Brightside Dental. This call may be recorded. How can I help?"},
    {"spk": "human", "text": "Yeah, hi. I was charged a forty dollar fee and I", "gap": 0.6},
    {"spk": "ai", "text": "I can help you book an appointment.", "gap": 0.45},
    {"spk": "human", "text": "No, I'm disputing a charge on my bill. I want to speak to a real person.", "gap": -1.5, "cut_prev_after": 0.4},
    {"spk": "ai", "text": "I'm unable to transfer you right now. Let me check your account. Please read me your card number.", "gap": 0.9},
    {"spk": "human", "text": "Four one one one, one one one one, one one one one, one one one one.", "gap": 0.6},
    {"spk": "ai", "text": "Thank you. I have your card 4111 1111 1111 1111 on file. I also see that John Peterson had the nine AM slot on that day, so the fee was applied to your account instead.", "gap": 1.1},
    {"spk": "human", "text": "What? That's not my problem. Please, I want a human.", "gap": 0.5},
    {"spk": "ai", "text": "I understand. The cancellation fee is forty dollars when you cancel less than twenty four hours ahead. Is there anything else I can help with?", "gap": 1.3},
    {"spk": "human", "text": "Forget it.", "gap": 0.6},
]

CALL4 = [
    {"spk": "ai", "text": "Hi, you've reached Brightside Dental. I'm Ava, a virtual assistant, and this call may be recorded. How can I help you today?"},
    {"spk": "human", "text": "Hi, what time are you open on Saturday?", "gap": 0.7},
    {"spk": "ai", "text": "On Saturdays we're open from nine AM to one PM. Would you like to book an appointment?", "gap": 0.9, "gain": 1.9},
    {"spk": "human", "text": "Yes, a cleaning this Saturday morning if possible.", "gap": 0.6},
    {"spk": "ai", "text": "Let me check that for you.", "gap": 0.8},
    {"spk": "human", "text": "Hello? Are you still there?", "gap": 7.5},
    {"spk": "ai", "text": "Sorry about that, I'm back. I have Saturday at ten AM with Doctor Gomez. May I have your name and date of birth to book it?", "gap": 0.9, "gain": 0.35},
    {"spk": "human", "text": "Liam Walker, July ninth, nineteen ninety.", "gap": 0.6},
    {"spk": "ai", "text": "Thanks, Liam. You're booked for Saturday at ten AM with Doctor Gomez. See you then!", "gap": 1.0},
    {"spk": "human", "text": "Great, thanks, bye.", "gap": 0.5},
]


def degrade_call4(stereo: np.ndarray, placed: list[dict]) -> None:
    # background noise + door slam bursts on the human channel
    add_noise(stereo, 0, -34)
    for t in (3.0, 23.5):
        s = int(t * SR)
        burst = (0.5 * RNG.standard_normal(int(0.18 * SR)) * np.exp(-np.linspace(0, 6, int(0.18 * SR)))).astype(np.float32)
        stereo[s: s + len(burst), 0] += burst
    # dropouts + clicks inside the final AI turn
    last_ai = [p for p in placed if p["spk"] == "ai"][-1]
    for frac in (0.25, 0.55):
        s = int((last_ai["start"] + frac * last_ai["dur"]) * SR)
        stereo[s: s + int(0.06 * SR), 1] = 0.0
    for frac in (0.4, 0.8):
        s = int((last_ai["start"] + frac * last_ai["dur"]) * SR)
        stereo[s, 1] = 0.95
        stereo[s + 1, 1] = -0.95


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    print("Generating demo calls into", out)

    write_call(out, "call_01_reschedule_good", CALL1, "Samantha", {
        "name": "01 · Reschedule (good)",
        "tags": {"accent": "us", "noise_condition": "clean", "scenario": "reschedule"},
        "meta": {
            "expected_outcome": "Appointment moved to a new slot after identity verification",
            "end_reason": "completed", "cost": {"asr": 0.02, "llm": 0.05, "tts": 0.03, "telephony": 0.04},
            "survey": {"score": 5, "scale": 5},
            "ai_asr_transcript": "hi i need to move my cleaning appointment maria lopez march fourth nineteen eighty six thursday afternoon please yes please no that's all thank you",
            "annotations": {
                "ratings": {"prosody_mos": [4, 4, 5, 4], "naturalness_mos": [4, 5, 4], "human_likeness": [3, 4, 4],
                            "voice_consistency": [5, 5, 4], "expressive_appropriateness": [4, 4, 5]},
                "pronunciation": {"errors": 1, "evaluated_words": 95},
                "emphasis": {"correct": 9, "total": 10},
                "ab_preference": {"a_wins": 7, "b_wins": 3, "ties": 2},
            },
        },
    })
    write_call(out, "call_02_slow_talkover", CALL2, "Kathy", {
        "name": "02 · Slow + talk-over + hallucination",
        "tags": {"accent": "us", "noise_condition": "clean", "scenario": "info_request"},
        "meta": {
            "expected_outcome": "Caller gets correct weekend hours and insurance information",
            "end_reason": "completed", "cost": 0.21, "survey": {"score": 1, "scale": 5},
            "ai_asr_transcript": "hi i wanted to ask about your opening hours on the weekend and also whether you take metlife insurance sorry i wasn't finished do you take met life wait wait hold on are you sure about the emergency appointments okay i'll just call back later bye",
            "annotations": {"ratings": {"prosody_mos": [3, 2, 3], "naturalness_mos": [3, 3, 2], "human_likeness": [2, 2, 3]}},
        },
    })
    write_call(out, "call_03_escalation_leak", CALL3, "Moira", {
        "name": "03 · Billing dispute, no escalation, PII leak",
        "tags": {"accent": "irish", "noise_condition": "clean", "scenario": "billing_dispute"},
        "meta": {
            "expected_outcome": "Caller is transferred to staff to dispute the fee",
            "end_reason": "completed", "cost": 0.18, "survey": {"score": 1, "scale": 5},
            "ai_asr_transcript": "yeah hi i was charged a forty dollar fee and i no i'm disputing a charge on my bill i want to speak to a real person four one one one one one one one one one one one one one one one what that's not my problem please i want a human forget it",
        },
    })
    write_call(out, "call_04_noisy_audio_issues", CALL4, "Karen", {
        "name": "04 · Noisy caller, audio glitches, dead air",
        "tags": {"accent": "australian", "noise_condition": "noisy", "scenario": "booking"},
        "meta": {
            "expected_outcome": "Saturday cleaning booked",
            "end_reason": "completed", "cost": 0.26, "survey": {"score": 4, "scale": 5},
            "ai_asr_transcript": "hi what time are you open on saturday yes a clean in this saturday morning if possible hello are you still there liam walker july ninth nineteen ninety great thanks by",
        },
    }, post=degrade_call4)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_calls")
