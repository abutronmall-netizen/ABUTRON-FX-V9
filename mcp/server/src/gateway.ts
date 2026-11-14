/**
 * ABUTRON Own MCP Gateway V1 — No OpenAI
 * Wraps @wonderwhy-er/desktop-commander stdio under ABUTRON security + exposes HTTP/SSE for remote clients.
 * Based on https://desktopcommander.app/mcp/#installation but decoupled from OpenAI tunnel-client.
 */
import { spawn } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import Fastify from "fastify";
import cors from "@fastify/cors";

// --- ABUTRON config (no OpenAI) ---
const CONFIG_PATH = process.env.ABUTRON_MCP_CONFIG || new URL("../../config/abutron-mcp.config.json", import.meta.url).pathname;
const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
const NODE_BIN_CANDIDATE = cfg.node?.pinned?.isolatedPath ? `${cfg.node.pinned.isolatedPath}/bin/node` : process.execPath;
const NODE_BIN = existsSync(NODE_BIN_CANDIDATE) ? NODE_BIN_CANDIDATE : process.execPath;
const DCM_ENTRY = cfg.server?.entry || "/opt/abutron-mcp/desktop-commander/dist/index.js";
const LISTEN_HOST = process.env.ABUTRON_GATEWAY_HOST || "127.0.0.1";
const LISTEN_PORT = Number(process.env.ABUTRON_GATEWAY_PORT || 3467);
const API_KEY = process.env.ABUTRON_MCP_API_KEY || ""; // from /etc/abutron-mcp/gateway.env (0600)

// --- Fastify own gateway (replaces OpenAI tunnel-client) ---
const app = Fastify({ logger: true });
await app.register(cors, { origin: true });

// Health probes (no auth — like tunnel-client /healthz, but local-only)
app.get("/healthz", async () => ({ status: "ok", gateway: "abutron-own-v1", upstream: "desktop-commander", node: process.version }));
app.get("/readyz", async () => {
  const nodeOk = existsSync(NODE_BIN);
  const dcmOk = existsSync(DCM_ENTRY);
  return { nodeBin: NODE_BIN, nodeOk, dcmEntry: DCM_ENTRY, dcmOk, ready: nodeOk && dcmOk };
});

// Simple auth for MCP endpoints — own API key, not OpenAI CONTROL_PLANE_API_KEY
function requireAuth(req: any, reply: any, done: any) {
  if (!API_KEY) return done(); // dev mode: allow if no key set
  const auth = req.headers.authorization || "";
  if (auth !== `Bearer ${API_KEY}`) {
    reply.code(401).send({ error: "UNAUTHORIZED_ABUTRON_MCP_KEY_REQUIRED" });
    return;
  }
  done();
}

// MCP smoke helper — proves stdio initialize + tools/list without invoking tools (mirrors abutron-ops 42483971911)
async function mcpSmoke(): Promise<{ init: boolean; tools: boolean; count: number }> {
  return new Promise((resolve, reject) => {
    const payload = [
      JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "abutron-own-smoke", version: "1.0" } } }),
      JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
      JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} })
    ].join("\n") + "\n";

    const child = spawn(NODE_BIN, [DCM_ENTRY, "--no-onboarding"], {
      env: { ...process.env, HOME: "/tmp/abutron-mcp-smoke", XDG_CONFIG_HOME: "/tmp/abutron-mcp-smoke", PATH: `${NODE_BIN.replace(/\/bin\/node$/, "/bin")}:/usr/bin:/bin` },
      stdio: ["pipe", "pipe", "pipe"]
    });

    let out = "";
    let err = "";
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.stdin.write(payload);
    child.stdin.end();
    const to = setTimeout(() => {
      child.kill();
      reject(new Error("MCP_SMOKE_TIMEOUT"));
    }, 8000);
    child.on("close", (code) => {
      clearTimeout(to);
      try {
        const lines = out.split("\n").filter(Boolean);
        const decoded: Record<number, any> = {};
        for (const line of lines) {
          try {
            const v = JSON.parse(line);
            if (typeof v.id === "number" && (v.id === 1 || v.id === 2)) decoded[v.id] = v;
          } catch {}
        }
        const initOk = typeof decoded[1]?.result?.protocolVersion === "string";
        const tools = decoded[2]?.result;
        const toolsOk = typeof tools === "object" && Array.isArray(tools.tools);
        const count = toolsOk ? tools.tools.length : 0;
        if (!initOk) throw new Error("MCP_INIT_FAILED: " + err.slice(0, 500));
        if (!toolsOk) throw new Error("MCP_TOOLS_LIST_FAILED: " + err.slice(0, 500));
        resolve({ init: initOk, tools: toolsOk, count });
      } catch (e) {
        reject(e);
      }
    });
    child.on("error", reject);
  });
}

app.get("/mcp/tools", { preHandler: requireAuth }, async () => {
  const r = await mcpSmoke();
  return { gateway: "abutron-own-v1", upstream: "@wonderwhy-er/desktop-commander", mcpInit: r.init ? "PASS" : "FAIL", mcpToolsList: r.tools ? "PASS" : "FAIL", mcpToolsCount: r.count, noToolInvoked: true };
});

// SSE endpoint for MCP clients (MCP SDK SSE transport — replaces OpenAI tunnel SSE)
// Minimal own impl: clients POST JSON-RPC to /mcp/messages, GET /mcp/sse for streamed responses.
// For brevity, this starter bridges to stdio child per request. Production should keep persistent child + session map.
app.get("/mcp/sse", { preHandler: requireAuth }, async (req, reply) => {
  // SSE headers
  reply.raw.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-ABUTRON-GATEWAY": "own-v1-no-openai"
  });
  reply.raw.write(`event: open\ndata: ${JSON.stringify({ gateway: "abutron-own-v1", status: "ready", hint: "POST JSON-RPC to /mcp/messages with same Bearer key" })}\n\n`);
  // keepalive
  const iv = setInterval(() => reply.raw.write(`: keepalive ${Date.now()}\n\n`), 15000);
  req.raw.on("close", () => clearInterval(iv));
});

app.post("/mcp/messages", { preHandler: requireAuth }, async (req, reply) => {
  // Proxy single JSON-RPC message to stdio child and return response
  const body: any = req.body;
  if (!body || typeof body.method !== "string") {
    return reply.code(400).send({ error: "INVALID_MCP_MESSAGE" });
  }
  // Block dangerous ABUTRON ops (own policy vs legacy blockedCommands)
  const blocked = cfg.security?.blockedCommands || [];
  const asStr = JSON.stringify(body).toLowerCase();
  for (const b of blocked) {
    if (asStr.includes(String(b).toLowerCase())) {
      return reply.code(403).send({ error: "BLOCKED_BY_ABUTRON_POLICY", blocked: b });
    }
  }
  // Spawn ephemeral stdio for this message (simple; persistent is next iter)
  // For tools/call with live-trading risk, LIVE_ALWAYS_OFF guard here could be extended.
  return new Promise((resolve) => {
    const child = spawn(NODE_BIN, [DCM_ENTRY, "--no-onboarding"], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let done = false;
    const finish = (payload: any) => {
      if (done) return;
      done = true;
      try { child.kill(); } catch {}
      resolve(reply.send(payload));
    };
    child.stdout.on("data", (d) => {
      out += d.toString();
      const lines = out.split("\n");
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const v = JSON.parse(line);
          if (v.id === body.id || (!body.id && v.method)) {
            finish(v);
            return;
          }
        } catch {}
      }
    });
    child.stderr.on("data", () => {});
    child.on("close", () => {
      if (!done) finish({ jsonrpc: "2.0", id: body.id ?? null, error: { code: -32603, message: "ABUTRON_MCP_NO_RESPONSE", data: out.slice(0, 2000) } });
    });
    child.on("error", (e) => finish({ jsonrpc: "2.0", id: body.id ?? null, error: { code: -32603, message: String(e) } }));
    child.stdin.write(JSON.stringify(body) + "\n");
    if (body.method === "initialize") {
      setTimeout(() => {
        try { child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n"); } catch {}
      }, 50);
    }
    // safety timeout
    setTimeout(() => finish({ jsonrpc: "2.0", id: body.id ?? null, error: { code: -32000, message: "ABUTRON_MCP_TIMEOUT" } }), 5000);
  });
});

app.listen({ host: LISTEN_HOST, port: LISTEN_PORT }, (err) => {
  if (err) {
    app.log.error(err);
    process.exit(1);
  }
  console.log(`ABUTRON Own MCP Gateway V1 listening on http://${LISTEN_HOST}:${LISTEN_PORT} — no OpenAI`);
  console.log(`Health: http://${LISTEN_HOST}:${LISTEN_PORT}/healthz | Tools: http://${LISTEN_HOST}:${LISTEN_PORT}/mcp/tools (Bearer ${API_KEY ? "***" : "none-dev"})`);
});
