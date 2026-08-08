-- 001_extras_tmdb_movie_id.sql
--
-- Carry the TMDB *movie* id of an anime film alongside its AniList entry.
--
-- Why
-- ---
-- The Fribb dataset gives a film TMDB tracks in its own right a
-- `themoviedb_id: {"movie": [1014505]}` and no `tv` key. The mapping build read
-- only the `tv` key, so every one of those entries (1280 of them, Overlord: The
-- Sacred Kingdom among them) was dropped on the floor: no tmdb_extras row, and
-- not even an anime_entries row. They are now kept -- attached to their parent
-- show when their tvdb_id identifies one, standalone otherwise -- and this
-- column is what makes them playable, by routing them through the TMDB movie
-- watch path instead of a season/episode URL that does not exist for a film.
--
-- Null for every other entry, which is the overwhelming majority. Populated by
-- the next mapping resync (metadata_engine.db_handler), which rebuilds both
-- tables wholesale; until then the column simply reads null and the extras
-- behave exactly as they did before.

ALTER TABLE anime_entries ADD COLUMN IF NOT EXISTS tmdb_movie_id INTEGER;
ALTER TABLE tmdb_extras   ADD COLUMN IF NOT EXISTS tmdb_movie_id INTEGER;
