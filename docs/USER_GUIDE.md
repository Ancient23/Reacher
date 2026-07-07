# Reachy Fleet Supervisor — User Guide

An embodied voice assistant for the **Reachy Mini Lite** that fronts **Claude Code**. You talk to
Reachy; it delegates real engineering to one or more Claude Code sessions ("managers") and reports
back by voice, on a web dashboard, and with the robot's body. This guide covers day-to-day use.

For the design and roadmap see [`plan.md`](../plan.md); to run the tests see
[`TESTING.md`](TESTING.md).

---

## 1. Prerequisites

- **Reachy Mini Lite** powered on the **rear USB-C** port. The front / USB-B port browns out the
  motors under load ("No motors detected" / frozen poses); the rear USB-C port is validated-stable.
  A powered USB hub is an acceptable fallback.
- **Windows host** (the whole stack — robot SDK, GStreamer, audio, motors — runs Windows-native).
- **Claude Code CLI** installed and logged in on the **Claude Max plan** (coding needs no Anthropic
  API key). Verify with `claude --version`.
- An **OpenAI API key with Realtime access** for the voice brain, set as a user env var:
  ```powershell
  setx OPENAI_API_KEY "sk-..."
  ```
  (Open a new terminal after `setx` so it takes effect.)
- `reachy-mini` is pinned to **1.8.4** (1.8.0 mis-resolves the local daemon on Windows).

---

## 2. Starting the app

From `reachy_fleet_supervisor/`:

```powershell
./run.ps1
```

`run.ps1` is self-healing: it starts the Reachy daemon, waits until the **motor backend** is
actually up (not just HTTP 200), launches the app attached, and auto-restarts either on a drop with
crash-loop backoff. The app is ready when:

- you can talk to Reachy and it answers by voice, and
- the dashboard loads at **http://127.0.0.1:7860/fleet**.

To stop, close the `run.ps1` terminal (Ctrl-C).

---

## 3. Talking to Reachy

Just speak. Reachy uses OpenAI Realtime for natural speech-to-speech, has a persona, and can emote,
dance, and move its head. Beyond chit-chat, the two things it does for you are **run one-off coding
tasks** and **manage a fleet of coding sessions**.

### One-off coding task

Ask for work directly, e.g.:

> "Create a file called hello.txt that says hi."

Reachy calls its `ask_claude_code` tool, Claude Code does the work on the Max plan, and Reachy tells
you the result.

### Spawning a fleet manager (confirm-before-kickoff)

A **manager** is a durable Claude Code session (`claude --bg`) working a task in a target repo. Ask
Reachy to spawn one:

> "Spawn a manager called builder on C:/Source/reacher to add a health-check endpoint."

Before kicking off, **Reachy reads back** the task, project, and (if non-default) the run mode /
permission mode, and asks you to confirm — say **"go ahead"** (or "yes"/"sounds good") to launch, or
correct it ("actually use remote-control mode"). To skip the readback, add **"just do it"** to your
request. Ask **"what are my options?"** for a spoken explanation of run modes, permission modes, and
the skip phrase.

### How Reachy reports fleet state

The fleet's state has several **renderers**, all reading one source of truth:

- **Voice** — Reachy speaks on state changes: a manager **completing**, a build/test **failing**,
  and a **HUMAN_GATE** ("builder needs your answer: …"). For a question it says it needs an *answer*;
  for an approval ask, that it needs your *approval*.
- **Body** — it turns its head toward the manager that needs you and signals state with its antennas.
- **Dashboard** — wide cards at `/fleet` (below).
- **Vision** — on a manager's completion Reachy can glance at the screen (or robot camera) and
  comment on the result.

### Answering a gate by voice

When Reachy says a manager needs you, just answer out loud:

> "Tell builder to use port 8080 and continue."

Reachy calls its `answer_gate` tool, injects your answer into that manager's session
(`claude --resume`), and confirms aloud that it resumed the work.

### Stopping managers

> "Stop and remove the builder manager."

---

## 4. The web dashboard

Open **http://127.0.0.1:7860/fleet**. It's a read-only view of `FleetState`: one wide card per
manager (state, current tool, last line, an expandable transcript), auto-polling every ~2s. Cards
stay put and expanded across polls, and a finished/gated manager's card is retained rather than
vanishing. On very wide screens (≥1400px) it uses two columns; otherwise one full-width column.

---

## 5. The headless `fleet` CLI

The same core, scriptable from a terminal — the dev/test harness and a keyboard alternative to
voice. Every invocation is stateless: it reconnects to the durable `claude --bg` roster, acts, and
exits. Run it via the installed entry point:

```powershell
cd reachy_fleet_supervisor
uv run fleet <command> ...
```

| Command | What it does |
|---|---|
| `fleet start` | Reconnect to durable managers and show the fleet. |
| `fleet spawn <task> --name N [--cwd … --model … --run-mode … --permission-mode … --mcp-config … -w]` | Spawn a background manager. |
| `fleet drive --name N --repo R [--command /drive-* --max-iterations K]` | Spawn a manager that drives a `/drive-*` ralph loop on a repo. |
| `fleet status [key] [--all] [--logs]` | One poll of `FleetState` (add `--json` for machine-readable). |
| `fleet logs <key> [--lines N]` | Recent `claude logs` tail for one manager (read-only). |
| `fleet send <key> <msg>` | Steer: send a message / approve a gate / reassign. |
| `fleet pause <key>` / `fleet resume <key>` | Pause (keeps the conversation) / resume a manager. |
| `fleet stop [key] [--rm]` / `fleet stop --all [--rm]` | Stop (and optionally remove) managers. |
| `fleet look "<question>" [--source screen\|camera] [--keep --out PATH]` | Vision: capture + Claude assessment of what was built. |

`key` is a manager's short id **or** its voice name. `--json` on any read command emits the same
JSON the tests assert against. The read commands (`status`/`logs`) never interrupt a running agent;
`send`/`pause`/`resume`/`stop` are the explicit write side.

**Examples:**

```powershell
uv run fleet spawn "add a health-check endpoint" --name builder --cwd C:/Source/reacher
uv run fleet status --all --logs
uv run fleet send builder "use port 8080 and continue"
uv run fleet look "does the login page look right?" --source screen --keep
uv run fleet stop builder --rm
```

---

## 6. Run modes, gates, and backends

- **Run mode** (`--run-mode`): `background` (default) is a durable `claude --bg` session, roster-
  visible and voice-steerable. `remote-control` is a claude.ai/mobile-steerable session (a separate
  must-stay-alive process needing full claude.ai OAuth). The two don't combine on one session.
- **Gate policy**: managers are autonomous by default; plan-defined gates and genuine ambiguity
  escalate to you (by voice). The default policy lives in the fleet config and is overridable per
  worker/project — an `autonomous` policy escalates only its configured triggers, a `gated` policy
  escalates every gate.
- **Coding backend**: Claude Code on the Max plan by default. A worker can instead route Claude Code
  at a **local Ollama model** by setting `ANTHROPIC_BASE_URL` in the project's `.claude/settings.json`
  env (bg sessions don't inherit it from the shell). See roadmap unit U19.

---

## 7. Fleet configuration

A fleet config lists your projects and defaults: each project has `{name, path, env, mcp[],
defaults}`, plus a fleet-wide default `model`, `gate_policy`, and named `personas`. Per-project
`defaults` (run mode, permission mode, backend, model, persona) override the fleet defaults, and an
explicit per-spawn override beats both. The dashboard's settings panel shows the effective resolved
values per project. See `fleet/config.py` for the schema and `plan.md` for the design intent.

---

## 8. Secrets

Never commit a `.env` file (`.gitignore` excludes it). Copy `.env.example` to `.env` for local runs.
When hosted as a Hugging Face Space, set `OPENAI_API_KEY` (and `HF_TOKEN` only if pulling gated
models) as **Space secrets**, not committed variables. Coding uses the Claude Max plan via the local
CLI — no Anthropic API key is stored by the app.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "No motors detected", frozen poses, lost connection | Motors browning out — use the **rear USB-C** port (or a powered hub). |
| Daemon says up but the app can't reach the robot | `/api/daemon/status` returns 200 even when the motor backend is dead; `run.ps1` gates on `state != "error"`. Restart via `run.ps1`. |
| Daemon resolves as `reachy-mini.local` and fails | You're on `reachy-mini` 1.8.0 — pin **1.8.4**. |
| No voice / Realtime errors | `OPENAI_API_KEY` missing or lacks Realtime access; re-`setx` and open a new terminal. |
| `fleet look --source camera` errors without the robot | The camera path is hardware-gated; use `--source screen` for digital work. |
| A manager vanished from the dashboard | Finished/gated managers are retained via `--all` polling; if truly gone, `fleet start` reconnects to the live roster. |
| Stray background agents after testing | `claude agents --json` to list, then `claude stop <id>` / `claude rm <id>` (or `fleet stop --all --rm`). |
