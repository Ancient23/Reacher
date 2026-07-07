# Reachy Fleet Supervisor

An embodied voice assistant for **Reachy Mini** that talks to **Claude Code** — built as a
Reachy Mini Python app, running Windows-native.

## What it is

- **Voice + personality:** OpenAI **Realtime** (natural speech-to-speech, low latency) with a
  Reachy persona, the full **emotion/dance** library, and head movement.
- **Engineering:** an `ask_claude_code` tool delegates real coding tasks to **Claude Code**
  (Claude Agent SDK) running on the user's **Claude Max plan** — no Anthropic API key needed.
- Designed to grow into a multi-session **fleet supervisor** over several Claude Code sessions
  (roadmap in [`plan.md`](plan.md)). The **Phase 2 fleet core is built and tested**
  (`reachy_fleet_supervisor/fleet/`): durable `claude --bg` managers tracked + reconnected by a
  `SessionManager`, a single-source-of-truth `FleetState`, and three renderers over it — a headless
  `fleet` CLI, a read-only web dashboard, and the **robot body** (it turns toward the manager needing
  you and signals state with its antennas). The full on-robot voice-to-fleet loop is the last
  Phase-2 item being verified on hardware.

## Run it

1. Power on the Reachy Mini Lite (USB connected — use the **rear USB-C** port).
2. Set an OpenAI key with **Realtime** access: `setx OPENAI_API_KEY "sk-..."`
3. From `reachy_fleet_supervisor/`, in a terminal: `.\run.ps1`
4. Talk to Reachy — chat, watch it emote, or ask it to do real work
   (e.g. *"create a file called hello.txt that says hi"* → it runs Claude Code and reports back).

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** — running the app, talking to Reachy, spawning + steering
  fleet managers, the web dashboard, the headless `fleet` CLI, config, and troubleshooting.
- **[Testing Guide](docs/TESTING.md)** — the automated software suite (`uv run pytest`) plus the
  human-run hardware/voice checks.
- **[plan.md](plan.md)** — design and phased roadmap.

## Layout

- `reachy_fleet_supervisor/` — the Reachy Mini app (forked from Pollen's conversation app).
  - `src/reachy_fleet_supervisor/openai_realtime.py` — OpenAI Realtime voice brain (the active path).
  - `src/reachy_fleet_supervisor/claude_brain.py` — persistent Claude Code worker session.
  - `src/reachy_fleet_supervisor/profiles/_reachy_fleet_supervisor_locked_profile/`
    — persona (`instructions.txt`), enabled tools (`tools.txt`), and the `ask_claude_code` tool.
  - `src/reachy_fleet_supervisor/{supervisor_app,voice,emotions}.py` — a local-first
    (Whisper + Piper, Max-plan-only, offline) alternative brain. Kept as a fallback; not the default.
  - `run.ps1` — launch helper (starts the daemon, waits for it, runs the app attached).
- `plan.md` — project plan + phased roadmap.
- `.phase0/` — Phase-0 feasibility spike scripts.

## Notes

- **Host:** Windows-native — the Reachy SDK, bundled GStreamer, audio, and motors all work on Windows.
- `reachy-mini` is pinned to **1.8.4**; 1.8.0 mis-resolves the local daemon (`reachy-mini.local`) on Windows.
- The OpenAI Realtime layer needs an OpenAI key; **coding stays on the Claude Max plan**.
- Worker sessions for Linux/Docker projects (e.g. seer-agent) are intended to run in **WSL2**;
  Unreal-targeted work runs on Windows.
