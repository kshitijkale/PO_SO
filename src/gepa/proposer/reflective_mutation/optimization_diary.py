"""Optimization Diary: global optimization context for the reflector.

Tracks accepted mutations across the entire run.  Two layers:

- Layer 1 (heuristic): Pure data — iteration stats, full diffs of accepted edits.
  Zero LLM calls.
- Layer 2 (strategy notes): One LLM call per iteration to curate a ≤400 char
  summary of optimization patterns.  Only active when ``strategy_notes_lm``
  is provided.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any

from gepa.proposer.reflective_mutation.base import LanguageModel

_logger = logging.getLogger(__name__)


def _unified_diff(old_text: str, new_text: str) -> str:
    """Compute a unified diff between two texts."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after", n=1)
    return "".join(diff).strip()


@dataclass
class DiaryEntry:
    """Record of a single optimization iteration outcome."""

    iteration: int
    accepted: bool
    score_before: float  # parent minibatch sum
    score_after: float  # proposed minibatch sum
    batch_size: int
    diff: str  # full unified diff for accepted; short summary for rejected
    component_name: str


@dataclass
class OptimizationDiary:
    """Global optimization memory injected into the reflection prompt.

    Unlike the per-parent RejectionLedger, this diary persists across all
    candidate transitions and records both accepted and rejected mutations.
    """

    max_accepted_display: int = 5
    max_strategy_notes_chars: int = 400
    strategy_notes_lm: LanguageModel | None = None
    callbacks: list[Any] | None = None

    _entries: list[DiaryEntry] = field(default_factory=list)
    _strategy_notes: str = ""

    def record(self, entry: DiaryEntry) -> None:
        """Append an iteration outcome."""
        self._entries.append(entry)

    def update_strategy_notes(self) -> None:
        """Layer 2: update strategy notes via LLM.  No-op if no LM configured."""
        if self.strategy_notes_lm is None:
            return

        recent = self._entries[-5:]
        if not recent:
            return

        context_lines: list[str] = []
        for e in recent:
            status = "ACCEPTED" if e.accepted else "REJECTED"
            # Truncate diff for L2 context to keep the prompt manageable
            diff_preview = e.diff[:200] + "..." if len(e.diff) > 200 else e.diff
            context_lines.append(
                f"  iter {e.iteration}: {status} | "
                f"scored {e.score_after:.0f}/{e.batch_size} (needed >{e.score_before:.0f}/{e.batch_size}) | "
                f'"{diff_preview}"'
            )
        context = "\n".join(context_lines)

        total = len(self._entries)
        accepted = sum(1 for e in self._entries if e.accepted)
        rate = (accepted / total * 100) if total else 0

        prompt = (
            "You are tracking the progress of an optimization run that improves AI prompts.\n"
            "Below are the current strategy notes and the last few iteration outcomes.\n\n"
            f"Current notes:\n{self._strategy_notes or '(none yet)'}\n\n"
            f"Recent history:\n{context}\n\n"
            f"Cumulative: {total} iterations, {accepted} accepted ({rate:.0f}%)\n\n"
            "Update the strategy notes in max 400 characters. Focus on:\n"
            "- What types of changes have worked vs failed\n"
            "- What direction to try next\n"
            "Respond with ONLY the updated notes, no explanation."
        )

        try:
            raw = self.strategy_notes_lm(prompt)
            if raw and raw.strip():
                self._strategy_notes = raw.strip()[: self.max_strategy_notes_chars]
        except Exception:
            _logger.debug("Strategy notes LLM call failed; keeping existing notes.")

    def format_for_prompt(self) -> str:
        """Render the full diary block for injection into the reflection prompt.

        Returns ``""`` when there are no entries yet.
        """
        if not self._entries:
            return ""

        total = len(self._entries)
        accepted_entries = [e for e in self._entries if e.accepted]
        accepted_count = len(accepted_entries)
        rate = accepted_count / total * 100

        # Best batch score seen
        best_batch = max(e.score_after for e in self._entries)
        best_batch_size = next(e.batch_size for e in self._entries if e.score_after == best_batch)

        lines: list[str] = []
        lines.append("== OPTIMIZATION CONTEXT ==")
        lines.append(
            f"Progress: {total} iterations | {accepted_count} accepted ({rate:.0f}%) | "
            f"Best batch: {best_batch:.0f}/{best_batch_size}"
        )

        # Accepted edits — full diffs
        if accepted_entries:
            lines.append("")
            lines.append("Edits that improved scores:")
            for e in accepted_entries[-self.max_accepted_display :]:
                delta = e.score_after - e.score_before
                lines.append(f"  --- iter {e.iteration} (+{delta:.1f} score) ---")
                lines.append(e.diff)

        # Layer 2 strategy notes
        if self._strategy_notes:
            lines.append("")
            lines.append("Strategy patterns:")
            lines.append(f"  {self._strategy_notes}")

        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for observability logging."""
        return {
            "total_entries": len(self._entries),
            "accepted_count": sum(1 for e in self._entries if e.accepted),
            "rejected_count": sum(1 for e in self._entries if not e.accepted),
            "strategy_notes": self._strategy_notes,
            "layer2_active": self.strategy_notes_lm is not None,
            "entries": [
                {
                    "iteration": e.iteration,
                    "accepted": e.accepted,
                    "score_before": e.score_before,
                    "score_after": e.score_after,
                    "batch_size": e.batch_size,
                    "diff": e.diff,
                    "component_name": e.component_name,
                }
                for e in self._entries
            ],
        }
