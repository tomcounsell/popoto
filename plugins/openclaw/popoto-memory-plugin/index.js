// Popoto subconscious memory for OpenClaw.
//
// This plugin is a translation layer and nothing more. OpenClaw calls its hooks
// in-process with two arguments, `(event, ctx)`; popoto's `popoto-memory hook`
// executable reads one JSON object on stdin and writes one on stdout. Everything
// that decides what a memory is, how it is scored, or where it is stored lives on
// the Python side of that pipe. Reimplementing any of it here is explicitly out of
// scope (see plugins/openclaw/README.md).
//
// Two hooks are registered:
//   before_prompt_build  Modify  -> returns {appendContext} to inject recalled memory
//   llm_output           Observe -> reports the turn's outcome, return value ignored

import { appendFileSync, writeFileSync } from "node:fs";
import { execFile, execFileSync } from "node:child_process";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// OpenClaw gives a Modify handler a 15-second budget and *skips* — but does not
// cancel — a handler that overruns it. Bounding the child ourselves is therefore
// the only thing that stops an overrun subprocess from outliving the turn that
// would ignore it. The same bound is applied to the llm_output shell-out for a
// different reason: that hook is Observe, has no runner timeout, and its return
// value is discarded, so nothing upstream would ever reap a hung child.
const TIMEOUT_MS = 10000;

// The failure-path check in the integration guide points this at a nonexistent
// binary to prove a memory outage degrades to "no memory" rather than a broken
// agent, so the override is a contract, not a convenience.
function memoryBin() {
  return process.env.POPOTO_MEMORY_BIN || "popoto-memory";
}

// Set POPOTO_HOOK_CAPTURE to a directory to tee each envelope to
// <dir>/<hook_event_name>.json. This is how the committed fixtures in
// tests/fixtures/harness_payloads/ were produced: they are what this function
// wrote during a live turn, not a transcription of vendor documentation.
function tee(name, payload) {
  const dir = process.env.POPOTO_HOOK_CAPTURE;
  if (!dir) return;
  try {
    writeFileSync(`${dir}/${name}.json`, payload);
  } catch (err) {
    logError(`tee ${name}`, err);
  }
}

function logError(where, err) {
  const dir = process.env.POPOTO_HOOK_CAPTURE;
  if (!dir) return;
  try {
    appendFileSync(`${dir}/errors.log`, `${where}: ${err}\n`);
  } catch {
    // Diagnostics are best-effort. A hook that throws while logging that it
    // threw is strictly worse than a silent one.
  }
}

// `session_id` and `cwd` come from the second argument, not the event: OpenClaw
// puts them on `ctx` as `sessionId` and `workspaceDir`. So does the per-turn
// identifier — `ctx.runId` is the same value on this turn's before_prompt_build
// and its llm_output, which is exactly what popoto's turn-keyed outcome handoff
// pairs on. Reading any of the three off `event` yields undefined.
function envelopeFromCtx(ctx) {
  return {
    session_id: ctx?.sessionId,
    cwd: ctx?.workspaceDir,
    turn_id: ctx?.runId,
  };
}

// The last user-authored text in the conversation array, used when `event.prompt`
// is empty (a continuation or tool-result-driven turn may populate only
// `event.messages`). This reads the one element shape that has actually been
// observed on a live turn and returns "" for anything else. Widening it to key
// names nobody has seen would reproduce, inside the plugin, the fabricate-then-
// hope failure that the captured fixtures exist to undo.
function lastUserMessageText(messages) {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === "user" && typeof m.content === "string") {
      return m.content;
    }
  }
  return "";
}

export default definePluginEntry({
  id: "popoto-memory",
  name: "Popoto Memory",
  description: "Subconscious memory recall and capture backed by popoto",
  register(api) {
    api.on("before_prompt_build", (event, ctx) => {
      try {
        const envelope = {
          hook_event_name: "before_prompt_build",
          // Always emit the key. An absent `prompt` and an empty one are the
          // same thing to the adapter, so omitting it would buy a branch for a
          // distinction nothing reads.
          prompt:
            event?.prompt || lastUserMessageText(event?.messages) || "",
          ...envelopeFromCtx(ctx),
        };
        const payload = JSON.stringify(envelope);
        tee("before_prompt_build", payload);
        const stdout = execFileSync(memoryBin(), ["hook"], {
          input: payload,
          encoding: "utf8",
          timeout: TIMEOUT_MS,
        });
        if (!stdout) return undefined;
        const parsed = JSON.parse(stdout);
        return parsed?.appendContext ? parsed : undefined;
      } catch (err) {
        // Inject nothing, fail nothing. A memory outage must degrade to an
        // agent without memory, never to an agent that cannot answer.
        logError("before_prompt_build", err);
        return undefined;
      }
    });

    api.on("llm_output", (event, ctx) => {
      try {
        const envelope = {
          hook_event_name: "llm_output",
          // Passed through as an array, deliberately unflattened: the adapter's
          // _first_string() reduces a list of strings, so the one non-trivial
          // transformation in this path stays on the tested Python side and the
          // committed fixture keeps documenting what OpenClaw actually sends.
          assistantTexts: event?.assistantTexts,
          ...envelopeFromCtx(ctx),
        };
        const payload = JSON.stringify(envelope);
        tee("llm_output", payload);
        // Observe hook: the return value is discarded, so there is nothing to
        // wait for. The child still gets a timeout — see TIMEOUT_MS.
        const child = execFile(
          memoryBin(),
          ["hook"],
          { timeout: TIMEOUT_MS },
          (err) => {
            if (err) logError("llm_output subprocess", err);
          },
        );
        child.on("error", (err) => logError("llm_output spawn", err));
        child.stdin?.on("error", (err) => logError("llm_output stdin", err));
        child.stdin?.end(payload);
      } catch (err) {
        logError("llm_output", err);
      }
    });
  },
});
