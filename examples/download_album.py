"""Minimal direct-download example using the same underlying library."""

import argparse
import os
from pathlib import Path

from yandex_music import Client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("album_id", type=int)
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    args = parser.parse_args()

    token = os.environ.get("TOKEN")
    if not token:
        raise SystemExit("Set TOKEN before running this example")
    album = Client(token).init().albums_with_tracks(args.album_id)
    args.output.mkdir(parents=True, exist_ok=True)
    for volume in album.volumes:
        for track in volume:
            position = track.albums[0].track_position
            destination = args.output / f"{position.volume:02d}-{position.index:02d}-{track.id}.mp3"
            track.download(str(destination))


if __name__ == "__main__":
    main()

