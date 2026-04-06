/**
 * HTTP client for CUA computer-server.
 *
 * Sends commands via POST /cmd and parses SSE-streamed responses.
 * Mirrors the protocol used by cua-computer-client (Python).
 */

export class CuaClient {
  private serverUrl: string;
  private timeout: number;

  constructor(serverUrl: string = "http://localhost:5000", timeout: number = 30000) {
    this.serverUrl = serverUrl.replace(/\/+$/, "");
    this.timeout = timeout;
  }

  /**
   * Send a command to the CUA server and return the parsed result.
   */
  async sendCommand(command: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    try {
      const resp = await fetch(`${this.serverUrl}/cmd`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command, params }),
        signal: controller.signal,
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new Error(`CUA server HTTP ${resp.status}: ${text}`);
      }

      // Parse SSE response: lines starting with "data: "
      const body = await resp.text();
      let result: Record<string, unknown> | null = null;

      for (const line of body.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            result = JSON.parse(line.slice(6));
          } catch {
            // skip malformed lines
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
  async screenshot(): Promise<{ base64: string; mimeType: string }> {
    const result = await this.sendCommand("screenshot");
    const imageData = result.image_data as string;
    if (!imageData) {
      throw new Error("Screenshot returned no image data");
    }
    return { base64: imageData, mimeType: "image/png" };
  }

  /**
   * Get screen dimensions.
   */
  async getScreenSize(): Promise<{ width: number; height: number }> {
    const result = await this.sendCommand("get_screen_size");
    const size = result.size as { width: number; height: number } | undefined;
    if (size) return size;
    return { width: result.width as number, height: result.height as number };
  }
}
