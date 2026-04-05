"""Bake preinstalled external-agent tooling into a Windows golden image.

This is an admin/maintenance workflow, not part of normal orchestration.
It allocates a temporary VM through agenthle-env-server, installs pinned
agent tooling inside the guest, verifies boot-time gateway startup, then
freezes the overlay back into the golden image on the KVM host.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from cua_house.remote.base import RemoteVMConfig
from cua_house.remote.remote import _run_remote, _upload_directory, _upload_file

NODE_VERSION = "24.12.0"
OPENCLAW_VERSION = "2026.3.13"
CLAUDE_CODE_VERSION = "2.1.85"
CODEX_VERSION = "0.114.0"

NODE_DIR = rf"C:\Users\User\node-v{NODE_VERSION}-win-x64"
NODE_ZIP = rf"C:\Users\User\node-v{NODE_VERSION}-win-x64.zip"
NODE_EXE = rf"{NODE_DIR}\node.exe"
NPM_CMD = rf"{NODE_DIR}\npm.cmd"
NPM_BIN = r"C:\Users\User\AppData\Roaming\npm"
OPENCLAW_CMD = rf"{NPM_BIN}\openclaw.cmd"
OPENCLAW_ENTRY = r"C:\Users\User\AppData\Roaming\npm\node_modules\openclaw\openclaw.mjs"
CLAUDE_CMD = rf"{NPM_BIN}\claude.cmd"
CODEX_CMD = rf"{NPM_BIN}\codex.cmd"

REMOTE_PLUGIN_DIR = r"C:\Users\User\openclaw-cua-plugin"
REMOTE_MCP_SERVER_DIR = r"C:\Users\User\cua_mcp_server"
REMOTE_CLAUDE_MCP_CONFIG = r"C:\Users\User\claude_mcp.json"
REMOTE_CODEX_CONFIG_DIR = r"C:\Users\User\.codex"
REMOTE_SCRIPTS_DIR = r"C:\Users\User\scripts"
REMOTE_GATEWAY_TASK = "OpenClaw Gateway"
REMOTE_GATEWAY_LEGACY_TASK = "OpenClawGateway-agenthle"
REMOTE_GATEWAY_HEALTH_URL = "http://127.0.0.1:18789/health"
REMOTE_OPENCLAW_CONFIG = r"C:\Users\User\.openclaw\openclaw.json"


def _default_bridge_root() -> Path:
    return Path(__file__).resolve().parents[1] / "remote" / "bridges"


class EnvServerAdminError(RuntimeError):
    """Raised when bake/provisioning hits an unrecoverable error."""


class EnvServerApi:
    """Small synchronous env-server client used by the bake workflow."""

    def __init__(self, base_url: str, token: str | None):
        self.base_url = base_url.rstrip("/")
        self.headers: dict[str, str] = {}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def request(self, method: str, path: str, *, timeout: float = 30.0, **kwargs) -> dict[str, Any]:
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=self.headers,
            timeout=timeout,
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


class HeartbeatLoop:
    """Keeps a temporary bake batch/lease alive during long provisioning."""

    def __init__(self, api: EnvServerApi, batch_id: str):
        self.api = api
        self.batch_id = batch_id
        self.lease_id: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.api.request("POST", f"/v1/batches/{self.batch_id}/heartbeat")
                if self.lease_id:
                    self.api.request("POST", f"/v1/leases/{self.lease_id}/heartbeat")
            except Exception as exc:  # noqa: BLE001
                print(f"[heartbeat] warning: {exc}", flush=True)
            self._stop.wait(20)


@dataclass
class BakeLease:
    batch_id: str
    task_id: str
    lease_id: str
    slot_id: str
    cua_url: str


class BakeVm:
    """Imperative remote admin helper backed by the CUA `/cmd` endpoint."""

    def __init__(self, server_url: str):
        self.vm_config = RemoteVMConfig(server_url=server_url)

    def run(self, command: str, *, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
        result = _run_remote(self.vm_config, command, timeout=timeout)
        print(
            json.dumps(
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "")[:1200],
                    "stderr": (result.stderr or "")[:1200],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if check and result.returncode != 0:
            raise EnvServerAdminError(
                f"Remote command failed ({result.returncode}): {command}\n"
                f"STDOUT={result.stdout}\nSTDERR={result.stderr}"
            )
        return result

    def run_ps(self, ps: str, *, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
        encoded = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
        return self.run(
            f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}",
            timeout=timeout,
            check=check,
        )

    def run_cli(self, cmd_path: str, args: str, *, timeout: float = 60.0, check: bool = True) -> subprocess.CompletedProcess:
        ps = (
            f"$env:PATH = [string]::Join(';', @($env:PATH, '{NODE_DIR}', '{NPM_BIN}')); "
            f"& '{cmd_path}' {args}"
        )
        return self.run_ps(ps, timeout=timeout, check=check)

    def upload_text(self, remote_path: str, content: str) -> None:
        if not _upload_file(self.vm_config, remote_path, content):
            raise EnvServerAdminError(f"Failed to upload {remote_path}")

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        _upload_directory(self.vm_config, local_dir, remote_dir)

    def wait_gateway_health(self, *, timeout_s: int = 120, interval_s: int = 5, phase: str) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            result = self.run_ps(
                (
                    "$conn = Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue; "
                    "if ($conn) { Write-Output 'gateway-port-ready'; exit 0 }; "
                    "$legacy = netstat -ano | Select-String ':18789'; "
                    "if ($legacy) { Write-Output 'gateway-port-ready'; exit 0 } else { exit 1 }"
                ),
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                print(f"[gateway] healthy during {phase}", flush=True)
                return
            time.sleep(interval_s)
        raise EnvServerAdminError(f"Gateway did not become healthy during {phase}")

    def wait_gateway_autostart(self, *, timeout_s: int = 300, interval_s: int = 5, phase: str) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last_state: dict[str, Any] | None = None
        while time.time() < deadline:
            task_result = self.run(
                f'schtasks /Query /TN "{REMOTE_GATEWAY_TASK}" /V /FO LIST',
                timeout=15,
                check=False,
            )
            if task_result.returncode != 0:
                task_result = self.run(
                    f'schtasks /Query /TN "{REMOTE_GATEWAY_LEGACY_TASK}" /V /FO LIST',
                    timeout=15,
                    check=False,
                )
            health_result = self.run(
                (
                    "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                    f"\"try {{ $resp = Invoke-WebRequest -UseBasicParsing -Uri '{REMOTE_GATEWAY_HEALTH_URL}' "
                    "-TimeoutSec 5; Write-Output $resp.Content; exit 0 } catch { $_ | Out-String; exit 1 }\""
                ),
                timeout=15,
                check=False,
            )
            last_state = {
                "task_query_rc": task_result.returncode,
                "task_summary": (task_result.stdout or "")[:1500],
                "health_rc": health_result.returncode,
                "health_output": ((health_result.stdout or "") + (health_result.stderr or ""))[:1500],
            }
            if health_result.returncode == 0:
                print(f"[gateway] autostart confirmed during {phase}: {json.dumps(last_state)}", flush=True)
                return last_state
            time.sleep(interval_s)
        raise EnvServerAdminError(
            f"Gateway did not confirm auto-start during {phase}. Last state: {json.dumps(last_state or {}, ensure_ascii=False)}"
        )

    def wait_cua_status(self, *, timeout_s: int = 900, interval_s: int = 5) -> None:
        deadline = time.time() + timeout_s
        status_url = f"{self.vm_config.server_url.rstrip('/')}/status"
        while time.time() < deadline:
            try:
                response = requests.get(status_url, timeout=10)
                if response.ok:
                    print(f"[guest] status after reboot: {response.text}", flush=True)
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval_s)
        raise EnvServerAdminError("Timed out waiting for guest to become ready after reboot")

    def wait_command_channel(self, *, timeout_s: int = 180, interval_s: int = 5) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            result = self.run("cmd /c echo cmd-ready", timeout=15, check=False)
            if result.returncode == 0 and "cmd-ready" in (result.stdout or "").lower():
                print("[guest] command channel ready after reboot", flush=True)
                return
            time.sleep(interval_s)
        raise EnvServerAdminError("Timed out waiting for guest command channel after reboot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-server-url", default=os.environ.get("AGENTHLE_ENV_SERVER_URL", ""))
    parser.add_argument("--env-server-token", default=os.environ.get("AGENTHLE_TOKEN"))
    parser.add_argument("--task-path", default="./tasks/demo_web_search")
    parser.add_argument("--image-key", default="cpu-free")
    parser.add_argument("--cpu-cores", type=int, default=4)
    parser.add_argument("--memory-gb", type=int, default=16)
    parser.add_argument("--remote-host", default="agenthle-nested-kvm-01")
    parser.add_argument("--golden-dir", default="/home/weichenzhang/agenthle-env-images/cpu-free")
    parser.add_argument("--bridge-root", default=str(_default_bridge_root()))
    parser.add_argument("--skip-freeze", action="store_true")
    parser.add_argument("--preserve-on-error", action="store_true")
    parser.add_argument("--report-path", default="")
    return parser


def require_arg(value: str, env_name: str) -> str:
    if value:
        return value
    raise EnvServerAdminError(f"{env_name} must be provided")


def allocate_bake_lease(
    api: EnvServerApi,
    *,
    task_path: str,
    image_key: str,
    cpu_cores: int,
    memory_gb: int,
) -> tuple[BakeLease, HeartbeatLoop]:
    task_id = f"image-bake-{int(time.time())}"
    payload = {
        "tasks": [
            {
                "task_id": task_id,
                "task_path": task_path,
                "image_key": image_key,
                "cpu_cores": cpu_cores,
                "memory_gb": memory_gb,
                "metadata": {"purpose": "image-bake"},
            }
        ]
    }
    batch = api.request("POST", "/v1/batches", json=payload)
    batch_id = batch["batch_id"]
    heartbeats = HeartbeatLoop(api, batch_id)
    heartbeats.start()
    print(json.dumps({"batch_id": batch_id, "task_id": task_id}, indent=2), flush=True)

    try:
        assignment: dict[str, Any] | None = None
        deadline = time.time() + 1800
        while time.time() < deadline:
            task = api.request("GET", f"/v1/tasks/{task_id}")
            if task.get("lease_id"):
                heartbeats.lease_id = task["lease_id"]
            if task.get("assignment") and task["state"] in {"ready", "leased"}:
                assignment = task["assignment"]
                break
            if task["state"] == "failed":
                raise EnvServerAdminError(f"Temporary bake VM failed before ready: {task.get('error')}")
            time.sleep(5)
        if assignment is None:
            raise EnvServerAdminError("Timed out waiting for temporary bake VM assignment")
        return (
            BakeLease(
                batch_id=batch_id,
                task_id=task_id,
                lease_id=assignment["lease_id"],
                slot_id=assignment["slot_id"],
                cua_url=assignment["cua_url"],
            ),
            heartbeats,
        )
    except Exception:
        heartbeats.stop()
        raise


def node_and_cli_install(vm: BakeVm) -> None:
    print("[bake] checking node", flush=True)
    node_check = vm.run_ps(
        f"if (Test-Path '{NODE_EXE}') {{ & '{NODE_EXE}' --version }}",
        timeout=20,
        check=False,
    )
    if node_check.returncode != 0 or not (node_check.stdout or "").strip().startswith("v"):
        print("[bake] downloading node zip with retry", flush=True)
        node_zip_url = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"
        download_ps = (
            f"$zip = '{NODE_ZIP}'; "
            "if (Test-Path $zip) { Remove-Item -Force $zip -ErrorAction SilentlyContinue }; "
            f"$url = '{node_zip_url}'; "
            "$curl = Get-Command curl.exe -ErrorAction SilentlyContinue; "
            "if ($curl) { "
            "  & $curl.Source -L --retry 8 --retry-delay 5 --retry-all-errors --connect-timeout 30 "
            "    --max-time 900 -o $zip $url; "
            "} else { "
            "  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
            "  Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing; "
            "}; "
            "if (-not (Test-Path $zip)) { throw 'Node zip download did not create the expected file' }; "
            "$size = (Get-Item $zip).Length; "
            "if ($size -lt 20000000) { throw ('Node zip too small (' + $size + ' bytes)') }"
        )
        vm.run_ps(download_ps, timeout=960)
        vm.run_ps(
            f"if (Test-Path '{NODE_DIR}') {{ Remove-Item -Recurse -Force '{NODE_DIR}' }}; "
            f"Expand-Archive -Path '{NODE_ZIP}' -DestinationPath 'C:\\Users\\User' -Force",
            timeout=300,
        )
    vm.run_ps(f"New-Item -ItemType Directory -Force -Path '{NPM_BIN}' | Out-Null", timeout=20)
    vm.run_ps(f"& '{NPM_CMD}' config set prefix '{NPM_BIN}'", timeout=30)
    vm.run_ps(f"& '{NODE_EXE}' --version; & '{NPM_CMD}' --version", timeout=30)
    vm.run_ps(f"& '{NPM_CMD}' config get prefix", timeout=30)

    print("[bake] installing pinned CLIs", flush=True)
    vm.run_cli(NPM_CMD, f"install -g openclaw@{OPENCLAW_VERSION}", timeout=1800)
    vm.run_cli(NPM_CMD, f"install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION}", timeout=1800)
    vm.run_cli(NPM_CMD, f"install -g @openai/codex@{CODEX_VERSION}", timeout=1800)

    vm.run_cli(OPENCLAW_CMD, "--version", timeout=30)
    vm.run_cli(CLAUDE_CMD, "--version", timeout=30)
    vm.run_cli(CODEX_CMD, "--version", timeout=30)
    vm.run("git --version", timeout=20, check=False)


def preinstall_bridges(vm: BakeVm, bridge_root: Path, *, openai_api_key: str | None) -> None:
    print("[bake] uploading bridge directories", flush=True)
    vm.upload_dir(bridge_root / "cua_mcp_server", REMOTE_MCP_SERVER_DIR)
    vm.upload_dir(bridge_root / "openclaw_plugin", REMOTE_PLUGIN_DIR)
    vm.run_ps(f"Set-Location '{REMOTE_MCP_SERVER_DIR}'; & '{NPM_CMD}' install --production", timeout=600)

    print("[bake] installing OpenClaw plugin", flush=True)
    plugin_install = vm.run_cli(OPENCLAW_CMD, f"plugins install '{REMOTE_PLUGIN_DIR}'", timeout=300, check=False)
    plugin_output = f"{plugin_install.stdout}\n{plugin_install.stderr}".lower()
    plugin_already_present = "plugin already exists" in plugin_output
    if (plugin_install.returncode != 0 or "failed during register" in plugin_output) and not plugin_already_present:
        if not openai_api_key:
            raise EnvServerAdminError(
                "OpenClaw plugin install failed without onboarding, and OPENAI_API_KEY is not available "
                "for temporary bootstrap."
            )
        print("[bake] plugin install required temporary onboarding", flush=True)
        vm.run_cli(
            OPENCLAW_CMD,
            (
                "onboard --non-interactive --accept-risk "
                f"--openai-api-key {openai_api_key} "
                "--skip-channels --skip-skills --no-install-daemon --skip-health --skip-ui"
            ),
            timeout=300,
        )
        plugin_install = vm.run_cli(OPENCLAW_CMD, f"plugins install '{REMOTE_PLUGIN_DIR}'", timeout=300, check=False)
        plugin_output = f"{plugin_install.stdout}\n{plugin_install.stderr}".lower()
        plugin_already_present = "plugin already exists" in plugin_output
        if (plugin_install.returncode != 0 or "failed during register" in plugin_output) and not plugin_already_present:
            raise EnvServerAdminError(
                "OpenClaw plugin install still failed after temporary onboarding. "
                f"stdout={plugin_install.stdout}\nstderr={plugin_install.stderr}"
            )
    current_cfg = vm.run_ps(f"Get-Content '{REMOTE_OPENCLAW_CONFIG}' -Raw", timeout=30)
    config_data = json.loads(current_cfg.stdout or "{}")
    config_data.setdefault("gateway", {})
    config_data["gateway"]["mode"] = "local"
    config_data.setdefault("plugins", {})
    config_data["plugins"]["allow"] = ["cua"]
    vm.upload_text(REMOTE_OPENCLAW_CONFIG, json.dumps(config_data, indent=2))

    print("[bake] writing default agent configs", flush=True)
    claude_mcp = json.dumps(
        {
            "mcpServers": {
                "cua": {
                    "command": NODE_EXE,
                    "args": [rf"{REMOTE_MCP_SERVER_DIR}\src\index.js"],
                }
            }
        },
        indent=2,
    )
    vm.upload_text(REMOTE_CLAUDE_MCP_CONFIG, claude_mcp)
    vm.run_ps(f"New-Item -ItemType Directory -Force -Path '{REMOTE_CODEX_CONFIG_DIR}' | Out-Null", timeout=20)
    codex_toml = (
        '[mcp_servers.cua]\n'
        'type = "stdio"\n'
        f'command = "{NODE_EXE.replace("\\", "/")}"\n'
        f'args = ["{(REMOTE_MCP_SERVER_DIR + r"\\src\\index.js").replace("\\", "/")}"]\n'
    )
    vm.upload_text(r"C:\Users\User\.codex\config.toml", codex_toml)
    vm.run_ps(
        "New-Item -ItemType Directory -Force -Path 'C:\\Users\\User\\claude_work' | Out-Null; "
        "New-Item -ItemType Directory -Force -Path 'C:\\Users\\User\\codex_work' | Out-Null",
        timeout=20,
    )


def configure_openclaw_gateway_autostart(vm: BakeVm) -> None:
    print("[bake] configuring OpenClaw gateway autostart", flush=True)
    vm.run_cli(OPENCLAW_CMD, "gateway install", timeout=180, check=False)

    official_task = vm.run(
        f'schtasks /Query /TN "{REMOTE_GATEWAY_TASK}" /V /FO LIST',
        timeout=20,
        check=False,
    )
    if official_task.returncode == 0:
        vm.run_ps(f"schtasks /Run /TN '{REMOTE_GATEWAY_TASK}'", timeout=20, check=False)
    else:
        legacy_task = vm.run(
            f'schtasks /Query /TN "{REMOTE_GATEWAY_LEGACY_TASK}" /V /FO LIST',
            timeout=20,
            check=False,
        )
        if legacy_task.returncode != 0:
            raise EnvServerAdminError(
                "Neither the official nor the legacy OpenClaw gateway task is present on the guest."
            )
        vm.run_ps(f"schtasks /Run /TN '{REMOTE_GATEWAY_LEGACY_TASK}'", timeout=20, check=False)

    vm.wait_gateway_autostart(timeout_s=180, phase="pre-reboot validation")


def clean_guest_image_state(vm: BakeVm) -> None:
    print("[bake] cleaning guest user-state before freeze", flush=True)
    cleanup_ps = r"""
$ErrorActionPreference = 'SilentlyContinue'

$downloads = 'C:\Users\User\Downloads'
if (Test-Path $downloads) {
  Get-ChildItem $downloads -Force | Where-Object { $_.Name -ne 'desktop.ini' } | ForEach-Object {
    Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$desktopRoots = @(
  'C:\Users\User\Desktop\finance',
  'C:\Users\User\Desktop\chemistry',
  'C:\Users\User\Desktop\hardware',
  'C:\Users\User\Desktop\pathology',
  'C:\Users\User\Desktop\precision_health'
)
foreach ($path in $desktopRoots) {
  if (Test-Path $path) {
    Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$desktopFiles = @(
  'C:\Users\User\Desktop\agenthle_desktop_plugin_demo.txt'
)
foreach ($path in $desktopFiles) {
  if (Test-Path $path) {
    Remove-Item $path -Force -ErrorAction SilentlyContinue
  }
}

$documentRoots = @(
  'C:\Users\User\Documents\Code',
  'C:\Users\User\Documents\KiCad',
  'C:\Users\User\Documents\MicroDicomDB'
)
foreach ($path in $documentRoots) {
  if (Test-Path $path) {
    Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$openclawEphemeral = @(
  'C:\Users\User\.openclaw\canvas',
  'C:\Users\User\.openclaw\cron',
  'C:\Users\User\.openclaw\logs',
  'C:\Users\User\.openclaw\agents',
  'C:\Users\User\.openclaw\update-check.json',
  'C:\Users\User\.openclaw\jobs.json'
)
foreach ($path in $openclawEphemeral) {
  if (Test-Path $path) {
    Remove-Item $path -Recurse -Force -ErrorAction SilentlyContinue
  }
}

$scriptLogs = @(
  'C:\Users\User\scripts\openclaw_gateway.log',
  'C:\Users\User\scripts\openclaw_gateway.err.log',
  'C:\Users\User\scripts\server.log'
)
foreach ($path in $scriptLogs) {
  if (Test-Path $path) {
    Remove-Item $path -Force -ErrorAction SilentlyContinue
  }
}

$runtimeFiles = @(
  'C:\Users\User\codex_output.json',
  'C:\Users\User\codex_error.log',
  'C:\Users\User\codex_stderr.log',
  'C:\Users\User\claude_error.log',
  'C:\Users\User\claude_stderr.log',
  'C:\Users\User\codex_pid.txt',
  'C:\Users\User\codex_prompt.txt',
  'C:\Users\User\codex_runner.ps1',
  'C:\Users\User\openclaw_response.txt',
  'C:\Users\User\openclaw_http_code.txt'
)
foreach ($path in $runtimeFiles) {
  if (Test-Path $path) {
    Remove-Item $path -Force -ErrorAction SilentlyContinue
  }
}
"""
    vm.run_ps(cleanup_ps, timeout=120)


def reboot_and_validate(vm: BakeVm) -> None:
    print("[bake] rebooting guest to validate auto-start", flush=True)
    vm.run("shutdown /r /t 0", timeout=15, check=False)
    vm.wait_cua_status(timeout_s=900)
    vm.wait_command_channel(timeout_s=180)
    vm.wait_gateway_autostart(timeout_s=480, interval_s=10, phase="post-reboot validation")


def collect_gateway_debug(vm: BakeVm) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}

    def capture(name: str, command: str, *, timeout: float = 30.0) -> None:
        result = vm.run(command, timeout=timeout, check=False)
        snapshots[name] = {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    capture(
        "gateway_task",
        f'schtasks /Query /TN "{REMOTE_GATEWAY_TASK}" /V /FO LIST',
        timeout=20,
    )
    capture(
        "gateway_legacy_task",
        f'schtasks /Query /TN "{REMOTE_GATEWAY_LEGACY_TASK}" /V /FO LIST',
        timeout=20,
    )
    capture(
        "gateway_status",
        f'"{OPENCLAW_CMD}" gateway status --json',
        timeout=20,
    )
    capture(
        "gateway_health",
        (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            "\"try { $resp = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18789/health' "
            "-TimeoutSec 5; Write-Output $resp.Content } catch { $_ | Out-String; exit 1 }\""
        ),
        timeout=20,
    )
    capture(
        "gateway_processes",
        (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            "\"Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -in @('node.exe','cmd.exe','powershell.exe') -or "
            "$_.CommandLine -like '*openclaw*' "
            "} | Select-Object Name,ProcessId,ParentProcessId,CommandLine | Format-List\""
        ),
        timeout=30,
    )
    return snapshots


def freeze_overlay(
    *,
    remote_host: str,
    slot_id: str,
    golden_dir: str,
) -> dict[str, str]:
    golden_path = f"{golden_dir}/golden.qcow2"
    slot_root = f"/home/weichenzhang/agenthle-env-runtime/slots/{slot_id}"
    container_name = f"agenthle-env-{slot_id}"
    ts = time.strftime("%Y%m%d-%H%M%S")
    new_golden_tmp = f"{golden_dir}/golden.qcow2.bake-{ts}.tmp"
    old_backup = f"{golden_dir}/golden.qcow2.backup-{ts}"
    ssh_script = f"""
set -euo pipefail
sudo systemctl stop agenthle-env-server
(docker rm -f {container_name} || true) >/dev/null 2>&1
qemu-img rebase -u -b '{golden_path}' -F qcow2 '{slot_root}/storage/data.qcow2'
qemu-img convert -p -O qcow2 '{slot_root}/storage/data.qcow2' '{new_golden_tmp}'
mv '{golden_path}' '{old_backup}'
mv '{new_golden_tmp}' '{golden_path}'
rm -rf '{slot_root}'
sudo systemctl start agenthle-env-server
sudo systemctl is-active agenthle-env-server
""".strip()
    subprocess.run(["ssh", remote_host, ssh_script], check=True)
    return {
        "golden_path": golden_path,
        "backup_path": old_backup,
    }


def write_report(path: str | Path, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    base_url = require_arg(args.env_server_url, "AGENTHLE_ENV_SERVER_URL")
    api = EnvServerApi(base_url, args.env_server_token)
    bridge_root = Path(args.bridge_root)
    if not bridge_root.exists():
        raise EnvServerAdminError(f"Bridge root not found: {bridge_root}")

    lease, heartbeat = allocate_bake_lease(
        api,
        task_path=args.task_path,
        image_key=args.image_key,
        cpu_cores=args.cpu_cores,
        memory_gb=args.memory_gb,
    )

    report: dict[str, Any] = {
        "batch_id": lease.batch_id,
        "task_id": lease.task_id,
        "lease_id": lease.lease_id,
        "slot_id": lease.slot_id,
        "cua_url": lease.cua_url,
        "image_key": args.image_key,
    }

    vm: BakeVm | None = None
    try:
        vm = BakeVm(lease.cua_url)
        node_and_cli_install(vm)
        preinstall_bridges(vm, bridge_root, openai_api_key=os.environ.get("OPENAI_API_KEY"))
        configure_openclaw_gateway_autostart(vm)
        clean_guest_image_state(vm)
        reboot_and_validate(vm)
        report["guest_validation"] = {"gateway_autostart": True}

        if not args.skip_freeze:
            heartbeat.stop()
            report["freeze"] = freeze_overlay(
                remote_host=args.remote_host,
                slot_id=lease.slot_id,
                golden_dir=args.golden_dir,
            )
        else:
            heartbeat.stop()

        if args.report_path:
            write_report(args.report_path, report)
        print(json.dumps(report, indent=2), flush=True)
        return 0
    except Exception as exc:
        heartbeat.stop()
        if vm is not None:
            try:
                report["debug"] = collect_gateway_debug(vm)
            except Exception as debug_exc:  # noqa: BLE001
                report["debug_error"] = str(debug_exc)
        report["error"] = str(exc)
        if args.report_path:
            write_report(args.report_path, report)
        if args.preserve_on_error:
            print(json.dumps(report, indent=2), flush=True)
            raise
        try:
            api.request(
                "POST",
                f"/v1/batches/{lease.batch_id}/cancel",
                json={
                    "reason": "image bake failed",
                    "details": {"error": str(exc)},
                },
            )
        except Exception as cancel_exc:  # noqa: BLE001
            print(f"[bake] warning: failed to cancel batch after error: {cancel_exc}", flush=True)
        raise
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvServerAdminError as exc:
        print(f"[bake] ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
