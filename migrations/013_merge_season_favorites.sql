-- 013_merge_season_favorites.sql
--
-- Every season of a multi-season anime is its own AniList entry, so a list could
-- hold Overlord and Overlord IV as two cards that open the same page. Favorites
-- are now stored under the show's first season (account_engine.library.
-- as_show_favorite); this moves the rows saved before that.
--
-- Within one list, rows that land on the same show collapse into one, keeping the
-- earliest added_at so the card stays where the list sorted it. The surviving row
-- takes the first season's title. Manga, films and extras are untouched: they are
-- not in tmdb_seasons.

CREATE TEMP TABLE season_favorite_moves ON COMMIT DROP AS
WITH first_season AS (
    SELECT DISTINCT ON (s.anilist_id)
           s.anilist_id AS from_id,
           c.anilist_id AS to_id,
           COALESCE(e.title_english, e.title_romaji, e.title_native) AS title
    FROM tmdb_seasons s
    JOIN tmdb_seasons c ON c.tmdb_id = s.tmdb_id
    JOIN anime_entries e ON e.anilist_id = c.anilist_id
    ORDER BY s.anilist_id, s.tmdb_id, c.season_number = 0, c.season_number, c.anilist_id
)
SELECT f.user_id, f.list_name, f.item_key, f.added_at,
       k.to_id, 'anilist:' || k.to_id AS to_key, k.title
FROM favorites f
JOIN first_season k ON k.from_id = f.anilist_id
WHERE f.item_key = 'anilist:' || f.anilist_id
  AND k.to_id <> f.anilist_id;

-- One survivor per (user, list, show): the row already on the show's key if there
-- is one, else the earliest moved row.
CREATE TEMP TABLE season_favorite_groups ON COMMIT DROP AS
SELECT user_id, list_name, to_key, to_id, MIN(title) AS title, MIN(added_at) AS added_at
FROM season_favorite_moves
GROUP BY user_id, list_name, to_key, to_id;

UPDATE favorites f
SET added_at = LEAST(f.added_at, g.added_at)
FROM season_favorite_groups g
WHERE f.user_id = g.user_id AND f.list_name = g.list_name AND f.item_key = g.to_key;

DELETE FROM season_favorite_groups g
USING favorites f
WHERE f.user_id = g.user_id AND f.list_name = g.list_name AND f.item_key = g.to_key;

-- Groups left have no row on the show's key yet: promote the earliest moved row.
UPDATE favorites f
SET item_key = g.to_key,
    anilist_id = g.to_id,
    title = COALESCE(g.title, f.title)
FROM (
    SELECT DISTINCT ON (m.user_id, m.list_name, m.to_key)
           m.user_id, m.list_name, m.item_key, m.to_key, m.to_id, g.title
    FROM season_favorite_moves m
    JOIN season_favorite_groups g USING (user_id, list_name, to_key)
    ORDER BY m.user_id, m.list_name, m.to_key, m.added_at, m.item_key
) g
WHERE f.user_id = g.user_id AND f.list_name = g.list_name AND f.item_key = g.item_key;

-- Every other moved row is now a duplicate of a row on the show's key.
DELETE FROM favorites f
USING season_favorite_moves m
WHERE f.user_id = m.user_id AND f.list_name = m.list_name AND f.item_key = m.item_key;
