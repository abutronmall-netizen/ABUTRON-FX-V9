import { spawn } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
const node = "/opt/abutron-mcp/node/bin/node";
const server = "/opt/abutron-mcp/desktop-commander/dist/index.js";
// arena fallback: use current node
const nodeBin = process.execPath;
const serverFallback = new URL("../server/src/gateway-simple.js", import.meta.url).pathname;
console.log("SMOKE_HINT: On Ubuntu localhost, run: sudo bash mcp/scripts/install-abutron-mcp-own-v1.sh --local");
console.log("This smoke would call:", node, server);
console.log("Fallback check: current node", process.version, "pack @wonderwhy-er/desktop-commander via npx");
