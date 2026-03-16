"""Tests for OutcomeInterpreter."""

import pytest

from gepa.proposer.reflective_mutation.outcome_interpreter import OutcomeInterpreter


def _mock_lm(response: str):
    """Create a mock LM that returns a fixed response."""
    def lm(prompt):
        return response
    return lm


def _mock_lm_error():
    """Create a mock LM that raises an exception."""
    def lm(prompt):
        raise RuntimeError("LLM call failed")
    return lm


WELL_FORMED_RESPONSE = """\
### Problem 1
Result: correct
Observation: Model correctly identified the geometric series and computed the sum.
Error type: none

### Problem 2
Result: incorrect
Observation: Model set up the equation correctly but made a sign error in step 3.
Error type: arithmetic

### Problem 3
Result: incorrect
Observation: Model misinterpreted the problem as asking for area instead of perimeter.
Error type: interpretation
"""

RECORDS = [
    {"Inputs": "Problem: Find the sum of the geometric series 1+2+4+...+512", "Generated Outputs": "Answer: 1023", "Feedback": "Correct"},
    {"Inputs": "Problem: Solve x^2 - 5x + 6 = 0", "Generated Outputs": "Answer: x=1,6", "Feedback": "Incorrect, answer is x=2,3"},
    {"Inputs": "Problem: Find the perimeter of the triangle", "Generated Outputs": "Answer: 24", "Feedback": "Incorrect, answer is 18"},
]
SCORES = [1.0, 0.0, 0.0]


class TestInterpret:
    def test_well_formed_response(self):
        oi = OutcomeInterpreter(lm=_mock_lm(WELL_FORMED_RESPONSE))
        results = oi.interpret({"p": "Solve carefully"}, RECORDS, SCORES, iteration=1)
        assert len(results) == 3
        assert results[0].result == "correct"
        assert results[0].error_type == "none"
        assert "geometric series" in results[0].observation
        assert results[1].result == "incorrect"
        assert results[1].error_type == "arithmetic"
        assert results[2].error_type == "interpretation"

    def test_prompt_contains_candidate(self):
        captured = []
        def capturing_lm(prompt):
            captured.append(prompt)
            return WELL_FORMED_RESPONSE
        oi = OutcomeInterpreter(lm=capturing_lm)
        oi.interpret({"system_prompt": "Be precise"}, RECORDS, SCORES, iteration=1)
        assert "Be precise" in captured[0]

    def test_empty_records(self):
        oi = OutcomeInterpreter(lm=_mock_lm(""))
        results = oi.interpret({"p": "test"}, [], [], iteration=1)
        assert results == []

    def test_lm_exception_returns_fallbacks(self):
        oi = OutcomeInterpreter(lm=_mock_lm_error())
        results = oi.interpret({"p": "test"}, RECORDS, SCORES, iteration=1)
        assert len(results) == 3
        assert all("OI unavailable" in r.observation for r in results)
        assert results[0].result == "correct"  # score > 0.5
        assert results[1].result == "incorrect"  # score 0.0

    def test_question_summary_from_llm(self):
        response_with_question = """\
### Problem 1
Question: Geometric series sum to 512
Result: correct
Observation: Model identified the pattern and summed correctly.
Error type: none

### Problem 2
Question: Quadratic x^2 - 5x + 6
Result: incorrect
Observation: Sign error in factoring.
Error type: arithmetic

### Problem 3
Question: Triangle perimeter
Result: incorrect
Observation: Computed area instead of perimeter.
Error type: interpretation
"""
        oi = OutcomeInterpreter(lm=_mock_lm(response_with_question))
        results = oi.interpret({"p": "Solve carefully"}, RECORDS, SCORES, iteration=1)
        assert len(results) == 3
        # When Question field is present in LLM response, it should be used
        assert results[0].question_summary == "Geometric series sum to 512"
        assert results[1].question_summary == "Quadratic x^2 - 5x + 6"
        assert results[2].question_summary == "Triangle perimeter"

    def test_question_summary_fallback_without_field(self):
        """When LLM response lacks Question field, falls back to truncated Inputs."""
        oi = OutcomeInterpreter(lm=_mock_lm(WELL_FORMED_RESPONSE))
        results = oi.interpret({"p": "Solve carefully"}, RECORDS, SCORES, iteration=1)
        # Should fall back to first 80 chars of record["Inputs"]
        assert results[0].question_summary.startswith("Problem: Find the sum")

    def test_malformed_response_partial_parse(self):
        # Response with only 1 block but 3 records
        partial = """\
### Problem 1
Result: correct
Observation: Solved correctly.
Error type: none
"""
        oi = OutcomeInterpreter(lm=_mock_lm(partial))
        results = oi.interpret({"p": "test"}, RECORDS, SCORES, iteration=1)
        assert len(results) == 3
        assert results[0].observation == "Solved correctly."
        # Missing blocks should have fallback
        assert "parse failed" in results[1].observation


class TestSummarize:
    def test_summarize_for_eviction(self):
        from gepa.proposer.reflective_mutation.memory_tree import OutcomeDescription
        oi = OutcomeInterpreter(lm=_mock_lm("Model struggles with geometry sign errors."))
        outcomes = [
            OutcomeDescription("Q1", "incorrect", "Sign error in cross product", "arithmetic", 0.0, 1),
            OutcomeDescription("Q2", "incorrect", "Wrong sign in area calculation", "arithmetic", 0.0, 2),
        ]
        summary = oi.summarize_for_eviction("", outcomes)
        assert "geometry" in summary.lower() or "sign" in summary.lower()

    def test_summarize_lm_failure_keeps_existing(self):
        oi = OutcomeInterpreter(lm=_mock_lm_error())
        summary = oi.summarize_for_eviction("Existing summary", [])
        assert summary == "Existing summary"


class TestCallbacks:
    def test_callbacks_fired_on_interpret(self):
        """Verify on_outcome_interpreter_call fires with correct fields."""
        fired_events = []

        class MockCallback:
            def on_outcome_interpreter_call(self, event):
                fired_events.append(event)

        oi = OutcomeInterpreter(lm=_mock_lm(WELL_FORMED_RESPONSE), callbacks=[MockCallback()])
        oi.interpret({"p": "Solve carefully"}, RECORDS, SCORES, iteration=3)

        assert len(fired_events) == 1
        evt = fired_events[0]
        assert evt["num_records"] == 3
        assert evt["fallback_used"] is False
        assert evt["oi_prompt"] != ""
        assert evt["iteration"] == 3

    def test_callbacks_fired_on_fallback(self):
        """Verify on_outcome_interpreter_call fires with fallback_used=True when LLM fails."""
        fired_events = []

        class MockCallback:
            def on_outcome_interpreter_call(self, event):
                fired_events.append(event)

        oi = OutcomeInterpreter(lm=_mock_lm_error(), callbacks=[MockCallback()])
        oi.interpret({"p": "test"}, RECORDS, SCORES, iteration=5)

        assert len(fired_events) == 1
        evt = fired_events[0]
        assert evt["fallback_used"] is True

    def test_eviction_summary_callbacks(self):
        """Verify on_oi_eviction_summary fires with summary_type=='node'."""
        from gepa.proposer.reflective_mutation.memory_tree import OutcomeDescription

        fired_events = []

        class MockCallback:
            def on_oi_eviction_summary(self, event):
                fired_events.append(event)

        oi = OutcomeInterpreter(
            lm=_mock_lm("Model struggles with geometry sign errors."),
            callbacks=[MockCallback()],
        )
        outcomes = [
            OutcomeDescription("Q1", "incorrect", "Sign error in cross product", "arithmetic", 0.0, 1),
        ]
        oi.summarize_for_eviction("", outcomes, node_id=42, iteration=7)

        assert len(fired_events) == 1
        evt = fired_events[0]
        assert evt["summary_type"] == "node"
        assert evt["node_id"] == 42
        assert evt["iteration"] == 7
