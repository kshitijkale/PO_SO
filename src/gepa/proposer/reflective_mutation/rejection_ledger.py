"""Rejection ledger: lightweight per-parent history of rejected mutations.

Records what was tried and how it scored so the reflection LLM avoids
repeating failed strategies.  ~300 chars of injection vs MemV0's 10K.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LedgerEntry:
    """One rejected mutation attempt."""

    diff_summary: str  # concrete string diff: what was literally added/removed/changed
    llm_summary: str  # LLM-generated semantic reason why it failed (empty if LM call failed)
    score: float  # new candidate's minibatch sum
    threshold: float  # parent's minibatch sum it needed to beat
    batch_size: int  # minibatch size — for display as "scored X/Y"
    iteration: int


@dataclass
class RejectionLedger:
    """Per-parent, per-component unbounded log of rejected mutations."""

    max_entries_per_parent: int = 8  # kept for API compatibility; no longer enforced
    max_prompt_chars: int = 600
    _store: dict[str, list[LedgerEntry]] = field(default_factory=dict)

    def _key(self, parent_hash: str, component_name: str) -> str:
        return f"{parent_hash}:{component_name}"

    def record(self, parent_hash: str, component_name: str, entry: LedgerEntry) -> None:
        """Append a rejection entry. All entries are kept — no eviction."""
        key = self._key(parent_hash, component_name)
        self._store.setdefault(key, []).append(entry)

    def get_entries(self, parent_hash: str, component_name: str) -> list[LedgerEntry]:
        """Return stored entries (may be empty)."""
        return list(self._store.get(self._key(parent_hash, component_name), []))

    def format_for_prompt(self, parent_hash: str, component_name: str) -> str:
        """Render rejection history for prompt injection, or ``""`` if empty."""
        entries = self.get_entries(parent_hash, component_name)
        if not entries:
            return ""

        lines = ["== PAST ATTEMPTS FROM THIS PROMPT (context only — use your judgment) =="]
        for e in entries:
            score_int = int(e.score) if e.score == int(e.score) else e.score
            thresh_int = int(e.threshold) if e.threshold == int(e.threshold) else e.threshold
            line = f'• Diff: {e.diff_summary}'
            if e.llm_summary:
                line += f' | Why it failed: "{e.llm_summary}"'
            line += f" — scored {score_int}/{e.batch_size}, needed >{thresh_int}/{e.batch_size}"
            lines.append(line)

        lines.append("")
        lines.append(
            "These attempts didn't help on past minibatches. They may or may not be relevant to the current one."
        )
        return "\n".join(lines)
