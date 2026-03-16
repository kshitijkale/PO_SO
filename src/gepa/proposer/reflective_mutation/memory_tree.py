# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)


@dataclass
class OutcomeDescription:
    """Per-question observation produced by the Outcome Interpreter."""

    question_summary: str  # first ~80 chars of problem
    result: str  # "correct" / "incorrect"
    observation: str  # 1-3 sentences grounded in reasoning trace
    error_type: str | None  # arithmetic | logic | setup | interpretation | none
    score: float
    iteration: int


@dataclass
class MemoryTreeNode:
    """A prompt candidate stored as a node in the memory tree."""

    node_id: int
    prompt: dict[str, str]
    parent_id: int | None
    children_ids: list[int] = field(default_factory=list)
    accepted: bool = True
    val_score: float | None = None
    minibatch_score: float | None = None
    outcomes: list[OutcomeDescription] = field(default_factory=list)
    outcome_summary: str = ""
    iteration_created: int = 0
    rejection_reason: str = ""


@dataclass
class MemoryTreeEdge:
    """An edge representing a mutation from parent to child prompt."""

    parent_id: int
    child_id: int
    minibatch_ids: list[Any] = field(default_factory=list)
    iteration: int = 0


class MemoryTree:
    """Tree-structured memory for prompt optimization history.

    Each node is a prompt candidate with per-question outcome observations.
    The tree is rooted at the seed prompt; children are mutations proposed by
    the reflection LLM. Both accepted and rejected candidates are stored.
    """

    def __init__(self, outcome_eviction_k: int = 10) -> None:
        self.nodes: dict[int, MemoryTreeNode] = {}
        self.edges: list[MemoryTreeEdge] = []
        self.root_id: int | None = None
        self._next_node_id: int = 0
        self.outcome_eviction_k: int = outcome_eviction_k
        self.global_summary: str = ""

    def _alloc_id(self) -> int:
        nid = self._next_node_id
        self._next_node_id += 1
        return nid

    def add_root(self, candidate: dict[str, str], iteration: int) -> int:
        """Add the seed prompt as the root node. Must be called exactly once."""
        if self.root_id is not None:
            raise ValueError("Root already exists")
        nid = self._alloc_id()
        node = MemoryTreeNode(node_id=nid, prompt=candidate, parent_id=None, iteration_created=iteration)
        self.nodes[nid] = node
        self.root_id = nid
        return nid

    def add_child(
        self,
        parent_id: int,
        candidate: dict[str, str],
        accepted: bool,
        minibatch_score: float | None,
        minibatch_ids: list[Any],
        iteration: int,
        rejection_reason: str = "",
    ) -> int:
        """Add a child node (mutation result) under the given parent."""
        if parent_id not in self.nodes:
            raise KeyError(f"Parent node {parent_id} not found")
        nid = self._alloc_id()
        node = MemoryTreeNode(
            node_id=nid,
            prompt=candidate,
            parent_id=parent_id,
            accepted=accepted,
            minibatch_score=minibatch_score,
            iteration_created=iteration,
            rejection_reason=rejection_reason,
        )
        self.nodes[nid] = node
        self.nodes[parent_id].children_ids.append(nid)
        edge = MemoryTreeEdge(parent_id=parent_id, child_id=nid, minibatch_ids=minibatch_ids, iteration=iteration)
        self.edges.append(edge)
        return nid

    def add_disconnected_node(self, candidate: dict[str, str], iteration: int) -> int:
        """Add a node not connected to any parent (e.g. merge-created candidates)."""
        nid = self._alloc_id()
        node = MemoryTreeNode(node_id=nid, prompt=candidate, parent_id=None, iteration_created=iteration)
        self.nodes[nid] = node
        _logger.warning("Added disconnected node %d (not linked to tree root)", nid)
        return nid

    def add_outcomes(self, node_id: int, outcomes: list[OutcomeDescription]) -> None:
        """Append outcome descriptions to a node."""
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id} not found")
        self.nodes[node_id].outcomes.extend(outcomes)

    def set_val_score(self, node_id: int, val_score: float) -> None:
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id} not found")
        self.nodes[node_id].val_score = val_score

    def get_node(self, node_id: int) -> MemoryTreeNode:
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id} not found")
        return self.nodes[node_id]

    def get_ancestry(self, node_id: int) -> list[MemoryTreeNode]:
        """Return [root, ..., parent, node] — the path from root to this node."""
        chain: list[MemoryTreeNode] = []
        current = self.nodes.get(node_id)
        while current is not None:
            chain.append(current)
            current = self.nodes.get(current.parent_id) if current.parent_id is not None else None
        chain.reverse()
        return chain

    def get_siblings(self, node_id: int) -> list[MemoryTreeNode]:
        """Other children of the same parent, excluding self."""
        node = self.get_node(node_id)
        if node.parent_id is None:
            return []
        parent = self.nodes[node.parent_id]
        return [self.nodes[cid] for cid in parent.children_ids if cid != node_id]

    def get_rejected_children(self, node_id: int) -> list[MemoryTreeNode]:
        """Children of this node that were rejected."""
        node = self.get_node(node_id)
        return [self.nodes[cid] for cid in node.children_ids if not self.nodes[cid].accepted]

    def get_branches_not_in_ancestry(
        self, node_id: int
    ) -> list[tuple[MemoryTreeNode, int, float | None]]:
        """Return (branch_root, depth, peak_val) for branches outside the ancestry of node_id.

        A "branch root" is any child of an ancestor that is NOT itself in the ancestry chain.
        """
        ancestry_ids = {n.node_id for n in self.get_ancestry(node_id)}
        branches: list[tuple[MemoryTreeNode, int, float | None]] = []

        for ancestor_id in ancestry_ids:
            ancestor = self.nodes[ancestor_id]
            for child_id in ancestor.children_ids:
                if child_id not in ancestry_ids:
                    branch_root = self.nodes[child_id]
                    depth, peak_val = self._branch_stats(child_id)
                    branches.append((branch_root, depth, peak_val))

        return branches

    def _branch_stats(self, root_id: int) -> tuple[int, float | None]:
        """Compute (depth, peak_val_score) for the subtree rooted at root_id."""
        peak_val: float | None = self.nodes[root_id].val_score
        max_depth = 0

        stack: list[tuple[int, int]] = [(root_id, 1)]
        while stack:
            nid, depth = stack.pop()
            max_depth = max(max_depth, depth)
            node = self.nodes[nid]
            if node.val_score is not None:
                if peak_val is None or node.val_score > peak_val:
                    peak_val = node.val_score
            for cid in node.children_ids:
                stack.append((cid, depth + 1))

        return max_depth, peak_val

    def needs_eviction(self, node_id: int) -> bool:
        """Check if a node has accumulated enough outcomes to trigger eviction."""
        node = self.get_node(node_id)
        return len(node.outcomes) > 2 * self.outcome_eviction_k

    def evict_oldest(self, node_id: int) -> list[OutcomeDescription]:
        """Remove and return the oldest k outcomes from a node."""
        node = self.get_node(node_id)
        k = self.outcome_eviction_k
        evicted = node.outcomes[:k]
        node.outcomes = node.outcomes[k:]
        return evicted

    def update_node_summary(self, node_id: int, summary: str) -> None:
        self.get_node(node_id).outcome_summary = summary

    def update_global_summary(self, summary: str) -> None:
        self.global_summary = summary

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot of the entire tree."""

        def outcome_to_dict(o: OutcomeDescription) -> dict[str, Any]:
            return {
                "question_summary": o.question_summary,
                "result": o.result,
                "observation": o.observation,
                "error_type": o.error_type,
                "score": o.score,
                "iteration": o.iteration,
            }

        def node_to_dict(n: MemoryTreeNode) -> dict[str, Any]:
            return {
                "node_id": n.node_id,
                "prompt": n.prompt,
                "parent_id": n.parent_id,
                "children_ids": n.children_ids,
                "accepted": n.accepted,
                "val_score": n.val_score,
                "minibatch_score": n.minibatch_score,
                "outcomes": [outcome_to_dict(o) for o in n.outcomes],
                "outcome_summary": n.outcome_summary,
                "iteration_created": n.iteration_created,
                "rejection_reason": n.rejection_reason,
            }

        def edge_to_dict(e: MemoryTreeEdge) -> dict[str, Any]:
            return {
                "parent_id": e.parent_id,
                "child_id": e.child_id,
                "minibatch_ids": [str(mid) for mid in e.minibatch_ids],
                "iteration": e.iteration,
            }

        return {
            "root_id": self.root_id,
            "global_summary": self.global_summary,
            "outcome_eviction_k": self.outcome_eviction_k,
            "nodes": {str(nid): node_to_dict(n) for nid, n in self.nodes.items()},
            "edges": [edge_to_dict(e) for e in self.edges],
        }

    def __repr__(self) -> str:
        return f"MemoryTree(nodes={len(self.nodes)}, root_id={self.root_id})"
