"""Abstract base classes for external agent deployers.

Every external agent (OpenClaw, Claude Code, Codex, ...) implements these
ABCs so the runner stays agent-agnostic.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ======================================================================
# Remote VM Config
# ======================================================================


@dataclass
class RemoteVMConfig:
    """Configuration for a remote CUA server endpoint."""

    server_url: str


# ======================================================================
# Unified Interaction Log
# ======================================================================


@dataclass
class InteractionStep:
    """A single step in the agent's interaction.

    Attributes:
        role: Who produced this step — "assistant", "tool", "user", "system".
        type: Step kind — "text", "tool_call", "tool_result", "reasoning",
              "command", "file_change", or agent-specific types.
        content: Human-readable text content (message text, tool output, etc.).
        tool_name: Tool/function name for tool_call / tool_result steps.
        tool_input: Structured input arguments (for tool_call steps).
        tool_call_id: Links a tool_result back to its tool_call.
        metadata: Agent-specific extra fields (exit_code, status, etc.).
    """

    role: str
    type: str
    content: str = ""
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_call_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"role": self.role, "type": self.type}
        if self.content:
            d["content"] = self.content
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.tool_input:
            d["tool_input"] = self.tool_input
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.metadata:
            d["metadata"] = self.metadata
        return d


@dataclass
class InteractionLog:
    """Unified interaction log across all external agents.

    Each deployer converts its native log format into this structure so
    that the runner and evaluation pipeline can inspect agent behavior
    in a consistent way.
    """

    agent_type: str
    steps: list[InteractionStep] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    duration_seconds: float = 0.0
    raw_output: Any = None
    raw_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "steps": [s.to_dict() for s in self.steps],
            "usage": self.usage,
            "duration_seconds": self.duration_seconds,
            "raw_path": self.raw_path,
        }

    def save(self, path: str | Path) -> None:
        """Save the parsed interaction log as JSON."""
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def tool_calls(self) -> list[InteractionStep]:
        return [s for s in self.steps if s.type == "tool_call"]

    @property
    def text_messages(self) -> list[InteractionStep]:
        return [s for s in self.steps if s.type == "text" and s.role == "assistant"]

    def summary(self) -> str:
        n_steps = len(self.steps)
        n_tool_calls = len(self.tool_calls)
        n_messages = len(self.text_messages)
        return (
            f"InteractionLog({self.agent_type}): {n_steps} steps, "
            f"{n_tool_calls} tool calls, {n_messages} messages, "
            f"{self.duration_seconds:.1f}s"
        )


# ======================================================================
# Agent Config / Deployer ABCs
# ======================================================================


@dataclass
class ExternalAgentConfig(ABC):
    """Base configuration for any external agent."""

    agent_type: str = ""
    model: str = ""
    timeout_seconds: int = 600

    # API provider routing: "direct" (default) or "openrouter"
    provider: str = "direct"

    vm_cua_port: int = 5000

    task_variant: int = 0
    task_skip_setup: bool = False
    # deploy_skip: True = golden image has agents pre-installed (default).
    # False = install agent CLIs on the VM before ensure_ready (slow, ~10min).
    deploy_skip: bool = True

    @property
    def is_openrouter(self) -> bool:
        return self.provider == "openrouter"

    @property
    def openrouter_api_key(self) -> str:
        """Read OpenRouter key from environment. Used when provider == 'openrouter'."""
        return os.environ.get("OPENROUTER_API_KEY", "")

    @staticmethod
    def resolve_env(val: Any) -> str | None:
        """Resolve "$VAR" strings to environment variable values."""
        if isinstance(val, str) and val.startswith("$"):
            return os.environ.get(val[1:], "")
        return val

    @classmethod
    @abstractmethod
    def from_yaml(cls, path: str | Path) -> "ExternalAgentConfig":
        """Load config from YAML file."""
        ...


class ExternalAgentDeployer(ABC):
    """Abstract deployer for an external agent on a remote VM.

    Lifecycle: [deploy()] → ensure_ready() → run_agent() → pull_logs()

    deploy() installs the agent CLI from scratch (slow, ~10 min).
    Only called when deploy.skip is false in config.
    ensure_ready() always runs — verifies tooling and writes runtime config.
    All remote operations go through CUA HTTP API (no SSH).
    """

    def __init__(self, vm_config: Any, config: ExternalAgentConfig):
        self.vm_config = vm_config
        self.config = config

    @abstractmethod
    def deploy(self) -> None:
        """Install the agent CLI and dependencies on the VM from scratch.

        Only called when deploy.skip is false. This is the slow path
        (~10 min) that downloads Node.js, installs npm packages, and
        uploads bridge code. For golden images this is already done
        at bake time.
        """
        ...

    @abstractmethod
    def ensure_ready(self) -> None:
        """Verify pre-baked tooling and apply lightweight runtime configuration.

        Always called before run_agent, regardless of deploy.skip.
        Checks that prerequisites exist (raises if missing) and writes
        runtime config files (MCP JSON, config.toml, auth profiles, etc.).
        """
        ...

    @abstractmethod
    def run_agent(self, message: str, timeout: float | None = None) -> dict:
        """Launch the agent, wait for completion.

        Returns dict with at least:
            status, execution_time, output, usage, timed_out, interaction_log
        """
        ...

    @abstractmethod
    def pull_logs(self, local_dir: str | Path) -> dict[str, Any]:
        """Retrieve agent logs from the VM.

        Returns dict with at least:
            transcript_path, usage, interaction_log
        """
        ...
