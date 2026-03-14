# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

import time
from collections.abc import Mapping, Sequence
from typing import Any

from gepa.core.adapter import DataInst, GEPAAdapter, ProposalFn, RolloutOutput, Trajectory
from gepa.core.callbacks import (
    CandidateSelectedEvent,
    EvaluationEndEvent,
    EvaluationSkippedEvent,
    EvaluationStartEvent,
    GEPACallback,
    LessonGeneratedEvent,
    MinibatchSampledEvent,
    ProposalEndEvent,
    ProposalStartEvent,
    ProposalTraceEvent,
    ReflectiveDatasetBuiltEvent,
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
from gepa.proposer.reflective_mutation.memory import (
    ReflectionMemory,
    ReflectionMemoryEntry,
    generate_lesson,
    summarize_change,
)
from gepa.strategies.batch_sampler import BatchSampler
from gepa.strategies.instruction_proposal import InstructionProposalSignature


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
        reflection_memory: ReflectionMemory | None = None,
        lesson_lm: LanguageModel | None = None,
        objective: str = "",
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
        self.reflection_memory = reflection_memory
        self.lesson_lm = lesson_lm
        self._objective = objective
        self._last_memory_attribution: dict[str, dict[str, Any]] = {}

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
    def _collect_unique_memory_signals(entries: list[ReflectionMemoryEntry]) -> tuple[list[str], list[str]]:
        intents: list[str] = []
        categories: list[str] = []
        for entry in entries:
            intent = entry.intent.strip()
            if intent and intent not in intents:
                intents.append(intent)
            for category in entry.categories_succeeded + entry.categories_failed:
                cat = category.strip()
                if cat and cat not in categories:
                    categories.append(cat)
        return intents, categories

    @staticmethod
    def _find_reuse_matches(text: str, phrases: list[str]) -> list[str]:
        text_l = text.lower()
        return [phrase for phrase in phrases if phrase.lower() in text_l]

    def propose_new_texts(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
        iteration: int = 0,
    ) -> dict[str, str]:
        if self.adapter.propose_new_texts is not None:
            return self.adapter.propose_new_texts(candidate, reflective_dataset, components_to_update)

        if self.custom_candidate_proposer is not None:
            return self.custom_candidate_proposer(candidate, reflective_dataset, components_to_update)

        if self.reflection_lm is None:
            raise ValueError("reflection_lm must be provided when adapter.propose_new_texts is None.")

        new_texts: dict[str, str] = {}
        self._last_memory_attribution = {}
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

            # Inject reflection memory into the prompt template if available
            effective_template = prompt_template
            memory_was_injected = False
            selected_entries: list[ReflectionMemoryEntry] = []
            if self.reflection_memory is not None:
                selected_entries = self.reflection_memory.get_recent(n=5, component_name=name)
                memory_text = self.reflection_memory.format_for_prompt(component_name=name)
                if memory_text:
                    base_template = effective_template or InstructionProposalSignature.default_prompt_template
                    effective_template = base_template + "\n\n" + memory_text
                    memory_was_injected = True

            result, trace = InstructionProposalSignature.run_with_trace(
                lm=self.reflection_lm,
                input_dict={
                    "current_instruction_doc": base_instruction,
                    "dataset_with_feedback": dataset_with_feedback,
                    "prompt_template": effective_template,
                },
            )
            new_texts[name] = result["new_instruction"]

            selected_intents, selected_categories = self._collect_unique_memory_signals(selected_entries)
            reused_intents = self._find_reuse_matches(result["new_instruction"], selected_intents)
            reused_categories = self._find_reuse_matches(result["new_instruction"], selected_categories)
            memory_attribution = {
                "selected_entry_ids": [entry.entry_id for entry in selected_entries if entry.entry_id],
                "selected_intents": selected_intents,
                "selected_categories": selected_categories,
                "reused_intents": reused_intents,
                "reused_categories": reused_categories,
                "reuse_detected": bool(reused_intents or reused_categories),
            }
            self._last_memory_attribution[name] = memory_attribution

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
                    memory_selected_entry_ids=memory_attribution["selected_entry_ids"],
                    memory_selected_intents=memory_attribution["selected_intents"],
                    memory_selected_categories=memory_attribution["selected_categories"],
                    memory_reused_intents=memory_attribution["reused_intents"],
                    memory_reused_categories=memory_attribution["reused_categories"],
                    memory_reuse_detected=memory_attribution["reuse_detected"],
                ),
            )
        return new_texts

    def propose(self, state: GEPAState) -> CandidateProposal | None:
        i = state.i + 1

        # Update memory iteration and fire snapshot
        if self.reflection_memory is not None:
            self.reflection_memory.set_iteration(i)
            self.reflection_memory.fire_snapshot_event("before_proposal")

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
        # Note: We don't use cache for capture_traces=True evaluations since we need fresh traces for reflection
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

        # Update cache with current program evaluation results (for future reuse when capture_traces=False)
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

        self.experiment_tracker.log_metrics(
            {"subsample_score": sum(eval_curr.scores), "total_metric_calls": state.total_num_evals}, step=i
        )

        # 2) Decide which predictors to update
        predictor_names_to_update = self.module_selector(
            state, eval_curr.trajectories, eval_curr.scores, curr_prog_id, curr_prog
        )

        # 3) Build reflective dataset and propose texts
        try:
            reflective_dataset = self.adapter.make_reflective_dataset(curr_prog, eval_curr, predictor_names_to_update)

            # Convert to concrete types for callback
            reflective_dataset_concrete: dict[str, list[dict[str, Any]]] = {
                k: [dict(item) for item in v] for k, v in reflective_dataset.items()
            }

            # Notify reflective dataset built
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

            # Notify proposal start
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

            new_texts = self.propose_new_texts(curr_prog, reflective_dataset, predictor_names_to_update, iteration=i)

            # Notify proposal end
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

        # 4) Create candidate, evaluate on same minibatch (no need to capture traces)
        new_candidate = curr_prog.copy()
        for pname, text in new_texts.items():
            assert pname in new_candidate, f"{pname} missing in candidate"
            new_candidate[pname] = text

        def evaluator(b, c):
            r = self.adapter.evaluate(b, c, capture_traces=False)
            return r.outputs, r.scores, list(r.objective_scores) if r.objective_scores else None

        # Evaluate new candidate (not yet in state)
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
        state.full_program_trace[-1]["new_subsample_scores"] = new_scores

        new_sum = sum(new_scores)
        self.experiment_tracker.log_metrics(
            {"new_subsample_score": new_sum, "total_metric_calls": state.total_num_evals}, step=i
        )

        # Record this reflection attempt in memory
        if self.reflection_memory is not None:
            old_sum = sum(eval_curr.scores)
            accepted = new_sum > old_sum

            for comp_name in predictor_names_to_update:
                # Collect evaluation context from all reflective examples
                example_feedbacks: list[str] = []
                if comp_name in reflective_dataset:
                    for record in reflective_dataset[comp_name]:
                        feedback = str(record.get("Feedback") or record.get("feedback") or "").strip()
                        if feedback:
                            example_feedbacks.append(feedback)
                        else:
                            # Fallback to full record when no explicit feedback field exists.
                            example_feedbacks.append(str(record))

                # Generate V2 lesson via LLM
                effective_lm = self.lesson_lm or self.reflection_lm
                intent, lesson, cats_ok, cats_fail = "", "", [], []
                fallback_used = False
                latency_ms = 0.0

                if effective_lm is not None:
                    t0 = time.perf_counter()
                    intent, lesson, cats_ok, cats_fail = generate_lesson(
                        lm=effective_lm,
                        old_text=curr_prog.get(comp_name, ""),
                        new_text=new_candidate.get(comp_name, ""),
                        failure_feedbacks=example_feedbacks,
                        score_before=old_sum,
                        score_after=new_sum,
                        accepted=accepted,
                        per_example_scores_before=list(eval_curr.scores),
                        per_example_scores_after=list(new_scores),
                        objective=self._objective,
                    )
                    latency_ms = (time.perf_counter() - t0) * 1000

                if not lesson:
                    # V1 fallback
                    change_summary = summarize_change(
                        curr_prog.get(comp_name, ""),
                        new_candidate.get(comp_name, ""),
                    )
                    fallback_used = True
                else:
                    change_summary = ""

                # Fire LessonGeneratedEvent
                memory_attribution = self._last_memory_attribution.get(comp_name, {})
                selected_entry_ids = memory_attribution.get("selected_entry_ids", [])
                reused_intents = memory_attribution.get("reused_intents", [])
                reused_categories = memory_attribution.get("reused_categories", [])
                reuse_detected = bool(memory_attribution.get("reuse_detected", False))
                notify_callbacks(
                    self.callbacks,
                    "on_lesson_generated",
                    LessonGeneratedEvent(
                        iteration=i,
                        component_name=comp_name,
                        intent=intent,
                        lesson=lesson,
                        categories_succeeded=cats_ok,
                        categories_failed=cats_fail,
                        score_before=old_sum,
                        score_after=new_sum,
                        accepted=accepted,
                        latency_ms=latency_ms,
                        fallback_used=fallback_used,
                        memory_selected_entry_ids=selected_entry_ids,
                        memory_reused_intents=reused_intents,
                        memory_reused_categories=reused_categories,
                        memory_reuse_detected=reuse_detected,
                    ),
                )

                self.reflection_memory.add(
                    ReflectionMemoryEntry(
                        iteration=i,
                        component_name=comp_name,
                        score_before=old_sum,
                        score_after=new_sum,
                        accepted=accepted,
                        failure_modes=example_feedbacks,
                        intent=intent,
                        lesson=lesson,
                        categories_succeeded=cats_ok,
                        categories_failed=cats_fail,
                        change_summary=change_summary,
                        referenced_memory_entry_ids=selected_entry_ids,
                        reused_memory_intents=reused_intents,
                        reused_memory_categories=reused_categories,
                    )
                )

            self.reflection_memory.fire_snapshot_event("after_proposal")

        return CandidateProposal(
            candidate=new_candidate,
            parent_program_ids=[curr_prog_id],
            subsample_indices=subsample_ids,
            subsample_scores_before=eval_curr.scores,
            subsample_scores_after=new_scores,
            tag="reflective_mutation",
        )
