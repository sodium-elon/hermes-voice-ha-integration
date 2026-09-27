-- Cluster-level prerequisite; intentionally separate from the schema migration.
-- Run once as a role with CREATEROLE. NOLOGIN prevents direct authentication.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'music_memory_app') THEN
        CREATE ROLE music_memory_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$role$;
