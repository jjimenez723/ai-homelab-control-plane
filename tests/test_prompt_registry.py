"""D-041 prompt registry: identity, immutability, determinism, shared path."""

from __future__ import annotations

import unittest

from tools.prompts.library import LOCKED_FINGERPRINTS, PROMPT_VERSIONS
from tools.prompts.production import (
    NoPromptForTaskClass,
    prompt_id_for_task_class,
    render_for_task_class,
)
from tools.prompts.registry import (
    PromptRegistry,
    PromptVersion,
    UnknownPrompt,
    load_default_registry,
)
from tools.prompts.renderer import ContextPack, render


def _registry() -> PromptRegistry:
    return load_default_registry()


class RegistryIdentityTests(unittest.TestCase):
    def test_default_registry_loads_the_two_gate2_prompts(self) -> None:
        registry = _registry()
        ids = {v.prompt_id for v in registry.all()}
        self.assertEqual(ids, {"acb-v1-routing-classification", "acb-v1-semantic-naming"})

    def test_get_unknown_prompt_id_raises(self) -> None:
        with self.assertRaises(UnknownPrompt):
            _registry().get("no-such-prompt", "1.0.0")

    def test_get_unknown_version_of_a_known_prompt_raises(self) -> None:
        with self.assertRaises(UnknownPrompt):
            _registry().get("acb-v1-routing-classification", "9.9.9")

    def test_active_returns_the_one_active_version(self) -> None:
        version = _registry().active("acb-v1-routing-classification")
        self.assertEqual(version.status, "active")
        self.assertEqual(version.version, "1.0.0")

    def test_active_raises_for_a_prompt_id_with_no_active_version(self) -> None:
        registry = PromptRegistry([
            PromptVersion(
                prompt_id="x", version="0.1.0", status="draft", capability="c",
                policy_template="p", role_template="r", task_template="{task}",
                output_schema="o", tool_schema_version="none", renderer_version="1",
            )
        ])
        with self.assertRaises(UnknownPrompt):
            registry.active("x")

    def test_two_active_versions_of_the_same_prompt_id_is_rejected_at_construction(self) -> None:
        def make(version: str) -> PromptVersion:
            return PromptVersion(
                prompt_id="dup", version=version, status="active", capability="c",
                policy_template="p", role_template="r", task_template="{task}",
                output_schema="o", tool_schema_version="none", renderer_version="1",
            )
        with self.assertRaises(ValueError):
            PromptRegistry([make("1.0.0"), make("2.0.0")])

    def test_duplicate_key_is_rejected(self) -> None:
        def make() -> PromptVersion:
            return PromptVersion(
                prompt_id="dup", version="1.0.0", status="draft", capability="c",
                policy_template="p", role_template="r", task_template="{task}",
                output_schema="o", tool_schema_version="none", renderer_version="1",
            )
        with self.assertRaises(ValueError):
            PromptRegistry([make(), make()])

    def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PromptVersion(
                prompt_id="x", version="1.0.0", status="published", capability="c",
                policy_template="p", role_template="r", task_template="{task}",
                output_schema="o", tool_schema_version="none", renderer_version="1",
            )

    def test_empty_template_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PromptVersion(
                prompt_id="x", version="1.0.0", status="draft", capability="c",
                policy_template="   ", role_template="r", task_template="{task}",
                output_schema="o", tool_schema_version="none", renderer_version="1",
            )


class ImmutabilityTests(unittest.TestCase):
    """A version's fingerprint is locked the moment it is authored."""

    def test_every_locked_version_still_matches_its_recorded_fingerprint(self) -> None:
        by_key = {v.key: v for v in PROMPT_VERSIONS}
        self.assertEqual(set(by_key), set(LOCKED_FINGERPRINTS))
        for key, locked in LOCKED_FINGERPRINTS.items():
            self.assertEqual(
                by_key[key].content_fingerprint, locked,
                f"{key} content changed without a version bump -- semantic prompt "
                "versions are immutable (D-041); add a new version instead",
            )

    def test_changing_task_template_changes_the_fingerprint(self) -> None:
        base = PROMPT_VERSIONS[0]
        mutated = PromptVersion(**{**base.__dict__, "task_template": base.task_template + " extra"})
        self.assertNotEqual(base.content_fingerprint, mutated.content_fingerprint)

    def test_changing_status_alone_does_not_change_the_fingerprint(self) -> None:
        base = PROMPT_VERSIONS[0]
        deprecated = PromptVersion(**{**base.__dict__, "status": "deprecated"})
        self.assertEqual(base.content_fingerprint, deprecated.content_fingerprint)


class RenderingDeterminismTests(unittest.TestCase):
    def test_same_inputs_render_byte_identical_text_and_hash(self) -> None:
        prompt = _registry().active("acb-v1-routing-classification")
        first = render(prompt, task_text="Classify this job.")
        second = render(prompt, task_text="Classify this job.")
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.sha256, second.sha256)

    def test_different_task_text_renders_different_hash(self) -> None:
        prompt = _registry().active("acb-v1-routing-classification")
        first = render(prompt, task_text="Classify job A.")
        second = render(prompt, task_text="Classify job B.")
        self.assertNotEqual(first.sha256, second.sha256)

    def test_rendered_prompt_carries_identity_fields(self) -> None:
        prompt = _registry().active("acb-v1-semantic-naming")
        rendered = render(prompt, task_text="evidence", extra={"original_filename": "IMG_0001.jpg"})
        self.assertEqual(rendered.prompt_id, "acb-v1-semantic-naming")
        self.assertEqual(rendered.version, "1.0.0")
        self.assertEqual(rendered.tool_schema_version, "none")
        self.assertEqual(len(rendered.sha256), 64)

    def test_context_pack_is_rendered_as_a_labeled_untrusted_section(self) -> None:
        prompt = _registry().active("acb-v1-routing-classification")
        context = ContextPack(items=(("evidence", "some untrusted text"),))
        rendered = render(prompt, task_text="Classify this job.", context=context)
        self.assertIn("Context (untrusted data", rendered.text)
        self.assertIn("some untrusted text", rendered.text)
        # The policy section (which forbids obeying task/context content) must
        # still appear before the context block -- composition order matters.
        self.assertLess(rendered.text.index(prompt.policy_template), rendered.text.index("some untrusted text"))

    def test_empty_context_pack_adds_no_section(self) -> None:
        prompt = _registry().active("acb-v1-routing-classification")
        rendered = render(prompt, task_text="Classify this job.")
        self.assertNotIn("Context (untrusted data", rendered.text)

    def test_context_sha256_reflects_only_context_content(self) -> None:
        prompt = _registry().active("acb-v1-routing-classification")
        a = render(prompt, task_text="job one", context=ContextPack(items=(("k", "same"),)))
        b = render(prompt, task_text="job two", context=ContextPack(items=(("k", "same"),)))
        self.assertEqual(a.context_sha256, b.context_sha256)
        self.assertNotEqual(a.sha256, b.sha256)


class SharedPathTests(unittest.TestCase):
    """Evaluation and production render through the exact same function."""

    def test_render_for_task_class_matches_direct_registry_render(self) -> None:
        registry = _registry()
        direct = render(registry.active("acb-v1-routing-classification"), task_text="Classify this job.")
        via_task_class = render_for_task_class("classification", task_text="Classify this job.", registry=registry)
        self.assertEqual(direct.text, via_task_class.text)
        self.assertEqual(direct.sha256, via_task_class.sha256)

    def test_classification_and_routing_task_classes_share_one_prompt(self) -> None:
        self.assertEqual(
            prompt_id_for_task_class("classification"),
            prompt_id_for_task_class("routing"),
        )

    def test_naming_semantic_maps_to_the_naming_prompt(self) -> None:
        self.assertEqual(prompt_id_for_task_class("naming.semantic"), "acb-v1-semantic-naming")

    def test_unmapped_task_class_raises_rather_than_guessing(self) -> None:
        with self.assertRaises(NoPromptForTaskClass):
            prompt_id_for_task_class("code.implementation")

    def test_two_calls_for_the_same_task_class_and_input_are_byte_identical(self) -> None:
        first = render_for_task_class("naming.semantic", task_text="An invoice PDF.",
                                       extra={"original_filename": "scan001.pdf"})
        second = render_for_task_class("naming.semantic", task_text="An invoice PDF.",
                                        extra={"original_filename": "scan001.pdf"})
        self.assertEqual(first.sha256, second.sha256)


if __name__ == "__main__":
    unittest.main()
