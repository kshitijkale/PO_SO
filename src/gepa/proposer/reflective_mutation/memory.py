# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gepa.core.callbacks import GEPACallback

from gepa.core.callbacks import (
    MemoryEntryAddedEvent,
    MemoryQueriedEvent,
    MemoryStateSnapshotEvent,
    notify_callbacks,
)


@dataclass
class ReflectionMemoryEntry:
    """One record of a reflection attempt: what was tried, why, and whether it worked."""

    iteration: int
    component_name: str
    change_summary: str
    score_before: float
    score_after: float
    accepted: bool
    failure_modes: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for logging/JSON output."""
        return {
            "iteration": self.iteration,
            "component_name": self.component_name,
            "change_summary": self.change_summary,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "accepted": self.accepted,
            "failure_modes": self.failure_modes,
        }


@dataclass
class ReflectionMemory:
    """Rolling episodic memory of past reflection attempts.

    Stores the last ``max_entries`` reflection outcomes so the reflection LLM
    can see what was tried before, what worked, and what failed.
    """

    max_entries: int = 10
    entries: list[ReflectionMemoryEntry] = field(default_factory=list)
    callbacks: list[GEPACallback] | None = field(default=None, repr=False)
    current_iteration: int = field(default=0, repr=False)

    def set_iteration(self, iteration: int) -> None:
        """Set the current iteration number for event reporting."""
        self.current_iteration = iteration

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable dict of the full memory state."""
        all_entries = [e.to_dict() for e in self.entries]
        component_counts: dict[str, int] = dict(Counter(e.component_name for e in self.entries))
        accepted_count = sum(1 for e in self.entries if e.accepted)
        total = len(self.entries)
        return {
            "max_entries": self.max_entries,
            "total_entries": total,
            "entries": all_entries,
            "entries_by_component": component_counts,
            "accepted_count": accepted_count,
            "rejected_count": total - accepted_count,
            "accepted_ratio": accepted_count / total if total > 0 else 0.0,
            "rejected_ratio": (total - accepted_count) / total if total > 0 else 0.0,
            "utilization": total / self.max_entries if self.max_entries > 0 else 0.0,
        }

    def fire_snapshot_event(self, phase: str) -> None:
        """Fire a MemoryStateSnapshotEvent with current state."""
        if not self.callbacks:
            return
        snap = self.snapshot()
        component_counts: dict[str, int] = dict(Counter(e.component_name for e in self.entries))
        total = len(self.entries)
        notify_callbacks(
            self.callbacks,
            "on_memory_state_snapshot",
            MemoryStateSnapshotEvent(
                iteration=self.current_iteration,
                phase=phase,
                all_entries=snap["entries"],
                total_entries=total,
                max_entries=self.max_entries,
                entries_by_component=component_counts,
                accepted_ratio=snap["accepted_ratio"],
                rejected_ratio=snap["rejected_ratio"],
            ),
        )

    def add(self, entry: ReflectionMemoryEntry) -> None:
        """Append an entry, evicting the oldest if over capacity."""
        size_before = len(self.entries)
        evicted_entry: ReflectionMemoryEntry | None = None

        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            evicted_entry = self.entries[0]
            self.entries = self.entries[-self.max_entries :]

        size_after = len(self.entries)

        if self.callbacks:
            notify_callbacks(
                self.callbacks,
                "on_memory_entry_added",
                MemoryEntryAddedEvent(
                    iteration=self.current_iteration,
                    component_name=entry.component_name,
                    entry=entry.to_dict(),
                    memory_size_before=size_before,
                    memory_size_after=size_after,
                    evicted_entry=evicted_entry.to_dict() if evicted_entry else None,
                    memory_utilization=size_after / self.max_entries if self.max_entries > 0 else 0.0,
                ),
            )

    def get_recent(self, n: int = 5, component_name: str | None = None) -> list[ReflectionMemoryEntry]:
        """Return the last *n* entries, optionally filtered by component name."""
        if component_name is not None:
            filtered = [e for e in self.entries if e.component_name == component_name]
        else:
            filtered = list(self.entries)
        return filtered[-n:]

    def format_for_prompt(self, component_name: str, max_recent: int = 7) -> str:
        """Render relevant memory entries as text to inject into the reflection prompt.

        Returns an empty string when there is nothing to show (first iteration,
        or no entries for this component), so the prompt degrades gracefully to
        the current memoryless behaviour.
        """
        relevant = self.get_recent(n=max_recent, component_name=component_name)
        if not relevant:
            if self.callbacks:
                notify_callbacks(
                    self.callbacks,
                    "on_memory_queried",
                    MemoryQueriedEvent(
                        iteration=self.current_iteration,
                        component_name=component_name,
                        query_n=max_recent,
                        entries_returned=[],
                        formatted_text="",
                        formatted_text_length=0,
                    ),
                )
            return ""

        lines: list[str] = ["## Optimization History\n"]
        for entry in relevant:
            status = "ACCEPTED" if entry.accepted else "REJECTED"
            lines.append(
                f"Iter {entry.iteration}: {entry.change_summary}. "
                f"Score: {entry.score_before:.2f} → {entry.score_after:.2f} ({status})"
            )
            if entry.failure_modes:
                # Show at most 2 failure modes to keep prompt compact
                modes = entry.failure_modes[:2]
                modes_str = "; ".join(f'"{m}"' for m in modes)
                lines.append(f"  Failures addressed: {modes_str}")
            lines.append("")

        lines.append("IMPORTANT: Do not repeat strategies that were REJECTED. Build on strategies that were ACCEPTED.")
        formatted_text = "\n".join(lines)

        if self.callbacks:
            notify_callbacks(
                self.callbacks,
                "on_memory_queried",
                MemoryQueriedEvent(
                    iteration=self.current_iteration,
                    component_name=component_name,
                    query_n=max_recent,
                    entries_returned=[e.to_dict() for e in relevant],
                    formatted_text=formatted_text,
                    formatted_text_length=len(formatted_text),
                ),
            )

        return formatted_text


def summarize_change(old_text: str, new_text: str, max_len: int = 120) -> str:
    """Produce a one-sentence description of how *new_text* differs from *old_text*.

    Uses ``difflib.SequenceMatcher`` to find the dominant change region.
    No LLM calls — purely heuristic.
    """
    if old_text == new_text:
        return "No changes"

    matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    opcodes = matcher.get_opcodes()

    # Collect the largest non-equal operation by size of the affected region
    best_tag: str | None = None
    best_old = ""
    best_new = ""
    best_size = 0

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        size = max(i2 - i1, j2 - j1)
        if size > best_size:
            best_size = size
            best_tag = tag
            best_old = old_text[i1:i2]
            best_new = new_text[j1:j2]

    if best_tag is None:
        return "No changes"

    def _truncate(s: str, limit: int) -> str:
        s = s.strip().replace("\n", " ")
        if len(s) > limit:
            return s[:limit] + "..."
        return s

    half = max_len // 2

    if best_tag == "insert":
        return f"Added: '{_truncate(best_new, max_len)}'"
    elif best_tag == "delete":
        return f"Removed: '{_truncate(best_old, max_len)}'"
    elif best_tag == "replace":
        return f"Changed: '{_truncate(best_old, half)}' → '{_truncate(best_new, half)}'"

    return "Modified component (large rewrite)"
