BEGIN;

-- Additive only: music.artist_names is an established resolver table and is
-- deliberately neither altered nor recreated here.
CREATE TABLE IF NOT EXISTS music.preference_entities (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type text NOT NULL CHECK (entity_type IN ('artist', 'song', 'album', 'genre', 'playlist', 'context')),
    name text NOT NULL,
    name_norm text NOT NULL,
    artist_name text,
    artist_name_norm text NOT NULL DEFAULT '',
    canonical_artist_name text,
    external_ids jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (entity_type, name_norm, artist_name_norm)
);

CREATE TABLE IF NOT EXISTS music.preference_interactions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_key text NOT NULL,
    interaction_kind text NOT NULL CHECK (interaction_kind IN (
        'request', 'like', 'dislike', 'skip', 'correction', 'retraction',
        'recommendation_response', 'temporary_exclusion'
    )),
    entity_id bigint REFERENCES music.preference_entities(id),
    preference smallint CHECK (preference IN (-1, 0, 1)),
    explicit boolean NOT NULL DEFAULT true,
    scope text NOT NULL DEFAULT 'durable' CHECK (scope IN ('durable', 'context', 'session')),
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at timestamptz,
    target_interaction_id bigint REFERENCES music.preference_interactions(id),
    source_platform text,
    source_session_id text,
    source_message_id text,
    source_request_id text,
    observed_at timestamptz NOT NULL,
    ingestion_key text NOT NULL UNIQUE,
    evidence_quote text,
    classifier jsonb,
    forgotten_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((interaction_kind IN ('correction', 'retraction')) = (target_interaction_id IS NOT NULL)),
    CHECK (interaction_kind NOT IN ('like', 'dislike') OR preference IN (-1, 1)),
    CHECK (interaction_kind <> 'temporary_exclusion' OR (preference = -1 AND scope <> 'durable')),
    CHECK (scope = 'durable' OR expires_at IS NOT NULL),
    CHECK (entity_id IS NOT NULL OR interaction_kind = 'retraction')
);

CREATE INDEX IF NOT EXISTS preference_interactions_person_time_idx
    ON music.preference_interactions (person_key, observed_at DESC);
CREATE INDEX IF NOT EXISTS preference_interactions_entity_time_idx
    ON music.preference_interactions (entity_id, observed_at DESC)
    WHERE forgotten_at IS NULL;
CREATE INDEX IF NOT EXISTS preference_interactions_target_idx
    ON music.preference_interactions (target_interaction_id)
    WHERE target_interaction_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS music.playback_lifecycle (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_interaction_id bigint NOT NULL REFERENCES music.preference_interactions(id),
    status text NOT NULL CHECK (status IN ('requested', 'resolved', 'dispatched', 'confirmed', 'failed')),
    observed_at timestamptz NOT NULL,
    ingestion_key text NOT NULL UNIQUE,
    source_platform text,
    source_session_id text,
    source_message_id text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS playback_lifecycle_request_time_idx
    ON music.playback_lifecycle (request_interaction_id, observed_at DESC);

-- Sourced catalog facts and assistant recommendations are intentionally not
-- preference evidence. They may refer to the same canonical entity.
CREATE TABLE IF NOT EXISTS music.knowledge_items (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_kind text NOT NULL CHECK (item_kind IN ('fact', 'recommendation')),
    entity_id bigint REFERENCES music.preference_entities(id),
    statement text NOT NULL,
    source_urls text[] NOT NULL DEFAULT '{}',
    generated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    ingestion_key text NOT NULL UNIQUE
);

CREATE OR REPLACE VIEW music.effective_preference_interactions AS
SELECT i.*
FROM music.preference_interactions AS i
WHERE i.forgotten_at IS NULL
  AND (i.expires_at IS NULL OR i.expires_at > now())
  AND NOT EXISTS (
      SELECT 1
      FROM music.preference_interactions AS invalidator
      WHERE invalidator.target_interaction_id = i.id
        AND invalidator.interaction_kind IN ('correction', 'retraction')
        AND invalidator.forgotten_at IS NULL
        AND NOT EXISTS (
            SELECT 1
            FROM music.preference_interactions AS canceller
            WHERE canceller.target_interaction_id = invalidator.id
              AND canceller.interaction_kind IN ('correction', 'retraction')
              AND canceller.forgotten_at IS NULL
        )
  );

CREATE OR REPLACE VIEW music.request_frequency AS
SELECT i.person_key, i.entity_id, count(*)::bigint AS request_count,
       max(i.observed_at) AS last_requested_at
FROM music.effective_preference_interactions AS i
WHERE i.interaction_kind = 'request'
GROUP BY i.person_key, i.entity_id;

COMMIT;
