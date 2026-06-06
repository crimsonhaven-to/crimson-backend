# Crimson Backend

Greetings from the heart of Crimson Haven! I am Luminas Crimsonveil—your curator of all things anime. You may call me Lumi ( ^ . ^ )

This is the robust, high-performance engine powering our streaming sanctuary. It handles multi-season anime mapping, automated metadata aggregation, and multi-source stream resolution with elegance and speed.

## 🩸 Core Features

- **Multi-Season Intelligence**: Automatically maps TMDB TV shows and seasons to their corresponding AniList IDs using the Fribb dataset.
- **Unified Search**: Search across TMDB with automatic suggestions and metadata enrichment.
- **Smart Metadata**: Aggregates data from TMDB and AniList for complete info (titles, posters, episode summaries).
- **Advanced Scraping**: Multi-threaded, async scraping from various sources (AnimeKai, AnimeSuge, GogoAnime, etc.).
- **Stream Resolution**: Resolves third-party embed URLs to direct HLS/MP4 streams or ad-free proxied players.
- **Automatic Sync**: Built-in scheduler keeps the local mapping database up-to-date with upstream sources.
- **Internal Proxies**: Custom reverse proxies for providers like VidKing, Movish, and Jellyfin to bypass ads and CORS.
- **Crimson Player**: A minimal, ad-free HLS/MP4 player served directly from the backend for a seamless experience.

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **Database**: SQLite (Metadata Mapping & API Cache)
- **Networking**: HTTPX (Async)
- **Parsing**: BeautifulSoup4, Selectolax, lxml
- **Scheduling**: APScheduler
- **Containerization**: Docker & Docker Compose

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**
- **TMDB API Key**: Required for metadata and search (Legacy API Key / Read Access Token).

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ramon/crimson-backend.git
   cd crimson-backend
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   Create a `.env` file in the root:
   ```env
   TMDB_API_KEY=your_tmdb_api_key_here
   DEBUG=False
   ```

### Running the API

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`. You can explore the interactive docs at `/docs`.

---

## 📜 API Reference

### Core Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/search/anime` | Search for anime by name (TMDB-based). |
| `GET` | `/trending` | Fetch popular shows. |
| `GET` | `/catalogue` | List all mapped anime in the local DB. |
| `GET` | `/show/{tmdb_id}` | Get full show details and season list. |
| `GET` | `/season/{tmdb_id}/{num}` | Get metadata for a specific season. |
| `GET` | `/watch/{tmdb_id}/{s}/{e}` | **Primary**: Retrieve streaming links for an episode. |
| `GET` | `/anilist/{id}` | Reverse lookup (AniList ID -> TMDB ID). |

### Internal Proxies & Utilities

- **`/player`**: Renders the Crimson Player for direct HLS/MP4 streams.
- **`/vidking_proxy`**: Removes ads and pop-unders from VidKing/Videasy embeds.
- **`/movish_proxy`**: Proxies Movish streams to handle headers/CORS.
- **`/jellyfin_proxy`**: Proxies Jellyfin HLS segments for same-origin playback.
- **`/health`**: Check system status and database health.

---

## 🏗 Architecture

- **`api.py`**: Main entry point, routing, and lifecycle management.
- **`metadata_engine/`**: Handles the complex mapping between TMDB and AniList. See its [README](metadata_engine/README.md) for details.
- **`scrapers/`**: Modular providers that find video embeds on streaming sites.
- **`resolvers/`**: Tools that extract raw video links from those embeds.
- **`player.py`**: The logic for our built-in HTML5 video player.

### Extending the Engine
To add a new source, implement a new scraper in `scrapers/` (inheriting from `BaseAnimeScraper`) and, if needed, a resolver in `resolvers/` (inheriting from `BaseResolver`).

---

## 🌹 TL;DR
Lumi's FastAPI backend for Crimson Haven. It maps TMDB to AniList, scrapes multiple sources, resolves direct links, and proxies streams to keep your viewing experience pure and ad-free. Set your key, run it, and enjoy! ( ^ ▿ ^ )
