"""A fictional archive profile shared by the archive-routing tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from tools.ingest.archive_profile import ArchiveProfile, parse_archive_profile


EXAMPLE_PROFILE_DOC = {
    "root_name": "Archive",
    "year": "2026",
    "categories": {
        "Weddings": {
            "prefix": "WED",
            "archive_folder_name": "1. Weddings",
            "folder_id": "folder-weddings",
            "media_folder_ids": {"Pics": "pics-weddings", "Vids": "vids-weddings"},
            "media_folder_names": {"Vids": "Videos"},
            "keywords": ["ceremony", "reception", "bride", "groom"],
        },
        "Portraits": {
            "prefix": None,
            "archive_folder_name": "2. Portraits",
            "folder_id": "folder-portraits",
            "media_folder_ids": {"Pics": "pics-portraits"},
            "media_folder_names": {"Pics": "Pictures"},
            "keywords": ["headshot", "studio", "portrait"],
        },
        "Events": {
            "prefix": "EV",
            "archive_folder_name": "3. Events",
            "folder_id": "folder-events",
            "keywords": ["party", "crowd", "stage"],
        },
        "Commercial": {
            "prefix": "COM",
            "archive_folder_name": "4. Commercial",
            "folder_id": "folder-commercial",
            "media_folder_ids": {"Pics": "pics-commercial"},
            "nested_paths": {
                "Spring Launch": {"folder_id": "folder-commercial-spring", "media_folder_ids": {}},
            },
            "keywords": ["product", "brand", "launch"],
        },
    },
    "aliases": {
        "wedding": "Weddings",
        "headshots": "Portraits",
    },
}


def example_profile() -> ArchiveProfile:
    return parse_archive_profile(EXAMPLE_PROFILE_DOC)


def write_example_profile(directory: Path) -> Path:
    path = directory / "archive-profile.yaml"
    path.write_text(yaml.safe_dump(EXAMPLE_PROFILE_DOC, sort_keys=False), encoding="utf-8")
    return path
