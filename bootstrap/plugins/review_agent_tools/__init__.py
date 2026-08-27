"""Bounded pull-request context and review-memory plugin."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Protocol, cast


class ToolRegistry(Protocol):
    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., str],
    ) -> None: ...


class _InstalledReviewContract(Protocol):
    plugin_result_max_chars: int


class _ReviewContractModule(Protocol):
    def load_installed_contract(self) -> _InstalledReviewContract: ...


def _installed_result_max_chars() -> int:
    """Read the result budget from the verified installed behavior receipt."""
    review_contract = cast(
        _ReviewContractModule, import_module(f"{__name__}.review_contract")
    )
    contract = review_contract.load_installed_contract()
    return contract.plugin_result_max_chars


def register(ctx: ToolRegistry) -> None:
    # Import concrete owners after package initialization so submodules can import
    # package-owned contracts without creating a static package import cycle.
    capacity = import_module(f"{__name__}.capacity")
    limits = capacity.configure(result_max_chars=_installed_result_max_chars())
    schemas = import_module(f"{__name__}.schemas")
    schemas.apply_capacity(limits)
    source_tools = import_module(f"{__name__}.review_source_tools")
    memory_tools = import_module(f"{__name__}.review_memory_tools")
    delivery_tool = import_module(f"{__name__}.review_delivery_tool")

    ctx.register_tool(
        name="review_agent_begin",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_BEGIN"),
        handler=getattr(source_tools, "review_begin"),
    )
    ctx.register_tool(
        name="review_agent_pr_diff",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_PR_DIFF"),
        handler=getattr(source_tools, "pr_diff"),
    )
    ctx.register_tool(
        name="review_agent_pr_files",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_PR_FILES"),
        handler=getattr(source_tools, "pr_files"),
    )
    ctx.register_tool(
        name="review_agent_pr_file",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_PR_FILE"),
        handler=getattr(source_tools, "pr_file"),
    )
    ctx.register_tool(
        name="review_agent_memory_context",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_MEMORY_CONTEXT"),
        handler=getattr(memory_tools, "review_memory_context"),
    )
    ctx.register_tool(
        name="review_agent_memory_record",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_MEMORY_RECORD"),
        handler=getattr(memory_tools, "review_memory_record"),
    )
    ctx.register_tool(
        name="review_agent_deliver",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_DELIVER"),
        handler=getattr(delivery_tool, "review_deliver"),
    )
