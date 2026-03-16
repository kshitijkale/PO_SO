# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

from __future__ import annotations

import difflib

from gepa.proposer.reflective_mutation.memory_tree import MemoryTree, OutcomeDescription


class TieredMemoryRenderer:
    """Renders memory tree into tiered text for the reflection LLM.

    Granularity decreases with genealogical distance from the current candidate:
    - Ring 1: Immediate family (parent, siblings, rejected children) — highest detail
    - Ring 2: Ancestry chain (root to grandparent) — diffs + val scores
    - Ring 3: Other branches — one-line summaries
    - Ring 4: Global compressed summary
    """

    def __init__(
        self,
        ring1_char_budget: int = 6000,
        ring2_char_budget: int = 2000,
        ring3_char_budget: int = 2000,
        ring4_char_budget: int = 1200,
    ) -> None:
        self.ring1_budget = ring1_char_budget
        self.ring2_budget = ring2_char_budget
        self.ring3_budget = ring3_char_budget
        self.ring4_budget = ring4_char_budget

    def render(self, tree: MemoryTree, current_node_id: int) -> str:
        """Render the memory tree from the perspective of the current node."""
        if current_node_id not in tree.nodes:
            return ""

        node = tree.get_node(current_node_id)

        # First iteration (root only, no outcomes yet) — nothing useful to show
        if node.parent_id is None and not node.outcomes and not node.children_ids:
            return ""

        sections: list[str] = []

        ring4 = self._render_ring4(tree)
        ring2 = self._render_ring2(tree, current_node_id)
        ring1 = self._render_ring1(tree, current_node_id)
        ring3 = self._render_ring3(tree, current_node_id)

        for section in [ring4, ring2, ring1, ring3]:
            if section:
                sections.append(section)

        if not sections:
            return ""

        body = "\n\n".join(sections)

        header = (
            "== OPTIMIZATION HISTORY ==\n"
            "Read this section carefully before proposing a new instruction.\n"
            "\n"
            "This shows the full history of prompt mutations tried so far, organized by proximity to the current prompt:\n"
            "- [How we got here]: the chain of accepted changes from the seed prompt to now, with validation scores\n"
            "- [Parent prompt]: the previous prompt this one was mutated from, shown in full\n"
            "- [What changed to get current prompt]: the specific edit that produced the current prompt\n"
            "- [Other attempts from same parent]: sibling mutations tried from the same parent — both accepted and rejected\n"
            "- [Rejected attempts from current prompt]: mutations tried from the current prompt that were rejected\n"
            "- [Other exploration branches]: other directions explored earlier in the run\n"
            "- [Global patterns]: compressed summary of broad patterns observed across the whole run\n"
            "\n"
            "Where shown, 'Outcomes:' lines are per-question observations of what the model actually did "
            "during evaluation (grounded in reasoning traces, not advice). Use them to understand WHY a "
            "direction helped or hurt, not just that it did.\n"
            "\n"
            "Use this history to:\n"
            "- AVOID repeating changes that were already REJECTED — they failed for the specific reasons shown\n"
            "- BUILD ON the trajectory of ACCEPTED changes that improved scores\n"
            "- Diagnose whether the current prompt's failures share a pattern with past failures or are new\n"
        )

        footer = (
            "\nTaking the history above into account, propose a new instruction that:\n"
            "1. Addresses the failures shown in the current evaluation examples\n"
            "2. Does not repeat any of the rejected strategies listed above\n"
            "3. Is informed by what the per-question outcome observations reveal about model behaviour\n"
        )

        return header + "\n" + body + "\n" + footer

    def _render_ring1(self, tree: MemoryTree, node_id: int) -> str:
        """Ring 1: Immediate family — parent, siblings, rejected children."""
        node = tree.get_node(node_id)
        parts: list[str] = []
        budget_remaining = self.ring1_budget

        # Parent full text + diff to current
        if node.parent_id is not None:
            parent = tree.get_node(node.parent_id)
            parent_text = self._candidate_text(parent.prompt)
            current_text = self._candidate_text(node.prompt)
            diff = self._compute_diff(parent_text, current_text)

            parent_section = f"[Parent prompt — val={self._fmt_score(parent.val_score)}]\n{parent_text}"
            budget_remaining -= len(parent_section)

            diff_section = f"\n[What changed to get current prompt]\n{diff}"
            budget_remaining -= len(diff_section)

            parts.append(parent_section)
            parts.append(diff_section)

            # Siblings (other children of same parent), newest first
            siblings = tree.get_siblings(node_id)
            siblings.sort(key=lambda s: s.iteration_created, reverse=True)
            if siblings:
                sib_lines: list[str] = ["[Other attempts from same parent]"]
                for sib in siblings:
                    sib_diff = self._compute_diff(parent_text, self._candidate_text(sib.prompt))
                    status = f"val={self._fmt_score(sib.val_score)}" if sib.accepted else "REJECTED"
                    if sib.rejection_reason:
                        status += f" ({sib.rejection_reason})"
                    outcomes_brief = self._format_outcomes_brief(sib.outcomes)
                    line = f"• {status}: {sib_diff}"
                    if outcomes_brief:
                        line += f"\n  Outcomes: {outcomes_brief}"
                    sib_lines.append(line)
                    budget_remaining -= len(line) + 1
                    if budget_remaining <= 0:
                        break
                parts.append("\n".join(sib_lines))

        # Rejected children of current node
        rejected = tree.get_rejected_children(node_id)
        if rejected:
            current_text = self._candidate_text(node.prompt)
            rej_lines: list[str] = ["[Rejected attempts from current prompt]"]
            for rej in rejected:
                rej_diff = self._compute_diff(current_text, self._candidate_text(rej.prompt))
                reason = f" ({rej.rejection_reason})" if rej.rejection_reason else ""
                outcomes_brief = self._format_outcomes_brief(rej.outcomes)
                line = f"• REJECTED{reason}: {rej_diff}"
                if outcomes_brief:
                    line += f"\n  Outcomes: {outcomes_brief}"
                rej_lines.append(line)
                budget_remaining -= len(line) + 1
                if budget_remaining <= 0:
                    break
            parts.append("\n".join(rej_lines))

        result = "\n\n".join(parts)
        return result[: self.ring1_budget] if len(result) > self.ring1_budget else result

    def _render_ring2(self, tree: MemoryTree, node_id: int) -> str:
        """Ring 2: Ancestry chain from root to grandparent as diffs + val scores."""
        ancestry = tree.get_ancestry(node_id)

        # Need at least 3 nodes (root, ..., parent, current) to have a grandparent
        if len(ancestry) < 3:
            return ""

        # Exclude the current node and its parent (shown in Ring 1)
        ancestors = ancestry[:-2]

        chain_parts: list[str] = []
        for i, anc in enumerate(ancestors):
            score_str = self._fmt_score(anc.val_score)
            if i == 0:
                chain_parts.append(f"Seed (val={score_str})")
            else:
                prev = ancestors[i - 1]
                diff = self._compute_diff(
                    self._candidate_text(prev.prompt),
                    self._candidate_text(anc.prompt),
                    max_len=80,
                )
                chain_parts.append(f'"{diff}" (val={score_str})')

        chain = " → ".join(chain_parts) + " → [parent] → current"
        result = f"[How we got here]\n{chain}"
        return result[: self.ring2_budget] if len(result) > self.ring2_budget else result

    def _render_ring3(self, tree: MemoryTree, node_id: int) -> str:
        """Ring 3: Branches not in the current ancestry — one-line summaries."""
        branches = tree.get_branches_not_in_ancestry(node_id)
        if not branches:
            return ""

        lines: list[str] = ["[Other exploration branches]"]
        budget_remaining = self.ring3_budget - len(lines[0])

        for branch_root, depth, peak_val in branches:
            parent_id = branch_root.parent_id
            parent_label = f"node {parent_id}" if parent_id is not None else "seed"
            status = "accepted" if branch_root.accepted else "rejected"
            peak_str = self._fmt_score(peak_val)
            line = f"• From {parent_label}: {depth} node(s), peaked val={peak_str}, {status}"
            if branch_root.outcome_summary:
                line += f" — {branch_root.outcome_summary[:60]}"
            lines.append(line)
            budget_remaining -= len(line) + 1
            if budget_remaining <= 0:
                break

        result = "\n".join(lines)
        return result[: self.ring3_budget] if len(result) > self.ring3_budget else result

    def _render_ring4(self, tree: MemoryTree) -> str:
        """Ring 4: Global compressed summary."""
        if not tree.global_summary:
            return ""
        summary = tree.global_summary[: self.ring4_budget - 20]
        return f"[Global patterns]\n{summary}"

    @staticmethod
    def _candidate_text(prompt: dict[str, str]) -> str:
        """Flatten a candidate dict into a single string for diffing."""
        if len(prompt) == 1:
            return next(iter(prompt.values()))
        return "\n---\n".join(f"[{k}]\n{v}" for k, v in prompt.items())

    @staticmethod
    def _compute_diff(old_text: str, new_text: str, max_len: int = 120) -> str:
        """Heuristic one-line diff summary using SequenceMatcher."""
        if old_text == new_text:
            return "No changes"

        matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
        opcodes = matcher.get_opcodes()

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
            return s[:limit] + "..." if len(s) > limit else s

        half = max_len // 2

        if best_tag == "insert":
            return f"Added: '{_truncate(best_new, max_len)}'"
        if best_tag == "delete":
            return f"Removed: '{_truncate(best_old, max_len)}'"
        if best_tag == "replace":
            return f"Changed: '{_truncate(best_old, half)}' → '{_truncate(best_new, half)}'"

        return "Modified (large rewrite)"

    @staticmethod
    def _format_outcomes_brief(outcomes: list[OutcomeDescription], max_chars: int = 200) -> str:
        """Format up to 3 most recent outcomes as a brief summary."""
        if not outcomes:
            return ""
        recent = outcomes[-3:]
        parts = [o.observation for o in recent if o.observation and not o.observation.startswith("(OI")]
        if not parts:
            return ""
        text = "; ".join(parts)
        return text[:max_chars] + "..." if len(text) > max_chars else text

    @staticmethod
    def _fmt_score(score: float | None) -> str:
        if score is None:
            return "?"
        return f"{score:.2f}"
