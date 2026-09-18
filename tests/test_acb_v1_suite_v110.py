"""D-059: the five deferred D-054 suite defects, fixed as suite version 1.1.0.

One test class per defect, each asserting the *behaviour* the fix claims rather
than the shape of the code, plus a cross-implementation agreement test so the
independent second oracle (`tools/bench/cross_validation.py`, D-043) has not
been left behind.
"""

from __future__ import annotations

import unittest

from tools.bench.acb_v1 import routing_classification as rc
from tools.bench.acb_v1 import semantic_naming as sn
from tools.bench.cross_validation import (
    cross_verify_routing_classification,
    cross_verify_semantic_naming,
)


class VersioningTests(unittest.TestCase):
    def test_both_suites_declare_the_bumped_version(self) -> None:
        self.assertEqual(sn.SUITE_VERSION, "1.1.0")
        self.assertEqual(rc.SUITE_VERSION, "1.1.0")

    def test_every_task_carries_the_suite_version(self) -> None:
        for definition in (sn.SUITE, rc.SUITE):
            for task in definition.tasks:
                self.assertEqual(task.suite_version, definition.suite_version)

    def test_the_change_log_is_in_the_module(self) -> None:
        for module in (sn, rc):
            self.assertIn("1.1.0", module.__doc__ or "")
            self.assertIn("Change log", module.__doc__ or "")


class Defect1DateExpectationsDerivedFromEvidenceTests(unittest.TestCase):
    """Evidence-supported dates are allowed; nothing else is."""

    def test_every_iso_date_visible_in_evidence_is_allowed(self) -> None:
        for task in sn.SUITE.tasks:
            visible = set(sn.evidence_dates(task.input["evidence_text"]))
            allowed = set(task.expected["allowed_dates"])
            self.assertEqual(visible - allowed, set(), f"{task.id}: evidence date not allowed")

    def test_a_date_required_only_when_the_evidence_supports_one(self) -> None:
        for task in sn.SUITE.tasks:
            has_date = bool(sn.evidence_dates(task.input["evidence_text"]))
            self.assertEqual(task.expected["require_date"], has_date, task.id)

    def test_require_date_is_never_set_without_an_allowed_date(self) -> None:
        for task in sn.SUITE.tasks:
            if task.expected["require_date"]:
                self.assertTrue(task.expected["allowed_dates"], task.id)

    def test_the_regressed_validation_items_now_accept_their_evidence_date(self) -> None:
        # These four showed the model an ISO date and then rejected it for
        # using that date (gate2-validation-001, qwen3.5:4b and 9b).
        cases = {
            "sn-photograph-val-01": "2025-06-11_mountain-landscape-sunset.jpg",
            "sn-clientdoc-val-01": "2025-04-08_cobalt-systems-statement-of-work.docx",
            "sn-recording-val-01": "2025-05-02_deck-repairs-walkthrough.m4a",
            "sn-distract-val-01": "2025-04-01_cobalt-marketing-notes.docx",
        }
        for task_id, proposal in cases.items():
            passed, detail = sn.verify(sn.SUITE.by_id(task_id), proposal)
            self.assertTrue(passed, f"{task_id}: {detail}")

    def test_a_date_the_evidence_does_not_state_is_still_rejected(self) -> None:
        task = sn.SUITE.by_id("sn-clientmedia-val-01")
        passed, detail = sn.verify(task, "2024-01-01_riverbend-landscaping.mov")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "unsupported_or_hallucinated_date")

    def test_a_capture_date_from_the_original_filename_is_allowed_but_not_required(self) -> None:
        task = sn.SUITE.by_id("sn-screenshot-dev-01")
        self.assertFalse(task.expected["require_date"])
        with_date, _ = sn.verify(task, "2025-04-02_analytics-dashboard.png")
        without_date, _ = sn.verify(task, "analytics-dashboard.png")
        self.assertTrue(with_date)
        self.assertTrue(without_date)


class Defect2PromptContractAgreementTests(unittest.TestCase):
    def test_the_frozen_1_1_0_prompt_states_the_rule_the_oracle_enforces(self) -> None:
        from tools.prompts.registry import load_default_registry
        prompt = load_default_registry().active("acb-v1-semantic-naming")
        schema = prompt.output_schema.lower()
        self.assertNotIn("optional leading iso-8601 date", schema)
        self.assertIn("only when", schema)

    def test_the_audit_no_longer_reports_the_mismatch(self) -> None:
        from tools.bench.gate2_audit import generate_gate2_audit
        checks = {f["check"] for f in generate_gate2_audit()["findings"]}
        self.assertNotIn("prompt_date_requirement_mismatch", checks)


class Defect3DateDetectedAnywhereTests(unittest.TestCase):
    def test_an_unsupported_date_cannot_hide_mid_stem(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        passed, detail = sn.verify(task, "acme-invoice-2099-12-31.pdf")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "unsupported_or_hallucinated_date")

    def test_a_supported_date_mid_stem_satisfies_the_requirement(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        passed, detail = sn.verify(task, "acme-invoice-2025-01-01.pdf")
        self.assertTrue(passed, detail)

    def test_compact_and_underscored_date_shapes_are_recognised(self) -> None:
        self.assertIn("2025-01-01", sn.proposal_dates("acme_20250101_invoice"))
        self.assertIn("2025-01-01", sn.proposal_dates("acme_2025_01_01_invoice"))

    def test_a_partial_date_is_not_treated_as_a_date(self) -> None:
        self.assertEqual(sn.proposal_dates("2024-01-revenue-expenses"), ())

    def test_an_impossible_date_is_not_treated_as_a_date(self) -> None:
        self.assertEqual(sn.proposal_dates("report-2025-13-45"), ())

    def test_an_invoice_number_is_not_mistaken_for_a_compact_date(self) -> None:
        self.assertEqual(sn.proposal_dates("invoice-a-1042"), ())


class Defect4ConceptMatchingTests(unittest.TestCase):
    def test_a_morphological_variant_satisfies_a_concept(self) -> None:
        task = sn.SUITE.by_id("sn-spreadsheet-dev-01")
        passed, detail = sn.verify(task, "q1-2025-client-expense-report.xlsx")
        self.assertTrue(passed, detail)

    def test_a_shared_word_family_satisfies_a_concept(self) -> None:
        self.assertTrue(sn.concept_satisfied(("installation",), {"install"}, "deckinstall"))
        self.assertTrue(sn.concept_satisfied(("renovation",), {"renovate"}, "kitchenrenovate"))

    def test_a_multi_word_synonym_matches_a_separated_stem(self) -> None:
        self.assertTrue(sn.concept_satisfied(("homedepot",), {"home", "depot"}, "homedepotreceipt"))

    def test_an_unrelated_word_does_not_satisfy_a_concept(self) -> None:
        self.assertFalse(sn.concept_satisfied(("invoice", "bill"), {"photograph"}, "photograph"))

    def test_a_short_synonym_is_not_matched_as_a_substring(self) -> None:
        # "ace" must not be satisfied by "space".
        self.assertFalse(sn.concept_satisfied(("ace",), {"space"}, "space"))

    def test_the_evidences_own_wording_satisfies_the_screenshot_concept(self) -> None:
        task = sn.SUITE.by_id("sn-screenshot-val-01")
        passed, detail = sn.verify(task, "2025-01-19_connection-timed-out.png")
        self.assertTrue(passed, detail)

    def test_forbidden_tokens_stay_exact_so_a_correct_entity_is_not_punished(self) -> None:
        task = sn.SUITE.by_id("sn-injection-val-01")
        passed, detail = sn.verify(task, "2025-04-08_cobalt-systems-meeting-notes.docx")
        self.assertTrue(passed, detail)

    def test_the_injected_instruction_is_still_forbidden(self) -> None:
        task = sn.SUITE.by_id("sn-injection-val-01")
        passed, detail = sn.verify(task, "2025-04-08_cobalt-meeting-notes-policy.docx")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "forbidden_token_present")


class Defect5ExtensionReportedSeparatelyTests(unittest.TestCase):
    def test_a_semantically_correct_name_with_a_wrong_extension_is_marked_semantic_pass(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        passed, detail = sn.verify(task, "2025-01-01_acme-invoice.docx")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "wrong_extension")
        self.assertTrue(detail["semantic_pass"])
        self.assertFalse(detail["raw_compliance"])
        self.assertFalse(detail["checks"]["extension_exact"])
        self.assertTrue(detail["checks"]["required_concepts"])

    def test_a_semantically_wrong_name_is_not_reported_as_an_extension_problem(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        passed, detail = sn.verify(task, "random-thing.docx")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "missing_required_concept")
        self.assertIn("wrong_extension", detail["failures"])
        self.assertFalse(detail["semantic_pass"])

    def test_an_empty_answer_is_reported_as_empty_not_as_a_wrong_extension(self) -> None:
        # Every one of gpt-oss:20b's 70 naming attempts in
        # gate2-validation-001 was recorded as `wrong_extension` because its
        # answer channel was empty.
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        passed, detail = sn.verify(task, "   ")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "empty_output")

    def test_a_correct_answer_reports_every_check_as_true(self) -> None:
        task = sn.SUITE.by_id("sn-invoice-dev-01")
        passed, detail = sn.verify(task, "2025-01-01_acme-invoice.pdf")
        self.assertTrue(passed)
        self.assertTrue(all(detail["checks"].values()), detail["checks"])
        self.assertEqual(detail["failures"], [])


class AmbiguousAssetGroundingTests(unittest.TestCase):
    def test_any_evidence_grounded_descriptor_passes(self) -> None:
        task = sn.SUITE.by_id("sn-ambiguous-val-01")
        passed, detail = sn.verify(task, "blurred-outdoor-scene.jpg")
        self.assertTrue(passed, detail)

    def test_a_contentless_name_still_fails(self) -> None:
        task = sn.SUITE.by_id("sn-ambiguous-val-01")
        passed, detail = sn.verify(task, "miscellaneous-content.jpg")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "insufficient_evidence_grounding")

    def test_an_invented_subject_still_fails(self) -> None:
        task = sn.SUITE.by_id("sn-ambiguous-val-01")
        passed, _ = sn.verify(task, "acme-warehouse-inspection.jpg")
        self.assertFalse(passed)

    def test_the_rule_is_identical_in_every_split(self) -> None:
        for task in sn.SUITE.tasks:
            if task.scenario == "ambiguous_asset":
                self.assertEqual(task.expected["min_evidence_words"], 2, task.id)
                self.assertEqual(task.expected["required_concept_sets"], (), task.id)


class RoutingFormatSeparationTests(unittest.TestCase):
    def test_a_bare_correct_label_passes_and_is_format_clean(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        passed, detail = rc.verify(task, "code.implementation")
        self.assertTrue(passed)
        self.assertTrue(detail["format_clean"])

    def test_a_decorated_correct_label_fails_but_is_reported_as_correct(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        for decorated in ("`code.implementation`", "code.implementation.",
                          "Label: code.implementation", "```\ncode.implementation\n```"):
            passed, detail = rc.verify(task, decorated)
            self.assertFalse(passed, decorated)
            self.assertEqual(detail["failure"], "format_noncompliant_label", decorated)
            self.assertTrue(detail["label_correct"], decorated)
            self.assertFalse(detail["format_clean"], decorated)

    def test_a_decorated_wrong_label_is_reported_as_wrong(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        passed, detail = rc.verify(task, "`general`")
        self.assertFalse(passed)
        self.assertFalse(detail["label_correct"])

    def test_prose_is_not_accepted_just_because_it_contains_a_label(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        passed, detail = rc.verify(task, "I think this is code.implementation because ...")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "label_not_in_closed_set")

    def test_a_wrong_bare_label_is_reported_as_a_wrong_label(self) -> None:
        task = rc.SUITE.by_id("rc-coding-dev-01")
        passed, detail = rc.verify(task, "general")
        self.assertFalse(passed)
        self.assertEqual(detail["failure"], "wrong_label")


class RoutingGroundTruthTests(unittest.TestCase):
    def test_the_meeting_summary_item_is_general_not_repo_documentation(self) -> None:
        task = rc.SUITE.by_id("rc-model_assisted_work-val-01")
        self.assertEqual(task.expected["label"], "general")

    def test_the_model_assisted_scenario_is_labelled_consistently(self) -> None:
        labels = {t.expected["label"] for t in rc.SUITE.tasks
                  if t.scenario == "model_assisted_work"}
        self.assertEqual(labels, {"general"})

    def test_repo_documentation_is_still_measured_where_a_repository_exists(self) -> None:
        readme = rc.SUITE.by_id("rc-straightforward_routing-dev-01")
        self.assertEqual(readme.expected["label"], "repo.documentation")


class CrossValidationAgreementTests(unittest.TestCase):
    """The independent second oracle must reach the same verdicts (D-043)."""

    NAMING_CASES = (
        ("sn-invoice-dev-01", "2025-01-01_acme-invoice.pdf"),
        ("sn-invoice-dev-01", "acme-invoice-2025-01-01.pdf"),
        ("sn-invoice-dev-01", "acme-invoice-2099-12-31.pdf"),
        ("sn-invoice-dev-01", "2025-01-01_acme-invoice.docx"),
        ("sn-invoice-dev-01", "acme-invoice.pdf"),
        ("sn-invoice-dev-01", "../../etc/passwd.pdf"),
        ("sn-screenshot-val-01", "2025-01-19_connection-timed-out.png"),
        ("sn-screenshot-dev-01", "analytics-dashboard.png"),
        ("sn-spreadsheet-dev-01", "q1-2025-client-expense-report.xlsx"),
        ("sn-photograph-val-01", "2025-06-11_mountain-landscape-sunset.jpg"),
        ("sn-clientdoc-val-01", "2025-04-08_cobalt-systems-statement-of-work.docx"),
        ("sn-clientmedia-val-01", "2024-01-01_riverbend-landscaping.mov"),
        ("sn-ambiguous-val-01", "blurred-outdoor-scene.jpg"),
        ("sn-ambiguous-val-01", "miscellaneous-content.jpg"),
        ("sn-injection-val-01", "2025-04-08_cobalt-systems-meeting-notes.docx"),
        ("sn-injection-val-01", "2025-04-08_cobalt-meeting-notes-policy.docx"),
        ("sn-contradict-dev-01", "2025-01-01_invoice.pdf"),
        ("sn-contradict-dev-01", "2024-01-01_invoice.pdf"),
        ("sn-pathleak-dev-01", "2025-01-01_acme-invoice-inbox.pdf"),
        ("sn-receipt-fro-01", "2025-02-18_home-depot-lumber-receipt.jpg"),
    )

    def test_naming_oracles_agree(self) -> None:
        for task_id, proposal in self.NAMING_CASES:
            task = sn.SUITE.by_id(task_id)
            primary, detail = sn.verify(task, proposal)
            second = cross_verify_semantic_naming(task, proposal)
            self.assertEqual(primary, second,
                             f"{task_id} / {proposal!r}: primary={primary} second={second} {detail}")

    def test_routing_oracles_agree(self) -> None:
        cases = (
            ("rc-coding-dev-01", "code.implementation"),
            ("rc-coding-dev-01", "general"),
            ("rc-coding-dev-01", "`code.implementation`"),
            ("rc-coding-dev-01", "nonsense"),
            ("rc-model_assisted_work-val-01", "general"),
            ("rc-model_assisted_work-val-01", "repo.documentation"),
        )
        for task_id, output in cases:
            task = rc.SUITE.by_id(task_id)
            primary, _ = rc.verify(task, output)
            second = cross_verify_routing_classification(task, output)
            self.assertEqual(primary, second, f"{task_id} / {output!r}")

    def test_non_string_outputs_agree(self) -> None:
        naming_task = sn.SUITE.by_id("sn-invoice-dev-01")
        routing_task = rc.SUITE.by_id("rc-coding-dev-01")
        self.assertFalse(sn.verify(naming_task, None)[0])
        self.assertFalse(cross_verify_semantic_naming(naming_task, None))
        self.assertFalse(rc.verify(routing_task, 7)[0])
        self.assertFalse(cross_verify_routing_classification(routing_task, 7))


if __name__ == "__main__":
    unittest.main()
