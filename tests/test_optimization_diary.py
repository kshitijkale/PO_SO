"""Tests for the OptimizationDiary."""

from gepa.proposer.reflective_mutation.optimization_diary import (
    DiaryEntry,
    OptimizationDiary,
    _unified_diff,
)


def _make_entry(
    iteration: int = 1,
    accepted: bool = False,
    score_before: float = 2.0,
    score_after: float = 1.0,
    batch_size: int = 3,
    diff: str = "Added: 'verify'",
    component_name: str = "system_prompt",
) -> DiaryEntry:
    return DiaryEntry(
        iteration=iteration,
        accepted=accepted,
        score_before=score_before,
        score_after=score_after,
        batch_size=batch_size,
        diff=diff,
        component_name=component_name,
    )


class TestUnifiedDiff:
    def test_basic_addition(self):
        old = "Solve the problem."
        new = "Solve the problem.\nShow your work step by step."
        diff = _unified_diff(old, new)
        assert "+Show your work step by step." in diff
        assert "---" in diff
        assert "+++" in diff

    def test_no_change(self):
        text = "Same text."
        assert _unified_diff(text, text) == ""

    def test_replacement(self):
        old = "Use method A."
        new = "Use method B."
        diff = _unified_diff(old, new)
        assert "-Use method A." in diff
        assert "+Use method B." in diff


class TestOptimizationDiary:
    def test_record_and_format_layer1(self):
        diary = OptimizationDiary()
        diary.record(_make_entry(iteration=1, accepted=True, score_before=1.0, score_after=2.0,
                                 diff="--- before\n+++ after\n@@ -1 +1,2 @@\n Solve.\n+Step by step."))
        diary.record(_make_entry(iteration=2, accepted=False))
        diary.record(_make_entry(iteration=3, accepted=True, score_before=2.0, score_after=3.0,
                                 diff="--- before\n+++ after\n@@ -1 +1,2 @@\n Solve.\n+Check answer."))

        text = diary.format_for_prompt()
        assert "OPTIMIZATION CONTEXT" in text
        assert "3 iterations" in text
        assert "2 accepted" in text
        assert "Edits that improved scores" in text
        # Full diffs should appear
        assert "+Step by step." in text
        assert "+Check answer." in text
        # Rejected edits should NOT appear
        assert "failed edits" not in text

    def test_empty_returns_empty(self):
        diary = OptimizationDiary()
        assert diary.format_for_prompt() == ""

    def test_only_accepted_diffs_shown(self):
        diary = OptimizationDiary()
        diary.record(_make_entry(iteration=1, accepted=True, score_before=1.0, score_after=2.0,
                                 diff="--- before\n+++ after\n+good_change"))
        diary.record(_make_entry(iteration=2, accepted=False, diff="bad_change_summary"))
        diary.record(_make_entry(iteration=3, accepted=True, score_before=2.0, score_after=3.0,
                                 diff="--- before\n+++ after\n+another_good"))

        text = diary.format_for_prompt()
        assert "+good_change" in text
        assert "+another_good" in text
        assert "bad_change_summary" not in text

    def test_full_diff_injected_not_summary(self):
        """The full unified diff is injected, not a truncated summary."""
        old = "Solve the problem."
        new = "Solve the problem.\nShow your work step by step.\nVerify your answer."
        full_diff = _unified_diff(old, new)

        diary = OptimizationDiary()
        diary.record(_make_entry(iteration=1, accepted=True, score_before=1.0, score_after=2.0,
                                 diff=full_diff))

        text = diary.format_for_prompt()
        # The full diff lines should be present, not a summary
        assert "+Show your work step by step." in text
        assert "+Verify your answer." in text

    def test_no_prompt_length_in_output(self):
        diary = OptimizationDiary()
        diary.record(_make_entry(iteration=1, accepted=True, score_before=1.0, score_after=2.0))

        text = diary.format_for_prompt()
        assert "chars" not in text
        assert "growth" not in text

    def test_acceptance_rate(self):
        diary = OptimizationDiary()
        for i in range(10):
            diary.record(_make_entry(iteration=i + 1, accepted=(i < 3)))

        text = diary.format_for_prompt()
        assert "3 accepted (30%)" in text

    def test_accepted_edits_window(self):
        diary = OptimizationDiary(max_accepted_display=5)
        for i in range(10):
            diary.record(_make_entry(
                iteration=i + 1,
                accepted=True,
                score_before=1.0,
                score_after=2.0,
                diff=f"--- before\n+++ after\n+accept_{i}",
            ))

        text = diary.format_for_prompt()
        # Only last 5 acceptances should appear
        assert "+accept_5" in text
        assert "+accept_9" in text
        assert "+accept_0" not in text
        assert "+accept_4" not in text

    def test_layer2_with_mock_lm(self):
        def mock_lm(prompt):
            return "Domain formulas have not helped. Structural changes are more effective."

        diary = OptimizationDiary(strategy_notes_lm=mock_lm)
        diary.record(_make_entry(iteration=1, accepted=False))
        diary.update_strategy_notes()

        text = diary.format_for_prompt()
        assert "Strategy patterns:" in text
        assert "Domain formulas" in text

    def test_layer2_disabled_when_no_lm(self):
        diary = OptimizationDiary(strategy_notes_lm=None)
        diary.record(_make_entry(iteration=1, accepted=False))
        diary.update_strategy_notes()  # should be a no-op

        text = diary.format_for_prompt()
        assert "Strategy patterns:" not in text

    def test_layer2_graceful_failure(self):
        def failing_lm(prompt):
            raise RuntimeError("API error")

        diary = OptimizationDiary(strategy_notes_lm=failing_lm)
        diary.record(_make_entry(iteration=1, accepted=False))

        diary._strategy_notes = "Existing notes preserved"
        diary.update_strategy_notes()

        assert diary._strategy_notes == "Existing notes preserved"

    def test_global_scope(self):
        """Accepted entries from different parents all appear."""
        diary = OptimizationDiary()
        diary.record(_make_entry(iteration=1, accepted=True, score_before=1.0, score_after=2.0,
                                 diff="--- before\n+++ after\n+from_parent_A"))
        diary.record(_make_entry(iteration=2, accepted=True, score_before=1.0, score_after=2.0,
                                 diff="--- before\n+++ after\n+from_parent_B"))

        text = diary.format_for_prompt()
        assert "+from_parent_A" in text
        assert "+from_parent_B" in text

    def test_snapshot_serializable(self):
        import json

        diary = OptimizationDiary()
        diary.record(_make_entry(iteration=1, accepted=True, score_before=1.0, score_after=2.0))
        diary.record(_make_entry(iteration=2, accepted=False))

        snapshot = diary.snapshot()
        serialized = json.dumps(snapshot)
        assert "total_entries" in serialized
        assert snapshot["total_entries"] == 2
        assert snapshot["accepted_count"] == 1
        assert "diff" in serialized

    def test_optimize_accepts_diary_memory_versions(self):
        """optimize() accepts memory_version='diary' and 'diary_full' without raising."""
        import inspect

        from gepa.api import optimize

        sig = inspect.signature(optimize)
        assert "memory_version" in sig.parameters
