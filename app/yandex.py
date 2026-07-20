from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from yandex_music import Album, Client


@dataclass(frozen=True)
class EpisodeMetadata:
    track_id: str
    season: int
    episode: int
    title: str
    artists: str
    duration_ms: int | None
    available: bool
    source_published_at: str | None


@dataclass(frozen=True)
class AlbumMetadata:
    album_id: int
    title: str
    artists: str
    description: str
    cover_url: str | None
    episodes: list[EpisodeMetadata]


class YandexGateway:
    def __init__(self, token: str | Callable[[], str], bitrate_kbps: int = 192):
        self.token = token
        self.bitrate_kbps = bitrate_kbps

    def _client(self) -> Client:
        token = self.token() if callable(self.token) else self.token
        if not token:
            raise RuntimeError("TOKEN is not configured")
        return Client(token).init()

    def album(self, album_id: int) -> AlbumMetadata:
        client = self._client()
        raw = client._request.get(f"{client.base_url}/albums/{album_id}/with-tracks")
        album = Album.de_json(raw, client)
        source_dates = {
            str(track["id"]): track.get("pubDate")
            for volume in raw.get("volumes", [])
            for track in volume
        }
        episodes: list[EpisodeMetadata] = []
        for volume_index, volume in enumerate(album.volumes, 1):
            for fallback_index, track in enumerate(reversed(volume), 1):
                position = track.albums[0].track_position if track.albums else None
                season = int(getattr(position, "volume", None) or volume_index or 1)
                episode = int(getattr(position, "index", None) or fallback_index)
                artists = ", ".join(a.name for a in (track.artists or []))
                episodes.append(
                    EpisodeMetadata(
                        track_id=str(track.id),
                        season=season,
                        episode=episode,
                        title=track.title,
                        artists=artists,
                        duration_ms=track.duration_ms,
                        available=bool(track.available),
                        source_published_at=source_dates.get(str(track.id)),
                    )
                )
        cover_url = None
        if album.cover_uri:
            cover_url = "https://" + album.cover_uri.replace("%%", "600x600").lstrip("/")
        description = (
            getattr(album, "description", None)
            or getattr(album, "short_description", None)
            or ""
        )
        return AlbumMetadata(
            album_id=int(album.id),
            title=album.title,
            artists=", ".join(a.name for a in (album.artists or [])),
            description=description,
            cover_url=cover_url,
            episodes=episodes,
        )

    def download_track(self, track_id: str, destination: Path) -> None:
        tracks = self._client().tracks([track_id])
        if not tracks:
            raise RuntimeError(f"Yandex track {track_id} was not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tracks[0].download(
            str(destination), codec="mp3", bitrate_in_kbps=self.bitrate_kbps
        )

    @staticmethod
    def download_cover(url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            destination.write_bytes(response.content)
