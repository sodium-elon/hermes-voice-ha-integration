"""One household-scoped confirmation via the existing Alexa/HA conversation route.

Design: a delayed DJ or Sportscoach yes/no confirmation is NOT spoken as a
one-way notification. Instead its reply is held in one bounded pending record,
the *same* "Hermes Assistant" skill is relaunched on the asking Echo, and the
existing HA Launch intent returns the pending reply with a reprompt. Fast replies
return a native reprompt directly. A single spoken yes/no is consumed and
forwarded to the saved specialist context; its result cannot arm another question.
The Jarvis wake pipeline and its 60s deadline are untouched.

Trust model: this route carries no Amazon device/user/session identity, so it
cannot prove which Echo or person supplied an answer. ``last_called`` targets
delivery, not authorization. Accepted for this shared household under the
existing authenticated HA transport; full-envelope identity is out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
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


# Coach can ask for yes/no approval as an imperative, without a question mark.
# Require a terminal proposition ("confirm X is Y"), not an open-ended prompt.
_COACH_CONFIRM_RE = re.compile(
    r"\bconfirm\s+(?!(?:that\s+)?(?:which|what|when|where|how|who)\b)"
    r"(?:that\s+)?[^.!?\n]*\b(?:is|are)\b[^.!?\n]*[.?]\s*$",
    re.IGNORECASE,
)


_IDENTITY_CONFIRM_RE = re.compile(
    r"\b(?:did|do) you mean\s+(?!(?:which|what|when|where|how|who)\b)[^.!?\n]+\?\s*$",
    re.IGNORECASE,
)


def eligible_confirmation(profile: str, reply: str) -> bool:
    """Recognize bounded yes/no requests from DJ or Sportscoach.

    A bare question mark or open-ended question does not qualify. This is a
    conservative heuristic, not semantic validation; unsupported cases stay
    on the notification path. Coach also uses terminal "confirm X is Y."
    """
    if profile not in ("music", "sportscoach"):
        return False
    text = (reply or "").strip()
    if not text or len(text) > 600:
        return False
    return bool(_CONFIRM_RE.search(text) or _IDENTITY_CONFIRM_RE.search(text) or (
        profile == "sportscoach" and _COACH_CONFIRM_RE.search(text)
    ))


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
        import json
        import urllib.request
        from .ha_conversation import _get_config

        url, token = _get_config()
        if not token:
            return False
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/services/media_player/play_media",
            data=json.dumps({
                "entity_id": target,
                "media_content_type": "skill",
                "media_content_id": SKILL_ID,
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        return True  # HA acceptance only; native Echo capture needs physical verification.
    except Exception as exc:
        logging.getLogger(__name__).warning("Alexa skill relaunch failed: %s", type(exc).__name__)
        return False


@dataclass
class Pending:
    generation: int
    request: str
    profile: str
    session: str
    deadline: float
    reply: str = ""
    session_title: str | None = None
    target: str = ""
    target_future: object | None = None


class PendingConfirmation:
    """One globally pending yes/no follow-up, generation-tagged and deadline-bounded."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.generation = 0
        self.pending = None

    def begin(self, request, profile, session, *, session_title=None, target_future=None):
        """Start a record for an in-flight slow turn (before it completes)."""
        with self.lock:
            self.generation += 1
            self.pending = Pending(
                self.generation, request, profile, session, self.clock() + 180,
                session_title=session_title, target_future=target_future,
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

    def native_response(self, generation, reply):
        """Arm one answer for a fast native reply, without notification/relaunch.

        The 30-second budget starts here, not at measured speech completion.
        Expired/stale work cannot arm or replace a newer pending question.
        """
        response = {"text": reply, "reprompt": ""}
        with self.lock:
            pending = self.pending
            if not pending or pending.generation != generation:
                return response
            if self.clock() >= pending.deadline or not eligible_confirmation(pending.profile, reply):
                self.pending = None
                return response
            pending.reply = reply
            pending.deadline = self.clock() + 30
            response["reprompt"] = ANSWER_REPROMPT
            return response

    def deliver(self, generation, reply, target, notify):
        """Route a finished slow answer: arm confirmation or notify declaratively."""
        with self.lock:
            pending = self.pending
            if not pending or pending.generation != generation:
                return
            # Sportscoach's declarative completion is a result notification,
            # not permission to reopen a microphone or authorize another write.
            # It must survive a slow import; questions retain their deadline.
            if self.clock() >= pending.deadline and (
                pending.profile != "sportscoach" or eligible_confirmation(pending.profile, reply)
                or bool(_CONFIRM_RE.search(reply or ""))
            ):
                self.pending = None
                return
            if eligible_confirmation(pending.profile, reply):
                pending.reply = reply
                pending.target = target
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
                "session_title": pending.session_title,
                "target": pending.target,
                "target_future": pending.target_future,
                "text": (
                    f"Original request: {pending.request}\n"
                    f"{'Coach' if pending.profile == 'sportscoach' else 'DJ'} reply/question: {pending.reply}\n"
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