#!/usr/bin/env node
/**
 * CUA MCP Server — Desktop Action Space over MCP (stdio).
 *
 * Wraps CUA computer-server HTTP API into MCP tools aligned with
 * CUALiteDesktopActionSpace (absolute pixel coordinates).
 *
 * Runs on the VM, consumed by Claude Code / Codex via stdio transport.
 *
 * Usage:
 *   node src/index.js                     # start MCP server (stdio)
 *   node src/index.js --test              # run smoke test against CUA server
 *   CUA_SERVER_URL=http://...:5000 node src/index.js
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { CuaClient } from "./cua-client.js";

// Key normalization — aligned with CUA server's _key_from_string
const KEY_MAP = {
  ARROWUP: "up",
  ARROWDOWN: "down",
  ARROWLEFT: "left",
  ARROWRIGHT: "right",
};

function normalizeKey(key) {
  return KEY_MAP[key] ?? key.toLowerCase();
}

const CUA_URL = process.env.CUA_SERVER_URL || "http://localhost:5000";
const client = new CuaClient(CUA_URL);

/**
 * Take screenshot and return as MCP content blocks.
 */
async function actionWithScreenshot(label) {
  const { base64, mimeType } = await client.screenshot();
  return {
    content: [
      { type: "text", text: label },
      { type: "image", data: base64, mimeType },
    ],
  };
}

// ------------------------------------------------------------------
// --test mode
// ------------------------------------------------------------------
if (process.argv.includes("--test")) {
  console.log(`Testing CUA MCP Server against ${CUA_URL} ...`);
  try {
    const size = await client.getScreenSize();
    console.log(`  Screen size: ${size.width}x${size.height}`);
    const shot = await client.screenshot();
    console.log(`  Screenshot: ${shot.base64.length} chars base64`);
    await client.sendCommand("move_cursor", { x: 100, y: 100 });
    console.log("  move_cursor: OK");
    const pos = await client.sendCommand("get_cursor_position");
    console.log(`  get_cursor_position: ${JSON.stringify(pos.position)}`);
    await client.sendCommand("key_down", { key: "shift" });
    await client.sendCommand("key_up", { key: "shift" });
    console.log("  key_down/key_up: OK");
    await client.sendCommand("scroll_direction", { direction: "down", clicks: 1 });
    console.log("  scroll_direction: OK");
    console.log("All tests passed.");
    process.exit(0);
  } catch (e) {
    console.error(`Test failed: ${e.message}`);
    process.exit(1);
  }
}

// ------------------------------------------------------------------
// MCP Server
// ------------------------------------------------------------------
const server = new McpServer({
  name: "cua-desktop",
  version: "0.2.0",
});

// ================================================================
// cua_screenshot — observe current screen
// ================================================================
server.tool(
  "cua_screenshot",
  "Take a screenshot of the current screen.",
  {},
  async () => {
    return actionWithScreenshot("Screenshot captured");
  }
);

// ================================================================
// cua_click — click at coordinates
// Aligned with cua-lite: supports left/right/middle, clicks 1/2/3
// ================================================================
server.tool(
  "cua_click",
  "Click at pixel coordinates (x, y). Supports left/right/middle button and single/double/triple click.",
  {
    x: z.number().describe("X coordinate"),
    y: z.number().describe("Y coordinate"),
    button: z.enum(["left", "right", "middle"]).optional().describe('Mouse button (default: "left")'),
    clicks: z.union([z.literal(1), z.literal(2), z.literal(3)]).optional().describe("Number of clicks: 1=single, 2=double, 3=triple (default: 1)"),
  },
  async ({ x, y, button, clicks }) => {
    const btn = button ?? "left";
    const n = clicks ?? 1;

    if (n === 2 && btn === "left") {
      // CUA server has native double_click
      await client.sendCommand("double_click", { x, y });
    } else if (btn === "middle") {
      // No native middle_click in server — use mouse_down/up
      await client.sendCommand("move_cursor", { x, y });
      await client.sendCommand("mouse_down", { button: "middle" });
      await client.sendCommand("mouse_up", { button: "middle" });
      if (n >= 2) {
        await client.sendCommand("mouse_down", { button: "middle" });
        await client.sendCommand("mouse_up", { button: "middle" });
      }
      if (n >= 3) {
        await client.sendCommand("mouse_down", { button: "middle" });
        await client.sendCommand("mouse_up", { button: "middle" });
      }
    } else {
      // left or right, single or multi-click
      const cmd = btn === "right" ? "right_click" : "left_click";
      await client.sendCommand(cmd, { x, y });
      for (let i = 1; i < n; i++) {
        await client.sendCommand(cmd, { x, y });
      }
    }
    return actionWithScreenshot(`Clicked (${btn}, ${n}x) at (${x}, ${y})`);
  }
);

// ================================================================
// cua_type — type text
// ================================================================
server.tool(
  "cua_type",
  "Type text at the current cursor position.",
  {
    text: z.string().describe("Text to type"),
  },
  async ({ text }) => {
    await client.sendCommand("type_text", { text });
    const preview = text.length > 50 ? text.slice(0, 50) + "..." : text;
    return actionWithScreenshot(`Typed: "${preview}"`);
  }
);

// ================================================================
// cua_key — press and release keys (aligned with cua-lite key())
// Accepts string ("ctrl+c") or array (["ctrl", "c"])
// ================================================================
server.tool(
  "cua_key",
  'Press and release keys. Examples: "enter", "ctrl+c", ["alt", "tab"].',
  {
    keys: z.union([z.string(), z.array(z.string())]).describe("Keys to press"),
  },
  async ({ keys }) => {
    let keyList;
    if (typeof keys === "string") {
      keyList = keys.replace(/-/g, "+").split("+");
    } else {
      keyList = keys;
    }
    const normalized = keyList.map(normalizeKey);

    if (normalized.length === 1) {
      await client.sendCommand("press_key", { key: normalized[0] });
    } else {
      await client.sendCommand("hotkey", { keys: normalized });
    }
    return actionWithScreenshot(`Pressed: ${normalized.join("+")}`);
  }
);

// ================================================================
// cua_key_down — press key without releasing (aligned with cua-lite)
// ================================================================
server.tool(
  "cua_key_down",
  "Press and hold a key without releasing it.",
  {
    key: z.string().describe("Key to press down (e.g. 'shift', 'ctrl', 'alt')"),
  },
  async ({ key }) => {
    const normalized = normalizeKey(key);
    await client.sendCommand("key_down", { key: normalized });
    return actionWithScreenshot(`Key down: ${normalized}`);
  }
);

// ================================================================
// cua_key_up — release a held key (aligned with cua-lite)
// ================================================================
server.tool(
  "cua_key_up",
  "Release a key that was previously pressed down.",
  {
    key: z.string().describe("Key to release (e.g. 'shift', 'ctrl', 'alt')"),
  },
  async ({ key }) => {
    const normalized = normalizeKey(key);
    await client.sendCommand("key_up", { key: normalized });
    return actionWithScreenshot(`Key up: ${normalized}`);
  }
);

// ================================================================
// cua_hold_key — hold keys for a duration (aligned with cua-lite)
// ================================================================
server.tool(
  "cua_hold_key",
  "Hold keys down for a specified duration, then release.",
  {
    keys: z.union([z.string(), z.array(z.string())]).describe("Keys to hold"),
    duration: z.number().describe("Duration in seconds"),
  },
  async ({ keys, duration }) => {
    let keyList = typeof keys === "string" ? [keys] : keys;
    const normalized = keyList.map(normalizeKey);

    for (const k of normalized) {
      await client.sendCommand("key_down", { key: k });
    }
    await new Promise((resolve) => setTimeout(resolve, duration * 1000));
    for (const k of normalized.reverse()) {
      await client.sendCommand("key_up", { key: k });
    }
    return actionWithScreenshot(`Held ${normalized.join("+")} for ${duration}s`);
  }
);

// ================================================================
// cua_mouse_move — move cursor (aligned with cua-lite mouse_move)
// ================================================================
server.tool(
  "cua_mouse_move",
  "Move the mouse cursor to pixel coordinates (x, y) without clicking.",
  {
    x: z.number().describe("X coordinate"),
    y: z.number().describe("Y coordinate"),
  },
  async ({ x, y }) => {
    await client.sendCommand("move_cursor", { x, y });
    return actionWithScreenshot(`Moved cursor to (${x}, ${y})`);
  }
);

// ================================================================
// cua_mouse_down — press mouse button (aligned with cua-lite)
// ================================================================
server.tool(
  "cua_mouse_down",
  "Press a mouse button without releasing.",
  {
    button: z.enum(["left", "right", "middle"]).optional().describe('Mouse button (default: "left")'),
  },
  async ({ button }) => {
    const btn = button ?? "left";
    await client.sendCommand("mouse_down", { button: btn });
    return actionWithScreenshot(`Mouse down: ${btn}`);
  }
);

// ================================================================
// cua_mouse_up — release mouse button (aligned with cua-lite)
// ================================================================
server.tool(
  "cua_mouse_up",
  "Release a mouse button.",
  {
    button: z.enum(["left", "right", "middle"]).optional().describe('Mouse button (default: "left")'),
  },
  async ({ button }) => {
    const btn = button ?? "left";
    await client.sendCommand("mouse_up", { button: btn });
    return actionWithScreenshot(`Mouse up: ${btn}`);
  }
);

// ================================================================
// cua_drag — drag from start to end (aligned with cua-lite)
// Uses drag_to server command which supports button param
// ================================================================
server.tool(
  "cua_drag",
  "Drag from start coordinates to end coordinates.",
  {
    start_x: z.number().describe("Starting X coordinate"),
    start_y: z.number().describe("Starting Y coordinate"),
    end_x: z.number().describe("Ending X coordinate"),
    end_y: z.number().describe("Ending Y coordinate"),
    button: z.enum(["left", "right", "middle"]).optional().describe('Mouse button (default: "left")'),
  },
  async ({ start_x, start_y, end_x, end_y, button }) => {
    const btn = button ?? "left";
    // Move to start, then use drag_to for the actual drag
    await client.sendCommand("move_cursor", { x: start_x, y: start_y });
    await client.sendCommand("drag_to", { x: end_x, y: end_y, button: btn });
    return actionWithScreenshot(
      `Dragged (${btn}) from (${start_x}, ${start_y}) to (${end_x}, ${end_y})`
    );
  }
);

// ================================================================
// cua_scroll — scroll with direction + amount (aligned with cua-lite)
// Uses scroll_direction server command
// ================================================================
server.tool(
  "cua_scroll",
  "Scroll in a direction by a specified amount.",
  {
    direction: z.enum(["up", "down", "left", "right"]).describe("Scroll direction"),
    amount: z.number().describe("Number of scroll units"),
    x: z.number().optional().describe("X coordinate to scroll at (moves cursor first)"),
    y: z.number().optional().describe("Y coordinate to scroll at (moves cursor first)"),
  },
  async ({ direction, amount, x, y }) => {
    if (x !== undefined && y !== undefined) {
      await client.sendCommand("move_cursor", { x, y });
    }
    await client.sendCommand("scroll_direction", { direction, clicks: amount });
    const pos = x !== undefined ? ` at (${x}, ${y})` : "";
    return actionWithScreenshot(`Scrolled ${direction} ${amount}${pos}`);
  }
);

// ================================================================
// cua_wait — wait then screenshot (seconds, aligned with cua-lite)
// ================================================================
server.tool(
  "cua_wait",
  "Pause for a specified duration in seconds, then return a screenshot.",
  {
    duration: z.number().describe("Time in seconds to wait"),
  },
  async ({ duration }) => {
    await new Promise((resolve) => setTimeout(resolve, duration * 1000));
    return actionWithScreenshot(`Waited ${duration}s`);
  }
);

// ================================================================
// cua_cursor_position — get current cursor position (aligned with cua-lite)
// ================================================================
server.tool(
  "cua_cursor_position",
  "Get the current cursor position.",
  {},
  async () => {
    const result = await client.sendCommand("get_cursor_position");
    const pos = result.position;
    return {
      content: [
        { type: "text", text: `Cursor at (${pos.x}, ${pos.y})` },
      ],
    };
  }
);

// ------------------------------------------------------------------
// Start stdio transport
// ------------------------------------------------------------------
const transport = new StdioServerTransport();
await server.connect(transport);
