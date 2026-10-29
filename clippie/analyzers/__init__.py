"""Media analyzers: faces, scene cuts, speech activity and transcript fillers.

Results are time-aligned evidence on the source time base. Heavy dependencies are
optional; an analyzer that cannot run raises AnalyzerUnavailable, never a guess.
"""
from __future__ import annotations


class AnalyzerUnavailable(RuntimeError):
    """A dependency or model file an analyzer needs is not installed."""


from . import fillers, scenes, speech, faces  # noqa: E402

__all__ = ["AnalyzerUnavailable", "faces", "scenes", "speech", "fillers"]
