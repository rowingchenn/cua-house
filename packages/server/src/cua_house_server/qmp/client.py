"""Async QMP (QEMU Machine Protocol) client for VM snapshot control.

Sends QMP commands to QEMU inside Docker containers via ``docker exec`` + ``nc``.
Direct TCP port forwarding does not reliably relay QMP data through Docker's proxy,
so we shell into the container for each command sequence.

QMP port 7200 is enabled inside dockur/windows containers via
``ENV ARGUMENTS="-qmp tcp:0.0.0.0:7200,server,nowait"`` in the Dockerfile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# QMP internal port inside the container (set by Dockerfile ENV ARGUMENTS)
QMP_PORT = 7200


class QMPError(Exception):
    """Raised when QMP returns an error response."""

    def __init__(self, message: str, raw: Any = None):
        self.raw = raw
        super().__init__(message)


def _qmp_exec_sync(
    container_name: str,
    commands: list[dict[str, Any]],
    *,
    inter_command_sleep: float = 0.3,
    final_sleep: float = 1.0,
) -> list[dict[str, Any]]:
    """Send QMP commands via ``docker exec`` + ``nc`` (synchronous).

    Constructs a shell one-liner that pipes a command sequence into nc,
    then parses the JSON responses.
    """
    # Build the shell input: greeting negotiation + user commands
    parts = [
        f"sleep {inter_command_sleep}",
        'echo \'{"execute":"qmp_capabilities"}\'',
        f"sleep {inter_command_sleep}",
    ]
    for cmd in commands:
        parts.append(f"echo '{json.dumps(cmd)}'")
        parts.append(f"sleep {inter_command_sleep}")

    # Final sleep to let nc read all responses before closing
    parts.append(f"sleep {final_sleep}")
    shell_input = "; ".join(parts)

    result = subprocess.run(
        [
            "docker", "exec", container_name, "sh", "-c",
            f"{{ {shell_input}; }} | nc -q1 127.0.0.1 {QMP_PORT}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )

    responses: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("QMP: unparseable line: %s", line)
    return responses


async def _qmp_exec(
    container_name: str,
    commands: list[dict[str, Any]],
    *,
    inter_command_sleep: float = 0.3,
    final_sleep: float = 1.0,
) -> list[dict[str, Any]]:
    """Async wrapper around _qmp_exec_sync -- runs in a thread."""
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _qmp_exec_sync(
            container_name,
            commands,
            inter_command_sleep=inter_command_sleep,
            final_sleep=final_sleep,
        ),
    )


def _check_hmp_result(responses: list[dict[str, Any]], operation: str) -> None:
    """Check human-monitor-command responses for errors."""
    for resp in responses:
        ret = resp.get("return", None)
        if isinstance(ret, str) and ret and "error" in ret.lower():
            raise QMPError(f"{operation} failed: {ret}", raw=resp)


class QMPClient:
    """QMP client for QEMU VMs inside Docker containers.

    Uses ``docker exec`` to send QMP commands via nc.  Each high-level
    operation (save_snapshot, load_snapshot, etc.) opens a fresh nc session.

    Usage::

        qmp = QMPClient("cua-house-env-abc123")
        await qmp.save_snapshot("clean-ready")
        # ... later ...
        await qmp.load_snapshot("clean-ready")
    """

    def __init__(self, container_name: str):
        self.container_name = container_name

    async def save_snapshot(
        self,
        name: str = "clean-ready",
        timeout: float = 300.0,
    ) -> None:
        """Pause VM, save full snapshot (RAM + CPU + disk), resume.

        Uses ``human-monitor-command`` to wrap the HMP ``savevm`` command.
        The snapshot stores compressed RAM, CPU state, and disk state inside
        the qcow2 overlay file.

        The nc pipe blocks on savevm (synchronous HMP command) -- subsequent
        commands queue and run after savevm completes.  final_sleep=5 is
        enough since savevm itself is the bottleneck.
        """
        logger.info(
            "Saving snapshot '%s' on %s (timeout=%ds)...",
            name, self.container_name, timeout,
        )
        responses = await asyncio.wait_for(
            _qmp_exec(
                self.container_name,
                [
                    {"execute": "stop"},
                    {"execute": "human-monitor-command",
                     "arguments": {"command-line": f"savevm {name}"}},
                    {"execute": "cont"},
                ],
                inter_command_sleep=1.0,
                final_sleep=5.0,
            ),
            timeout=timeout,
        )
        _check_hmp_result(responses, "savevm")
        logger.info("Snapshot '%s' saved on %s", name, self.container_name)

    async def load_snapshot(
        self,
        name: str = "clean-ready",
        timeout: float = 300.0,
    ) -> None:
        """Load snapshot, restoring full VM state, then resume.

        After ``loadvm`` the VM is paused; we issue ``cont`` to resume.
        The nc pipe blocks on loadvm -- cont queues and runs after loadvm
        completes.
        """
        logger.info(
            "Loading snapshot '%s' on %s (timeout=%ds)...",
            name, self.container_name, timeout,
        )
        responses = await asyncio.wait_for(
            _qmp_exec(
                self.container_name,
                [
                    {"execute": "human-monitor-command",
                     "arguments": {"command-line": f"loadvm {name}"}},
                    {"execute": "cont"},
                ],
                inter_command_sleep=1.0,
                final_sleep=5.0,
            ),
            timeout=timeout,
        )
        _check_hmp_result(responses, "loadvm")
        logger.info("Snapshot '%s' loaded on %s", name, self.container_name)

    async def query_status(self) -> dict[str, Any]:
        """Return VM running status."""
        responses = await _qmp_exec(
            self.container_name,
            [{"execute": "query-status"}],
        )
        for resp in responses:
            ret = resp.get("return")
            if isinstance(ret, dict) and "status" in ret:
                return ret
        raise QMPError("No status response", raw=responses)

    async def query_snapshots(self) -> str:
        """List snapshots via HMP ``info snapshots``."""
        responses = await _qmp_exec(
            self.container_name,
            [{"execute": "human-monitor-command",
              "arguments": {"command-line": "info snapshots"}}],
        )
        for resp in responses:
            ret = resp.get("return")
            if isinstance(ret, str):
                return ret
        return ""

    async def is_alive(self) -> bool:
        """Check if QMP is responsive."""
        try:
            await self.query_status()
            return True
        except Exception:
            return False
