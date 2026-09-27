"""Preference memory tests.

Unit tests are hermetic. PostgreSQL integration tests use an explicitly supplied
HERMES_TEST_POSTGRES_DSN and create/drop a temporary schema only.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.voice_stack import music_preferences as mp


def test_api_key_loader_accepts_export_syntax(tmp_path: Path, monkeypatch):
    home = tmp_path / "profile"
    (home / "secrets").mkdir(parents=True)
    (home / "secrets" / "typesafe.env").write_text("export TYPESAFE_API_KEY='test-key'\n")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "expanduser", lambda self: self)
    assert mp._api_key() == "test-key"


def test_normalize_event_rejects_invalid_combinations():
    with pytest.raises(ValueError, match="preference"):
        mp.PreferenceEvent(
            person_key="john",
            kind="like",
            entity_type="song",
            entity_name="Example",
            preference=0,
            ingestion_key="telegram:1",
            observed_at=datetime.now(timezone.utc),
        ).validate()

    with pytest.raises(ValueError, match="target"):
        mp.PreferenceEvent(
            person_key="john",
            kind="retraction",
            ingestion_key="telegram:2",
            observed_at=datetime.now(timezone.utc),
            source_platform="telegram",
            source_message_id="2",
        ).validate()


def test_classification_is_bounded_and_confidence_gated(monkeypatch):
    answer = {
        "kind": SimpleNamespace(choice="dislike", confidence=0.94),
        "scope": SimpleNamespace(choice="durable", confidence=0.91),
        "polarity": SimpleNamespace(choice="negative", confidence=0.93),
    }
    monkeypatch.setattr(mp, "_call_jev", lambda _text: (answer, "jev-1.13.0", {"input_tokens": 12, "output_tokens": 3}))
    result = mp.classify_interaction("I never want to hear that artist again")
    assert result == {
        "kind": "dislike",
        "scope": "durable",
        "preference": -1,
        "confidence": pytest.approx(0.91),
        "model": "jev-1.13.0",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }

    answer["kind"].confidence = 0.4
    with pytest.raises(mp.AmbiguousClassification):
        mp.classify_interaction("maybe")


def test_contextual_dislike_is_normalized_to_temporary_exclusion(monkeypatch):
    answer = {
        "kind": SimpleNamespace(choice="dislike", confidence=0.91),
        "scope": SimpleNamespace(choice="context", confidence=0.90),
        "polarity": SimpleNamespace(choice="negative", confidence=0.89),
    }
    monkeypatch.setattr(mp, "_call_jev", lambda _text: (answer, "jev-test", {}))
    assert mp.classify_interaction("not while working")["kind"] == "temporary_exclusion"


def test_classification_rejects_out_of_vocabulary(monkeypatch):
    answer = {
        "kind": SimpleNamespace(choice="invented", confidence=1.0),
        "scope": SimpleNamespace(choice="durable", confidence=1.0),
        "polarity": SimpleNamespace(choice="positive", confidence=1.0),
    }
    monkeypatch.setattr(mp, "_call_jev", lambda _text: (answer, "jev-test", {}))
    with pytest.raises(mp.AmbiguousClassification):
        mp.classify_interaction("anything")


def test_reconcile_legacy_rows_does_not_count_lifecycle_retries_twice(tmp_path: Path):
    rows = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "kind": "song", "name": "One", "artist": "A", "status": "failed"},
        {"timestamp": "2026-01-01T00:01:00+00:00", "kind": "song", "name": "One", "artist": "A", "status": "confirmed", "retry": True, "counts_toward_frequency": False},
        {"timestamp": "2026-01-02T00:00:00+00:00", "kind": "song", "name": "One", "artist": "A", "status": "dispatched"},
    ]
    path = tmp_path / "music_requests.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    plan = mp.reconcile_legacy_jsonl(path)
    assert plan.row_count == 3
    assert plan.logical_request_count == 2
    assert len(plan.requests) == 2
    assert len(plan.lifecycle_events) == 3
    assert plan.requests[0].ingestion_key != plan.requests[1].ingestion_key


def test_reconcile_is_deterministic(tmp_path: Path):
    path = tmp_path / "music_requests.jsonl"
    path.write_text(json.dumps({
        "timestamp": "2026-01-01T00:00:00+00:00",
        "kind": "artist", "name": "Björk", "status": "dispatched",
    }) + "\n")
    first = mp.reconcile_legacy_jsonl(path)
    second = mp.reconcile_legacy_jsonl(path)
    assert first.requests[0].ingestion_key == second.requests[0].ingestion_key
    assert first.lifecycle_events[0].ingestion_key == second.lifecycle_events[0].ingestion_key


def test_context_orders_explicit_dislikes_before_inferred_requests():
    context = mp.PreferenceContext(
        explicit_dislikes=[{"name": "Artist X", "kind": "artist"}],
        temporary_exclusions=[],
        explicit_likes=[],
        top_requests=[{"name": "Artist Y", "kind": "artist", "request_count": 9}],
    )
    text = context.to_prompt()
    assert text.index("Explicit dislikes") < text.index("Most requested")
    assert "Never infer a dislike from playback failure" in text


def test_sql_migration_is_additive_and_preserves_artist_names():
    sql = (Path(__file__).parents[1] / "deploy" / "music_preferences.sql").read_text()
    lowered = sql.lower()
    assert "create table if not exists music.artist_names" not in lowered
    assert "drop table" not in lowered
    assert "create table if not exists music.preference_entities" in lowered
    assert "create table if not exists music.preference_interactions" in lowered
    assert "create table if not exists music.playback_lifecycle" in lowered
    assert "create or replace view music.effective_preference_interactions" in lowered


def test_permissions_are_least_privilege():
    sql = (Path(__file__).parents[1] / "deploy" / "music_preferences_grants.sql").read_text().lower()
    assert 'grant select on music.artist_names' in sql
    assert 'grant all' not in sql
    assert 'public' not in sql


@pytest.fixture
def postgres_store():
    """Create an isolated database; never write fixtures to John's music schema."""
    admin_dsn = os.getenv("HERMES_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("set HERMES_TEST_POSTGRES_DSN to run PostgreSQL integration tests")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"music_pref_test_{uuid.uuid4().hex[:12]}"
    admin = conninfo_to_dict(admin_dsn)
    admin_db = admin.get("dbname", "postgres")
    root_dsn = make_conninfo(**{**admin, "dbname": admin_db})
    with psycopg.connect(root_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    test_dsn = make_conninfo(**{**admin, "dbname": name})
    try:
        with psycopg.connect(test_dsn, autocommit=True) as conn:
            conn.execute("CREATE SCHEMA music")
            conn.execute(
                """CREATE TABLE music.artist_names (
                    name text PRIMARY KEY, canonical_name text NOT NULL,
                    popularity integer, source text NOT NULL DEFAULT 'fixture',
                    updated_at timestamptz NOT NULL DEFAULT now(), name_norm text,
                    name_dm text, mbid text, artist_type text, fame integer DEFAULT 0
                )"""
            )
            conn.execute(
                "INSERT INTO music.artist_names(name, canonical_name, name_norm, fame) VALUES ('Björk', 'Björk', 'björk', 10)"
            )
            migration = (Path(__file__).parents[1] / "deploy" / "music_preferences.sql").read_text()
            conn.execute(migration)
            conn.execute(migration)  # migration-twice idempotency
        yield mp.MusicPreferenceStore(test_dsn, role=None), test_dsn
    finally:
        with psycopg.connect(root_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _event(key: str, kind: str, name: str = "Björk", **changes):
    values = {
        "person_key": "john",
        "kind": kind,
        "entity_type": "artist",
        "entity_name": name,
        "preference": {"like": 1, "dislike": -1}.get(kind, 0),
        "ingestion_key": key,
        "observed_at": datetime.now(timezone.utc),
        "source_platform": "fixture",
        "source_message_id": key,
        "evidence_quote": "minimal fixture quotation",
    }
    values.update(changes)
    return mp.PreferenceEvent(**values)


def test_postgres_lifecycle_precedence_context_and_forgetting(postgres_store):
    store, dsn = postgres_store
    request = _event("req:1", "request")
    request_id = store.record(request)
    assert store.record(request) == request_id  # ingestion is idempotent
    store.record_lifecycle(mp.LifecycleEvent("req:1", "failed", request.observed_at, "life:1"))
    store.record_lifecycle(mp.LifecycleEvent("req:1", "failed", request.observed_at, "life:1"))

    liked = store.record(_event("like:1", "like"))
    disliked = store.record(_event("dislike:1", "dislike", observed_at=request.observed_at + timedelta(seconds=1)))
    context = store.context("john")
    assert [item["name"] for item in context.explicit_dislikes] == ["Björk"]
    assert context.explicit_likes == []
    assert context.top_requests[0]["request_count"] == 1

    correction = store.record(_event(
        "correction:1", "correction", preference=1, target_interaction_id=disliked,
        observed_at=request.observed_at + timedelta(seconds=2),
    ))
    context = store.context("john")
    assert context.explicit_dislikes == []
    assert [item["name"] for item in context.explicit_likes] == ["Björk"]

    temporary_id = store.record(_event(
        "temp:1", "temporary_exclusion", preference=-1, scope="context",
        context={"activity": "workout"}, expires_at=request.observed_at + timedelta(days=1),
    ))
    assert store.context("john", {"activity": "workout"}).temporary_exclusions
    assert not store.context("john", {"activity": "dinner"}).temporary_exclusions

    store.record(
        mp.PreferenceEvent(
            person_key="john",
            kind="retraction",
            ingestion_key="retract:1",
            observed_at=request.observed_at + timedelta(seconds=3),
            target_interaction_id=correction,
            source_platform="fixture",
            source_message_id="retract:1",
        )
    )
    restored = store.context("john")
    assert restored.explicit_likes == []
    assert [item["name"] for item in restored.explicit_dislikes] == ["Björk"]

    assert store.forget("john", interaction_id=temporary_id) == 1
    assert not store.context("john", {"activity": "workout"}).temporary_exclusions

    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn) as conn:
        forgotten = conn.execute(
            "SELECT evidence_quote, source_session_id, source_message_id, classifier, forgotten_at FROM music.preference_interactions WHERE id = %s",
            (temporary_id,),
        ).fetchone()
        assert forgotten[:4] == (None, None, None, None)
        assert forgotten[4] is not None
        assert conn.execute("SELECT count(*) FROM music.playback_lifecycle").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM music.preference_interactions WHERE interaction_kind='request'").fetchone()[0] == 1
