var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// ../../mnt/datablaze2/weichenzhang/agenthle-overall/agenthle/agenthle/orchestration/external/bridges/openclaw_plugin/index.ts
var index_exports = {};
__export(index_exports, {
  default: () => register
});
module.exports = __toCommonJS(index_exports);

// ../../mnt/datablaze2/weichenzhang/agenthle-overall/agenthle/agenthle/orchestration/external/bridges/openclaw_plugin/src/cua-client.ts
var CuaClient = class {
  serverUrl;
  timeout;
  constructor(serverUrl = "http://localhost:5000", timeout = 3e4) {
    this.serverUrl = serverUrl.replace(/\/+$/, "");
    this.timeout = timeout;
  }
  /**
   * Send a command to the CUA server and return the parsed result.
   */
  async sendCommand(command, params = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const resp = await fetch(`${this.serverUrl}/cmd`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command, params }),
        signal: controller.signal
      });
      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new Error(`CUA server HTTP ${resp.status}: ${text}`);
      }
      const body = await resp.text();
      let result = null;
      for (const line of body.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            result = JSON.parse(line.slice(6));
          } catch {
          }
        }
      }
      if (!result) {
        throw new Error(`No valid response for command '${command}'`);
      }
      if (result.success === false) {
        throw new Error(`Command '${command}' failed: ${result.error ?? "unknown error"}`);
      }
      return result;
    } finally {
      clearTimeout(timer);
    }
  }
  /**
   * Take a screenshot and return raw base64 image data.
   */
  async screenshot() {
    const result = await this.sendCommand("screenshot");
    const imageData = result.image_data;
    if (!imageData) {
      throw new Error("Screenshot returned no image data");
    }
    return { base64: imageData, mimeType: "image/png" };
  }
  /**
   * Get screen dimensions.
   */
  async getScreenSize() {
    const result = await this.sendCommand("get_screen_size");
    const size = result.size;
    if (size) return size;
    return { width: result.width, height: result.height };
  }
};

// ../../mnt/datablaze2/weichenzhang/agenthle-overall/agenthle/agenthle/orchestration/external/bridges/openclaw_plugin/src/tools.ts
var KEY_MAP = {
  ARROWUP: "up",
  ARROWDOWN: "down",
  ARROWLEFT: "left",
  ARROWRIGHT: "right"
};
function normalizeKey(key) {
  return KEY_MAP[key] ?? key.toLowerCase();
}
async function screenshotResult(client, label) {
  const { base64, mimeType } = await client.screenshot();
  return {
    content: [
      { type: "text", text: label },
      { type: "image", data: base64, mimeType }
    ]
  };
}
function createCuaTools(client) {
  return [
    // ================================================================
    // cua_screenshot
    // ================================================================
    {
      name: "cua_screenshot",
      description: "Take a screenshot of the current screen.",
      parameters: {
        type: "object",
        properties: {},
        additionalProperties: false
      },
      async execute() {
        return screenshotResult(client, "Screenshot captured");
      }
    },
    // ================================================================
    // cua_click — left/right/middle, single/double/triple
    // ================================================================
    {
      name: "cua_click",
      description: "Click at pixel coordinates (x, y). Supports left/right/middle button and single/double/triple click.",
      parameters: {
        type: "object",
        properties: {
          x: { type: "number", description: "X coordinate" },
          y: { type: "number", description: "Y coordinate" },
          button: {
            type: "string",
            enum: ["left", "right", "middle"],
            description: 'Mouse button (default: "left")'
          },
          clicks: {
            type: "number",
            enum: [1, 2, 3],
            description: "Number of clicks: 1=single, 2=double, 3=triple (default: 1)"
          }
        },
        required: ["x", "y"],
        additionalProperties: false
      },
      async execute(_id, params) {
        const btn = params.button ?? "left";
        const n = params.clicks ?? 1;
        if (n === 2 && btn === "left") {
          await client.sendCommand("double_click", { x: params.x, y: params.y });
        } else if (btn === "middle") {
          await client.sendCommand("move_cursor", { x: params.x, y: params.y });
          for (let i = 0; i < n; i++) {
            await client.sendCommand("mouse_down", { button: "middle" });
            await client.sendCommand("mouse_up", { button: "middle" });
          }
        } else {
          const cmd = btn === "right" ? "right_click" : "left_click";
          for (let i = 0; i < n; i++) {
            await client.sendCommand(cmd, { x: params.x, y: params.y });
          }
        }
        return screenshotResult(client, `Clicked (${btn}, ${n}x) at (${params.x}, ${params.y})`);
      }
    },
    // ================================================================
    // cua_type
    // ================================================================
    {
      name: "cua_type",
      description: "Type text at the current cursor position.",
      parameters: {
        type: "object",
        properties: {
          text: { type: "string", description: "Text to type" }
        },
        required: ["text"],
        additionalProperties: false
      },
      async execute(_id, params) {
        await client.sendCommand("type_text", { text: params.text });
        const preview = params.text.length > 50 ? params.text.slice(0, 50) + "..." : params.text;
        return screenshotResult(client, `Typed: "${preview}"`);
      }
    },
    // ================================================================
    // cua_key — press and release keys
    // ================================================================
    {
      name: "cua_key",
      description: 'Press and release keys. Examples: "enter", "ctrl+c", ["alt", "tab"].',
      parameters: {
        type: "object",
        properties: {
          keys: {
            oneOf: [
              { type: "array", items: { type: "string" }, description: "List of keys" },
              { type: "string", description: 'Key combination string, e.g. "ctrl+c"' }
            ],
            description: "Keys to press"
          }
        },
        required: ["keys"],
        additionalProperties: false
      },
      async execute(_id, params) {
        let keys;
        if (typeof params.keys === "string") {
          keys = params.keys.replace(/-/g, "+").split("+");
        } else {
          keys = params.keys;
        }
        const normalized = keys.map(normalizeKey);
        if (normalized.length === 1) {
          await client.sendCommand("press_key", { key: normalized[0] });
        } else {
          await client.sendCommand("hotkey", { keys: normalized });
        }
        return screenshotResult(client, `Pressed: ${normalized.join("+")}`);
      }
    },
    // ================================================================
    // cua_key_down — press key without releasing
    // ================================================================
    {
      name: "cua_key_down",
      description: "Press and hold a key without releasing it.",
      parameters: {
        type: "object",
        properties: {
          key: { type: "string", description: "Key to press down (e.g. 'shift', 'ctrl')" }
        },
        required: ["key"],
        additionalProperties: false
      },
      async execute(_id, params) {
        const normalized = normalizeKey(params.key);
        await client.sendCommand("key_down", { key: normalized });
        return screenshotResult(client, `Key down: ${normalized}`);
      }
    },
    // ================================================================
    // cua_key_up — release a held key
    // ================================================================
    {
      name: "cua_key_up",
      description: "Release a key that was previously pressed down.",
      parameters: {
        type: "object",
        properties: {
          key: { type: "string", description: "Key to release (e.g. 'shift', 'ctrl')" }
        },
        required: ["key"],
        additionalProperties: false
      },
      async execute(_id, params) {
        const normalized = normalizeKey(params.key);
        await client.sendCommand("key_up", { key: normalized });
        return screenshotResult(client, `Key up: ${normalized}`);
      }
    },
    // ================================================================
    // cua_hold_key — hold keys for a duration
    // ================================================================
    {
      name: "cua_hold_key",
      description: "Hold keys down for a specified duration, then release.",
      parameters: {
        type: "object",
        properties: {
          keys: {
            oneOf: [
              { type: "array", items: { type: "string" } },
              { type: "string" }
            ],
            description: "Keys to hold"
          },
          duration: { type: "number", description: "Duration in seconds" }
        },
        required: ["keys", "duration"],
        additionalProperties: false
      },
      async execute(_id, params) {
        const keyList = typeof params.keys === "string" ? [params.keys] : params.keys;
        const normalized = keyList.map(normalizeKey);
        for (const k of normalized) {
          await client.sendCommand("key_down", { key: k });
        }
        await new Promise((r) => setTimeout(r, params.duration * 1e3));
        for (const k of [...normalized].reverse()) {
          await client.sendCommand("key_up", { key: k });
        }
        return screenshotResult(client, `Held ${normalized.join("+")} for ${params.duration}s`);
      }
    },
    // ================================================================
    // cua_mouse_move
    // ================================================================
    {
      name: "cua_mouse_move",
      description: "Move the mouse cursor to pixel coordinates (x, y) without clicking.",
      parameters: {
        type: "object",
        properties: {
          x: { type: "number", description: "X coordinate" },
          y: { type: "number", description: "Y coordinate" }
        },
        required: ["x", "y"],
        additionalProperties: false
      },
      async execute(_id, params) {
        await client.sendCommand("move_cursor", { x: params.x, y: params.y });
        return screenshotResult(client, `Moved cursor to (${params.x}, ${params.y})`);
      }
    },
    // ================================================================
    // cua_mouse_down — press mouse button without releasing
    // ================================================================
    {
      name: "cua_mouse_down",
      description: "Press a mouse button without releasing.",
      parameters: {
        type: "object",
        properties: {
          button: {
            type: "string",
            enum: ["left", "right", "middle"],
            description: 'Mouse button (default: "left")'
          }
        },
        additionalProperties: false
      },
      async execute(_id, params) {
        const btn = params.button ?? "left";
        await client.sendCommand("mouse_down", { button: btn });
        return screenshotResult(client, `Mouse down: ${btn}`);
      }
    },
    // ================================================================
    // cua_mouse_up — release mouse button
    // ================================================================
    {
      name: "cua_mouse_up",
      description: "Release a mouse button.",
      parameters: {
        type: "object",
        properties: {
          button: {
            type: "string",
            enum: ["left", "right", "middle"],
            description: 'Mouse button (default: "left")'
          }
        },
        additionalProperties: false
      },
      async execute(_id, params) {
        const btn = params.button ?? "left";
        await client.sendCommand("mouse_up", { button: btn });
        return screenshotResult(client, `Mouse up: ${btn}`);
      }
    },
    // ================================================================
    // cua_drag — drag with button support
    // ================================================================
    {
      name: "cua_drag",
      description: "Drag from start coordinates to end coordinates.",
      parameters: {
        type: "object",
        properties: {
          start_x: { type: "number", description: "Starting X coordinate" },
          start_y: { type: "number", description: "Starting Y coordinate" },
          end_x: { type: "number", description: "Ending X coordinate" },
          end_y: { type: "number", description: "Ending Y coordinate" },
          button: {
            type: "string",
            enum: ["left", "right", "middle"],
            description: 'Mouse button (default: "left")'
          }
        },
        required: ["start_x", "start_y", "end_x", "end_y"],
        additionalProperties: false
      },
      async execute(_id, params) {
        const btn = params.button ?? "left";
        await client.sendCommand("move_cursor", { x: params.start_x, y: params.start_y });
        await client.sendCommand("drag_to", { x: params.end_x, y: params.end_y, button: btn });
        return screenshotResult(
          client,
          `Dragged (${btn}) from (${params.start_x}, ${params.start_y}) to (${params.end_x}, ${params.end_y})`
        );
      }
    },
    // ================================================================
    // cua_scroll — direction + amount (aligned with cua-lite)
    // ================================================================
    {
      name: "cua_scroll",
      description: "Scroll in a direction by a specified amount.",
      parameters: {
        type: "object",
        properties: {
          direction: {
            type: "string",
            enum: ["up", "down", "left", "right"],
            description: "Scroll direction"
          },
          amount: { type: "number", description: "Number of scroll units" },
          x: { type: "number", description: "X coordinate to scroll at (optional)" },
          y: { type: "number", description: "Y coordinate to scroll at (optional)" }
        },
        required: ["direction", "amount"],
        additionalProperties: false
      },
      async execute(_id, params) {
        if (params.x !== void 0 && params.y !== void 0) {
          await client.sendCommand("move_cursor", { x: params.x, y: params.y });
        }
        await client.sendCommand("scroll_direction", {
          direction: params.direction,
          clicks: params.amount
        });
        const pos = params.x !== void 0 ? ` at (${params.x}, ${params.y})` : "";
        return screenshotResult(
          client,
          `Scrolled ${params.direction} ${params.amount}${pos}`
        );
      }
    },
    // ================================================================
    // cua_wait — wait in seconds (aligned with cua-lite)
    // ================================================================
    {
      name: "cua_wait",
      description: "Pause for a specified duration in seconds, then return a screenshot.",
      parameters: {
        type: "object",
        properties: {
          duration: { type: "number", description: "Time in seconds to wait" }
        },
        required: ["duration"],
        additionalProperties: false
      },
      async execute(_id, params) {
        await new Promise((resolve) => setTimeout(resolve, params.duration * 1e3));
        return screenshotResult(client, `Waited ${params.duration}s`);
      }
    },
    // ================================================================
    // cua_cursor_position — get current cursor position
    // ================================================================
    {
      name: "cua_cursor_position",
      description: "Get the current cursor position.",
      parameters: {
        type: "object",
        properties: {},
        additionalProperties: false
      },
      async execute() {
        const result = await client.sendCommand("get_cursor_position");
        const pos = result.position;
        return {
          content: [{ type: "text", text: `Cursor at (${pos.x}, ${pos.y})` }]
        };
      }
    }
  ];
}

// ../../mnt/datablaze2/weichenzhang/agenthle-overall/agenthle/agenthle/orchestration/external/bridges/openclaw_plugin/index.ts
function register(api) {
  const cfg = api.pluginConfig;
  const serverUrl = cfg?.serverUrl ?? "http://localhost:5000";
  const client = new CuaClient(serverUrl);
  const tools = createCuaTools(client);
  for (const tool of tools) {
    api.registerTool(tool);
  }
}
