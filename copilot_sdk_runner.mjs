import { CopilotClient } from "@github/copilot-sdk";
import { existsSync, realpathSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { createInterface } from "node:readline";
import path from "node:path";
import { fileURLToPath } from "node:url";

const blockedNames = new Set([
  ".aws", ".codex", ".env", ".env.local", ".env.production", ".git", ".hg",
  ".netrc", ".npmrc", ".pypirc", ".ssh", ".state", ".svn", ".test-temp",
  ".venv", "__pycache__", "build", "coverage", "credentials", "dist", "id_rsa",
  "imports", "node_modules", "private", "reference", "secrets", "venv",
]);
const protectedNames = new Set([
  "agents.md", "coordinator.md", "codex-session-source.json", "owner-file-access.json",
  "owner_requirements.md", "project-context.json", "project-progress-sources.json",
  "project-report-catalog.json", "settings.json", "skill.md", "speech-config.json", "tasks.json",
]);
const secretName = /(?:^|[._ -])(?:credentials?|passwords?|secrets?|tokens?|private[-_ ]?keys?)(?:[._ -]|$)/i;

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function canonicalCandidate(candidate, root) {
  const absolute = path.isAbsolute(candidate) ? path.resolve(candidate) : path.resolve(root, candidate);
  let existing = absolute;
  const suffix = [];
  while (!existsSync(existing)) {
    const parent = path.dirname(existing);
    if (parent === existing) throw new Error("The requested path could not be verified.");
    suffix.unshift(path.basename(existing));
    existing = parent;
  }
  return path.resolve(realpathSync.native(existing), ...suffix);
}

function insideRoot(candidate, root) {
  if (typeof candidate !== "string" || !candidate.trim()) return false;
  let resolved;
  try {
    resolved = canonicalCandidate(candidate, root);
  } catch {
    return false;
  }
  const relative = path.relative(root, resolved);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return false;
  const parts = relative.split(path.sep).filter(Boolean);
  if (parts.some(part => blockedNames.has(part.toLowerCase()) || part.startsWith(".") || part.includes("~")
      || /^\.env(?:\.|$)/i.test(part)
      || secretName.test(part))) return false;
  return true;
}

export function supportedProviderBaseUrl(value) {
  try {
    const url = new URL(value);
    const localLoopback = url.protocol === "http:"
      && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      && url.pathname === "/v1";
    const openRouter = url.protocol === "https:"
      && url.hostname === "openrouter.ai"
      && url.pathname === "/api/v1"
      && !url.port;
    return (localLoopback || openRouter) && !url.username && !url.password && !url.search && !url.hash;
  } catch {
    return false;
  }
}

export function permissionFor(root, request) {
  if (request.managedApprovalRequired || request.requestSandboxBypass) {
    return { kind: "reject", feedback: "Vega does not auto-approve managed or sandbox-bypass requests." };
  }
  if (request.kind === "read") {
    return insideRoot(request.resolvedPath ?? request.path, root)
      ? { kind: "approve-once" }
      : { kind: "reject", feedback: "Vega can only read ordinary files inside this registered project folder." };
  }
  if (request.kind === "write") {
    const edits = request.fileEdits ?? [];
    const deletesOrMoves = edits.some(edit =>
      (edit.before && !edit.after) ||
      (edit.before && edit.after && edit.before.path !== edit.after.path)
    );
    const fileName = request.resolvedPath ?? request.fileName;
    const baseName = path.basename(fileName).toLowerCase();
    const deletionDiff = /^\+\+\+ \/dev\/null$/m.test(request.diff ?? "");
    if (!insideRoot(fileName, root) || protectedNames.has(baseName) || secretName.test(baseName)
        || deletesOrMoves || deletionDiff) {
      return { kind: "reject", feedback: "Vega blocks deletions, protected files, and writes outside this project." };
    }
    if (!edits.length || edits.some(edit =>
      !edit.after || typeof edit.after.path !== "string" || typeof edit.after.content !== "string"
      || (edit.before && (typeof edit.before.path !== "string" || typeof edit.before.content !== "string"))
      || !insideRoot(edit.after.path, root)
      || (edit.before && !insideRoot(edit.before.path, root))
    )) {
      return { kind: "reject", feedback: "Vega requires a complete, in-project before/after preview for file changes." };
    }
    return { kind: "approval-required" };
  }
  if (request.kind === "shell") {
    return { kind: "reject", feedback: "Vega blocks shell commands in Copilot sessions." };
  }
  return { kind: "reject", feedback: "Vega does not auto-approve this tool or external action." };
}

async function main() {
  let apiKey = "";
  const inputLines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const inputIterator = inputLines[Symbol.asyncIterator]();
  const nextInput = async () => {
    const line = await inputIterator.next();
    if (line.done) throw new Error("The Vega session owner disconnected before deciding this file change.");
    return line.value;
  };
  try {
    const input = JSON.parse(await nextInput());
    apiKey = process.env.COPILOT_API_KEY ?? "";
    if (!input || typeof input !== "object" || !["create", "send"].includes(input.operation)
        || typeof input.root !== "string" || typeof input.project !== "string"
        || !supportedProviderBaseUrl(input.baseUrl)
        || typeof input.model !== "string" || !/^[A-Za-z0-9._:/-]{1,200}$/.test(input.model)
        || typeof input.prompt !== "string" || Array.from(input.prompt).length > 24000
        || (input.operation === "create" && !input.prompt.trim())) {
      throw new Error("The Copilot session request or model provider is invalid.");
    }
    const root = realpathSync.native(input.root);
    if (!existsSync(root)) throw new Error("The registered project folder is unavailable.");

    const client = new CopilotClient();
    await client.start();
    try {
      const config = {
        model: input.model,
        provider: {
          type: "openai",
          baseUrl: input.baseUrl,
          ...(apiKey ? { apiKey } : {}),
        },
        workingDirectory: root,
        onPermissionRequest: async request => {
          const decision = permissionFor(root, request);
          if (decision.kind !== "approval-required") return decision;
          const permission = {
            kind: "write",
            fileName: request.resolvedPath ?? request.fileName,
            intention: request.intention,
            diff: request.diff,
            fileEdits: request.fileEdits,
            toolCallId: request.toolCallId,
            managedApprovalRequired: request.managedApprovalRequired,
            requestSandboxBypass: request.requestSandboxBypass,
          };
          if (Buffer.byteLength(JSON.stringify(permission), "utf8") > 768 * 1024) {
            return { kind: "reject", feedback: "This file change is too large for Vega's review queue." };
          }
          const requestId = randomUUID();
          emit({ type: "permission_request", requestId, sessionId: input.sessionId, permission });
          let response;
          try {
            response = JSON.parse(await nextInput());
          } catch {
            return { kind: "reject", feedback: "Vega could not confirm the owner decision." };
          }
          if (response?.type !== "permission_decision" || response.requestId !== requestId
              || !["approve", "reject"].includes(response.decision)) {
            return { kind: "reject", feedback: "The owner decision did not match this file change." };
          }
          return response.decision === "approve"
            ? { kind: "approve-once" }
            : { kind: "reject", feedback: "The owner rejected this file change." };
        },
      };
      const session = input.operation === "create"
        ? await client.createSession({ ...config, sessionId: input.sessionId })
        : await client.resumeSession(input.sessionId, config);
      let content = "";
      if (input.prompt) {
        const response = await session.sendAndWait({ prompt: input.prompt }, 600_000);
        content = response?.data?.content ?? "";
        if (!content.trim()) throw new Error("The Copilot session returned no assistant message.");
      }
      emit({ ok: true, sessionId: session.sessionId, content });
    } finally {
      await client.stop();
    }
  } catch (error) {
    const message = String(error?.message ?? "Copilot SDK request failed.");
    emit({ ok: false, error: apiKey ? message.split(apiKey).join("[redacted]") : message });
    process.exitCode = 1;
  } finally {
    inputLines.close();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
