# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""Shared redaction helpers for observability callbacks."""

from __future__ import annotations

import re


def redact_qa_text(text: str) -> str:
    """Redact question/answer-bearing fields from text blocks.

    This intentionally keeps most diagnostics while removing explicit
    question/answer content to reduce sensitive data exposure in logs.
    """
    if not text:
        return text

    redacted = text
    redacted = re.sub(
        r"(\*\*Inputs:\*\*\n)(.*?)(\n\n)",
        r"\1[REDACTED]\3",
        redacted,
        flags=re.DOTALL | re.IGNORECASE,
    )
    redacted = re.sub(
        r"(\*\*Generated Outputs:\*\*\n)(.*?)(\n\n)",
        r"\1[REDACTED]\3",
        redacted,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for field_name in ("Inputs", "Generated Outputs", "Problem", "Answer", "Question"):
        redacted = re.sub(
            rf"({field_name}:\s*)(.*)",
            r"\1[REDACTED]",
            redacted,
            flags=re.IGNORECASE,
        )

    return redacted
