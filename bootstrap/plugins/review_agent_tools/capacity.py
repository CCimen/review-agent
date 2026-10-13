"""Per-response capacity for model-facing review tools."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .domain.review import MAX_REVIEW_PARTS
from .model_window import ModelWindow


DEFAULT_RESULT_MAX_CHARS = 160_000
MIN_TEXT_PAGE_CHARS = 1_000
_JSON_WORST_CASE_EXPANSION = 6
_TEXT_PAGE_DIVISOR = _JSON_WORST_CASE_EXPANSION + 1
MIN_RESULT_MAX_CHARS = MIN_TEXT_PAGE_CHARS * _TEXT_PAGE_DIVISOR
# The review procedure issues at most this many diff calls in one response.
PARALLEL_DIFF_CALLS = 4


class CapacityError(ValueError):
    """A plugin capacity setting is invalid."""


@dataclass(frozen=True, slots=True)
class CapacityLimits:
    """The operator's result setting within what Hermes lets one session hold.

    ``result_max_chars`` bounds one tool result and ``response_max_chars`` all
    results of one model response; above either, Hermes replaces results with
    a preview the reviewer cannot open.
    """

    result_max_chars: int
    text_page_max_chars: int
    response_max_chars: int

    @property
    def batch_result_max_chars(self) -> int:
        """Serialized size of one batched diff.

        Four parallel batches leave one equal share of the response total for
        any other tool result issued beside them, which the plugin cannot see.
        """
        return min(
            self.result_max_chars, self.response_max_chars // (PARALLEL_DIFF_CALLS + 1)
        )

    def part_report_max_chars(self, parts: int) -> int:
        """Serialized size of one report of a review split into ``parts`` parts.

        The final session may request every report in one response, so all of
        them plus one equal share for another tool result fit the response total.
        """
        if not 1 <= parts <= MAX_REVIEW_PARTS:
            raise CapacityError(f"parts must be between 1 and {MAX_REVIEW_PARTS}")
        return min(self.result_max_chars, self.response_max_chars // (parts + 1))


def _limits(configured: int, *, result_chars: int, turn_chars: int) -> CapacityLimits:
    result_max_chars = min(configured, result_chars)
    return CapacityLimits(
        result_max_chars=result_max_chars,
        # JSON may escape one source character as six. The seventh share keeps
        # room for stable metadata, so any page fits the session's result limit.
        # Hermes' smallest per-result limit (8,000) keeps it above the minimum page.
        text_page_max_chars=result_max_chars // _TEXT_PAGE_DIVISOR,
        response_max_chars=min(configured, turn_chars),
    )


_configured = DEFAULT_RESULT_MAX_CHARS
_default = CapacityLimits(
    result_max_chars=DEFAULT_RESULT_MAX_CHARS,
    text_page_max_chars=DEFAULT_RESULT_MAX_CHARS // _TEXT_PAGE_DIVISOR,
    response_max_chars=DEFAULT_RESULT_MAX_CHARS,
)
_execution: ContextVar[CapacityLimits | None] = ContextVar(
    "review_agent_capacity", default=None
)


def configure(
    *, result_max_chars: object, result_chars: int, turn_chars: int
) -> CapacityLimits:
    """Install the operator setting and the limits used before a model is known."""
    if isinstance(result_max_chars, bool) or not isinstance(result_max_chars, int):
        raise CapacityError("result_max_chars must be an integer")
    if result_max_chars < MIN_RESULT_MAX_CHARS:
        raise CapacityError(
            f"result_max_chars must be at least {MIN_RESULT_MAX_CHARS}"
        )
    global _configured, _default
    _configured = result_max_chars
    _default = _limits(result_max_chars, result_chars=result_chars, turn_chars=turn_chars)
    return _default


def for_window(window: ModelWindow) -> CapacityLimits:
    """Limits for one execution of the model with this window."""
    return _limits(
        _configured, result_chars=window.result_chars, turn_chars=window.turn_chars
    )


@contextmanager
def execution(limits: CapacityLimits) -> Iterator[None]:
    """Apply one execution's limits to the tool call running in this context."""
    token = _execution.set(limits)
    try:
        yield
    finally:
        _execution.reset(token)


def current() -> CapacityLimits:
    """Return the running execution's limits, or the startup limits outside one."""
    return _execution.get() or _default
