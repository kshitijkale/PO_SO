# Copyright (c) 2025 Lakshya A Agrawal and the GEPA contributors
# https://github.com/gepa-ai/gepa

"""Research observability callbacks for GEPA optimization.

This package provides callbacks that make the optimization process
fully transparent for researchers. All callbacks implement the
``GEPACallback`` protocol and can be registered via the ``callbacks``
parameter of ``optimize()`` or ``optimize_anything()``.

Quick start — enable everything with ``research_mode=True``::

    result = gepa.optimize(
        ...,
        research_mode=True,
        run_dir="./runs/exp1",
    )

Or register individual callbacks::

    from gepa.callbacks import ResearchLogger, LineageTracker, LiveDisplay

    result = gepa.optimize(
        ...,
        callbacks=[
            ResearchLogger(output_dir="./runs/exp1"),
            LineageTracker(output_dir="./runs/exp1"),
            LiveDisplay(),
        ],
    )
"""

from gepa.callbacks.lineage_tracker import LineageTracker
from gepa.callbacks.live_display import LiveDisplay
from gepa.callbacks.research_logger import ResearchLogger
from gepa.callbacks.state_logger import StateLogger

__all__ = [
    "ResearchLogger",
    "StateLogger",
    "LineageTracker",
    "LiveDisplay",
]
