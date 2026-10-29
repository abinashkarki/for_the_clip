"""Provider-neutral request, result and error types for the model gateway."""
from __future__ import annotations

from dataclasses import dataclass, field

STAGES = ('map', 'discover', 'plan', 'review', 'repair', 'recommend', 'other')
CATEGORIES = frozenset({'auth', 'quota', 'config', 'service', 'network', 'timeout',
                        'output_limit', 'invalid_output', 'retry_budget'})


@dataclass
class ModelRequest:
    before_attempt: object = None
    after_attempt: object = None
    checkpoint: object = None
    stage: str = 'other'
    prompt: str = ''
    json_schema: dict | None = None
    images: list = field(default_factory=list)        # {"mime", "data": bytes, optional "label"}
    max_output_tokens: int = 8192
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    idle_timeout: float = 45.0
    total_timeout: float = 300.0
    temperature: float | None = None
    metadata: dict = field(default_factory=dict)
    system: str | None = None
    options: dict = field(default_factory=dict)       # provider body extras from routing (chat providers)


@dataclass
class ModelResult:
    text: str
    json: object
    provider: str
    model: str
    model_version: str | None
    usage: dict
    cost_usd: float | None
    latency_ms: int
    ttft_ms: int | None
    finish_reason: str | None
    call_id: str
    attempts: int = 1
    timings: dict = field(default_factory=dict)


class ModelError(RuntimeError):
    """Safe-to-display failure. Never carries keys, prompts or provider bodies."""

    def __init__(self, category, message, call_id=None, *, status_code=None, retry_after=None, usage=None):
        super().__init__(message)
        self.category = category if category in CATEGORIES else 'service'
        self.call_id = call_id
        self.status_code = status_code
        self.retry_after = retry_after
        self.usage = usage or {}
        self.attempts = 1
        self.detail = None
        self.partial = None   # what streamed before the failure: ttft_ms, reasoning_chars, text_chars
