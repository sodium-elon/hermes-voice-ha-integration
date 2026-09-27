"""Durable, auditable music-preference memory backed by PostgreSQL.

The module deliberately separates preference evidence, request lifecycle, and
catalog/recommendation knowledge. Classifier output is bounded and fail-closed;
callers must supply the resolved music entity rather than asking an LLM to
invent one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EVENT_KINDS = {
    "request",
    "like",
    "dislike",
    "skip",
    "correction",
    "retraction",
    "recommendation_response",
    "temporary_exclusion",
}
ENTITY_TYPES = {"artist", "song", "album", "genre", "playlist", "context"}
SCOPES = {"durable", "context", "session"}
LIFECYCLE_STATUSES = {"requested", "resolved", "dispatched", "confirmed", "failed"}
CLASSIFIED_KINDS = {
    "request",
    "like",
    "dislike",
    "skip",
    "correction",
    "retraction",
    "recommendation_response",
    "playback_status",
    "not_music",
}
CLASSIFIER_THRESHOLD = 0.65


class AmbiguousClassification(ValueError):
    """Raised when Jev output cannot safely be persisted."""


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is absent."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _compact_quote(value: str | None) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact[:240] or None


@dataclass(frozen=True)
class PreferenceEvent:
    person_key: str
    kind: str
    ingestion_key: str
    observed_at: datetime
    entity_type: str | None = None
    entity_name: str | None = None
    artist_name: str | None = None
    preference: int | None = None
    explicit: bool = True
    scope: str = "durable"
    context: Mapping[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None
    target_interaction_id: int | None = None
    source_platform: str | None = None
    source_session_id: str | None = None
    source_message_id: str | None = None
    source_request_id: str | None = None
    evidence_quote: str | None = None
    classifier: Mapping[str, Any] | None = None

    def validate(self) -> "PreferenceEvent":
        if not self.person_key.strip() or not self.ingestion_key.strip():
            raise ValueError("person_key and ingestion_key are required")
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unsupported interaction kind: {self.kind}")
        if self.scope not in SCOPES:
            raise ValueError(f"unsupported scope: {self.scope}")
        targeted = self.kind in {"correction", "retraction"}
        if targeted != (self.target_interaction_id is not None):
            raise ValueError("correction/retraction requires exactly one target")
        if self.kind != "retraction":
            if self.entity_type not in ENTITY_TYPES or not (self.entity_name or "").strip():
                raise ValueError("a valid entity_type and entity_name are required")
        if self.kind == "retraction" and (self.entity_type or self.entity_name):
            raise ValueError("retraction inherits its target entity")
        if self.kind == "like" and self.preference != 1:
            raise ValueError("like preference must be 1")
        if self.kind == "dislike" and self.preference != -1:
            raise ValueError("dislike preference must be -1")
        if self.kind == "temporary_exclusion":
            if self.preference != -1 or self.scope == "durable":
                raise ValueError("temporary exclusions need negative, non-durable preference")
        if self.scope != "durable" and self.expires_at is None:
            raise ValueError("context/session evidence needs expires_at")
        if not (self.source_platform or "").strip():
            raise ValueError("source_platform is required")
        if not any((self.source_session_id, self.source_message_id, self.source_request_id)):
            raise ValueError("at least one source session/message/request identifier is required")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


@dataclass(frozen=True)
class LifecycleEvent:
    request_ingestion_key: str
    status: str
    observed_at: datetime
    ingestion_key: str
    source_platform: str | None = None
    source_session_id: str | None = None
    source_message_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationPlan:
    row_count: int
    logical_request_count: int
    requests: tuple[PreferenceEvent, ...]
    lifecycle_events: tuple[LifecycleEvent, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreferenceContext:
    explicit_dislikes: Sequence[Mapping[str, Any]]
    temporary_exclusions: Sequence[Mapping[str, Any]]
    explicit_likes: Sequence[Mapping[str, Any]]
    top_requests: Sequence[Mapping[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt(self) -> str:
        def names(items: Sequence[Mapping[str, Any]], count: bool = False) -> str:
            if not items:
                return "none"
            rendered = []
            for item in items:
                label = str(item["name"])
                if item.get("artist_name"):
                    label += f" — {item['artist_name']}"
                if count:
                    label += f" ({item.get('request_count', 0)} requests)"
                rendered.append(label)
            return "; ".join(rendered)

        return "\n".join(
            [
                "Personal music context (evidence, not catalog facts):",
                f"Explicit dislikes (hard constraint): {names(self.explicit_dislikes)}",
                f"Temporary exclusions for this context: {names(self.temporary_exclusions)}",
                f"Explicit likes: {names(self.explicit_likes)}",
                f"Most requested (inferred affinity only): {names(self.top_requests, count=True)}",
                "Rules: explicit dislikes outrank request frequency. A temporary exclusion is not a permanent blacklist. Never infer a dislike from playback failure.",
            ]
        )


def _api_key() -> str:
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    candidates = []
    profile = os.getenv("HERMES_HOME", "").strip()
    if profile:
        candidates.append(Path(profile).expanduser() / "secrets" / "typesafe.env")
    real_home = os.getenv("HERMES_REAL_HOME", "").strip()
    if real_home:
        candidates.append(Path(real_home) / ".hermes" / "secrets" / "typesafe.env")
    candidates.append(Path("~/.hermes/secrets/typesafe.env").expanduser())
    for path in candidates:
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line.startswith("export "):
                    line = line.removeprefix("export ").strip()
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    raise ConfigurationError("TYPESAFE_API_KEY is not configured")


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    if isinstance(usage, Mapping):
        return {str(k): int(v) for k, v in usage.items() if isinstance(v, (int, float))}
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, (int, float)):
            result[key] = int(value)
    return result


def _call_jev(text: str) -> tuple[Mapping[str, Any], str, dict[str, int]]:
    from typesafe_sdk import Choice, TypeSafeClient

    questions = {
        "kind": Choice(
            instructions="Classify only the user's music interaction. Playback or recognition failure is playback_status, never dislike.",
            criteria={
                "request": "Asks to hear, play, find, or queue music.",
                "like": "Direct durable positive preference statement.",
                "dislike": "Direct durable negative preference statement.",
                "skip": "Asks to skip/stop the current item without stating a durable dislike.",
                "correction": "Corrects an earlier stored music request or preference.",
                "retraction": "Withdraws an earlier request or preference without replacing it.",
                "recommendation_response": "Accepts or rejects an assistant recommendation.",
                "playback_status": "Reports resolution, dispatch, audible confirmation, playback failure, or recognition failure.",
                "not_music": "No music-memory interaction is present.",
            },
        ),
        "scope": Choice(
            instructions="Choose how long the stated preference applies.",
            criteria={
                "durable": "General preference with no temporary qualifier.",
                "context": "Limited to an activity, mood, location, or phrase such as not tonight.",
                "session": "Limited to the current playback/listening session.",
            },
        ),
        "polarity": Choice(
            instructions="Classify the user's preference polarity, independent of playback success.",
            criteria={
                "positive": "Direct approval, liking, or recommendation acceptance.",
                "negative": "Direct disapproval, dislike, skip, temporary exclusion, or recommendation rejection.",
                "neutral": "Request, correction/retraction mechanics, or playback state without preference.",
            },
        ),
    }
    with TypeSafeClient(api_key=_api_key()) as client:
        response = client.system_one(state=text, questions=questions, model="jev-latest")
    return response.answers, str(response.model), _usage_dict(getattr(response, "usage", None))


def classify_interaction(text: str, threshold: float = CLASSIFIER_THRESHOLD) -> dict[str, Any]:
    if not text.strip():
        raise AmbiguousClassification("blank input")
    answers, model, usage = _call_jev(text)
    required = {"kind", "scope", "polarity"}
    if set(answers) < required:
        raise AmbiguousClassification("classifier omitted a required answer")
    values = {key: getattr(answers[key], "choice", None) for key in required}
    confidences = [float(getattr(answers[key], "confidence", 0.0)) for key in required]
    if values["kind"] not in CLASSIFIED_KINDS or values["scope"] not in SCOPES or values["polarity"] not in {"positive", "negative", "neutral"}:
        raise AmbiguousClassification("classifier returned an out-of-vocabulary answer")
    confidence = min(confidences)
    if confidence < threshold:
        raise AmbiguousClassification(f"classifier confidence {confidence:.3f} is below {threshold:.3f}")
    preference = {"positive": 1, "negative": -1, "neutral": 0}[values["polarity"]]
    kind = values["kind"]
    if kind == "dislike" and values["scope"] in {"context", "session"} and preference == -1:
        kind = "temporary_exclusion"
    return {
        "kind": kind,
        "scope": values["scope"],
        "preference": preference,
        "confidence": confidence,
        "model": model,
        "usage": usage,
    }


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hash_key(prefix: str, payload: Mapping[str, Any], ordinal: int) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{ordinal}:{raw}".encode()).hexdigest()
    return f"legacy-jsonl:{prefix}:{digest}"


def reconcile_legacy_jsonl(path: Path, person_key: str = "john") -> ReconciliationPlan:
    """Build a deterministic import plan without writing production state.

    A retry/counts_toward_frequency=false row and a resolved/confirmed duplicate
    are lifecycle transitions on the most recent matching logical request.
    Repeated requested/dispatched rows remain distinct requests.
    """
    rows = []
    warnings: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL line {number}: {exc}") from exc
        rows.append(row)

    requests: list[PreferenceEvent] = []
    lifecycle: list[LifecycleEvent] = []
    latest_by_signature: dict[tuple[str, str, str], PreferenceEvent] = {}
    latest_request: PreferenceEvent | None = None

    for ordinal, row in enumerate(rows, 1):
        kind = str(row.get("kind", "")).strip().lower()
        name = " ".join(str(row.get("name", "")).split())
        artist = " ".join(str(row.get("artist") or "").split()) or None
        status = str(row.get("status") or "requested").lower()
        if kind not in ENTITY_TYPES or not name or status not in LIFECYCLE_STATUSES:
            warnings.append(f"row {ordinal}: unsupported or incomplete; skipped")
            continue
        observed = _parse_time(str(row["timestamp"]))
        signature = (kind, normalize_name(name), normalize_name(artist or ""))
        matching = latest_by_signature.get(signature)
        lifecycle_only = bool(row.get("retry") or row.get("counts_toward_frequency") is False)
        if matching and status in {"resolved", "confirmed"}:
            lifecycle_only = True
        target = matching
        if lifecycle_only and target is None:
            target = latest_request
        if not lifecycle_only:
            request_key = _hash_key("request", row, ordinal)
            target = PreferenceEvent(
                person_key=person_key,
                kind="request",
                entity_type=kind,
                entity_name=name,
                artist_name=artist,
                preference=0,
                explicit=True,
                ingestion_key=request_key,
                observed_at=observed,
                source_platform="legacy-jsonl",
                source_request_id=str(ordinal),
                evidence_quote=_compact_quote(str(row.get("command") or "")),
            ).validate()
            requests.append(target)
            latest_by_signature[signature] = target
            latest_request = target
        if target is None:
            warnings.append(f"row {ordinal}: lifecycle transition has no request; skipped")
            continue
        lifecycle.append(
            LifecycleEvent(
                request_ingestion_key=target.ingestion_key,
                status=status,
                observed_at=observed,
                ingestion_key=_hash_key("lifecycle", row, ordinal),
                source_platform="legacy-jsonl",
                source_message_id=str(ordinal),
                detail={"legacy_retry": bool(row.get("retry", False))},
            )
        )
    return ReconciliationPlan(len(rows), len(requests), tuple(requests), tuple(lifecycle), tuple(warnings))


def _require_psycopg():
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise ConfigurationError("psycopg 3 is required for music preference storage") from exc
    return psycopg, sql


class MusicPreferenceStore:
    def __init__(self, dsn: str, role: str | None = None):
        if not dsn.strip():
            raise ConfigurationError("HERMES_MUSIC_MEMORY_DSN is required")
        self.dsn = dsn
        self.role = role

    @classmethod
    def from_env(cls) -> "MusicPreferenceStore":
        return cls(
            os.getenv("HERMES_MUSIC_MEMORY_DSN", ""),
            os.getenv("HERMES_MUSIC_MEMORY_ROLE", "music_memory_app").strip() or None,
        )

    def _connect(self):
        psycopg, sql = _require_psycopg()
        conn = psycopg.connect(self.dsn)
        if self.role:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.role)))
        return conn

    @staticmethod
    def _row_dict(cursor, row: Sequence[Any]) -> dict[str, Any]:
        return {column.name: value for column, value in zip(cursor.description, row)}

    def _entity_id(self, conn, event: PreferenceEvent) -> int | None:
        if event.kind == "retraction":
            return None
        artist_norm = normalize_name(event.artist_name or "")
        canonical_artist = None
        if event.artist_name:
            found = conn.execute(
                "SELECT canonical_name FROM music.artist_names WHERE name_norm = %s OR lower(name) = %s ORDER BY fame DESC NULLS LAST LIMIT 1",
                (artist_norm, artist_norm),
            ).fetchone()
            canonical_artist = found[0] if found else None
        row = conn.execute(
            """
            INSERT INTO music.preference_entities
                (entity_type, name, name_norm, artist_name, artist_name_norm, canonical_artist_name)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_type, name_norm, artist_name_norm)
            DO UPDATE SET canonical_artist_name = COALESCE(
                music.preference_entities.canonical_artist_name,
                EXCLUDED.canonical_artist_name
            )
            RETURNING id
            """,
            (event.entity_type, event.entity_name, normalize_name(event.entity_name or ""), event.artist_name, artist_norm, canonical_artist),
        ).fetchone()
        return int(row[0])

    def record(self, event: PreferenceEvent) -> int:
        event.validate()
        with self._connect() as conn:
            if event.target_interaction_id is not None:
                target = conn.execute(
                    "SELECT person_key FROM music.preference_interactions WHERE id = %s",
                    (event.target_interaction_id,),
                ).fetchone()
                if not target or target[0] != event.person_key:
                    raise ValueError("target does not belong to this person")
            entity_id = self._entity_id(conn, event)
            row = conn.execute(
                """
                INSERT INTO music.preference_interactions (
                    person_key, interaction_kind, entity_id, preference, explicit,
                    scope, context, expires_at, target_interaction_id,
                    source_platform, source_session_id, source_message_id,
                    source_request_id, observed_at, ingestion_key,
                    evidence_quote, classifier
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (ingestion_key) DO NOTHING
                RETURNING id
                """,
                (
                    event.person_key, event.kind, entity_id, event.preference,
                    event.explicit, event.scope, json.dumps(dict(event.context)),
                    event.expires_at, event.target_interaction_id,
                    event.source_platform, event.source_session_id,
                    event.source_message_id, event.source_request_id,
                    event.observed_at, event.ingestion_key,
                    _compact_quote(event.evidence_quote),
                    json.dumps(dict(event.classifier)) if event.classifier else None,
                ),
            ).fetchone()
            if row:
                return int(row[0])
            existing = conn.execute(
                "SELECT id FROM music.preference_interactions WHERE ingestion_key = %s",
                (event.ingestion_key,),
            ).fetchone()
            return int(existing[0])

    def record_lifecycle(self, event: LifecycleEvent) -> int:
        if event.status not in LIFECYCLE_STATUSES:
            raise ValueError(f"invalid lifecycle status: {event.status}")
        with self._connect() as conn:
            request = conn.execute(
                "SELECT id, interaction_kind FROM music.preference_interactions WHERE ingestion_key = %s",
                (event.request_ingestion_key,),
            ).fetchone()
            if not request or request[1] != "request":
                raise ValueError("lifecycle target is not a request")
            row = conn.execute(
                """
                INSERT INTO music.playback_lifecycle (
                    request_interaction_id, status, observed_at, ingestion_key,
                    source_platform, source_session_id, source_message_id, detail
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (ingestion_key) DO NOTHING RETURNING id
                """,
                (
                    request[0], event.status, event.observed_at, event.ingestion_key,
                    event.source_platform, event.source_session_id,
                    event.source_message_id, json.dumps(dict(event.detail)),
                ),
            ).fetchone()
            if row:
                return int(row[0])
            return int(conn.execute(
                "SELECT id FROM music.playback_lifecycle WHERE ingestion_key = %s",
                (event.ingestion_key,),
            ).fetchone()[0])

    def import_plan(self, plan: ReconciliationPlan) -> dict[str, int]:
        for request in plan.requests:
            self.record(request)
        for lifecycle in plan.lifecycle_events:
            self.record_lifecycle(lifecycle)
        return {
            "source_rows": plan.row_count,
            "logical_requests": plan.logical_request_count,
            "lifecycle_events": len(plan.lifecycle_events),
            "warnings": len(plan.warnings),
        }

    def context(self, person_key: str, context: Mapping[str, Any] | None = None, limit: int = 5) -> PreferenceContext:
        current_context = dict(context or {})
        with self._connect() as conn:
            signals = conn.execute(
                """
                WITH ranked AS (
                    SELECT e.name, e.entity_type AS kind, e.artist_name,
                           i.preference, i.interaction_kind,
                           row_number() OVER (PARTITION BY i.entity_id ORDER BY i.observed_at DESC, i.id DESC) AS rank
                    FROM music.effective_preference_interactions i
                    JOIN music.preference_entities e ON e.id = i.entity_id
                    WHERE i.person_key = %s AND i.explicit AND i.scope = 'durable'
                      AND i.preference IN (-1, 1)
                      AND i.interaction_kind IN ('like', 'dislike', 'correction', 'recommendation_response')
                )
                SELECT name, kind, artist_name, preference FROM ranked
                WHERE rank = 1 ORDER BY name
                """,
                (person_key,),
            ).fetchall()
            exclusions = conn.execute(
                """
                SELECT e.name, e.entity_type AS kind, e.artist_name
                FROM music.effective_preference_interactions i
                JOIN music.preference_entities e ON e.id = i.entity_id
                WHERE i.person_key = %s AND i.interaction_kind = 'temporary_exclusion'
                  AND %s::jsonb @> i.context
                ORDER BY i.observed_at DESC LIMIT %s
                """,
                (person_key, json.dumps(current_context), limit),
            ).fetchall()
            requests = conn.execute(
                """
                SELECT e.name, e.entity_type AS kind, e.artist_name,
                       f.request_count, f.last_requested_at
                FROM music.request_frequency f
                JOIN music.preference_entities e ON e.id = f.entity_id
                WHERE f.person_key = %s
                ORDER BY f.request_count DESC, f.last_requested_at DESC
                LIMIT %s
                """,
                (person_key, limit),
            ).fetchall()
        signal_dicts = [
            {"name": row[0], "kind": row[1], "artist_name": row[2]}
            for row in signals
        ]
        return PreferenceContext(
            explicit_dislikes=[item for item, row in zip(signal_dicts, signals) if row[3] == -1][:limit],
            temporary_exclusions=[{"name": row[0], "kind": row[1], "artist_name": row[2]} for row in exclusions],
            explicit_likes=[item for item, row in zip(signal_dicts, signals) if row[3] == 1][:limit],
            top_requests=[
                {"name": row[0], "kind": row[1], "artist_name": row[2], "request_count": row[3], "last_requested_at": row[4].isoformat()}
                for row in requests
            ],
        )

    def inspect(self, person_key: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT i.id, i.interaction_kind, e.entity_type, e.name, e.artist_name,
                       i.preference, i.scope, i.context, i.observed_at,
                       i.source_platform, i.source_session_id, i.source_message_id,
                       i.source_request_id, i.evidence_quote, i.target_interaction_id
                FROM music.effective_preference_interactions i
                LEFT JOIN music.preference_entities e ON e.id = i.entity_id
                WHERE i.person_key = %s ORDER BY i.observed_at DESC, i.id DESC LIMIT %s
                """,
                (person_key, limit),
            )
            return [self._row_dict(cursor, row) for row in cursor.fetchall()]

    def forget(self, person_key: str, interaction_id: int | None = None, entity_id: int | None = None) -> int:
        if (interaction_id is None) == (entity_id is None):
            raise ValueError("provide exactly one of interaction_id or entity_id")
        column = "id" if interaction_id is not None else "entity_id"
        value = interaction_id if interaction_id is not None else entity_id
        with self._connect() as conn:
            result = conn.execute(
                f"""
                UPDATE music.preference_interactions
                SET forgotten_at = now(), evidence_quote = NULL,
                    source_session_id = NULL, source_message_id = NULL,
                    source_request_id = NULL, classifier = NULL
                WHERE person_key = %s AND {column} = %s AND forgotten_at IS NULL
                """,
                (person_key, value),
            )
            return result.rowcount


def _event_from_args(args: argparse.Namespace) -> PreferenceEvent:
    context = json.loads(args.context)
    classifier = json.loads(args.classifier) if args.classifier else None
    return PreferenceEvent(
        person_key=args.person,
        kind=args.kind,
        entity_type=args.entity_type,
        entity_name=args.name,
        artist_name=args.artist,
        preference=args.preference,
        explicit=not args.inferred,
        scope=args.scope,
        context=context,
        expires_at=_parse_time(args.expires_at) if args.expires_at else None,
        target_interaction_id=args.target,
        source_platform=args.platform,
        source_session_id=args.session_id,
        source_message_id=args.message_id,
        source_request_id=args.request_id,
        observed_at=_parse_time(args.observed_at) if args.observed_at else utcnow(),
        ingestion_key=args.ingestion_key,
        evidence_quote=args.quote,
        classifier=classifier,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    classify = sub.add_parser("classify")
    classify.add_argument("text")

    record = sub.add_parser("record")
    record.add_argument("--person", default="john")
    record.add_argument("--kind", required=True, choices=sorted(EVENT_KINDS))
    record.add_argument("--entity-type", choices=sorted(ENTITY_TYPES))
    record.add_argument("--name")
    record.add_argument("--artist")
    record.add_argument("--preference", type=int, choices=(-1, 0, 1))
    record.add_argument("--scope", default="durable", choices=sorted(SCOPES))
    record.add_argument("--context", default="{}")
    record.add_argument("--expires-at")
    record.add_argument("--target", type=int)
    record.add_argument("--platform")
    record.add_argument("--session-id")
    record.add_argument("--message-id")
    record.add_argument("--request-id")
    record.add_argument("--observed-at")
    record.add_argument("--ingestion-key", required=True)
    record.add_argument("--quote")
    record.add_argument("--classifier")
    record.add_argument("--inferred", action="store_true")

    lifecycle = sub.add_parser("lifecycle")
    lifecycle.add_argument("--request-ingestion-key", required=True)
    lifecycle.add_argument("--status", required=True, choices=sorted(LIFECYCLE_STATUSES))
    lifecycle.add_argument("--ingestion-key", required=True)
    lifecycle.add_argument("--observed-at")
    lifecycle.add_argument("--platform")
    lifecycle.add_argument("--session-id")
    lifecycle.add_argument("--message-id")

    context = sub.add_parser("context")
    context.add_argument("--person", default="john")
    context.add_argument("--context", default="{}")
    context.add_argument("--limit", type=int, default=5)
    context.add_argument("--prompt", action="store_true")

    inspect_cmd = sub.add_parser("inspect")
    inspect_cmd.add_argument("--person", default="john")
    inspect_cmd.add_argument("--limit", type=int, default=50)

    forget = sub.add_parser("forget")
    forget.add_argument("--person", default="john")
    target = forget.add_mutually_exclusive_group(required=True)
    target.add_argument("--interaction-id", type=int)
    target.add_argument("--entity-id", type=int)

    reconcile = sub.add_parser("reconcile-jsonl")
    reconcile.add_argument("path", type=Path)
    reconcile.add_argument("--person", default="john")
    reconcile.add_argument("--apply", action="store_true")
    return parser


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "classify":
            result: Any = classify_interaction(args.text)
        elif args.command == "reconcile-jsonl":
            plan = reconcile_legacy_jsonl(args.path, args.person)
            result = {
                "source_rows": plan.row_count,
                "logical_requests": plan.logical_request_count,
                "lifecycle_events": len(plan.lifecycle_events),
                "warnings": list(plan.warnings),
                "apply": args.apply,
            }
            if args.apply:
                result.update(MusicPreferenceStore.from_env().import_plan(plan))
        else:
            store = MusicPreferenceStore.from_env()
            if args.command == "record":
                result = {"interaction_id": store.record(_event_from_args(args))}
            elif args.command == "lifecycle":
                event = LifecycleEvent(
                    request_ingestion_key=args.request_ingestion_key,
                    status=args.status,
                    observed_at=_parse_time(args.observed_at) if args.observed_at else utcnow(),
                    ingestion_key=args.ingestion_key,
                    source_platform=args.platform,
                    source_session_id=args.session_id,
                    source_message_id=args.message_id,
                )
                result = {"lifecycle_id": store.record_lifecycle(event)}
            elif args.command == "context":
                pref_context = store.context(args.person, json.loads(args.context), args.limit)
                result = pref_context.to_prompt() if args.prompt else pref_context.as_dict()
            elif args.command == "inspect":
                result = store.inspect(args.person, args.limit)
            elif args.command == "forget":
                result = {"forgotten": store.forget(args.person, args.interaction_id, args.entity_id)}
            else:  # pragma: no cover
                raise AssertionError(args.command)
        if isinstance(result, str):
            print(result)
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=_json_default))
        return 0
    except (AmbiguousClassification, ConfigurationError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
