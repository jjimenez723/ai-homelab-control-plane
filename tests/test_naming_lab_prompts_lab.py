"""Draft prompt versioning: identity preserved, never overwritten in place."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.naming_lab.prompts_lab import (
    DRAFT_PROMPT_ID_PREFIX,
    PromptDraftStore,
    load_lab_registry,
    prompt_version_from_json,
    prompt_version_to_json,
    save_draft,
    starter_vnext_draft,
)
from tools.prompts.library import SEMANTIC_NAMING_V1


class SaveDraftTests(unittest.TestCase):
    def test_draft_prompt_id_is_namespaced_away_from_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PromptDraftStore(Path(tmp))
            draft = save_draft(store, SEMANTIC_NAMING_V1, notes="first draft")
            self.assertTrue(draft.prompt_id.startswith(DRAFT_PROMPT_ID_PREFIX))
            self.assertNotEqual(draft.prompt_id, SEMANTIC_NAMING_V1.prompt_id)
            self.assertEqual(draft.status, "draft")

    def test_saving_twice_never_overwrites_the_first_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PromptDraftStore(Path(tmp))
            first = save_draft(store, SEMANTIC_NAMING_V1, task_template="draft one")
            second = save_draft(store, SEMANTIC_NAMING_V1, task_template="draft two")
            self.assertNotEqual(first.version, second.version)
            # The first version, read back, is unchanged by the second save.
            reloaded_first = store.versions_for(first.prompt_id)[0]
            self.assertEqual(reloaded_first.task_template, "draft one")

    def test_production_prompt_is_never_written_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PromptDraftStore(Path(tmp))
            save_draft(store, SEMANTIC_NAMING_V1, task_template="edited")
            self.assertEqual(SEMANTIC_NAMING_V1.task_template, SEMANTIC_NAMING_V1.task_template)
            self.assertNotIn(SEMANTIC_NAMING_V1.prompt_id, [d.prompt_id for d in store.all()])

    def test_iterating_on_an_existing_draft_keeps_its_prompt_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PromptDraftStore(Path(tmp))
            first = save_draft(store, SEMANTIC_NAMING_V1, task_template="v1")
            second = save_draft(store, first, task_template="v2")
            self.assertEqual(first.prompt_id, second.prompt_id)
            self.assertNotEqual(first.version, second.version)

    def test_content_fingerprint_changes_with_a_different_version_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PromptDraftStore(Path(tmp))
            first = save_draft(store, SEMANTIC_NAMING_V1, task_template="v1")
            second = save_draft(store, first, task_template="v2")
            self.assertNotEqual(first.content_fingerprint, second.content_fingerprint)


class PromptVersionJsonRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        restored = prompt_version_from_json(prompt_version_to_json(SEMANTIC_NAMING_V1))
        self.assertEqual(restored, SEMANTIC_NAMING_V1)


class StarterVnextDraftTests(unittest.TestCase):
    def test_never_asks_the_model_to_emit_a_date(self):
        starter = starter_vnext_draft(SEMANTIC_NAMING_V1)
        self.assertIn("Never include a date", starter.output_schema)
        self.assertEqual(starter.status, "draft")


class LoadLabRegistryTests(unittest.TestCase):
    def test_includes_both_production_and_draft_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PromptDraftStore(Path(tmp))
            draft = save_draft(store, SEMANTIC_NAMING_V1, task_template="v1")
            registry = load_lab_registry(store)
            self.assertEqual(registry.get(SEMANTIC_NAMING_V1.prompt_id, SEMANTIC_NAMING_V1.version), SEMANTIC_NAMING_V1)
            self.assertEqual(registry.get(draft.prompt_id, draft.version), draft)


if __name__ == "__main__":
    unittest.main()
