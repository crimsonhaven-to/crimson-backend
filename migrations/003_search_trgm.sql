-- 003_search_trgm.sql
--
-- Trigram indexes on the anime_entries titles, so /search/anime can be answered
-- from the local catalogue instead of a round trip to TMDB.
--
-- Why an index is needed
-- ----------------------
-- The search is a plain `ILIKE '%q%'` across title_romaji, title_english and
-- title_native. A leading wildcard defeats a btree index, so without pg_trgm
-- every keystroke is a sequential scan of the whole catalogue. A GIN index with
-- gin_trgm_ops is the index type that does serve a leading-wildcard ILIKE.
--
-- Why the index is optional, not required
-- ---------------------------------------
-- Nothing in the query changes when it is absent, only the speed. Result order
-- is a plain CASE expression rather than pg_trgm's similarity(), so no code path
-- asks whether the extension exists and there is no second query to keep in
-- sync. A database where CREATE EXTENSION is refused runs the same search, just
-- more slowly.
--
-- Why CREATE EXTENSION is wrapped in an exception handler
-- -------------------------------------------------------
-- core/migrations.py applies the whole pending batch in ONE transaction, so a
-- statement that raises takes every other pending migration with it, on this
-- boot and every future one. pg_trgm is a trusted extension and the app role
-- owns its database (deploy/postgres-ha/init-app-db.sh), so on Postgres 13+ this
-- is expected to succeed. "Expected to" is not good enough when the blast radius
-- is every migration that comes after, hence the DO block: a refusal becomes a
-- warning, and the indexes below are guarded on the extension actually being
-- present rather than on the CREATE having appeared to work.
--
-- CREATE INDEX CONCURRENTLY cannot be used, for the same single-transaction
-- reason. A plain CREATE INDEX takes a brief ACCESS EXCLUSIVE lock on
-- anime_entries; at catalogue scale that is sub-second, and it happens during
-- boot before the replica serves traffic.

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE WARNING 'pg_trgm not created (insufficient privilege): /search/anime still works, without its index';
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        CREATE INDEX IF NOT EXISTS idx_anime_entries_romaji_trgm
            ON anime_entries USING GIN (title_romaji gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_anime_entries_english_trgm
            ON anime_entries USING GIN (title_english gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_anime_entries_native_trgm
            ON anime_entries USING GIN (title_native gin_trgm_ops);
    ELSE
        RAISE WARNING 'pg_trgm absent: skipping the /search/anime trigram indexes';
    END IF;
END
$$;
