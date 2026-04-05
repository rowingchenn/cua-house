/**
 * OpenClaw CUA Plugin — Computer-Use-Preview Action Space.
 *
 * Registers native agent tools that call the local CUA computer-server
 * (default http://localhost:5000) and return screenshots as image blocks
 * for VLM consumption.
 */

import { CuaClient } from "./src/cua-client.ts";
import { createCuaTools } from "./src/tools.ts";

type PluginApi = {
  registerTool: (tool: unknown) => void;
  pluginConfig?: Record<string, unknown>;
};

export default function register(api: PluginApi) {
  const cfg = api.pluginConfig as { serverUrl?: string } | undefined;
  const serverUrl = cfg?.serverUrl ?? "http://localhost:5000";

  const client = new CuaClient(serverUrl);
  const tools = createCuaTools(client);

  for (const tool of tools) {
    api.registerTool(tool);
  }
}
