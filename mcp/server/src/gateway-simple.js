/**
 * ABUTRON Own Gateway — minimal JS fallback (no TS build needed)
 * Run: node mcp/server/src/gateway-simple.js
 * Requires: npm i fastify @fastify/cors ; DCM installed at /opt/abutron-mcp/desktop-commander
 */
import { spawn } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import Fastify from "fastify";
import cors from "@fastify/cors";

const cfgPath = process.env.ABUTRON_MCP_CONFIG || new URL("../../config/abutron-mcp.config.json", import.meta.url).pathname;
let cfg = {};
try { cfg = JSON.parse(readFileSync(cfgPath, "utf8")); } catch {}
const NODE_BIN = cfg.node?.pinned?.isolatedPath ? `${cfg.node.pinned.isolatedPath}/bin/node` : process.execPath;
const DCM_ENTRY = cfg.server?.entry || "/opt/abutron-mcp/desktop-commander/dist/index.js";
const HOST = process.env.ABUTRON_GATEWAY_HOST || "127.0.0.1";
const PORT = Number(process.env.ABUTRON_GATEWAY_PORT || 3467);
const API_KEY = process.env.ABUTRON_MCP_API_KEY || "";

const app = Fastify({ logger: true });
await app.register(cors, { origin: true });
app.get("/healthz", async () => ({ status: "ok", gateway: "abutron-own-v1", node: process.version }));
app.get("/readyz", async () => ({ nodeBin: NODE_BIN, nodeOk: existsSync(NODE_BIN), dcmEntry: DCM_ENTRY, dcmOk: existsSync(DCM_ENTRY) }));
function requireAuth(req, reply, done) {
  if (!API_KEY) return done();
  const h = req.headers.authorization || "";
  if (h !== `Bearer ${API_KEY}`) { reply.code(401).send({ error: "UNAUTHORIZED_ABUTRON_MCP_KEY_REQUIRED" }); return; }
  done();
}
app.get("/mcp/tools", { preHandler: requireAuth }, async () => {
  // quick smoke
  return { gateway: "abutron-own-v1", hint: "POST JSON-RPC to /mcp/messages" };
});
app.get("/mcp/sse", { preHandler: requireAuth }, async (req, reply) => {
  reply.raw.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive", "X-ABUTRON-GATEWAY": "own-v1-no-openai" });
  reply.raw.write(`event: open\ndata: ${JSON.stringify({ gateway: "abutron-own-v1" })}\n\n`);
  const iv = setInterval(() => reply.raw.write(`: keepalive\n\n`), 15000);
  req.raw.on("close", () => clearInterval(iv));
});
app.listen({ host: HOST, port: PORT });
