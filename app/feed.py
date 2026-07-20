from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET


ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def _text(parent, tag: str, value: object | None, attributes=None):
    element = ET.SubElement(parent, tag, attributes or {})
    if value is not None:
        element.text = str(value)
    return element


def duration_text(duration_ms: int | None) -> str:
    seconds = int((duration_ms or 0) / 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def build_feed(album, episodes, base_url: str) -> bytes:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    feed_url = f"{base_url}/{album['id']}.xml"
    _text(channel, "title", album["title"])
    _text(channel, "link", f"{base_url}/albums/{album['id']}")
    _text(channel, "description", album["description"] or album["title"])
    _text(channel, "language", "ru")
    _text(channel, f"{{{ITUNES}}}author", album["artists"])
    _text(channel, f"{{{ITUNES}}}explicit", "false")
    _text(channel, f"{{{ATOM}}}link", None, {"href": feed_url, "rel": "self", "type": "application/rss+xml"})
    if album["cover_path"] and Path(album["cover_path"]).exists():
        cover_url = f"{base_url}/covers/{album['id']}.jpg"
        image = ET.SubElement(channel, "image")
        _text(image, "url", cover_url)
        _text(image, "title", album["title"])
        _text(image, "link", f"{base_url}/albums/{album['id']}")
        _text(channel, f"{{{ITUNES}}}image", None, {"href": cover_url})
    for episode in episodes:
        path = Path(episode["local_path"]) if episode["local_path"] else None
        item = ET.SubElement(channel, "item")
        _text(item, "title", episode["title"])
        _text(item, "description", episode["title"])
        _text(item, "guid", f"yandex-music:track:{episode['track_id']}", {"isPermaLink": "false"})
        media_url = f"{base_url}/media/{episode['track_id']}.mp3"
        media_size = episode["media_size"] or 0
        if path and path.exists():
            media_size = path.stat().st_size
        _text(item, "enclosure", None, {"url": media_url, "length": str(media_size), "type": "audio/mpeg"})
        if episode["source_published_at"]:
            published = datetime.fromisoformat(episode["source_published_at"])
            _text(item, "pubDate", format_datetime(published))
        _text(item, f"{{{ITUNES}}}duration", duration_text(episode["duration_ms"]))
        _text(item, f"{{{ITUNES}}}season", episode["season_number"])
        _text(item, f"{{{ITUNES}}}episode", episode["episode_number"])
        _text(item, f"{{{ITUNES}}}episodeType", "full")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
