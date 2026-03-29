# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

from __future__ import annotations

import difflib


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
