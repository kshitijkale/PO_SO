# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from gepa.core.adapter import DataInst, GEPAAdapter, ProposalFn, RolloutOutput, Trajectory
from gepa.core.callbacks import (
    CandidateSelectedEvent,
    DiaryInjectedEvent,
    EvaluationEndEvent,
    EvaluationSkippedEvent,
    EvaluationStartEvent,
    GEPACallback,
    LedgerInjectedEvent,
    MinibatchSampledEvent,
    ProposalEndEvent,
    ProposalStartEvent,
    ProposalTraceEvent,
    ReflectiveDatasetBuiltEvent,
    RefinementStepEvent,
    notify_callbacks,
)
from gepa.core.data_loader import DataId, DataLoader, ensure_loader
from gepa.core.state import GEPAState
from gepa.proposer.base import CandidateProposal, ProposeNewCandidate
from gepa.proposer.reflective_mutation.base import (
    CandidateSelector,
    LanguageModel,
    ReflectionComponentSelector,
)
from gepa.proposer.reflective_mutation.memory import summarize_change
from gepa.proposer.reflective_mutation.optimization_diary import DiaryEntry, OptimizationDiary
from gepa.proposer.reflective_mutation.rejection_ledger import LedgerEntry, RejectionLedger
from gepa.strategies.batch_sampler import BatchSampler
from gepa.strategies.instruction_proposal import InstructionProposalSignature

_logger = logging.getLogger(__name__)


class ReflectiveMutationProposer(ProposeNewCandidate[DataId]):
    """
    Implements current reflective mutation flow:
    - Select candidate via selector
    - Select minibatch via sampler
    - capture_traces_and_eval -> trajectories, subsample_scores
    - skip if all scores==perfect and skip_perfect_score
    - reflection + mutate -> new candidate
    - evaluate new candidate on same minibatch -> new_subsample_scores
    - Return proposal if improved; else None
    """

    def __init__(
        self,
        logger: Any,
        trainset: list[DataInst] | DataLoader[DataId, DataInst],
        adapter: GEPAAdapter[DataInst, Trajectory, RolloutOutput],
        candidate_selector: CandidateSelector,
        module_selector: ReflectionComponentSelector,
        batch_sampler: BatchSampler[DataId, DataInst],
        perfect_score: float | None,
        skip_perfect_score: bool,
        experiment_tracker: Any,
        reflection_lm: LanguageModel | None = None,
        reflection_prompt_template: str | dict[str, str] | None = None,
        custom_candidate_proposer: ProposalFn | None = None,
        callbacks: list[GEPACallback] | None = None,
        rejection_ledger: RejectionLedger | None = None,
        optimization_diary: OptimizationDiary | None = None,
        summarizer_lm: LanguageModel | None = None,
        refinement_steps: int = 1,
    ):
        self.logger = logger
        self.trainset = ensure_loader(trainset)
        self.adapter = adapter
        self.candidate_selector = candidate_selector
        self.module_selector = module_selector
        self.batch_sampler = batch_sampler
        self.perfect_score = perfect_score
        self.skip_perfect_score = skip_perfect_score
        self.experiment_tracker = experiment_tracker
        self.reflection_lm = reflection_lm
        self.custom_candidate_proposer = custom_candidate_proposer
        self.callbacks = callbacks

        # Rejection ledger + dedicated summarizer LM
        self.rejection_ledger = rejection_ledger
        self.summarizer_lm = summarizer_lm

        # Optimization diary (global, fixed-size alternative to ledger)
        self.optimization_diary = optimization_diary

        # Multi-turn refinement: K inner loops per outer iteration
        self.refinement_steps = max(1, refinement_steps)

        self.reflection_prompt_template = reflection_prompt_template
        # Track parameters for which we've already logged missing template warnings
        self._missing_template_warnings: set[str] = set()

        if isinstance(reflection_prompt_template, dict):
            for _param_name, template in reflection_prompt_template.items():
                InstructionProposalSignature.validate_prompt_template(template)
        else:
            InstructionProposalSignature.validate_prompt_template(reflection_prompt_template)

        if self.skip_perfect_score and self.perfect_score is None:
            raise ValueError(
                "perfect_score must be provided when skip_perfect_score is True. "
                "If you do not have a perfect target score, set skip_perfect_score=False."
            )

    @staticmethod
    def _hash_candidate(candidate: dict[str, str]) -> str:
        return hashlib.sha256(json.dumps(sorted(candidate.items())).encode()).hexdigest()[:16]

    def _summarize_rejection(
        self,
        old_prompt: str,
        new_prompt: str,
        minibatch: list[Any],
        proposed_outputs: list[Any],
        scores: list[float],
        threshold: float,
    ) -> tuple[str, str]:
        """Return ``(diff_summary, llm_summary)`` for a rejected mutation.

        ``diff_summary`` is always a concrete string diff (heuristic, no LLM call).
        ``llm_summary`` is a one-sentence semantic reason from the summarizer LM,
        or ``""`` if no summarizer is configured or the call fails.
        """
        diff_summary = summarize_change(old_prompt, new_prompt, max_len=80)

        if self.summarizer_lm is None:
            return diff_summary, ""
        try:
            examples_text = ""
            for i, (inp, out, score) in enumerate(zip(minibatch, proposed_outputs, scores, strict=False)):
                mark = "PASS" if score >= 1.0 else "FAIL"
                examples_text += f"  Example {i + 1} [{mark}]: input={inp!r} output={out!r}\n"

            prompt = (
                "You are analyzing a failed prompt change for an AI system.\n"
                "In one sentence (max 120 chars), describe what strategy the new prompt attempted "
                "and why it didn't improve results.\n\n"
                f"Old prompt:\n{old_prompt[:400]}\n\n"
                f"New prompt:\n{new_prompt[:400]}\n\n"
                f"Results on minibatch (scored {int(sum(scores))}/{len(scores)}, "
                f"needed >{int(threshold)}/{len(scores)}):\n{examples_text}\n"
                "Respond with only the one-sentence description."
            )
            raw = self.summarizer_lm(prompt)
            llm_summary = raw.strip()[:150] if raw.strip() else ""
            return diff_summary, llm_summary
        except Exception:
            return diff_summary, ""

    def propose_new_texts(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
        iteration: int = 0,
    ) -> dict[str, str]:
        # Reset conversation capture — populated during the call for multi-turn reuse
        self._last_conversation_histories: dict[str, list[dict[str, Any]]] = {}

        if self.adapter.propose_new_texts is not None:
            return self.adapter.propose_new_texts(candidate, reflective_dataset, components_to_update)

        if self.custom_candidate_proposer is not None:
            return self.custom_candidate_proposer(candidate, reflective_dataset, components_to_update)

        if self.reflection_lm is None:
            raise ValueError("reflection_lm must be provided when adapter.propose_new_texts is None.")

        new_texts: dict[str, str] = {}
        for name in components_to_update:
            # Gracefully handle cases where a selected component has no data in reflective_dataset
            if name not in reflective_dataset or not reflective_dataset.get(name):
                self.logger.log(f"Component '{name}' is not in reflective dataset. Skipping.")
                continue

            base_instruction = candidate[name]
            dataset_with_feedback = reflective_dataset[name]

            # Determine which prompt template to use for this parameter
            prompt_template = None
            if isinstance(self.reflection_prompt_template, dict):
                # Use parameter-specific template if available
                prompt_template = self.reflection_prompt_template.get(name)
                if prompt_template is None and name not in self._missing_template_warnings:
                    self.logger.log(
                        f"No reflection_prompt_template found for parameter '{name}'. Using default template."
                    )
                    self._missing_template_warnings.add(name)
            else:
                # Use the single template for all parameters
                prompt_template = self.reflection_prompt_template

            # Inject memory into the prompt template if available
            effective_template = prompt_template
            memory_was_injected = False

            if self.rejection_ledger is not None:
                # Rejection ledger path: lightweight per-parent rejection history
                parent_hash = self._hash_candidate(candidate)
                ledger_text = self.rejection_ledger.format_for_prompt(parent_hash, name)
                if ledger_text:
                    base_template = effective_template or InstructionProposalSignature.default_prompt_template
                    last_para_marker = "Provide the new instructions"
                    idx = base_template.rfind(last_para_marker)
                    if idx > 0:
                        effective_template = (
                            base_template[:idx].rstrip() + "\n\n" + ledger_text + "\n\n" + base_template[idx:]
                        )
                    else:
                        effective_template = base_template + "\n\n" + ledger_text
                    memory_was_injected = True
                notify_callbacks(
                    self.callbacks,
                    "on_ledger_injected",
                    LedgerInjectedEvent(
                        type="ledger_injected",
                        iteration=iteration,
                        parent_hash=parent_hash,
                        component_name=name,
                        num_entries=len(self.rejection_ledger.get_entries(parent_hash, name)),
                        rendered_text=ledger_text,
                        char_count=len(ledger_text),
                    ),
                )

            if self.optimization_diary is not None:
                # Optimization diary: global, fixed-size context injection
                diary_text = self.optimization_diary.format_for_prompt()
                if diary_text:
                    base_template = effective_template or InstructionProposalSignature.default_prompt_template
                    last_para_marker = "Provide the new instructions"
                    idx = base_template.rfind(last_para_marker)
                    if idx > 0:
                        effective_template = (
                            base_template[:idx].rstrip() + "\n\n" + diary_text + "\n\n" + base_template[idx:]
                        )
                    else:
                        effective_template = base_template + "\n\n" + diary_text
                    memory_was_injected = True
                notify_callbacks(
                    self.callbacks,
                    "on_diary_injected",
                    DiaryInjectedEvent(
                        type="diary_injected",
                        iteration=iteration,
                        component_name=name,
                        rendered_text=diary_text,
                        char_count=len(diary_text),
                        total_entries=len(self.optimization_diary._entries),
                        layer2_active=self.optimization_diary.strategy_notes_lm is not None,
                    ),
                )

            result, trace = InstructionProposalSignature.run_with_trace(
                lm=self.reflection_lm,
                input_dict={
                    "current_instruction_doc": base_instruction,
                    "dataset_with_feedback": dataset_with_feedback,
                    "prompt_template": effective_template,
                },
            )
            new_texts[name] = result["new_instruction"]

            # Capture conversation history for multi-turn refinement
            rendered_prompt = trace["rendered_prompt"]
            raw_response = trace["raw_response"]
            if isinstance(rendered_prompt, list):
                # Multimodal messages — use directly
                self._last_conversation_histories[name] = list(rendered_prompt) + [
                    {"role": "assistant", "content": raw_response},
                ]
            else:
                self._last_conversation_histories[name] = [
                    {"role": "user", "content": rendered_prompt},
                    {"role": "assistant", "content": raw_response},
                ]

            # Fire proposal trace event
            notify_callbacks(
                self.callbacks,
                "on_proposal_trace",
                ProposalTraceEvent(
                    iteration=iteration,
                    component_name=name,
                    prompt_template=effective_template or InstructionProposalSignature.default_prompt_template,
                    rendered_prompt=trace["rendered_prompt"],
                    raw_response=trace["raw_response"],
                    extracted_instruction=result["new_instruction"],
                    model_id=getattr(self.reflection_lm, "__name__", str(self.reflection_lm)),
                    latency_ms=trace["latency_ms"],
                    memory_was_injected=memory_was_injected,
                ),
            )
        return new_texts

    def _build_refinement_feedback(
        self,
        candidate: dict[str, str],
        eval_result: Any,
        components_to_update: list[str],
        step: int,
        total_steps: int,
        parent_sum: float,
    ) -> str:
        """Build a feedback message for the next refinement turn."""
        scores = eval_result.scores
        score_sum = sum(scores)
        batch_size = len(scores)

        lines: list[str] = []
        lines.append(
            f"I evaluated your proposed prompt on the same {batch_size} problems. "
            f"Score: {score_sum:.0f}/{batch_size} (parent scored {parent_sum:.0f}/{batch_size})."
        )
        lines.append("")

        # Build per-example feedback using the adapter's reflective dataset
        try:
            reflective_ds = self.adapter.make_reflective_dataset(candidate, eval_result, components_to_update)
            # Use the first component's records (single-component case)
            comp_name = components_to_update[0]
            records = reflective_ds.get(comp_name, [])
            for idx, (record, score) in enumerate(zip(records, scores, strict=False)):
                status = "CORRECT" if score >= 1.0 else "WRONG"
                lines.append(f"Example {idx + 1} [{status}]:")
                inputs_text = record.get("Inputs", "")
                if inputs_text:
                    lines.append(f"  {inputs_text[:200]}")
                if score < 1.0:
                    feedback = record.get("Feedback", "")
                    if feedback:
                        lines.append(f"  Feedback: {feedback[:500]}")
                lines.append("")
        except Exception:
            # Fall back to simple score listing if reflective dataset fails
            for idx, score in enumerate(scores):
                status = "CORRECT" if score >= 1.0 else "WRONG"
                lines.append(f"  Example {idx + 1}: {status}")

        lines.append(f"This is refinement step {step} of {total_steps}.")
        lines.append(
            "Analyze what's still going wrong with the failing examples. "
            "Think about why your previous attempt didn't fix these issues. "
            "Propose a refined prompt that addresses the specific failures."
        )
        lines.append("Provide the refined instructions within ''' blocks.")
        return "\n".join(lines)

    def propose(self, state: GEPAState) -> CandidateProposal | None:
        i = state.i + 1

        curr_prog_id = self.candidate_selector.select_candidate_idx(state)
        curr_prog = state.program_candidates[curr_prog_id]
        state.full_program_trace[-1]["selected_program_candidate"] = curr_prog_id
        self.logger.log(
            f"Iteration {i}: Selected program {curr_prog_id} score: {state.program_full_scores_val_set[curr_prog_id]}"
        )

        # Notify candidate selected
        notify_callbacks(
            self.callbacks,
            "on_candidate_selected",
            CandidateSelectedEvent(
                iteration=i,
                candidate_idx=curr_prog_id,
                candidate=curr_prog,
                score=state.program_full_scores_val_set[curr_prog_id],
            ),
        )

        self.experiment_tracker.log_metrics(
            {"iteration": i, "selected_program_candidate": curr_prog_id, "total_metric_calls": state.total_num_evals},
            step=i,
        )

        subsample_ids = self.batch_sampler.next_minibatch_ids(self.trainset, state)
        state.full_program_trace[-1]["subsample_ids"] = subsample_ids
        minibatch = self.trainset.fetch(subsample_ids)

        # Notify minibatch sampled
        notify_callbacks(
            self.callbacks,
            "on_minibatch_sampled",
            MinibatchSampledEvent(
                iteration=i,
                minibatch_ids=subsample_ids,
                trainset_size=len(self.trainset),
            ),
        )

        # 1) Evaluate current program with traces
        curr_parent_ids = [p for p in state.parent_program_for_candidate[curr_prog_id] if p is not None]
        is_seed_candidate = curr_prog_id == 0
        notify_callbacks(
            self.callbacks,
            "on_evaluation_start",
            EvaluationStartEvent(
                iteration=i,
                candidate_idx=curr_prog_id,
                batch_size=len(minibatch),
                capture_traces=True,
                parent_ids=curr_parent_ids,
                inputs=minibatch,
                is_seed_candidate=is_seed_candidate,
            ),
        )
        eval_curr = self.adapter.evaluate(minibatch, curr_prog, capture_traces=True)
        state.increment_evals(len(subsample_ids))
        state.full_program_trace[-1]["subsample_scores"] = eval_curr.scores
        notify_callbacks(
            self.callbacks,
            "on_evaluation_end",
            EvaluationEndEvent(
                iteration=i,
                candidate_idx=curr_prog_id,
                scores=eval_curr.scores,
                has_trajectories=bool(eval_curr.trajectories),
                capture_traces=True,
                parent_ids=curr_parent_ids,
                outputs=eval_curr.outputs,
                trajectories=eval_curr.trajectories,
                objective_scores=eval_curr.objective_scores,
                is_seed_candidate=is_seed_candidate,
            ),
        )

        # Update cache with current program evaluation results
        if state.evaluation_cache is not None:
            objective_scores_list = list(eval_curr.objective_scores) if eval_curr.objective_scores else None
            state.evaluation_cache.put_batch(
                curr_prog, subsample_ids, eval_curr.outputs, eval_curr.scores, objective_scores_list
            )

        if not eval_curr.trajectories or len(eval_curr.trajectories) == 0:
            self.logger.log(f"Iteration {i}: No trajectories captured. Skipping.")
            notify_callbacks(
                self.callbacks,
                "on_evaluation_skipped",
                EvaluationSkippedEvent(
                    iteration=i,
                    candidate_idx=curr_prog_id,
                    reason="no_trajectories",
                    scores=eval_curr.scores,
                    is_seed_candidate=is_seed_candidate,
                ),
            )
            return None

        if (
            self.skip_perfect_score
            and self.perfect_score is not None
            and all(s is not None and s >= self.perfect_score for s in eval_curr.scores)
        ):
            self.logger.log(f"Iteration {i}: All subsample scores perfect. Skipping.")
            notify_callbacks(
                self.callbacks,
                "on_evaluation_skipped",
                EvaluationSkippedEvent(
                    iteration=i,
                    candidate_idx=curr_prog_id,
                    reason="all_scores_perfect",
                    scores=eval_curr.scores,
                    is_seed_candidate=is_seed_candidate,
                ),
            )
            return None

        parent_sum = sum(eval_curr.scores)
        self.experiment_tracker.log_metrics(
            {"subsample_score": parent_sum, "total_metric_calls": state.total_num_evals}, step=i
        )

        # 2) Decide which predictors to update
        predictor_names_to_update = self.module_selector(
            state, eval_curr.trajectories, eval_curr.scores, curr_prog_id, curr_prog
        )

        # 3) Build reflective dataset and propose texts (Turn 1)
        try:
            reflective_dataset = self.adapter.make_reflective_dataset(curr_prog, eval_curr, predictor_names_to_update)

            reflective_dataset_concrete: dict[str, list[dict[str, Any]]] = {
                k: [dict(item) for item in v] for k, v in reflective_dataset.items()
            }

            notify_callbacks(
                self.callbacks,
                "on_reflective_dataset_built",
                ReflectiveDatasetBuiltEvent(
                    iteration=i,
                    candidate_idx=curr_prog_id,
                    components=predictor_names_to_update,
                    dataset=reflective_dataset_concrete,
                ),
            )

            notify_callbacks(
                self.callbacks,
                "on_proposal_start",
                ProposalStartEvent(
                    iteration=i,
                    parent_candidate=curr_prog,
                    components=predictor_names_to_update,
                    reflective_dataset=reflective_dataset_concrete,
                ),
            )

            new_texts = self.propose_new_texts(
                curr_prog, reflective_dataset, predictor_names_to_update,
                iteration=i,
            )

            notify_callbacks(
                self.callbacks,
                "on_proposal_end",
                ProposalEndEvent(
                    iteration=i,
                    new_instructions=new_texts,
                ),
            )

            for pname, text in new_texts.items():
                self.logger.log(f"Iteration {i}: Proposed new text for {pname}: {text}")
            self.experiment_tracker.log_metrics(
                {f"new_instruction_{pname}": text for pname, text in new_texts.items()}, step=i
            )
        except Exception as e:
            self.logger.log(f"Iteration {i}: Exception during reflection/proposal: {e}")
            import traceback

            self.logger.log(traceback.format_exc())
            return None

        # 4) Evaluate Turn 1 candidate on same minibatch
        new_candidate = curr_prog.copy()
        for pname, text in new_texts.items():
            assert pname in new_candidate, f"{pname} missing in candidate"
            new_candidate[pname] = text

        needs_traces = self.refinement_steps > 1
        _proposed_outputs: list[Any] = []

        if needs_traces:
            # When doing multi-turn refinement, evaluate with traces directly so we
            # get both scores and trace data in a single call (no redundant eval).
            notify_callbacks(
                self.callbacks,
                "on_evaluation_start",
                EvaluationStartEvent(
                    iteration=i,
                    candidate_idx=None,
                    batch_size=len(minibatch),
                    capture_traces=True,
                    parent_ids=[curr_prog_id],
                    inputs=minibatch,
                    is_seed_candidate=False,
                ),
            )
            turn1_eval = self.adapter.evaluate(minibatch, new_candidate, capture_traces=True)
            new_scores = list(turn1_eval.scores)
            outputs = list(turn1_eval.outputs)
            _proposed_outputs = list(turn1_eval.outputs)
            notify_callbacks(
                self.callbacks,
                "on_evaluation_end",
                EvaluationEndEvent(
                    iteration=i,
                    candidate_idx=None,
                    scores=new_scores,
                    has_trajectories=True,
                    capture_traces=True,
                    parent_ids=[curr_prog_id],
                    outputs=outputs,
                    trajectories=None,
                    objective_scores=list(turn1_eval.objective_scores) if turn1_eval.objective_scores else None,
                    is_seed_candidate=False,
                ),
            )
            state.increment_evals(len(subsample_ids))
        else:
            # Single-shot path: use cached evaluation (no traces needed)
            turn1_eval = None

            def evaluator(b: Any, c: Any) -> tuple[Any, Any, Any]:
                r = self.adapter.evaluate(b, c, capture_traces=False)
                _proposed_outputs.extend(r.outputs)
                return r.outputs, r.scores, list(r.objective_scores) if r.objective_scores else None

            notify_callbacks(
                self.callbacks,
                "on_evaluation_start",
                EvaluationStartEvent(
                    iteration=i,
                    candidate_idx=None,
                    batch_size=len(minibatch),
                    capture_traces=False,
                    parent_ids=[curr_prog_id],
                    inputs=minibatch,
                    is_seed_candidate=False,
                ),
            )
            outputs_by_id, scores_by_id, objective_by_id, actual_evals_count = state.cached_evaluate_full(
                new_candidate, subsample_ids, self.trainset.fetch, evaluator
            )
            new_scores = [scores_by_id[eid] for eid in subsample_ids]
            outputs = [outputs_by_id[eid] for eid in subsample_ids]
            notify_callbacks(
                self.callbacks,
                "on_evaluation_end",
                EvaluationEndEvent(
                    iteration=i,
                    candidate_idx=None,
                    scores=new_scores,
                    has_trajectories=False,
                    capture_traces=False,
                    parent_ids=[curr_prog_id],
                    outputs=outputs,
                    trajectories=None,
                    objective_scores=[objective_by_id[eid] for eid in subsample_ids] if objective_by_id else None,
                    is_seed_candidate=False,
                ),
            )
            state.increment_evals(actual_evals_count)

        # Track best candidate across refinement steps
        best_candidate = new_candidate
        best_scores = new_scores
        best_sum = sum(new_scores)
        best_outputs = _proposed_outputs[:]

        self.logger.log(
            f"Iteration {i}: Refinement step 1/{self.refinement_steps} — "
            f"score {best_sum:.0f}/{len(new_scores)} (parent {parent_sum:.0f}/{len(eval_curr.scores)})"
        )
        notify_callbacks(
            self.callbacks,
            "on_refinement_step",
            RefinementStepEvent(
                iteration=i,
                refinement_step=1,
                total_refinement_steps=self.refinement_steps,
                scores=new_scores,
                score_sum=best_sum,
                parent_score_sum=parent_sum,
                is_best_so_far=True,
            ),
        )

        # 5) Multi-turn refinement loop (steps 2..K)
        if self.refinement_steps > 1 and self.reflection_lm is not None:
            # Use the actual Turn 1 conversation captured by propose_new_texts
            # (exact prompt sent + raw response received — no reconstruction needed)
            conversation_histories: dict[str, list[dict[str, Any]]] = {
                comp: [dict(m) for m in msgs]
                for comp, msgs in self._last_conversation_histories.items()
            }

            step_candidate = new_candidate
            # turn1_eval already has traces from the eval above
            prev_step_eval = turn1_eval

            for step in range(2, self.refinement_steps + 1):
                # Build feedback from the previous step's trace eval
                feedback_text = self._build_refinement_feedback(
                    candidate=step_candidate,
                    eval_result=prev_step_eval,
                    components_to_update=predictor_names_to_update,
                    step=step,
                    total_steps=self.refinement_steps,
                    parent_sum=parent_sum,
                )

                # Call reflection LLM with accumulated conversation
                step_new_texts: dict[str, str] = {}
                for comp_name in predictor_names_to_update:
                    conversation_histories[comp_name].append({"role": "user", "content": feedback_text})
                    try:
                        raw_response = self.reflection_lm(conversation_histories[comp_name])
                        extracted = InstructionProposalSignature.output_extractor(raw_response.strip())
                        step_new_texts[comp_name] = extracted["new_instruction"]
                        conversation_histories[comp_name].append({"role": "assistant", "content": raw_response})
                    except Exception as e:
                        self.logger.log(
                            f"Iteration {i}: Refinement step {step} failed for {comp_name}: {e}"
                        )
                        # Keep the previous candidate text
                        step_new_texts[comp_name] = step_candidate.get(comp_name, "")
                        conversation_histories[comp_name].append(
                            {"role": "assistant", "content": step_candidate.get(comp_name, "")}
                        )

                # Build the refined candidate
                step_candidate = curr_prog.copy()
                for pname, text in step_new_texts.items():
                    step_candidate[pname] = text

                # Single eval with traces: scores for best-tracking + traces for next step's feedback
                step_eval = self.adapter.evaluate(minibatch, step_candidate, capture_traces=True)
                state.increment_evals(len(subsample_ids))
                step_scores = step_eval.scores
                step_sum = sum(step_scores)

                is_new_best = step_sum > best_sum
                if is_new_best:
                    best_candidate = step_candidate
                    best_scores = step_scores
                    best_sum = step_sum
                    best_outputs = list(step_eval.outputs)

                self.logger.log(
                    f"Iteration {i}: Refinement step {step}/{self.refinement_steps} — "
                    f"score {step_sum:.0f}/{len(step_scores)}"
                    f"{' (new best!)' if is_new_best else ''}"
                )
                notify_callbacks(
                    self.callbacks,
                    "on_refinement_step",
                    RefinementStepEvent(
                        iteration=i,
                        refinement_step=step,
                        total_refinement_steps=self.refinement_steps,
                        scores=step_scores,
                        score_sum=step_sum,
                        parent_score_sum=parent_sum,
                        is_best_so_far=is_new_best,
                    ),
                )

                # Carry forward for next step's feedback
                prev_step_eval = step_eval

        # Use the best candidate from all refinement steps
        new_candidate = best_candidate
        new_scores = best_scores
        new_sum = best_sum

        state.full_program_trace[-1]["new_subsample_scores"] = new_scores

        self.experiment_tracker.log_metrics(
            {"new_subsample_score": new_sum, "total_metric_calls": state.total_num_evals}, step=i
        )

        # Record rejected mutation in rejection ledger
        if self.rejection_ledger is not None and new_sum <= parent_sum:
            parent_hash = self._hash_candidate(curr_prog)
            for comp_name in predictor_names_to_update:
                diff_summary, llm_summary = self._summarize_rejection(
                    old_prompt=curr_prog.get(comp_name, ""),
                    new_prompt=new_candidate.get(comp_name, ""),
                    minibatch=minibatch,
                    proposed_outputs=best_outputs,
                    scores=new_scores,
                    threshold=parent_sum,
                )
                self.rejection_ledger.record(
                    parent_hash=parent_hash,
                    component_name=comp_name,
                    entry=LedgerEntry(
                        diff_summary=diff_summary,
                        llm_summary=llm_summary,
                        score=new_sum,
                        threshold=parent_sum,
                        batch_size=len(new_scores),
                        iteration=i,
                    ),
                )

        # Record iteration in optimization diary (both accepted AND rejected)
        if self.optimization_diary is not None:
            from gepa.proposer.reflective_mutation.optimization_diary import _unified_diff

            minibatch_accepted = new_sum > parent_sum
            for comp_name in predictor_names_to_update:
                old_text = curr_prog.get(comp_name, "")
                new_text = new_candidate.get(comp_name, "")
                if minibatch_accepted:
                    diff_text = _unified_diff(old_text, new_text)
                else:
                    diff_text = summarize_change(old_text, new_text, max_len=80)
                self.optimization_diary.record(
                    DiaryEntry(
                        iteration=i,
                        accepted=minibatch_accepted,
                        score_before=parent_sum,
                        score_after=new_sum,
                        batch_size=len(new_scores),
                        diff=diff_text,
                        component_name=comp_name,
                    )
                )
            self.optimization_diary.update_strategy_notes()

        return CandidateProposal(
            candidate=new_candidate,
            parent_program_ids=[curr_prog_id],
            subsample_indices=subsample_ids,
            subsample_scores_before=eval_curr.scores,
            subsample_scores_after=new_scores,
            tag="reflective_mutation",
        )
