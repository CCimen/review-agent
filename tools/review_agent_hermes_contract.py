#!/usr/bin/env python3
"""Verify the pinned Hermes adapter contracts used by the review worker."""

from __future__ import annotations

from pathlib import Path


HERMES_ROOT = Path("/opt/hermes")


def _source(relative_path: str) -> str:
    path = HERMES_ROOT / relative_path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Hermes contract source is unavailable: {path}") from exc


def _require(source: str, fragment: str, contract: str) -> None:
    if fragment not in source:
        raise SystemExit(f"Pinned Hermes contract changed: {contract}")


def main() -> int:
    api_server = _source("gateway/platforms/api_server.py")
    tool_executor = _source("agent/tool_executor.py")
    model_tools = _source("model_tools.py")
    registry = _source("tools/registry.py")
    system_prompt = _source("agent/system_prompt.py")

    _require(
        api_server,
        'request.headers.get("X-Hermes-Session-Id", "").strip()',
        "chat requests must read the trusted session header",
    )
    _require(
        api_server,
        '"session_id": session_id,',
        "the API server must construct the agent with that session id",
    )
    _require(
        tool_executor,
        'session_id=agent.session_id or "",',
        "tool execution must forward the agent session id",
    )
    _require(
        model_tools,
        "return registry.dispatch(\n"
        "                        function_name, next_args,\n"
        "                        task_id=task_id,\n"
        "                        session_id=session_id,\n"
        "                        user_task=user_task,",
        "model tool dispatch must forward the session id",
    )
    _require(
        registry,
        "def dispatch(\n"
        "        self,\n"
        "        name: str,\n"
        "        args: dict,\n"
        "        *,\n"
        "        scope: Optional[str] = None,\n"
        "        **kwargs,\n"
        "    ) -> str | dict:",
        "the registry must receive forwarded tool context as kwargs",
    )
    _require(
        registry,
        "result = entry.handler(args, **kwargs)",
        "the registry must pass trusted context to custom handlers",
    )
    _require(
        system_prompt,
        "if agent.load_soul_identity or not agent.skip_context_files:",
        "the API agent must retain native SOUL loading",
    )
    _require(
        system_prompt,
        "context_files_prompt = _r.build_context_files_prompt(",
        "the API agent must retain native AGENTS context loading",
    )
    print("Pinned Hermes worker contracts verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
