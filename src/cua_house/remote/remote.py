"""Shared remote execution primitives via CUA HTTP API.

All external agent deployers use these to run commands, upload/download files
on the remote VM. No SSH — everything goes through CUA server port 5000.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import requests

from .base import RemoteVMConfig

logger = logging.getLogger(__name__)

REMOTE_NODE_VERSION = "24.12.0"
REMOTE_NODE_DIR = rf"C:\Users\User\node-v{REMOTE_NODE_VERSION}-win-x64"
REMOTE_NODE_EXE = rf"{REMOTE_NODE_DIR}\node.exe"
REMOTE_NPM_CMD = rf"{REMOTE_NODE_DIR}\npm.cmd"
REMOTE_NPM_BIN = r"C:\Users\User\AppData\Roaming\npm"
REMOTE_NODE_ZIP = rf"C:\Users\User\node-v{REMOTE_NODE_VERSION}-win-x64.zip"
REMOTE_MCP_SERVER_DIR = r"C:\Users\User\cua_mcp_server"


def _cua_url(vm_config: RemoteVMConfig) -> str:
    return vm_config.server_url.rstrip("/")


def _read_first_sse_event(resp: requests.Response) -> dict[str, Any] | None:
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        if raw_line.startswith("data: "):
            return json.loads(raw_line[6:])
    return None


def _run_remote(vm_config: RemoteVMConfig, command: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Run a command on the remote VM via CUA server run_command API."""
    wrapped = (
        "set PATH=%PATH%;"
        f"{REMOTE_NODE_DIR};"
        f"{REMOTE_NPM_BIN};"
        "C:\\Users\\User\\AppData\\Roaming\\npm;"
        "C:\\Program Files\\nodejs;"
        "C:\\Program Files (x86)\\nodejs"
        f" && {command}"
    )
    payload = {"command": "run_command", "params": {"command": wrapped}}

    try:
        with requests.post(
            f"{_cua_url(vm_config)}/cmd",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            stream=True,
        ) as resp:
            data = _read_first_sse_event(resp)
        if data is not None:
            return subprocess.CompletedProcess(
                args=command,
                returncode=data.get("return_code", data.get("returncode", 0)),
                stdout=data.get("stdout", data.get("output", "")),
                stderr=data.get("stderr", ""),
            )
    except requests.Timeout:
        return subprocess.CompletedProcess(args=command, returncode=-1, stdout="", stderr="timeout")
    except Exception as e:
        return subprocess.CompletedProcess(args=command, returncode=-1, stdout="", stderr=str(e))

    return subprocess.CompletedProcess(args=command, returncode=-1, stdout="", stderr="no response")


def _upload_file(vm_config: RemoteVMConfig, remote_path: str, content: str, timeout: float = 15) -> bool:
    """Upload a text file to the remote VM via CUA write_text API."""
    url = f"{_cua_url(vm_config)}/cmd"
    try:
        with requests.post(
            url,
            json={"command": "write_text", "params": {"path": remote_path, "content": content}},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            stream=True,
        ) as resp:
            data = _read_first_sse_event(resp)
        if data is not None:
            return data.get("success", False)
    except Exception as e:
        logger.debug("write_text failed for %s: %s", remote_path, e)
    return False


def _download_file(vm_config: RemoteVMConfig, remote_path: str, local_path: str, timeout: float = 60) -> bool:
    """Download a text file from remote VM via CUA read_text API.

    Falls back to run_command + Get-Content if read_text fails.
    """
    url = f"{_cua_url(vm_config)}/cmd"

    # Try read_text first
    try:
        with requests.post(
            url,
            json={"command": "read_text", "params": {"path": remote_path}},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            stream=True,
        ) as resp:
            data = _read_first_sse_event(resp)
        if data and data.get("success"):
            content = data.get("content", "")
            if content:
                Path(local_path).write_text(content, encoding="utf-8")
                return True
    except Exception as e:
        logger.debug("read_text failed for %s: %s", remote_path, e)

    # Fallback: Get-Content via run_command
    try:
        escaped_path = remote_path.replace("'", "''")
        with requests.post(
            url,
            json={
                "command": "run_command",
                "params": {"command": f"powershell -Command \"Get-Content -Path '{escaped_path}' -Raw -ErrorAction Stop\""},
            },
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            stream=True,
        ) as resp:
            data = _read_first_sse_event(resp)
        if data:
            stdout = data.get("stdout", "")
            if stdout and data.get("return_code", 1) == 0:
                Path(local_path).write_text(stdout, encoding="utf-8")
                return True
    except Exception as e:
        logger.debug("Get-Content fallback failed for %s: %s", remote_path, e)

    return False


def _upload_directory(vm_config: RemoteVMConfig, local_dir: Path, remote_dir: str, timeout: float = 15) -> None:
    """Upload a directory tree to the remote VM via CUA write_text API."""
    _run_remote(vm_config,
                f'powershell -Command "New-Item -ItemType Directory -Force -Path \'{remote_dir}\' | Out-Null"',
                timeout=10)

    for local_file in local_dir.rglob("*"):
        if not local_file.is_file():
            continue
        # Skip node_modules, .git, etc.
        rel = local_file.relative_to(local_dir)
        if any(part.startswith(".") or part == "node_modules" for part in rel.parts):
            continue
        remote_path = f"{remote_dir}\\{str(rel).replace('/', chr(92))}"
        # Ensure parent directory exists
        parent = "\\".join(remote_path.split("\\")[:-1])
        if parent != remote_dir:
            _run_remote(vm_config,
                        f'powershell -Command "New-Item -ItemType Directory -Force -Path \'{parent}\' | Out-Null"',
                        timeout=10)
        content = local_file.read_text(encoding="utf-8")
        _upload_file(vm_config, remote_path, content, timeout=timeout)

    logger.info("Uploaded %s → %s", local_dir.name, remote_dir)


# ======================================================================
# Shared deploy helpers (install Node.js + npm CLIs on bare VM)
# ======================================================================


def _install_node_if_needed(vm_config: RemoteVMConfig) -> None:
    """Download and extract Node.js on the VM if not already present."""
    check = _run_remote(vm_config, f'"{REMOTE_NODE_EXE}" --version', timeout=15)
    if check.returncode == 0 and (check.stdout or "").strip().startswith("v"):
        logger.info("Node.js already installed: %s", check.stdout.strip())
        return

    logger.info("Installing Node.js %s on VM...", REMOTE_NODE_VERSION)
    node_zip_url = f"https://nodejs.org/dist/v{REMOTE_NODE_VERSION}/node-v{REMOTE_NODE_VERSION}-win-x64.zip"
    _run_remote(
        vm_config,
        f'powershell -Command "'
        f"$zip = '{REMOTE_NODE_ZIP}'; "
        f"if (Test-Path $zip) {{ Remove-Item -Force $zip }}; "
        f"$curl = Get-Command curl.exe -ErrorAction SilentlyContinue; "
        f"if ($curl) {{ "
        f"  & $curl.Source -L --retry 8 --retry-delay 5 --retry-all-errors "
        f"    --connect-timeout 30 --max-time 900 -o $zip '{node_zip_url}' "
        f"}} else {{ "
        f"  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
        f"  Invoke-WebRequest -Uri '{node_zip_url}' -OutFile $zip -UseBasicParsing "
        f"}}; "
        f"if (-not (Test-Path $zip)) {{ throw 'Node zip download failed' }}; "
        f"$s = (Get-Item $zip).Length; "
        f'if ($s -lt 20000000) {{ throw "Node zip too small ($s bytes)" }}"',
        timeout=960,
    )
    _run_remote(
        vm_config,
        f'powershell -Command "'
        f"if (Test-Path '{REMOTE_NODE_DIR}') {{ Remove-Item -Recurse -Force '{REMOTE_NODE_DIR}' }}; "
        f"Expand-Archive -Path '{REMOTE_NODE_ZIP}' -DestinationPath 'C:\\Users\\User' -Force\"",
        timeout=300,
    )
    _run_remote(
        vm_config,
        f'powershell -Command "New-Item -ItemType Directory -Force -Path \'{REMOTE_NPM_BIN}\' | Out-Null; '
        f"& '{REMOTE_NPM_CMD}' config set prefix '{REMOTE_NPM_BIN}'\"",
        timeout=30,
    )
    logger.info("Node.js %s installed", REMOTE_NODE_VERSION)


def _npm_install_global(vm_config: RemoteVMConfig, package: str, *, timeout: float = 1800) -> None:
    """Install a global npm package on the VM."""
    logger.info("npm install -g %s ...", package)
    _run_remote(
        vm_config,
        f"set PATH=%PATH%;{REMOTE_NODE_DIR};{REMOTE_NPM_BIN} && "
        f'"{REMOTE_NPM_CMD}" install -g {package}',
        timeout=timeout,
    )


def _upload_mcp_server(vm_config: RemoteVMConfig) -> None:
    """Upload CUA MCP Server bridge to the VM and install its dependencies."""
    bridge_root = Path(__file__).resolve().parent / "bridges" / "cua_mcp_server"
    if not bridge_root.exists():
        raise RuntimeError(f"CUA MCP Server bridge not found at {bridge_root}")
    _upload_directory(vm_config, bridge_root, REMOTE_MCP_SERVER_DIR)
    _run_remote(
        vm_config,
        f'powershell -Command "Set-Location \'{REMOTE_MCP_SERVER_DIR}\'; '
        f"& '{REMOTE_NPM_CMD}' install --production\"",
        timeout=600,
    )
