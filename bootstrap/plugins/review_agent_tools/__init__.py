"""Bounded pull-request context and review-memory plugin."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Protocol, cast


class ToolRegistry(Protocol):
    def register_platform_handler(
        self, platform: str, factory: Callable[[object, object], None]
    ) -> None: ...

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
    graph_tool = import_module(f"{__name__}.review_code_graph_tool")
    runtime_api = import_module(f"{__name__}.hermes_runtime_api")
    docs_tools = import_module(f"{__name__}.documentation_tools")
    docs_findings = import_module(f"{__name__}.documentation_findings")
    ctx.register_platform_handler("api_server", getattr(runtime_api, "wire_api"))

    ctx.register_tool(name="review_agent_docs_begin", toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_DOCS_BEGIN"), handler=getattr(docs_tools, "docs_begin"))
    ctx.register_tool(name="review_agent_docs_scope", toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_DOCS_SCOPE"), handler=getattr(docs_tools, "docs_scope"))
    ctx.register_tool(name="review_agent_docs_file", toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_DOCS_FILE"), handler=getattr(docs_tools, "docs_file"))
    ctx.register_tool(name="review_agent_docs_deliver", toolset="review_agent", schema={
        "name": "review_agent_docs_deliver",
        "description": "Validate one complete documentation assessment against recorded exact evidence and hand its advisory check to the publisher. The engine computes finding identity, suppression, outcome, and coverage. No repository edits.",
        "parameters": {
            "type": "object", "properties": {
                "run_id": {"type": "integer", "minimum": 1},
                "assessment": getattr(docs_findings, "DOCUMENTATION_ASSESSMENT_SCHEMA"),
            }, "required": ["run_id", "assessment"], "additionalProperties": False,
        },
    }, handler=getattr(docs_tools, "docs_deliver"))

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
        name="review_agent_related_code",
        toolset="review_agent",
        schema=getattr(schemas, "REVIEW_AGENT_RELATED_CODE"),
        handler=getattr(graph_tool, "related_code"),
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
