"""Observational Jev labels for spoken Assist replies, never playback verification."""
from __future__ import annotations

import math

from . import music


def label_spoken(text: str) -> dict:
    """Classify a reply's meaning; unknown on ambiguity/outage. Never gate speech."""
    if not text or not text.strip():
        return {"kind": "unknown", "confidence": 0.0}
    try:
        from typesafe_sdk import Choice, TypeSafeClient
        key = music._load_api_key()
        if not key:
            raise ValueError("Jev unavailable")
        with TypeSafeClient(api_key=key) as client:
            answer = client.system_one(
                model="jev-latest",
                state={"spoken_text": text},
                questions={"kind": Choice(
                    instructions=(
                        "What does this spoken assistant response communicate? "
                        "Classify only its meaning, not whether music actually played."
                    ),
                    criteria={
                        "acknowledgment": "A work-in-progress or request-received reply, not a result.",
                        "music_content": "Information about an artist, song, album or recommendation without claiming playback began.",
                        "playback_claim": "Claims a song is playing, has started, or was sent for playback; this is a claim, not evidence of audible music.",
                        "other": "Any other response or unclear content.",
                    },
                )},
            ).answers["kind"]
        kind = str(getattr(answer, "choice", ""))
        confidence = float(getattr(answer, "confidence", 0))
        if (kind not in {"acknowledgment", "music_content", "playback_claim", "other"}
                or not math.isfinite(confidence) or confidence < 0.6 or confidence > 1):
            raise ValueError("ambiguous label")
        return {"kind": kind, "confidence": confidence}
    except Exception:
        return {"kind": "unknown", "confidence": 0.0}
