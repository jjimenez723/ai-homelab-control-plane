"""Validator for templates/backend-profiles/ (Angel V1 completion, workstream F).

Loads every profile with PyYAML and checks required keys, stable ids against
index.yaml, and that every reference (in profiles and in
reference-boilerplates.yaml) carries both a URL and an accessed/verified
date. This is a schema/shape guard, not a fact-checker: it cannot verify
that a cited price or GA status is still true, only that the document is
structurally honest about having a source at all.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "templates" / "backend-profiles"
INDEX_PATH = PROFILES_DIR / "index.yaml"
BOILERPLATES_PATH = PROFILES_DIR / "reference-boilerplates.yaml"

# Ids the app-spec workstream depends on; must remain exactly this set.
EXPECTED_PROFILE_IDS = {
    "social-behavior-change",
    "nursing-shift-productivity",
    "single-player-game",
    "multiplayer-game",
}

REQUIRED_TOP_LEVEL_KEYS = {
    "profile_id",
    "version",
    "summary",
    "fit",
    "services",
    "data_model_guidance",
    "security_rules_posture",
    "offline",
    "realtime",
    "notifications",
    "storage_media",
    "privacy_compliance",
    "analytics",
    "testing_emulators",
    "deployment",
    "observability",
    "known_pitfalls",
    "references",
}

REQUIRED_SERVICE_KEYS = {"chosen", "alternatives", "rationale", "cost_driver", "quota_watch_outs"}


def _load_yaml(path: Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _profile_paths() -> list[Path]:
    return sorted(
        p for p in PROFILES_DIR.glob("*.yaml")
        if p.name not in {"index.yaml", "reference-boilerplates.yaml"}
    )


class IndexTests(unittest.TestCase):
    def test_index_exists_and_loads(self):
        self.assertTrue(INDEX_PATH.is_file(), f"missing {INDEX_PATH}")
        data = _load_yaml(INDEX_PATH)
        self.assertIsInstance(data, dict)
        self.assertIn("profiles", data)

    def test_index_lists_exactly_the_expected_stable_ids(self):
        data = _load_yaml(INDEX_PATH)
        ids = {entry["id"] for entry in data["profiles"]}
        self.assertEqual(ids, EXPECTED_PROFILE_IDS)

    def test_index_entries_have_file_and_title(self):
        data = _load_yaml(INDEX_PATH)
        for entry in data["profiles"]:
            with self.subTest(id=entry.get("id")):
                self.assertIn("file", entry)
                self.assertIn("title", entry)
                profile_file = PROFILES_DIR / entry["file"]
                self.assertTrue(profile_file.is_file(), f"{profile_file} referenced by index.yaml does not exist")

    def test_profile_files_on_disk_match_index_exactly(self):
        """Every profile file is indexed, and there are no orphan/extra profiles."""
        data = _load_yaml(INDEX_PATH)
        indexed_files = {entry["file"] for entry in data["profiles"]}
        on_disk = {p.name for p in _profile_paths()}
        self.assertEqual(indexed_files, on_disk)


class ProfileShapeTests(unittest.TestCase):
    def test_at_least_four_profiles_exist(self):
        self.assertGreaterEqual(len(_profile_paths()), 4)

    def test_every_profile_loads_as_a_mapping(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                self.assertIsInstance(data, dict, f"{path.name} did not parse to a mapping")

    def test_every_profile_has_all_required_top_level_keys(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                missing = REQUIRED_TOP_LEVEL_KEYS - set(data.keys())
                self.assertFalse(missing, f"{path.name} missing keys: {sorted(missing)}")

    def test_profile_id_matches_filename_stem(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                self.assertEqual(data["profile_id"], path.stem)

    def test_profile_id_is_one_of_the_stable_ids(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                self.assertIn(data["profile_id"], EXPECTED_PROFILE_IDS)

    def test_version_is_a_non_empty_string(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                version = data["version"]
                self.assertIsInstance(version, str)
                self.assertTrue(version.strip())

    def test_services_is_a_non_empty_mapping_with_required_subkeys(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                services = data["services"]
                self.assertIsInstance(services, dict)
                self.assertGreater(len(services), 0, f"{path.name} has no services entries")
                for capability, entry in services.items():
                    with self.subTest(profile=path.name, capability=capability):
                        self.assertIsInstance(entry, dict)
                        missing = REQUIRED_SERVICE_KEYS - set(entry.keys())
                        self.assertFalse(
                            missing,
                            f"{path.name} service '{capability}' missing keys: {sorted(missing)}",
                        )

    def test_known_pitfalls_is_a_non_empty_list(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                pitfalls = data["known_pitfalls"]
                self.assertIsInstance(pitfalls, list)
                self.assertGreater(len(pitfalls), 0, f"{path.name} lists no known_pitfalls")

    def test_fit_has_application_traits_and_scale_assumptions(self):
        for path in _profile_paths():
            with self.subTest(profile=path.name):
                data = _load_yaml(path)
                fit = data["fit"]
                self.assertIsInstance(fit, dict)
                self.assertIn("application_traits", fit)
                self.assertIn("scale_assumptions", fit)
                self.assertIsInstance(fit["application_traits"], list)
                self.assertGreater(len(fit["application_traits"]), 0)


class ReferenceSourcingTests(unittest.TestCase):
    """Every claim needs a URL and a date it was checked -- no bare assertions."""

    def _assert_valid_reference_list(self, references, *, context: str):
        self.assertIsInstance(references, list, f"{context}: references is not a list")
        self.assertGreater(len(references), 0, f"{context}: no references at all")
        for ref in references:
            with self.subTest(context=context, ref=ref):
                self.assertIsInstance(ref, dict)
                url = ref.get("url")
                accessed = ref.get("accessed")
                self.assertTrue(url, f"{context}: reference missing url: {ref}")
                self.assertTrue(
                    isinstance(url, str) and url.startswith("http"),
                    f"{context}: reference url is not an http(s) URL: {ref}",
                )
                self.assertTrue(accessed, f"{context}: reference missing accessed date: {ref}")

    def test_every_profile_reference_has_url_and_accessed_date(self):
        for path in _profile_paths():
            data = _load_yaml(path)
            self._assert_valid_reference_list(data["references"], context=path.name)

    def test_reference_boilerplates_file_exists_and_loads(self):
        self.assertTrue(BOILERPLATES_PATH.is_file())
        data = _load_yaml(BOILERPLATES_PATH)
        self.assertIsInstance(data, dict)
        self.assertIn("boilerplates", data)
        self.assertIsInstance(data["boilerplates"], list)
        self.assertGreater(len(data["boilerplates"]), 0)

    def test_every_boilerplate_entry_has_url_license_and_verified_date(self):
        data = _load_yaml(BOILERPLATES_PATH)
        for entry in data["boilerplates"]:
            with self.subTest(name=entry.get("name")):
                self.assertIn("url", entry)
                self.assertTrue(entry["url"].startswith("http"))
                self.assertIn("license", entry)
                self.assertTrue(entry["license"])
                self.assertIn("last_commit_verified", entry)
                self.assertTrue(entry["last_commit_verified"])
                self.assertIn("reuse", entry)
                self.assertIn("do_not_copy_blindly", entry)

    def test_no_boilerplate_entry_duplicated_by_url(self):
        data = _load_yaml(BOILERPLATES_PATH)
        urls = [entry["url"] for entry in data["boilerplates"]]
        self.assertEqual(len(urls), len(set(urls)), "duplicate boilerplate URL entries")


class ReadmeExistsTests(unittest.TestCase):
    def test_backend_profiles_readme_exists(self):
        self.assertTrue((PROFILES_DIR / "README.md").is_file())

    def test_backend_baselines_doc_exists(self):
        self.assertTrue((REPO_ROOT / "docs" / "27-backend-baselines.md").is_file())


if __name__ == "__main__":
    unittest.main()
