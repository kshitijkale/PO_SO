# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

import time
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable

from gepa.core.adapter import Trajectory
from gepa.core.state import GEPAState


@runtime_checkable
class CandidateSelector(Protocol):
    def select_candidate_idx(self, state: GEPAState) -> int: ...


class ReflectionComponentSelector(Protocol):
    def __call__(
        self,
        state: GEPAState,
        trajectories: list[Trajectory],
        subsample_scores: list[float],
        candidate_idx: int,
        candidate: dict[str, str],
    ) -> list[str]: ...


class LanguageModel(Protocol):
    def __call__(self, prompt: str | list[dict[str, Any]]) -> str: ...


@dataclass
class Signature:
    prompt_template: ClassVar[str]
    input_keys: ClassVar[list[str]]
    output_keys: ClassVar[list[str]]

    @classmethod
    def prompt_renderer(cls, input_dict: Mapping[str, Any]) -> str | list[dict[str, Any]]:
        raise NotImplementedError

    @classmethod
    def output_extractor(cls, lm_out: str) -> dict[str, str]:
        raise NotImplementedError

    @classmethod
    def run(cls, lm: LanguageModel, input_dict: Mapping[str, Any]) -> dict[str, str]:
        full_prompt = cls.prompt_renderer(input_dict)
        lm_res = lm(full_prompt)
        lm_out = lm_res.strip()
        return cls.output_extractor(lm_out)

    @classmethod
    def run_with_trace(
        cls, lm: LanguageModel, input_dict: Mapping[str, Any]
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Like run(), but also returns a trace dict with prompt, raw response, and timing."""
        full_prompt = cls.prompt_renderer(input_dict)
        rendered_prompt = full_prompt if isinstance(full_prompt, str) else str(full_prompt)
        prompt_template = input_dict.get("prompt_template", "") or ""

        t0 = time.perf_counter()
        lm_res = lm(full_prompt)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        lm_out = lm_res.strip()
        result = cls.output_extractor(lm_out)

        trace = {
            "prompt_template": prompt_template,
            "rendered_prompt": rendered_prompt,
            "raw_response": lm_res,
            "latency_ms": latency_ms,
        }
        return result, trace
