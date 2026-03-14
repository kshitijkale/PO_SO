# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""Unit tests for ReflectionMemory, ReflectionMemoryEntry, summarize_change, and generate_lesson."""

from unittest.mock import MagicMock

from gepa.proposer.reflective_mutation.memory import (
    ReflectionMemory,
    ReflectionMemoryEntry,
    generate_lesson,
    summarize_change,
)


def _make_entry(
    iteration: int = 1,
    component_name: str = "system_prompt",
    change_summary: str = "Added: 'Think step by step'",
    score_before: float = 2.0,
    score_after: float = 3.0,
    accepted: bool = True,
    failure_modes: list[str] | None = None,
) -> ReflectionMemoryEntry:
    return ReflectionMemoryEntry(
        iteration=iteration,
        component_name=component_name,
        change_summary=change_summary,
        score_before=score_before,
        score_after=score_after,
        accepted=accepted,
        failure_modes=failure_modes or [],
    )


class TestReflectionMemoryEntry:
    def test_construction(self):
        entry = _make_entry(iteration=5, component_name="prompt_a", score_before=1.0, score_after=2.5)
        assert entry.iteration == 5
        assert entry.component_name == "prompt_a"
        assert entry.score_before == 1.0
        assert entry.score_after == 2.5
        assert entry.accepted is True
        assert entry.failure_modes == []

    def test_failure_modes_stored(self):
        entry = _make_entry(failure_modes=["Model gave wrong answer", "Output not parseable"])
        assert len(entry.failure_modes) == 2
        assert "Model gave wrong answer" in entry.failure_modes


class TestReflectionMemory:
    def test_add_and_retrieve(self):
        mem = ReflectionMemory(max_entries=10)
        mem.add(_make_entry(iteration=1))
        mem.add(_make_entry(iteration=2))
        assert len(mem.entries) == 2

    def test_fifo_eviction(self):
        mem = ReflectionMemory(max_entries=5)
        for i in range(8):
            mem.add(_make_entry(iteration=i))
        assert len(mem.entries) == 5
        assert mem.entries[0].iteration == 3
        assert mem.entries[-1].iteration == 7

    def test_get_recent_no_filter(self):
        mem = ReflectionMemory(max_entries=10)
        for i in range(6):
            mem.add(_make_entry(iteration=i))
        recent = mem.get_recent(n=3)
        assert len(recent) == 3
        assert [e.iteration for e in recent] == [3, 4, 5]

    def test_get_recent_with_component_filter(self):
        mem = ReflectionMemory(max_entries=10)
        mem.add(_make_entry(iteration=1, component_name="A"))
        mem.add(_make_entry(iteration=2, component_name="B"))
        mem.add(_make_entry(iteration=3, component_name="A"))
        mem.add(_make_entry(iteration=4, component_name="B"))
        mem.add(_make_entry(iteration=5, component_name="A"))

        recent_a = mem.get_recent(n=10, component_name="A")
        assert len(recent_a) == 3
        assert all(e.component_name == "A" for e in recent_a)

        recent_b = mem.get_recent(n=1, component_name="B")
        assert len(recent_b) == 1
        assert recent_b[0].iteration == 4

    def test_format_for_prompt_empty(self):
        mem = ReflectionMemory(max_entries=10)
        assert mem.format_for_prompt("system_prompt") == ""

    def test_format_for_prompt_no_matching_component(self):
        mem = ReflectionMemory(max_entries=10)
        mem.add(_make_entry(component_name="other_component"))
        assert mem.format_for_prompt("system_prompt") == ""

    def test_format_for_prompt_content(self):
        mem = ReflectionMemory(max_entries=10)
        mem.add(
            _make_entry(
                iteration=1,
                component_name="prompt",
                change_summary="Added: 'Think step by step'",
                score_before=1.0,
                score_after=2.0,
                accepted=True,
                failure_modes=["Model gave direct answer"],
            )
        )
        mem.add(
            _make_entry(
                iteration=2,
                component_name="prompt",
                change_summary="Added: 'Format as JSON'",
                score_before=2.0,
                score_after=1.5,
                accepted=False,
            )
        )

        text = mem.format_for_prompt("prompt")
        assert "## Optimization History" in text
        assert "Iter 1" in text
        assert "ACCEPTED" in text
        assert "Iter 2" in text
        assert "REJECTED" in text
        assert "Model gave direct answer" in text
        assert "Do not repeat REJECTED strategies" in text

    def test_format_for_prompt_limits_failure_modes(self):
        mem = ReflectionMemory(max_entries=10)
        mem.add(
            _make_entry(
                failure_modes=["fail1", "fail2", "fail3"],
                component_name="p",
            )
        )
        text = mem.format_for_prompt("p")
        assert "fail1" in text
        assert "fail2" in text
        assert "fail3" not in text  # only first 2 shown


class TestSummarizeChange:
    def test_no_change(self):
        assert summarize_change("hello world", "hello world") == "No changes"

    def test_insertion(self):
        result = summarize_change("Hello", "Hello, think step by step")
        assert result.startswith("Added:") or result.startswith("Changed:")

    def test_deletion(self):
        result = summarize_change("Hello, think step by step", "Hello")
        assert result.startswith("Removed:") or result.startswith("Changed:")

    def test_replacement(self):
        result = summarize_change("Be concise", "Provide detailed reasoning")
        assert "Changed:" in result or "Added:" in result or "Removed:" in result

    def test_truncation(self):
        long_text = "x" * 500
        result = summarize_change("", long_text, max_len=50)
        assert len(result) < 200  # reasonably bounded

    def test_empty_to_content(self):
        result = summarize_change("", "New instruction text")
        assert "Added:" in result

    def test_content_to_empty(self):
        result = summarize_change("Old instruction text", "")
        assert "Removed:" in result


# =========================================================================
# V2 Tests
# =========================================================================


def _make_v2_entry(
    iteration: int = 1,
    component_name: str = "system_prompt",
    score_before: float = 2.0,
    score_after: float = 3.0,
    accepted: bool = True,
    failure_modes: list[str] | None = None,
    intent: str = "",
    lesson: str = "",
    categories_succeeded: list[str] | None = None,
    categories_failed: list[str] | None = None,
    change_summary: str = "",
) -> ReflectionMemoryEntry:
    return ReflectionMemoryEntry(
        iteration=iteration,
        component_name=component_name,
        score_before=score_before,
        score_after=score_after,
        accepted=accepted,
        failure_modes=failure_modes or [],
        intent=intent,
        lesson=lesson,
        categories_succeeded=categories_succeeded or [],
        categories_failed=categories_failed or [],
        change_summary=change_summary,
    )


class TestReflectionMemoryV2:
    def test_entry_v2_fields(self):
        """V2 fields are stored and included in to_dict()."""
        entry = _make_v2_entry(
            intent="Add verification step",
            lesson="Verification reduced arithmetic errors",
            categories_succeeded=["number_theory"],
            categories_failed=["geometry"],
        )
        assert entry.intent == "Add verification step"
        assert entry.lesson == "Verification reduced arithmetic errors"
        assert entry.categories_succeeded == ["number_theory"]
        assert entry.categories_failed == ["geometry"]

        d = entry.to_dict()
        assert d["intent"] == "Add verification step"
        assert d["lesson"] == "Verification reduced arithmetic errors"
        assert d["categories_succeeded"] == ["number_theory"]
        assert d["categories_failed"] == ["geometry"]

    def test_format_for_prompt_v2_path(self):
        """V2 entries render with Intent/Lesson/Categories format."""
        mem = ReflectionMemory(max_entries=10)
        mem.add(_make_v2_entry(
            iteration=1,
            component_name="p",
            accepted=True,
            intent="Add step-by-step verification",
            lesson="Verification reduced arithmetic errors but not geometry",
            categories_succeeded=["number_theory", "combinatorics"],
            categories_failed=["geometry"],
        ))
        mem.add(_make_v2_entry(
            iteration=2,
            component_name="p",
            accepted=False,
            score_before=3.0,
            score_after=2.5,
            intent="Strengthen verification language",
            lesson="Minor restatement with no new strategy doesn't help",
            categories_succeeded=["number_theory"],
            categories_failed=["geometry", "algebra"],
        ))

        text = mem.format_for_prompt("p")
        assert "## Optimization History" in text
        assert "Intent: Add step-by-step verification" in text
        assert "Lesson: Verification reduced arithmetic errors" in text
        assert "Strong on: number_theory, combinatorics" in text
        assert "Still failing: geometry" in text
        assert "Intent: Strengthen verification language" in text
        assert "ACCEPTED" in text
        assert "REJECTED" in text

    def test_format_for_prompt_v1_fallback(self):
        """Entries with empty lesson use V1 format (change_summary)."""
        mem = ReflectionMemory(max_entries=10)
        mem.add(_make_v2_entry(
            iteration=1,
            component_name="p",
            change_summary="Added: 'Think step by step'",
            lesson="",  # empty => V1 fallback
            failure_modes=["Wrong answer"],
        ))

        text = mem.format_for_prompt("p")
        assert "Added: 'Think step by step'" in text
        assert "Wrong answer" in text
        # V2 headers should NOT appear
        assert "Intent:" not in text
        assert "Lesson:" not in text

    def test_format_for_prompt_5_recent_default(self):
        """Default max_recent is 5 — only 5 most recent entries are rendered."""
        mem = ReflectionMemory(max_entries=20)
        for i in range(8):
            mem.add(_make_v2_entry(
                iteration=i,
                component_name="p",
                lesson=f"Lesson for iter {i}",
                intent=f"Intent {i}",
            ))

        text = mem.format_for_prompt("p")
        # Should contain iterations 3-7 (last 5), not 0-2
        assert "Iter 3" in text
        assert "Iter 7" in text
        assert "Iter 2" not in text

    def test_persistent_weak_spots(self):
        """Categories failing in 2+ entries appear as persistent weak spots."""
        mem = ReflectionMemory(max_entries=10)
        for i in range(3):
            mem.add(_make_v2_entry(
                iteration=i,
                component_name="p",
                lesson=f"Lesson {i}",
                categories_failed=["geometry", "algebra"] if i < 2 else ["geometry"],
            ))

        text = mem.format_for_prompt("p")
        assert "Persistent weak spots:" in text
        assert "geometry" in text.split("Persistent weak spots:")[1]


class TestGenerateLesson:
    def test_success(self):
        """Valid JSON response is parsed correctly."""
        mock_lm = MagicMock()
        mock_lm.return_value = '{"intent": "Add verification", "lesson": "Helped with arithmetic", "categories_succeeded": ["algebra"], "categories_failed": ["geometry"]}'

        intent, lesson, cats_ok, cats_fail = generate_lesson(
            lm=mock_lm,
            old_text="Be concise",
            new_text="Verify each step",
            failure_feedbacks=["Wrong calculation"],
            score_before=2.0,
            score_after=3.0,
            accepted=True,
        )
        assert intent == "Add verification"
        assert lesson == "Helped with arithmetic"
        assert cats_ok == ["algebra"]
        assert cats_fail == ["geometry"]
        mock_lm.assert_called_once()

    def test_bad_json_fallback(self):
        """Non-JSON response returns empty fallback tuple."""
        mock_lm = MagicMock()
        mock_lm.return_value = "This is not JSON at all"

        intent, lesson, cats_ok, cats_fail = generate_lesson(
            lm=mock_lm, old_text="a", new_text="b",
            failure_feedbacks=[], score_before=1.0, score_after=1.0, accepted=False,
        )
        assert intent == ""
        assert lesson == ""
        assert cats_ok == []
        assert cats_fail == []

    def test_exception_fallback(self):
        """LLM call exception returns empty fallback tuple."""
        mock_lm = MagicMock()
        mock_lm.side_effect = RuntimeError("API error")

        intent, lesson, cats_ok, cats_fail = generate_lesson(
            lm=mock_lm, old_text="a", new_text="b",
            failure_feedbacks=[], score_before=1.0, score_after=1.0, accepted=False,
        )
        assert intent == ""
        assert lesson == ""
        assert cats_ok == []
        assert cats_fail == []

    def test_markdown_code_fence_stripped(self):
        """JSON wrapped in markdown code fences is handled."""
        mock_lm = MagicMock()
        mock_lm.return_value = '```json\n{"intent": "Test", "lesson": "Works", "categories_succeeded": [], "categories_failed": []}\n```'

        intent, lesson, cats_ok, cats_fail = generate_lesson(
            lm=mock_lm, old_text="a", new_text="b",
            failure_feedbacks=[], score_before=1.0, score_after=2.0, accepted=True,
        )
        assert intent == "Test"
        assert lesson == "Works"
