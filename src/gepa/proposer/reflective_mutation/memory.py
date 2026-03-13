# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

from __future__ import annotations

import difflib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gepa.core.callbacks import GEPACallback
    from gepa.proposer.reflective_mutation.base import LanguageModel

_logger = logging.getLogger(__name__)

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
    score_before: float
    score_after: float
    accepted: bool
    failure_modes: list[str]

    # V2 fields (populated by lesson LLM; all default to "" / [] for fallback path)
    intent: str = ""
    lesson: str = ""
    categories_succeeded: list[str] = field(default_factory=list)
    categories_failed: list[str] = field(default_factory=list)

    # V1 fallback (populated only when lesson LLM call fails)
    change_summary: str = ""

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
            "intent": self.intent,
            "lesson": self.lesson,
            "categories_succeeded": self.categories_succeeded,
            "categories_failed": self.categories_failed,
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

    def format_for_prompt(self, component_name: str, max_recent: int = 5) -> str:
        """Render relevant memory entries as text to inject into the reflection prompt.

        Returns an empty string when there is nothing to show (first iteration,
        or no entries for this component), so the prompt degrades gracefully to
        the current memoryless behaviour.

        Uses V2 format (Intent/Lesson/Categories) when ``lesson`` is populated,
        otherwise falls back to V1 format (change_summary + failure modes).
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
            delta = entry.score_after - entry.score_before
            status = "ACCEPTED" if entry.accepted else "REJECTED"

            if entry.lesson:
                # V2 path: structured lesson from LLM
                lines.append(f"### Iter {entry.iteration} [{status} {delta:+.2f}]")
                if entry.intent:
                    lines.append(f"Intent: {entry.intent}")
                lines.append(f"Lesson: {entry.lesson}")
                if entry.categories_succeeded:
                    lines.append(f"Strong on: {', '.join(entry.categories_succeeded)}")
                if entry.categories_failed:
                    lines.append(f"Still failing: {', '.join(entry.categories_failed)}")
            else:
                # V1 fallback path: heuristic change summary
                lines.append(
                    f"Iter {entry.iteration}: {entry.change_summary}. "
                    f"Score: {entry.score_before:.2f} → {entry.score_after:.2f} ({status})"
                )
                if entry.failure_modes:
                    modes = entry.failure_modes[:2]
                    modes_str = "; ".join(f'"{m}"' for m in modes)
                    lines.append(f"  Failures addressed: {modes_str}")

            lines.append("")

        # Persistent weak spots: categories failing across 2+ entries
        all_failed: list[str] = []
        for e in relevant:
            all_failed.extend(e.categories_failed)
        persistent = [cat for cat, count in Counter(all_failed).items() if count >= 2]
        if persistent:
            lines.append(f"Persistent weak spots: {', '.join(persistent)}")
            lines.append("")

        lines.append("IMPORTANT: Do not repeat REJECTED strategies. Build on ACCEPTED ones.")
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


_LESSON_PROMPT_TEMPLATE = """\
You are analyzing one iteration of a prompt optimization loop.

Task objective: {objective}

## Prompt Before
{old_text}

## Prompt After
{new_text}

## Evaluation Examples (from evaluation before this edit)
{evaluation_examples_block}

## Aggregate Scores
Total before: {score_before:.2f}
Total after: {score_after:.2f}
Delta: {score_delta:+.2f}

## Per-Example Scores
{per_example_scores_block}

## Outcome
Score: {score_before:.2f} → {score_after:.2f} ({status})

---
Analyze this step. Be specific — reference the actual failures and changes.

Respond with ONLY a JSON object, no other text:
{{
  "intent": "<1 sentence: what was this edit trying to achieve?>",
  "lesson": "<1-2 sentences: what does the outcome reveal about this strategy?>",
  "categories_succeeded": ["<problem type>", ...],
  "categories_failed": ["<problem type>", ...]
}}

For categories, infer concise labels directly from per-example score deltas and\
 the evaluation context above (e.g., recurring failure/success patterns).\
 If the evidence is weak, use empty lists.\
"""


def generate_lesson(
    lm: LanguageModel,
    old_text: str,
    new_text: str,
    failure_feedbacks: list[str],
    score_before: float,
    score_after: float,
    accepted: bool,
    per_example_scores_before: list[float] | None = None,
    per_example_scores_after: list[float] | None = None,
    objective: str = "",
) -> tuple[str, str, list[str], list[str]]:
    """Call the lesson LLM to produce a structured lesson from one optimization step.

    Returns ``(intent, lesson, categories_succeeded, categories_failed)``.
    Returns ``("", "", [], [])`` on any exception so the caller can fall back
    to :func:`summarize_change`.

    Note:
        ``failure_feedbacks`` now carries general per-example evaluation context
        (not only failures) for backward compatibility with existing call sites.
    """
    try:
        if failure_feedbacks:
            evaluation_examples_block = "\n".join(f"- {fb}" for fb in failure_feedbacks)
        else:
            evaluation_examples_block = "(no evaluation examples available)"

        if per_example_scores_before and per_example_scores_after:
            pairs = zip(per_example_scores_before, per_example_scores_after, strict=False)
            per_example_scores_block = "\n".join(
                f"- Example {idx + 1}: before={before:.2f}, after={after:.2f}, delta={after - before:+.2f}"
                for idx, (before, after) in enumerate(pairs)
            )
        else:
            per_example_scores_block = "(no per-example scores available)"

        prompt = _LESSON_PROMPT_TEMPLATE.format(
            objective=objective or "(not specified)",
            old_text=old_text or "(empty)",
            new_text=new_text or "(empty)",
            evaluation_examples_block=evaluation_examples_block,
            score_before=score_before,
            score_after=score_after,
            score_delta=score_after - score_before,
            per_example_scores_block=per_example_scores_block,
            status="ACCEPTED" if accepted else "REJECTED",
        )

        raw_response = lm(prompt)

        # Strip markdown code fences if present
        text = raw_response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        parsed = json.loads(text)

        intent = str(parsed.get("intent", ""))
        lesson = str(parsed.get("lesson", ""))
        cats_ok = [str(c) for c in parsed.get("categories_succeeded", [])]
        cats_fail = [str(c) for c in parsed.get("categories_failed", [])]

        return intent, lesson, cats_ok, cats_fail

    except Exception as e:
        _logger.warning("generate_lesson failed, falling back to summarize_change: %s", e)
        return "", "", [], []
