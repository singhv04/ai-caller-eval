# Samples

`real-calls/` contains two real outbound AI-caller recordings used to develop and validate the platform:

| File | Length | Format | Scenario |
|---|---|---|---|
| `medical-appointment.wav` | 4:49 | mono, 24 kHz | Dental clinic re-activation call, books an appointment |
| `real-estate-appointment.wav` | 5:24 | mono, 24 kHz | Land-investment outreach, books a follow-up call |

Both are **mono** (caller and AI mixed on one track), so ingest separates the speakers automatically.
Overlap and interruption timing is approximate for these files.

```bash
python -m voiceeval ingest samples/real-calls/
python -m voiceeval run --profile outbound_appointment_setter --metrics preset:core --wait
```

These recordings contain real conversations. Keep this repository private.

Synthetic dual-channel calls with deliberate defects (talk-over, hallucinations, PII leaks, clipping, dead air)
can be generated on macOS with `python scripts/generate_demo_calls.py demo_calls`.
