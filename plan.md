# Reachy Mini — Fleet Supervisor

> Status: **Phase 0 + Phase 1 shipped** (works on hardware, committed & pushed to
> `github.com/Ancient23/Reacher`, branch `main`). Updated 2026-06-27.

## 1. Vision

An **embodied assistant for Claude Code**: Reachy Mini is the voice + body of a "Reachy LLM" you
talk to, that can ultimately **observe and control multiple Claude Code worker sessions at once** —
a fleet of coding agents, surfaced physically through the robot. You talk to Reachy; Reachy does
real engineering (via Claude Code) and expresses status/personality through its body.

**Phase 1 (shipped) is the single-session version:** you talk to Reachy, it chats + emotes, and
delegates real coding to one Claude Code worker. The multi-session "fleet" is the Phase 2/3
roadmap below.

### Core behavior — "plan-gated autonomy" (target for the fleet)
- A worker runs **fully autonomously while its plan is solid and unambiguous.**
- **Plan-defined gates** (e.g. "pause before pushing") → Reachy **asks you out loud** to approve.
- **Ambiguity** not covered by the plan → Reachy asks; your spoken answer routes back.
- **Start with 1, architect for N.**

## 2. Key decisions (as built)

| Decision | Choice | Notes |
|---|---|---|
| App model | **Python app**, **Windows-native host** | Only Python can spawn/observe local Claude Code; Windows confirmed working (SDK + GStreamer + motors). |
| **Voice / personality** | **OpenAI Realtime** (forked Pollen conversation app) | Natural 'cedar' voice, good hearing, low latency, native tool-calling + persona, 81-move emotion/dance libs. Needs an **OpenAI key with Realtime access**. |
| **Engineering** | **Claude Code via `ask_claude_code` tool** (claude-agent-sdk `WorkerSession`) | Coding stays on the **Claude Max plan** — no Anthropic API key. Default model `claude-sonnet-4-6`, effort low (env-tunable). |
| Local fallback brain | **Whisper + Piper** (`supervisor_app/voice/emotions.py`) | Built & working, Max-plan-only/offline, but too robotic/laggy live → kept as fallback; revisit with CUDA + neural TTS on the 5090. |
| Auth | Claude: Max-plan login (Agent SDK reuses it). OpenAI: `OPENAI_API_KEY` user env var. | |
| Autonomy | **Plan-gated** (target for fleet) | |
| Hardware | **Reachy Mini Lite** (USB), **rear USB-C port** | Front/USB-B port browned out the motors under load; USB-C is stable (validated: 540 motion frames, 0 errors). |
| Launcher | **`run.ps1`** — self-healing supervisor | Starts daemon + app, verifies the **motor backend** (not just HTTP 200), auto-restarts both on any drop, crash-loop backoff. |
| `reachy-mini` version | **pinned 1.8.4** | 1.8.0 mis-resolves the local daemon as `reachy-mini.local` (mDNS) on Windows. |

## 3. Architecture (as built — Phase 1)

```
You ⇄ (robot mic/speaker) ⇄  OpenAI Realtime  ──── persona + emotion/dance tools
                                    │   ask_claude_code(task)
                                    ▼
                            Claude Code WorkerSession  (claude-agent-sdk, Max plan)
                                    │  runs in a project dir / git worktree
                                    ▼  → short spoken summary back to Realtime
```
- `openai_realtime.py` = the active voice brain (Pollen's, unmodified).
- `profiles/_reachy_fleet_supervisor_locked_profile/` = Reachy persona (`instructions.txt`),
  enabled tools (`tools.txt`: play_emotion, dance, move_head, sweep_look, **ask_claude_code**),
  and `ask_claude_code.py`.
- `claude_brain.py` = `WorkerSession` (persistent `ClaudeSDKClient`).

### Target architecture (Phase 2+ — the fleet)
**One source of truth, many renderers.** A `SessionManager` holds N worktree-isolated
`WorkerSession`s; each streams `WorkerEvent`s into a single **`FleetState`** over a pub/sub event
bus. Everything that *shows* the fleet — the robot body, the web dashboard, a future terminal TUI —
is just a subscriber to FleetState. Nothing polls the workers directly.

```
         ┌───────────────── FleetState (pub/sub) ─────────────────┐
         │   worker[reacher]    worker[unreal]    worker[…]        │
         └────────▲────────────────────┬──────────────────┬───────┘
    WorkerEvents  │                     │  subscribers     │
   ┌──────────────┴───┐      ┌──────────▼─────┐   ┌────────▼───────┐   ┌─────────────┐
   │ SessionManager   │      │  Reachy body   │   │  Web dashboard │   │ (later) TUI │
   │  N WorkerSessions│      │  (attention)   │   │  FastAPI       │   │             │
   └──────────────────┘      └────────────────┘   └────────────────┘   └─────────────┘
```

- Each **fleet manager is a full Claude Code session** (Max plan) that orchestrates its OWN team —
  subagents, dynamic **Workflows**, and **ralph/drive loops** (cf. the `drive-loop` skill) that
  drive a goal to completion: fresh-context subagent per iteration, sentinel-gated
  (`CONTINUE | HUMAN_GATE | COMPLETE | FAILED`). **Durability lives in committed git state, not the
  process** → the loop and its workers are disposable and resumable. We do NOT build an
  orchestration engine; Claude already is one.
- **Realtime stays the orchestrator + voice** — it spawns / routes to / observes N managers. No
  separate Claude orchestrator brain (each manager already orchestrates).
- **Sessions outlive the voice layer.** Top-level managers run **supervisor-backed / detached**
  (`claude --bg`) so they keep running if the app or Reachy goes away; on restart the app
  **reconnects** (resume by session id; `claude agents` / `logs` / `attach`; tail the JSONL
  transcript under `~/.claude/projects/`). Each manager enables **`/remote-control`** so it's
  observable/steerable from **claude.ai or mobile** (Max-plan OAuth) when you're away from the robot.
- We render our own status/transcript views — from the **JSONL transcript** for detached managers
  (Agent SDK typed events only for quick in-process one-shots). The body + web dashboard + CLI all
  read **FleetState**; nothing polls a worker directly. Borrow seer-agent's ADW *pattern* (worktree +
  isolated ports + state JSON) but **don't depend on it** — the supervisor stays project-agnostic.

## 4. Phased build

- **Phase 0 — Foundations ✅** Windows-native confirmed; robot moves + mic + speaker + Claude
  Agent SDK on the Max plan all validated on hardware.
- **Phase 1 — Single-session assistant ✅ (shipped & committed)** OpenAI Realtime voice +
  personality + emotions/dances + `ask_claude_code` → Claude Code does real work, speaks results.
- **Phase 2 — Fleet core + observability + persistence — NEXT.**
  - `SessionManager` = **track + route N persistent manager sessions** (generalize the existing
    single worker). It does NOT implement orchestration — each manager orchestrates its own
    subagents / Workflows / ralph-loops natively. Per-agent **identity** (name + color) so you can
    refer to one by voice ("how's the Unreal one doing?"); one git **worktree per manager**.
  - **Persistence (promoted from Phase 4 — now core):** managers run **supervisor-backed/detached**
    (`claude --bg`) and the app **reconnects on restart** (resume by session id; `claude
    agents`/`logs`/`attach`; tail the JSONL transcript). They survive the voice layer going away.
    *(Verify exact flags/version on the box; pure in-process SDK survival across app death is
    unconfirmed → prefer `--bg`/supervisor.)*
  - **`/remote-control` per manager** — observe/steer each top-level session from **claude.ai or
    mobile** (Max-plan OAuth, no API key), enabled at spawn. A remote control plane for when you're
    away from the robot.
  - `FleetState` (single source of truth, pub/sub) aggregates each manager's (possibly nested)
    events. **Read-only web dashboard** (FastAPI on `settings_app`, browser-reachable) + a **headless
    `fleet` CLI** render it; the **body** renders it too. Per agent: status, current tool, last
    spoken line, transcript ring buffer; a 2–4 card grid.
  - **Hybrid worker setup:** a `fleet` config lists available projects (`path`, `env`, `mcp[]`);
    tasks given by **voice** at runtime.
  - Design target: **2–4 concurrent managers** (Max-plan rate limits; body can distinctly signal each).
- **Phase 3 — Steering + plan-gated autonomy.**
  - Dashboard + CLI become **interactive**: approve a gate, send a message, pause/resume, spawn/kill,
    reassign — **voice, screen, and claude.ai are surfaces for the same actions**.
  - **Plan-gated autonomy via the ralph/drive contract:** a manager loops a `/drive-*` command toward
    a goal; its **`HUMAN_GATE` sentinel _is_ the gate → voice escalation** (Reachy relays it verbatim;
    your spoken answer resumes the loop). `COMPLETE` ends it; `FAILED`×3 stops; usage-limit → schedule
    a wakeup and resume. Durable committed state makes a successor resume seamlessly.
  - Optional **written plan file** per manager (the drive contract's committed state files).
  - Autonomous by default; a **default gate policy lives in the fleet config**, overridable per
    worker/project.
  - Worker model `{ project_path, environment, mcp_servers, plan, gate_policy }`. **MCP-general:** if
    Claude can drive a tool via MCP, a manager can use it — covers the **Unreal 5.8 MCP**, browser
    MCPs, etc. Example repos are *test targets*, not plan dependencies.
- **Phase 4 — Reachy vision + local backends.**
  - **Vision (both directions):** a **worker-callable tool** ("look at what I built" — screen
    capture for digital work, robot camera for physical) that feeds an assessment back into the
    worker's loop; **and** a **supervisor observer** (Reachy proactively glances and comments).
    Meta-dev loop: an agent edits a Reachy app → it runs → Reachy *sees* the result → assessment →
    back to the agent. Reuse `reachy_claude_vision` (`.phase0/vision_ref/`).
  - **Pluggable coding backend:** alongside Claude Code (Max plan), allow a manager to run a **local
    model via Ollama** (option 1: **Claude Code routed to a local model**, keeping the same agent
    loop/tooling — likely via a base-URL router; offline / free / no rate limits). Chosen per worker.
  - **5090 local voice mode:** CUDA Whisper + neural TTS (Kokoro/XTTS) — an OpenAI-free brain.
  - Remaining rate-limit/robustness hardening (persistence groundwork already shipped in Phase 2).
- **Phase 5 — Customization + share.**
  - User-configurable keys, models, projects, personas; UI polish.
  - The **OpenAI-free voice mode + Ollama coding backend** become the low-barrier path for people
    without a Realtime key or Max plan.
  - Publish as a shareable **Reachy Mini app on Hugging Face** (like the vision ref). Sort licensing
    (Pollen base + our additions) and secrets handling for a public app.

## 5. Risks & status

- **Windows feasibility — RESOLVED.** Everything runs Windows-native (reachy_mini bundles
  GStreamer + pycaw + Rust kinematics; motors on COM3).
- **Motor power — RESOLVED (was the big one).** Lite motors run on USB 5 V and browned out under
  motion on the front/USB-B port → "No motors" / "Lost connection" / frozen poses. **Rear USB-C
  is stable.** (A powered USB hub is the fallback; the 5090 desktop should be fine too.)
- **Daemon stability** — mitigated by `run.ps1` self-heal + motor-backend readiness gate.
- **OpenAI cost** — the voice layer is pay-per-minute OpenAI Realtime (coding stays on Max plan).
- **Rate limits (future fleet)** — N concurrent Max-plan workers share one subscription pool.
- **Worktree isolation (future fleet)** — one worktree per worker.

## 6. Decisions log (answered)

1. **Template base** → **forked the `conversation` template** (kept its OpenAI Realtime brain;
   added `ask_claude_code` + Reachy persona).
2. **Worker plans** → for now you give tasks by voice; plan-gated fleet gating is Phase 3.
3. **Voice stack** → **OpenAI Realtime** (chosen over local Whisper+Piper for quality/latency).
4. **On exit** → workers stop with the app for now (detached fleet survival is a Phase 2/3 item).
5. **Host OS** → **Windows-native**.
6. **Publish** → committed to a **private GitHub repo** (`Ancient23/Reacher`); not on HF.

### Fleet design decisions (brainstormed 2026-06-27)
7. **Observability surface** → **web dashboard first** (FastAPI on `settings_app`, browser-reachable
   so the 5090 can be watched from a laptop). Terminal TUI is a cheap follow-on over the same
   FleetState. Both render FleetState; the robot body is a third renderer.
8. **Worker transport** → **long-running top-level managers are supervisor-backed/detached**
   (`claude --bg`), NOT pure in-process SDK clients — that's what gives detached survival +
   `/remote-control`, both CLI-only features. We render status from the **JSONL transcript**
   (`~/.claude/projects/`) + `claude agents`/`logs`. The in-process Agent SDK (`ClaudeSDKClient`)
   stays fine for quick one-shots. (Optional later: OTEL export.)
9. **Vision (Phase 4)** → **both** a worker-callable verification tool *and* a proactive supervisor
   observer.
10. **Fleet scale** → design around **2–4 concurrent agents**.
11. **Worker setup** → **hybrid**: a fleet config lists projects (`path`, `env`, `mcp[]`); simple
    tasks by voice, with an optional **written plan file** per worker for plan-gated autonomy.
12. **Persistence** → **promoted to Phase 2 core** (was deferred): managers run detached and the app
    **reconnects on restart**. Justified because top-level managers are now full autonomous ralph-loop
    orchestrators — they must survive the voice layer going away, not die with it.
13. **Gating** → **fully autonomous by default**, with a default policy in the fleet config that's
    **overridable per worker/project**.
14. **Coding backend** → **pluggable**: Claude Code on the Max plan (default) *or* a **local model
    via Ollama** (offline / free / no rate limits), selectable per worker. **Option 1 chosen:** route
    Claude Code itself at a local model (keep the agent loop/tooling/event stream; only the brain
    changes) rather than a native Ollama agent. Mirrors the OpenAI-Realtime ↔ local-Whisper duality.
15. **Manager autonomy model** → each top-level manager is a **full orchestrator** running a
    **ralph/drive loop** (cf. `drive-loop` skill): fresh-context subagent per iteration, sentinel-
    gated, durable committed git state. We delegate orchestration to Claude rather than building a
    scheduler. No separate Claude orchestrator brain — **Realtime stays the host**.
16. **Detached survival + remote control** → managers spawn **`claude --bg`** (supervisor-backed,
    survives app/voice death) with **`/remote-control`** enabled (steer from claude.ai/mobile,
    Max OAuth). Both are CLI-only → confirmed reason to NOT use pure in-process SDK for managers.
    *To verify on the installed version: exact `--bg`/`--remote-control` flags and non-interactive
    enablement.*
17. **Gate → voice escalation** → the ralph loop's **`HUMAN_GATE` sentinel** is the escalation
    channel: Reachy speaks the gate; the spoken answer resumes the loop. (Implements the earlier
    `ask_human` idea concretely.)
18. **Fleet skill** → a `drive-loop` variant installed into manager sessions teaching the fleet
    conventions: orchestrate via subagents/Workflows, keep spoken replies short, escalate
    `HUMAN_GATE` by voice. Doubles as our headless dev/test harness. Build it once the `fleet` CLI
    surface is real.

## 7. How to run

1. Robot powered + USB-C connected; `setx OPENAI_API_KEY "sk-..."` (Realtime access).
2. From `reachy_fleet_supervisor/`, in a terminal: `.\run.ps1`.
3. Talk to Reachy — chat, watch it emote, or ask it to do real work
   (*"create a file called hello.txt that says hi"* → `ask_claude_code` → Claude Code → spoken result).
