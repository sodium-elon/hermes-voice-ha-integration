"""One household-scoped confirmation via the existing Alexa/HA conversation route.

Design (user-mandated, minimal): a delayed DJ answer that ends in a yes/no
question is NOT spoken as a one-way notification. Instead its reply is held in
one bounded pending record, the *same* "Hermes Assistant" skill is relaunched
on the asking Echo, the existing HA Launch intent returns that pending reply
with a reprompt (so Alexa keeps listening), and a single spoken yes/no is
consumed and forwarded to the same DJ session. The Jarvis wake pipeline and its
60s deadline are untouched.

Trust model: this route carries no Amazon device/user/session identity, so it
cannot prove which Echo or person supplied an answer. ``last_called`` targets
delivery, not authorization. Accepted for this shared household under the
existing authenticated HA transport; full-envelope identity is out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time

CONVERSATION_ID = "alexa_hermes"
SKILL_ID = "amzn1.ask.skill.089b605e-8c45-4e3a-b5d2-9fca1ad7ed67"
LAUNCH = "__hermes_alexa_launch__"
YES = "__hermes_alexa_yes__"
NO = "__hermes_alexa_no__"

READY = "Hermes is ready. What would you like to know?"
ANSWER_REPROMPT = "Please answer yes or no."
NO_PENDING = "There is no pending question right now."
RECOVERY = ("I found it, but I could not reopen the question. "
            "Please open Hermes and make a new request.")

_QUESTION_STARTERS = (
    "want me to ", "would you like ", "do you want ", "shall i ",
    "should i ", "would you like me to ", "can i ", "want to ",
)

# A yes/no question starter that leads (no internal sentence punctuation) to the
# terminal "?". This tolerates an earlier "?" in a song title before the starter.
_CONFIRM_RE = re.compile(
    r"\b(want me to|should i|would you like|do you want|shall i|can i|want to)\b[^.!?\n]*\?\s*$",
    re.IGNORECASE,
)


def eligible_confirmation(profile: str, reply: str) -> bool:
    """Only a DJ/music reply that ends in a yes/no question.

    Deliberately conservative: a bare question mark or an open-ended question
    does not qualify. This is a heuristic (matching the DJ's actual phrasings),
    not semantic validation; unsupported cases stay on the notification path.
    """
    if profile != "music":
        return False
    text = (reply or "").strip()
    if not text or len(text) > 600:
        return False
    return bool(_CONFIRM_RE.search(text))


def launch_skill(target: str) -> bool:
    """Relaunch the existing skill on the asking Echo via Alexa Media Player.

    ``media_content_type: skill`` is documented and confirmed in the installed
    Alexa Media Player (5.16.0) source. Best-effort; False on any failure so the
    caller can fall back to a declarative message instead of an unanswered
    notification question.
    """
    if not target:
        return False
    try:
        from ..home_assistant.ha_assistant import call_service
        result = call_service(
            "media_player",
            "play_media",
            entity_id=target,
            data={
                "media_content_type": "skill",
                "media_content_id": SKILL_ID,
            },
        )
        return "error" not in result
    except Exception:
        return False


@dataclass
class Pending:
    generation: int
    request: str
    profile: str
    session: str
    deadline: float
    reply: str = ""


class PendingConfirmation:
    """One globally pending yes/no follow-up, generation-tagged and deadline-bounded."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.generation = 0
        self.pending = None

    def begin(self, request, profile, session):
        """Start a record for an in-flight slow turn (before it completes)."""
        with self.lock:
            self.generation += 1
            self.pending = Pending(
                self.generation, request, profile, session, self.clock() + 180
            )
            return self.generation

    def launch(self):
        """Handle the skill Launch intent: return armed reply or ordinary prompt."""
        with self.lock:
            pending = self.pending
            if pending and pending.reply and self.clock() < pending.deadline:
                return {"text": pending.reply, "reprompt": ANSWER_REPROMPT}
            # New request or expiry: launch intent surfaces the normal ready prompt.
            return {"text": READY, "reprompt": READY}

    def deliver(self, generation, reply, target, notify):
        """Route a finished slow answer: arm confirmation or notify declaratively."""
        with self.lock:
            pending = self.pending
            if not pending or pending.generation != generation:
                return
            if self.clock() >= pending.deadline:
                self.pending = None
                return
            if eligible_confirmation(pending.profile, reply):
                pending.reply = reply
                pending.deadline = self.clock() + 30
                if target and launch_skill(target):
                    return  # armed; the Launch intent now speaks the question.
                self.pending = None
                notify(RECOVERY, target=target)
                return
            self.pending = None
            notify(reply, target=target)

    def answer(self, answer):
        """Consume the pending record and package a one-shot yes/no follow-up."""
        with self.lock:
            pending = self.pending
            if not pending or not pending.reply:
                return None
            if self.clock() >= pending.deadline or answer not in ("yes", "no"):
                self.pending = None
                return None
            work = {
                "profile": pending.profile,
                "session": pending.session,
                "text": (
                    f"Original request: {pending.request}\n"
                    f"DJ reply/question: {pending.reply}\n"
                    f"User answer: {answer}\n"
                    "Handle this answer only; do not ask another question."
                ),
            }
            self.pending = None
            return work

    def cancel(self, generation=None):
        """Clear pending state (new request, stop/cancel, or generation supersede)."""
        with self.lock:
            if generation is None or (self.pending and self.pending.generation == generation):
                self.pending = None