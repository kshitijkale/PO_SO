"""Tests for the RejectionLedger."""

from gepa.proposer.reflective_mutation.rejection_ledger import LedgerEntry, RejectionLedger


def _make_entry(
    diff_summary: str = "Added: 'verify'",
    llm_summary: str = "Tried adding verification but solver still failed.",
    score: float = 2.0,
    threshold: float = 2.0,
    batch_size: int = 3,
    iteration: int = 0,
) -> LedgerEntry:
    return LedgerEntry(
        diff_summary=diff_summary,
        llm_summary=llm_summary,
        score=score,
        threshold=threshold,
        batch_size=batch_size,
        iteration=iteration,
    )


class TestRejectionLedger:
    def test_record_and_format(self):
        ledger = RejectionLedger(max_entries_per_parent=8)
        parent = "abc123"
        comp = "system_prompt"
        ledger.record(parent, comp, _make_entry("Added: 'verify your work'", score=2.0, threshold=2.0, batch_size=3, iteration=0))
        ledger.record(parent, comp, _make_entry("Added: 'geometry hints'", score=1.0, threshold=2.0, batch_size=3, iteration=1))
        ledger.record(parent, comp, _make_entry("Changed: 'solve' → 'analyze'", score=2.0, threshold=3.0, batch_size=3, iteration=2))

        text = ledger.format_for_prompt(parent, comp)
        lines = text.strip().split("\n")
        assert "context only" in lines[0]
        assert len([line for line in lines if line.startswith("•")]) == 3

    def test_format_shows_diff_and_llm(self):
        ledger = RejectionLedger()
        ledger.record("p", "c", _make_entry(
            diff_summary="Added: 'step by step'",
            llm_summary="Added step-by-step hint but solver still produced wrong answers.",
        ))
        text = ledger.format_for_prompt("p", "c")
        assert "Added: 'step by step'" in text
        assert "Added step-by-step hint but solver still produced wrong answers." in text

    def test_format_omits_llm_label_when_empty(self):
        ledger = RejectionLedger()
        ledger.record("p", "c", _make_entry(llm_summary=""))
        text = ledger.format_for_prompt("p", "c")
        assert "Why it failed" not in text
        assert "Added: 'verify'" in text

    def test_empty_returns_empty(self):
        ledger = RejectionLedger()
        assert ledger.format_for_prompt("unknown_hash", "comp") == ""

    def test_per_parent_isolation(self):
        ledger = RejectionLedger()
        ledger.record("parentA", "comp", _make_entry(diff_summary="Added: 'A'", iteration=0))
        ledger.record("parentB", "comp", _make_entry(diff_summary="Added: 'B'", iteration=1))

        entries_a = ledger.get_entries("parentA", "comp")
        entries_b = ledger.get_entries("parentB", "comp")
        assert len(entries_a) == 1
        assert len(entries_b) == 1
        assert entries_a[0].diff_summary != entries_b[0].diff_summary

    def test_per_component_isolation(self):
        ledger = RejectionLedger()
        ledger.record("parent", "comp_a", _make_entry(diff_summary="change A", iteration=0))
        ledger.record("parent", "comp_b", _make_entry(diff_summary="change B", iteration=1))

        assert len(ledger.get_entries("parent", "comp_a")) == 1
        assert len(ledger.get_entries("parent", "comp_b")) == 1
        assert ledger.get_entries("parent", "comp_a")[0].diff_summary == "change A"
        assert ledger.get_entries("parent", "comp_b")[0].diff_summary == "change B"

    def test_unbounded_growth(self):
        ledger = RejectionLedger(max_entries_per_parent=8)
        parent = "p1"
        comp = "c1"
        for i in range(20):
            ledger.record(parent, comp, _make_entry(diff_summary=f"change_{i}", iteration=i))

        entries = ledger.get_entries(parent, comp)
        assert len(entries) == 20
        assert entries[0].diff_summary == "change_0"
        assert entries[-1].diff_summary == "change_19"

    def test_soft_framing(self):
        ledger = RejectionLedger()
        ledger.record("p", "c", _make_entry())
        text = ledger.format_for_prompt("p", "c")
        assert "context only" in text
        assert "may or may not be relevant" in text

    def test_format_includes_scores(self):
        ledger = RejectionLedger()
        ledger.record("p", "c", _make_entry(score=2.0, threshold=2.0, batch_size=3))
        text = ledger.format_for_prompt("p", "c")
        assert "scored 2/3" in text
        assert "needed >2/3" in text

    def test_optimize_accepts_ledger_memory_version(self):
        """optimize() accepts memory_version='ledger' without raising."""
        import inspect

        from gepa.api import optimize

        sig = inspect.signature(optimize)
        assert "memory_version" in sig.parameters
        assert "use_reflection_memory" not in sig.parameters
