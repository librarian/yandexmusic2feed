# YandexMusic2Feed

YandexMusic2Feed turns Yandex Music podcast albums into ordinary RSS podcast feeds
backed by a local audio archive. It is designed for self-hosting and works with
podcast clients that support standard RSS enclosures and HTTP range requests.

> [!CAUTION]
> This is an unofficial project. It is not affiliated with or endorsed by
> Yandex. The underlying API is undocumented and may change.

## Features

- One RSS endpoint per album: `/{album_id}.xml`
- Genuine episode publication dates from Yandex metadata
- Season discovery and configurable daily or weekly checks
- Automatic prefetch for selected seasons
- Lazy download when a client requests an unmirrored enclosure
- Atomic downloads, SQLite state, persistent covers and audio
- HTTP range requests for seeking and resuming
- Minimal management UI and browser audio player
- OAuth Device Flow with encrypted-in-transit, private local token storage
- Automatic refresh-token rotation, with `TOKEN` as a legacy fallback

## Quick start

```sh
cp .env.example .env
docker compose up -d --build
```

Open <http://localhost:8000/auth>, click **Start Device Flow**, then open the
displayed Yandex URL and enter the code. After authorization, credentials are
stored as `/data/oauth.json` with mode `0600`; tokens are never displayed in the
browser or application logs.

Then open <http://localhost:8000>, add an album ID, and subscribe to:

```text
http://localhost:8000/ALBUM_ID.xml
```

Set `PUBLIC_BASE_URL` to the externally reachable URL before subscribing.

## Authentication

Device Flow is recommended. It returns both access and refresh tokens and works
without a browser on the server. YandexMusic2Feed refreshes stored credentials near
expiry or after roughly 90 days. The pinned `yandex-music` library supplies the
Device Flow client; YandexMusic2Feed implements persistence and refresh rotation.

You can alternatively set `TOKEN` in the environment. Environment tokens cannot
be refreshed automatically. Stored Device Flow credentials take precedence once
authorization succeeds.

Custom OAuth applications can set `YANDEX_CLIENT_ID` and
`YANDEX_CLIENT_SECRET`. Otherwise the defaults supplied by `yandex-music` are
used consistently for authorization and refresh.

References:

- [Yandex device-code OAuth flow](https://yandex.ru/dev/id/doc/en/codes/screen-code-oauth)
- [Yandex refresh-token flow](https://yandex.ru/dev/id/doc/en/tokens/refresh-client)
- [`yandex-music` Device Flow documentation](https://github.com/MarshalX/yandex-music-api)

## Configuration

See [`.env.example`](.env.example) for all settings. By default, Compose uses
named volumes:

- `yandexmusic2feed-data` for SQLite, OAuth credentials, and covers
- `yandexmusic2feed-media` for downloaded audio

Set `DATA_VOLUME` and `MEDIA_VOLUME` to absolute host paths if bind mounts are
preferred. The container runs as `PUID:PGID`, defaulting to `1000:1000`.

The selected seasons are prefetched during synchronization. Every discovered
episode remains visible in RSS; requesting an unselected episode downloads it
on demand and then serves the local copy. `HEAD` probes do not trigger downloads.

Files are never deleted automatically. Deselecting a season only prevents future
automatic downloads from that season.

## Reverse proxy

The default service binds to `127.0.0.1:8000`. A generic Traefik overlay is
provided:

```sh
APP_HOST=podcasts.example.com \
docker compose -f compose.yaml -f compose.traefik.example.yaml up -d
```

Configure `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, and
`TRAEFIK_CERT_RESOLVER` when they differ from the defaults. The management and
OAuth pages have no built-in user authentication; expose them only on a trusted
network or protect them with reverse-proxy authentication.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATA_DIR=./data MEDIA_DIR=./media .venv/bin/uvicorn app.asgi:app --reload
```

Run tests with:

```sh
.venv/bin/python -m pytest
```

## Container publishing

Pull requests and pushes run the Python test matrix and a container build. Tags
matching `v*` publish multi-platform metadata and an attested image to:

```text
ghcr.io/OWNER/REPOSITORY
```

## License

MIT. See [LICENSE](LICENSE).
