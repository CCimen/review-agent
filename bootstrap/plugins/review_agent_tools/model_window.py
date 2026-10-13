"""The context window and tool-result budgets Hermes gives one review model."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import threading
from typing import Protocol, cast


class ModelWindowError(RuntimeError):
    """Hermes could not resolve a usable context window for the model."""


@dataclass(frozen=True, slots=True)
class ModelWindow:
    """What one session of the model can hold.

    ``result_chars`` bounds one tool result and ``turn_chars`` all tool results
    of one model response; above either, Hermes replaces results with a preview
    the reviewer cannot open. ``compaction_tokens`` is where the configured
    compression threshold starts summarizing the session.
    """

    context_tokens: int
    result_chars: int
    turn_chars: int
    compaction_tokens: int


class _RuntimeProviderModule(Protocol):
    def resolve_runtime_provider(
        self, *, requested: str | None = None, target_model: str | None = None
    ) -> dict[str, object]: ...


class _ModelMetadataModule(Protocol):
    def get_model_context_length(
        self,
        model: str,
        base_url: str = "",
        api_key: str = "",
        config_context_length: int | None = None,
        provider: str = "",
    ) -> int: ...


class _Budget(Protocol):
    default_result_size: int
    turn_budget: int


class _BudgetConfigModule(Protocol):
    def budget_for_context_window(self, context_length: int | None) -> _Budget: ...


class _ConfigModule(Protocol):
    def load_config_readonly(self) -> object: ...


def _positive(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelWindowError("Hermes returned an invalid model window")
    return value


def _window(context_tokens: int | None) -> tuple[int, int]:
    budgets = cast(_BudgetConfigModule, import_module("tools.budget_config"))
    budget = budgets.budget_for_context_window(context_tokens)
    return _positive(budget.default_result_size), _positive(budget.turn_budget)


def _compression_threshold() -> float:
    """The managed compression threshold Hermes applies to review sessions.

    The managed config reserves no output tokens and sets no per-model or
    absolute compaction caps, so this fraction of the window is where a
    session starts compacting (Hermes may only raise it).
    """
    config = cast(
        _ConfigModule, import_module("hermes_cli.config")
    ).load_config_readonly()
    compression = (
        cast(dict[str, object], config).get("compression")
        if isinstance(config, dict)
        else None
    )
    threshold = (
        cast(dict[str, object], compression).get("threshold")
        if isinstance(compression, dict)
        else None
    )
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int | float)
        or not 0 < threshold <= 1
    ):
        raise ModelWindowError("the managed compression threshold is invalid")
    return float(threshold)


def default_budget() -> tuple[int, int]:
    """Hermes' per-result and per-turn budgets before a model is known."""
    try:
        return _window(None)
    except ModelWindowError:
        raise
    except Exception as exc:  # Hermes has no shared error base for this lookup.
        raise ModelWindowError("Hermes tool-result budget is unavailable") from exc


def resolve(provider: str, model: str) -> ModelWindow:
    """Resolve the window the way Hermes resolves it for this model's session.

    A model connection runs its own Hermes home with one account per provider,
    so the runtime provider resolved here is the session's account.
    """
    try:
        runtime = cast(
            _RuntimeProviderModule, import_module("hermes_cli.runtime_provider")
        ).resolve_runtime_provider(requested=provider, target_model=model)
        metadata = cast(_ModelMetadataModule, import_module("agent.model_metadata"))
        context_tokens = _positive(
            metadata.get_model_context_length(
                model,
                base_url=str(runtime.get("base_url") or ""),
                api_key=str(runtime.get("api_key") or ""),
                provider=str(runtime.get("provider") or provider),
            )
        )
        result_chars, turn_chars = _window(context_tokens)
        compaction_tokens = int(context_tokens * _compression_threshold())
    except ModelWindowError:
        raise
    except Exception as exc:  # Hermes has no shared error base for provider resolution.
        raise ModelWindowError("model context window could not be resolved") from exc
    return ModelWindow(context_tokens, result_chars, turn_chars, compaction_tokens)


# One window per admitted execution, so every tool call of a session uses the
# value resolved once at admission. Admission registers it after commit; the
# execution's recorded completion removes it.
_executions: dict[tuple[int, int], ModelWindow] = {}
_executions_lock = threading.Lock()


def register_execution(job_id: int, lease_generation: int, window: ModelWindow) -> None:
    with _executions_lock:
        _executions[(job_id, lease_generation)] = window


def release_execution(job_id: int, lease_generation: int) -> None:
    with _executions_lock:
        _executions.pop((job_id, lease_generation), None)


def execution_window(job_id: int, lease_generation: int) -> ModelWindow:
    with _executions_lock:
        window = _executions.get((job_id, lease_generation))
    if window is None:
        raise ModelWindowError("this review execution was not admitted by this runtime")
    return window
