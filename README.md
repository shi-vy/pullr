# Pullr

A self-hosted, Dockerized service that automates RealDebrid torrent downloads to a local media drive. Submit magnet links via a web UI; Pullr handles everything from RealDebrid polling through file transfer.

## Quick Start

```bash
# Build and start (uses config/config.yaml for UID/GID)
make dev BRANCH=main

# Other targets
make build    # Build image only
make up       # Start containers
make down     # Stop containers
make show-config  # Print resolved build vars
```

## Configuration

Mount a `config/config.yaml` with the following keys:

```yaml
# Required
realdebrid_api_token: "<your token>"
poll_interval_seconds: 10
download_temp_path: "/downloads/tmp"
media_path: "/media"
port: 8080

# Optional
log_to_file: true
deletion_delay_hours: 3           # Hours after completion before deleting from RD (default: 3)
external_torrent_scan_interval_seconds: 15  # 0 to disable external torrent scanning
unsorted_path: "/media/unsorted"  # Override default unsorted destination

# Jellyfin mode (requires tmdb_api_key)
jellyfin_mode: false
tmdb_api_key: "<your TMDB Bearer Token>"

# File ownership on the media drive (match your SMB share user)
media_uid: 1000
media_gid: 1000
```

### Jellyfin Mode

When `jellyfin_mode: true`, files are organized into Jellyfin-compatible folder structures:

- Movies → `/media/Movies/<Title> [tmdbid-12345]/`
- TV Shows → `/media/Shows/<Show Name>/`

A TMDB ID and media type (`movie` or `tv`) can be supplied at submission time. Without metadata, files land in `/media/unsorted/` and wait in **Unsorted** state until metadata is added via the UI.

## Volume Layout

```
/config/config.yaml   # Configuration
/downloads/tmp        # Temporary download staging area
/media                # Final media destination
/logs/pullr.log       # Persistent logs
```

## Docker Compose

```yaml
services:
  pullr:
    image: pullr:latest
    container_name: pullr
    user: "1000:1000"       # Match media_uid/media_gid in config
    ports:
      - "8080:8080"
    volumes:
      - /mnt/downloads:/downloads
      - /mnt/media:/media
      - ./config:/config
      - ./logs:/logs
    restart: unless-stopped
```

The image runs as a non-root `media` user with the UID/GID baked in at build time via `MEDIA_UID`/`MEDIA_GID` build args. The Makefile reads these automatically from `config/config.yaml`.

## Torrent States

| State | Description |
|-------|-------------|
| `WAITING_FOR_REALDEBRID` | Submitted to RD; waiting for it to process/cache |
| `DOWNLOADING_ON_REALDEBRID` | RD is downloading the torrent on its side |
| `WAITING_FOR_SELECTION` | Multi-file torrent requires manual file selection |
| `AVAILABLE_FROM_REALDEBRID` | Download links ready; waiting for queue slot |
| `WAITING_IN_QUEUE` | Queued behind an active download |
| `DOWNLOADING_FROM_REALDEBRID` | Pullr is actively downloading files |
| `TRANSFERRING_TO_MEDIA_SERVER` | Files being moved to media destination |
| `WAITING_FOR_METADATA` | Download complete; awaiting TMDB metadata via UI |
| `FINISHED` | Transfer complete; scheduled for RD deletion |
| `FAILED` | Terminal error |
