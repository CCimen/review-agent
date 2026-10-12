"""Operator-tunable per-response capacity for model-facing review tools."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_RESULT_MAX_CHARS = 160_000
MIN_TEXT_PAGE_CHARS = 1_000
_JSON_WORST_CASE_EXPANSION = 6
_TEXT_PAGE_DIVISOR = _JSON_WORST_CASE_EXPANSION + 1
MIN_RESULT_MAX_CHARS = MIN_TEXT_PAGE_CHARS * _TEXT_PAGE_DIVISOR
# The pinned Hermes runtime replaces tool results with a preview the reviewer
# cannot open once the results of one model response exceed this total
# (tools/budget_config.py, DEFAULT_TURN_BUDGET_CHARS).
RUNTIME_RESPONSE_RESULT_CHARS = 200_000
# The review procedure issues at most this many diff calls in one response.
PARALLEL_DIFF_CALLS = 4


class CapacityError(ValueError):
    """A plugin capacity setting is invalid."""


@dataclass(frozen=True, slots=True)
class CapacityLimits:
    result_max_chars: int
    text_page_max_chars: int

    @property
    def batch_result_max_chars(self) -> int:
        """Serialized size one batched diff may use when four run in parallel."""
        return (
            min(self.result_max_chars, RUNTIME_RESPONSE_RESULT_CHARS)
            // PARALLEL_DIFF_CALLS
        )


_active = CapacityLimits(
    result_max_chars=DEFAULT_RESULT_MAX_CHARS,
    text_page_max_chars=DEFAULT_RESULT_MAX_CHARS // _TEXT_PAGE_DIVISOR,
)


def configure(*, result_max_chars: object) -> CapacityLimits:
    """Validate and install the process-wide plugin capacity selected at startup."""
    if isinstance(result_max_chars, bool) or not isinstance(result_max_chars, int):
        raise CapacityError("result_max_chars must be an integer")
    if result_max_chars < MIN_RESULT_MAX_CHARS:
        raise CapacityError(
            f"result_max_chars must be at least {MIN_RESULT_MAX_CHARS}"
        )
    global _active
    _active = CapacityLimits(
        result_max_chars=result_max_chars,
        # JSON may escape one source character as six. The seventh share keeps
        # room for stable metadata; _output enforces the actual complete result.
        text_page_max_chars=result_max_chars // _TEXT_PAGE_DIVISOR,
    )
    return _active


def current() -> CapacityLimits:
    """Return the immutable capacity selected when the plugin registered."""
    return _active
