# TMDB API Integration Documentation

Pullr integrates with [The Movie Database (TMDB)](https://www.themoviedb.org/) to provide metadata tagging for media files. This allows Pullr to identify shows and movies correctly for downstream services like Jellyfin or Plex.

## 1. Authentication
Pullr uses **Bearer Token** authentication. The API key is provided in the `config.yaml` as `tmdb_api_key`.

- **Base URL:** `https://api.themoviedb.org/3`
- **Headers:**
  ```http
  Authorization: Bearer <TMDB_API_KEY>
  Accept: application/json
  ```

## 2. Core Features

### A. Search Suggestions (`/search/multi`)
When a user clicks the 🔍 icon in the dashboard, Pullr performs a "multi-search" which returns both movies and TV shows.

- **Endpoint:** `GET /search/multi?query=<cleaned_query>`
- **Fields Used:**
  - `id`: Used as the primary TMDB identifier.
  - `media_type`: Distinguishes between `movie` and `tv`.
  - `poster_path`: Used to display thumbnails in the UI (`https://image.tmdb.org/t/p/w92<path>`).
  - `release_date` / `first_air_date`: Used to extract the release year.

### B. Metadata Fetching (`/movie/{id}` or `/tv/{id}`)
Once a torrent is added with a TMDB ID, Pullr fetches the "Official Title" to ensure folder names are clean (e.g., "Slow Horses" instead of "Slow.Horses.S01.1080p...").

- **Movie Endpoint:** `GET /movie/{tmdb_id}` (Extracts `title`)
- **TV Endpoint:** `GET /tv/{tmdb_id}` (Extracts `name`)

### C. Query Cleaning Logic
To improve search accuracy from messy magnet links or filenames, Pullr applies a regex-based cleaning process before sending the query to TMDB.

**Removed Patterns:**
- Quality tags: `1080p`, `720p`, `2160p`, `4k`
- Format tags: `WEB-DL`, `BluRay`, `BRRip`, `HEVC`, `x264`, `H.265`
- Episode/Season markers: `S01E01`, `S01`
- Years: `(2024)`, `[2023]`

**Example Transformation:**
- **Input:** `Invincible.S04E01.MAKING.THE.WORLD.A.BETTER.PLACE.1080p.AMZN.WEB-DL...`
- **Cleaned:** `Invincible`

## 3. Caching
Pullr maintains a local cache in `/config/tv_cache.json` for TV shows. This maps cleaned show names to their verified TMDB IDs to speed up future lookups of the same series.

## 4. UI Implementation

### A. Search Term Extraction from Magnets
When the user clicks the 🔍 icon while the TMDB ID field is empty, Pullr attempts to automatically derive a search term from the magnet link provided in the main input field.

**Process:**
1. **Parameter Extraction:** It looks for the `dn=` (Display Name) parameter within the magnet URI using a regular expression: `dn=([^&]+)`.
2. **Decoding:** The captured string is URL-decoded (e.g., `%20` becomes a space).
3. **Normalization:** Plus signs (`+`) are replaced with spaces to handle different magnet encoding styles.
4. **Pre-filling:** The resulting string is automatically populated into the TMDB ID input field, allowing the user to see, verify, and edit the query before the actual search is triggered.

**Example:**
- **Magnet:** `magnet:?xt=urn:btih:...&dn=Invincible.S04E01.1080p...&tr=...`
- **Extracted Term:** `Invincible.S04E01.1080p...`
- **Refinement:** This extracted term is then sent to the backend where it undergoes the [Query Cleaning Logic](#c-query-cleaning-logic) to become `Invincible`.

### B. Visual Suggestions
Suggestions are rendered with:
