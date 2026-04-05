/**
 * OpenClaw CUA Plugin — Computer-Use-Preview Action Space.
 *
 * Registers native agent tools that call the local CUA computer-server
 * (default http://localhost:5000) and return screenshots as image blocks
 * for VLM consumption.
 */
import { CuaClient } from "./src/cua-client.js";
import { createCuaTools } from "./src/tools.js";
export default function register(api) {
    const cfg = api.pluginConfig;
    const serverUrl = cfg?.serverUrl ?? "http://localhost:5000";
    const client = new CuaClient(serverUrl);
    const tools = createCuaTools(client);
    for (const tool of tools) {
        api.registerTool(tool);
    }
}
