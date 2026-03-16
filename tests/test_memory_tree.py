"""Tests for MemoryTree data structure."""

import json

import pytest

from gepa.proposer.reflective_mutation.memory_tree import (
    MemoryTree,
    OutcomeDescription,
)


def _make_outcome(question: str = "Q", result: str = "correct", iteration: int = 0) -> OutcomeDescription:
    return OutcomeDescription(
        question_summary=question,
        result=result,
        observation=f"Observation for {question}",
        error_type=None if result == "correct" else "arithmetic",
        score=1.0 if result == "correct" else 0.0,
        iteration=iteration,
    )


class TestAddRootAndChild:
    def test_add_root(self):
        tree = MemoryTree()
        nid = tree.add_root({"p": "hello"}, iteration=0)
        assert nid == 0
        assert tree.root_id == 0
        assert tree.nodes[0].prompt == {"p": "hello"}

    def test_add_root_twice_raises(self):
        tree = MemoryTree()
        tree.add_root({"p": "hello"}, iteration=0)
        with pytest.raises(ValueError, match="Root already exists"):
            tree.add_root({"p": "world"}, iteration=1)

    def test_add_child(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "v1"}, iteration=0)
        child = tree.add_child(root, {"p": "v2"}, accepted=True, minibatch_score=0.5, minibatch_ids=[1, 2], iteration=1)
        assert child == 1
        assert tree.nodes[child].parent_id == root
        assert child in tree.nodes[root].children_ids
        assert len(tree.edges) == 1
        assert tree.edges[0].parent_id == root
        assert tree.edges[0].child_id == child

    def test_add_child_invalid_parent(self):
        tree = MemoryTree()
        with pytest.raises(KeyError):
            tree.add_child(999, {"p": "v"}, accepted=True, minibatch_score=None, minibatch_ids=[], iteration=0)


class TestGetAncestry:
    def test_root_ancestry(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        ancestry = tree.get_ancestry(root)
        assert len(ancestry) == 1
        assert ancestry[0].node_id == root

    def test_three_level_ancestry(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(root, {"p": "v1"}, True, 0.5, [], 1)
        c2 = tree.add_child(c1, {"p": "v2"}, True, 0.6, [], 2)
        ancestry = tree.get_ancestry(c2)
        assert [n.node_id for n in ancestry] == [root, c1, c2]


class TestGetSiblings:
    def test_siblings(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(root, {"p": "v1"}, True, 0.5, [], 1)
        c2 = tree.add_child(root, {"p": "v2"}, False, 0.3, [], 1, rejection_reason="worse")
        siblings = tree.get_siblings(c1)
        assert len(siblings) == 1
        assert siblings[0].node_id == c2

    def test_root_has_no_siblings(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        assert tree.get_siblings(root) == []


class TestGetRejectedChildren:
    def test_rejected_children(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.add_child(root, {"p": "good"}, True, 0.6, [], 1)
        c_rej = tree.add_child(root, {"p": "bad"}, False, 0.2, [], 2, rejection_reason="worse")
        rejected = tree.get_rejected_children(root)
        assert len(rejected) == 1
        assert rejected[0].node_id == c_rej


class TestOutcomesAndEviction:
    def test_add_outcomes(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        outcomes = [_make_outcome("Q1"), _make_outcome("Q2")]
        tree.add_outcomes(root, outcomes)
        assert len(tree.nodes[root].outcomes) == 2

    def test_outcomes_accumulate(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.add_outcomes(root, [_make_outcome("Q1")])
        tree.add_outcomes(root, [_make_outcome("Q2")])
        assert len(tree.nodes[root].outcomes) == 2

    def test_eviction_trigger(self):
        tree = MemoryTree(outcome_eviction_k=3)
        root = tree.add_root({"p": "seed"}, iteration=0)
        # Add 7 outcomes (> 2*3=6 threshold)
        tree.add_outcomes(root, [_make_outcome(f"Q{i}", iteration=i) for i in range(7)])
        assert tree.needs_eviction(root)

    def test_eviction_not_triggered(self):
        tree = MemoryTree(outcome_eviction_k=5)
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.add_outcomes(root, [_make_outcome(f"Q{i}") for i in range(8)])
        assert not tree.needs_eviction(root)  # 8 <= 2*5=10

    def test_evict_oldest(self):
        tree = MemoryTree(outcome_eviction_k=3)
        root = tree.add_root({"p": "seed"}, iteration=0)
        outcomes = [_make_outcome(f"Q{i}", iteration=i) for i in range(7)]
        tree.add_outcomes(root, outcomes)
        evicted = tree.evict_oldest(root)
        assert len(evicted) == 3
        assert evicted[0].question_summary == "Q0"
        assert len(tree.nodes[root].outcomes) == 4


class TestBranches:
    def test_branches_not_in_ancestry(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        c1 = tree.add_child(root, {"p": "v1"}, True, 0.5, [], 1)
        c2 = tree.add_child(root, {"p": "v2"}, True, 0.4, [], 1)
        tree.add_child(c1, {"p": "v3"}, True, 0.6, [], 2)  # in ancestry of c3
        c3 = tree.add_child(c1, {"p": "v4"}, True, 0.7, [], 3)

        branches = tree.get_branches_not_in_ancestry(c3)
        branch_roots = {b[0].node_id for b in branches}
        # c2 is a branch from root not in ancestry; c3's sibling (v3) is also a branch
        assert c2 in branch_roots


class TestSetValScore:
    def test_set_val_score(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.set_val_score(root, 0.75)
        assert tree.nodes[root].val_score == 0.75


class TestToDict:
    def test_serializable(self):
        tree = MemoryTree()
        root = tree.add_root({"p": "seed"}, iteration=0)
        tree.add_child(root, {"p": "v1"}, True, 0.5, ["id1"], 1)
        tree.add_outcomes(root, [_make_outcome("Q1")])
        tree.update_global_summary("test summary")

        d = tree.to_dict()
        # Should be JSON-serializable
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        assert d["root_id"] == root
        assert d["global_summary"] == "test summary"
        assert len(d["nodes"]) == 2
        assert len(d["edges"]) == 1


class TestDisconnectedNode:
    def test_add_disconnected(self):
        tree = MemoryTree()
        tree.add_root({"p": "seed"}, iteration=0)
        nid = tree.add_disconnected_node({"p": "merged"}, iteration=5)
        assert tree.nodes[nid].parent_id is None
        assert nid != tree.root_id
