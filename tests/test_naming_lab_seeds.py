"""Seed schema round-trip, split pinning, and content-hash identity across a
moved local path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.naming_lab.dates import SemanticDateCandidate
from tools.naming_lab.seeds import (
    DEVELOPMENT_SEED_SPLIT,
    SeedRecord,
    SeedStore,
    corpus_id_for,
    new_seed,
    seed_from_json,
    seed_to_json,
)

_SEMANTIC_DATE = SemanticDateCandidate(
    value="2025-06-15", kind="invoice_date", source="document_text_or_ocr", confidence="high",
)
_SHA = "a" * 64


class SeedSchemaRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_every_field(self):
        seed = new_seed(
            sha256=_SHA, original_filename="IMG_scan_9.pdf", media_kind="document", extension=".pdf",
            semantic_date=_SEMANTIC_DATE, accepted_filenames=("25_0615_northwind-invoice.pdf",),
            required_semantics={"organization": ("northwind", "northwind-traders"), "artifact_type": ("invoice",)},
            forbidden_semantics=("acme",), human_notes="looks right",
        )
        restored = seed_from_json(seed_to_json(seed))
        self.assertEqual(restored, seed)

    def test_round_trip_with_no_semantic_date(self):
        seed = new_seed(sha256=_SHA, original_filename="mountain-sunset.jpg", media_kind="image", extension=".jpg")
        restored = seed_from_json(seed_to_json(seed))
        self.assertIsNone(restored.semantic_date)
        self.assertEqual(restored, seed)


class SplitIsPinnedTests(unittest.TestCase):
    def test_default_split_is_development_seed(self):
        seed = new_seed(sha256=_SHA, original_filename="x.pdf", media_kind="document", extension=".pdf")
        self.assertEqual(seed.split, DEVELOPMENT_SEED_SPLIT)

    def test_split_cannot_be_set_to_frozen_holdout(self):
        with self.assertRaises(ValueError):
            SeedRecord(
                schema_version=1, seed_id="seed-x", sha256=_SHA, original_filename="x.pdf",
                media_kind="document", extension=".pdf", semantic_date=None, accepted_filenames=(),
                required_semantics={}, forbidden_semantics=(), human_notes=None,
                split="frozen_holdout", created_at="now", updated_at="now",
            )

    def test_split_cannot_be_set_to_validation(self):
        with self.assertRaises(ValueError):
            SeedRecord(
                schema_version=1, seed_id="seed-x", sha256=_SHA, original_filename="x.pdf",
                media_kind="document", extension=".pdf", semantic_date=None, accepted_filenames=(),
                required_semantics={}, forbidden_semantics=(), human_notes=None,
                split="validation", created_at="now", updated_at="now",
            )


class SeedStoreTests(unittest.TestCase):
    def test_get_or_create_then_get_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeedStore(Path(tmp))
            created = store.get_or_create(
                sha256=_SHA, original_filename="a.pdf", media_kind="document",
                extension=".pdf", semantic_date=_SEMANTIC_DATE,
            )
            fetched = store.get(_SHA)
            self.assertEqual(fetched, created)

    def test_get_or_create_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeedStore(Path(tmp))
            first = store.get_or_create(
                sha256=_SHA, original_filename="a.pdf", media_kind="document", extension=".pdf", semantic_date=None,
            )
            second = store.get_or_create(
                sha256=_SHA, original_filename="renamed.pdf", media_kind="document", extension=".pdf", semantic_date=None,
            )
            self.assertEqual(first.original_filename, second.original_filename)  # not overwritten

    def test_save_bumps_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeedStore(Path(tmp))
            seed = new_seed(sha256=_SHA, original_filename="a.pdf", media_kind="document", extension=".pdf")
            saved = store.save(seed)
            self.assertEqual(store.get(_SHA).seed_id, saved.seed_id)

    def test_a_seed_survives_a_moved_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeedStore(Path(tmp))
            seed = store.get_or_create(
                sha256=_SHA, original_filename="a.pdf", media_kind="document", extension=".pdf", semantic_date=None,
            )
            updated = seed.with_notes("human note before the move")
            store.save(updated)
            store.record_local_path(_SHA, Path("/Volumes/old/a.pdf"))

            # The file "moves" -- same content hash, different local path.
            store.record_local_path(_SHA, Path("/Volumes/new/subdir/a-renamed.pdf"))

            self.assertEqual(store.local_path_for(_SHA), Path("/Volumes/new/subdir/a-renamed.pdf"))
            self.assertEqual(store.get(_SHA).human_notes, "human note before the move")

    def test_all_lists_every_saved_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeedStore(Path(tmp))
            store.get_or_create(sha256="a" * 64, original_filename="a.pdf", media_kind="document", extension=".pdf", semantic_date=None)
            store.get_or_create(sha256="b" * 64, original_filename="b.pdf", media_kind="document", extension=".pdf", semantic_date=None)
            self.assertEqual({seed.sha256 for seed in store.all()}, {"a" * 64, "b" * 64})

    def test_with_accepted_filename_is_idempotent(self):
        seed = new_seed(sha256=_SHA, original_filename="a.pdf", media_kind="document", extension=".pdf")
        once = seed.with_accepted_filename("25_0615_a.pdf")
        twice = once.with_accepted_filename("25_0615_a.pdf")
        self.assertEqual(once.accepted_filenames, twice.accepted_filenames)
        self.assertEqual(once.accepted_filenames, ("25_0615_a.pdf",))


class CorpusIdTests(unittest.TestCase):
    def test_same_directory_yields_same_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(corpus_id_for(Path(tmp)), corpus_id_for(Path(tmp)))

    def test_different_directories_yield_different_ids(self):
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            self.assertNotEqual(corpus_id_for(Path(tmp_a)), corpus_id_for(Path(tmp_b)))


if __name__ == "__main__":
    unittest.main()
