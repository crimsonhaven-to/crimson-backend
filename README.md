# Crimson Backend

Greetings from the throne room of Crimson Haven! I am Luminas Crimsonveil—your vampire queen and curator of all things anime. You may call me Lumi ( ^ . ^ ) 

This is the robust, high-performance heart of our streaming sanctuary. We built this engine to handle the complexities of multi-season anime by mapping TMDB show data to AniList entries. It features our custom automated metadata engine, multi-source scraping, and elegant stream resolution.

## Features

- Multi-Season Intelligence: We automatically map TMDB TV shows and seasons to their corresponding AniList IDs so you never lose your way in the archives.
- Unified Search: Search across TMDB with automatic suggestions provided by our loyal familiars.
- Smart Metadata: We aggregate data from TMDB and AniList for the most complete information possible.
- Advanced Scraping: Multi-threaded scraping from various anime sources (AnimeKai, AnimeSuge, GogoAnime, etc.).
- Stream Resolution: We resolve embed URLs to direct HLS/MP4 streams where possible for a seamless viewing experience.
- Automatic Sync: A built-in scheduler to keep our mapping database up-to-date with upstream sources.
- Performant Caching: SQLite-based caching to ensure the castle remains responsive.

## Tech Stack

- Framework: FastAPI (Python)
- Database: SQLite (Metadata & Cache)
- Networking: HTTPX (Async requests)
- Parsing: BeautifulSoup4, Selectolax
- Scheduling: APScheduler
- Containerization: Docker

## Getting Started

### Prerequisites

- Python 3.10+
- TMDB API Key (Necessary for my magic to work)

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/ramon/crimson-backend.git
   cd crimson-backend
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables:
   Create a .env file in the root directory:
   ```env
   TMDB_API_KEY=your_tmdb_api_key_here
   DEBUG=False
   ```

### Running the API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

### Using Docker

```bash
docker-compose up -d
```

---

## API Documentation

Our API runs on port 8000 by default. You can view the full grimoire of endpoints via Swagger UI at /docs ( > ◡ < )

### Core Endpoints

#### GET /search/anime
Search for anime by name.
- Query Params: query_name (string, required)
- Returns: List of suggestions with TMDB and AniList IDs.

#### GET /trending
Fetch what is currently popular among my subjects.
- Query Params: limit (int, default: 10)

#### GET /show/{tmdb_id}
Get comprehensive show details, including all seasons and "extras" (OVAs/Movies).

#### GET /season/{tmdb_id}/{season_number}
Get metadata for a specific season, merging TMDB and AniList data.

#### GET /watch/{tmdb_id}/{season_number}/{episode_number}
Retrieve streaming links for a specific episode.
- Returns: List of streams (HLS, MP4, or Iframe) from various sources.

#### GET /anilist/{anilist_id}
Reverse lookup to find the corresponding TMDB ID and season number.

### Legacy/Compatibility Endpoints
We maintain these for our older scrolls and frontend versions.

- GET /info/{tmdb_id}: Merged TMDB + AniList metadata (flat structure).
- GET /watch/{anilist_id}/{episode_number}: Watch using AniList ID (redirects to canonical watch).
- GET /seasons/{anilist_id}: Get all seasons for an AniList ID.

### Health & Status
- GET /health: Returns system health, database status, and active scraper count.

---

## Architecture

- api.py: The central entry point and API routing logic.
- metadata_engine/: Handles the complex mapping between TMDB (show-centric) and AniList (release-centric) using the Fribb dataset.
- scrapers/: Modular scrapers that search and extract video page URLs from anime sites.
- resolvers/: Resolvers that take video host URLs (e.g., VidKing, VidMoly) and extract the final playable stream.

---

## TL;DR:
This is Lumi's FastAPI-based backend for Crimson Haven. It uses TMDB for IDs but maps everything to AniList for accuracy. We scrape multiple sites, resolve direct links, and cache everything in SQLite. Set your TMDB_API_KEY, run it, and enjoy your stay in my kingdom! ( ^ ▿ ^ )
