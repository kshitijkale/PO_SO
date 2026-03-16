"""Tests for TieredMemoryRenderer."""

from gepa.proposer.reflective_mutation.memory_renderer import TieredMemoryRenderer
from gepa.proposer.reflective_mutation.memory_tree import MemoryTree, OutcomeDescription


def _make_outcome(q: str = "Q", result: str = "incorrect", obs: str = "Obs", iteration: int = 0) -> OutcomeDescription:
    return OutcomeDescription(q, result, obs, "arithmetic" if result == "incorrect" else None, 0.0 if result == "incorrect" else 1.0, iteration)


class TestEmptyAndMinimalTrees:
    def test_empty_tree(self):
        tree = MemoryTree()
        tree.add_root({"p": "seed"}, iteration=0)
        renderer = TieredMemoryRenderer()
        # Root with no outcomes and no children → nothing to show
        assert renderer.render(tree, 0) == ""

    def test_invalid_node_id(self):
        tree = MemoryTree()
        tree.add_root({"p": "seed"}, iteration=0)
        renderer = TieredMemoryRenderer()
        assert renderer.render(tree, 999) == ""


class TestRing1:
    def test_parent_and_diff_shown(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "Solve carefully."}, iteration=0)
        tree.set_val_score(root, 0.45)
        child = tree.add_child(root, {"p": "Solve carefully. Verify your answer."}, True, 0.6, [], 1)

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, child)
        assert "OPTIMIZATION HISTORY" in text
        assert "Parent prompt" in text
        assert "Solve carefully." in text
        assert "Verify your answer" in text or "Added" in text

    def test_siblings_listed(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(root, 0.45)
        c1 = tree.add_child(root, {"p": "seed v1"}, True, 0.5, [], 1)
        tree.add_child(root, {"p": "seed v2"}, False, 0.3, [], 1, rejection_reason="scored 0.3/3 vs 0.5/3")

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        assert "Other attempts from same parent" in text
        assert "REJECTED" in text

    def test_rejected_children_shown(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(root, 0.45)
        c1 = tree.add_child(root, {"p": "seed improved"}, True, 0.5, [], 1)
        tree.add_child(c1, {"p": "seed improved v2"}, False, 0.3, [], 2, rejection_reason="worse")
        tree.add_outcomes(tree.nodes[2].node_id, [_make_outcome("Q1", obs="Model failed on geometry")])

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        assert "Rejected attempts from current prompt" in text

    def test_sibling_outcomes_shown(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(root, 0.45)
        c1 = tree.add_child(root, {"p": "seed v1"}, True, 0.5, [], 1)
        c2 = tree.add_child(root, {"p": "seed v2"}, False, 0.3, [], 1, rejection_reason="worse")
        tree.add_outcomes(c2, [_make_outcome("Q1", obs="Model confused area with perimeter")])

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        assert "confused area" in text or "Outcomes:" in text


class TestRing2:
    def test_ancestry_chain(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(r, 0.45)
        c1 = tree.add_child(r, {"p": "seed step-by-step"}, True, 0.48, [], 1)
        tree.set_val_score(c1, 0.48)
        c2 = tree.add_child(c1, {"p": "seed step-by-step verify"}, True, 0.51, [], 2)
        tree.set_val_score(c2, 0.51)
        c3 = tree.add_child(c2, {"p": "seed step-by-step verify final"}, True, 0.52, [], 3)

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c3)
        assert "How we got here" in text
        assert "Seed" in text

    def test_short_ancestry_no_ring2(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(r, {"p": "seed v1"}, True, 0.5, [], 1)
        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        # Only 2 nodes in ancestry (root + child) → no Ring 2 section rendered
        # The header mentions "[How we got here]" as a section name, but the actual
        # section body line starts with "[How we got here]\n" followed by chain text.
        # Check that no ancestry chain content appears (no "→" chain line)
        assert "Seed (val=" not in text


class TestRing3:
    def test_other_branches(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(r, {"p": "branch A"}, True, 0.5, [], 1)
        tree.set_val_score(c1, 0.5)
        c2 = tree.add_child(r, {"p": "branch B"}, True, 0.4, [], 1)
        tree.set_val_score(c2, 0.4)
        # c2 is an "other branch" from c1's perspective
        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        assert "Other exploration branches" in text


class TestRing4:
    def test_global_summary(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(r, {"p": "v1"}, True, 0.5, [], 1)
        tree.update_global_summary("Verification steps help on geometry but hurt combinatorics.")

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        assert "Global patterns" in text
        assert "Verification steps" in text

    def test_no_global_summary(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(r, {"p": "v1"}, True, 0.5, [], 1)

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, c1)
        # "[Global patterns]\n" is the rendered ring body header; it should not appear
        # (the explanation header mentions "[Global patterns]:" with colon + description, not a standalone section)
        assert "[Global patterns]\n" not in text


class TestUsageInstructions:
    def test_header_explains_sections(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(r, 0.45)
        tree.add_child(r, {"p": "v1"}, True, 0.5, [], 1)

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, 1)
        # Header explains what each section means
        assert "Read this section carefully before proposing" in text
        assert "AVOID repeating changes that were already REJECTED" in text
        assert "BUILD ON the trajectory of ACCEPTED changes" in text
        # Explains what Outcomes lines are
        assert "Outcomes:" in text or "per-question observations" in text

    def test_header_explains_outcomes(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(r, 0.45)
        tree.add_child(r, {"p": "v1"}, True, 0.5, [], 1)

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, 1)
        assert "grounded in reasoning traces" in text

    def test_footer_numbered_action_steps(self):
        tree = MemoryTree()
        r = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(r, 0.45)
        tree.add_child(r, {"p": "v1"}, True, 0.5, [], 1)

        renderer = TieredMemoryRenderer()
        text = renderer.render(tree, 1)
        # Footer gives numbered criteria for the new instruction
        assert "Addresses the failures shown in the current evaluation" in text
        assert "Does not repeat any of the rejected strategies" in text
        assert "per-question outcome observations" in text


class TestComputeDiff:
    def test_added(self):
        diff = TieredMemoryRenderer._compute_diff("Hello", "Hello World")
        assert "Added" in diff

    def test_removed(self):
        diff = TieredMemoryRenderer._compute_diff("Hello World", "Hello")
        assert "Removed" in diff

    def test_changed(self):
        diff = TieredMemoryRenderer._compute_diff("Hello World", "Hello Earth")
        assert "Changed" in diff

    def test_no_changes(self):
        diff = TieredMemoryRenderer._compute_diff("same", "same")
        assert diff == "No changes"
