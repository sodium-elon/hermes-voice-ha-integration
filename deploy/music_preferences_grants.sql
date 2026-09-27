-- Run with psql -v app_role=music_memory_app after creating the NOLOGIN role.
-- The runtime connects as an authenticated DB user and SET ROLEs to this role.
GRANT USAGE ON SCHEMA music TO :"app_role";
GRANT SELECT ON music.artist_names TO :"app_role";
GRANT SELECT, INSERT, UPDATE ON music.preference_entities TO :"app_role";
GRANT SELECT, INSERT, UPDATE ON music.preference_interactions TO :"app_role";
GRANT SELECT, INSERT ON music.playback_lifecycle TO :"app_role";
GRANT SELECT, INSERT, UPDATE ON music.knowledge_items TO :"app_role";
GRANT SELECT ON music.effective_preference_interactions TO :"app_role";
GRANT SELECT ON music.request_frequency TO :"app_role";
GRANT USAGE, SELECT ON SEQUENCE
    music.preference_entities_id_seq,
    music.preference_interactions_id_seq,
    music.playback_lifecycle_id_seq,
    music.knowledge_items_id_seq
TO :"app_role";
