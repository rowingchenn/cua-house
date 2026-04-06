# bridges

Non-Python bridge assets for agent integration. These are installed into Windows VMs during the image bake process (`admin/bake_image.py`).

## cua_mcp_server

Node.js MCP (Model Context Protocol) server that exposes computer-use tools to Claude Code and Codex. Installed at `C:\Users\User\cua_mcp_server` inside the guest.

## openclaw_plugin

OpenClaw CUA plugin that provides computer-use capabilities through the OpenClaw agent framework. Installed at `C:\Users\User\openclaw-cua-plugin` inside the guest.

## How they are used

During image bake:

1. `bake_image.py` uploads these directories into a temporary VM via the CUA `/cmd` endpoint.
2. `cua_mcp_server` dependencies are installed with `npm install --production`.
3. The OpenClaw plugin is registered with `openclaw plugins install`.
4. Default agent configs (Claude MCP config, Codex config) are written to point at these bridges.
5. The VM state is frozen into the golden image.

These assets are not used directly by cua-house-server at runtime. They are pre-baked into VM images so that agents have the necessary tooling available when they connect.
