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
- **Durable substrate = background agents (`claude --bg`).** Managers run as supervisor-hosted
  background sessions that survive the app/terminal closing and machine *sleep* (machine *shutdown*
  stops them → they show failed but **restart from where they left off** on reattach). The app
  **reconnects on restart** via the supervisor roster (`~/.claude/daemon/roster.json`) +
  `claude respawn` / `--resume`. *Verified on 2.1.195.*
- **Remote control is a SEPARATE overlay, NOT composable with `--bg`** (verified: passing both,
  `--bg` silently wins and RC never activates). To steer a manager from **claude.ai / mobile**, run
  it in RC **server mode** (`claude remote-control --spawn worktree --capacity N` — one long-lived
  host for N remote sessions) or as an interactive `claude --remote-control`. RC needs a **full
  claude.ai OAuth login** (not an API key, not a `setup-token`/`CLAUDE_CODE_OAUTH_TOKEN`), and its
  host process must stay alive (dies on close or >~10 min network outage). So: **`--bg` for fleet
  durability + observability; RC as an optional human-steering overlay** — different processes.
- **Observability is non-interactive:** `claude agents --json` (per session: `id`, `sessionId`,
  `cwd`, `kind`, `name`, `state` ∈ working/blocked/done/failed/stopped, `waitingFor`) is the
  **FleetState source**; `claude logs <id>` for recent output; deep history in the JSONL transcript
  under `~/.claude/projects/`. Background agents **auto-isolate into `.claude/worktrees/`** (we don't
  manage worktrees); `/loop` ralph sessions are first-class rows; parallel subagents surface a
  `done/total` count. Body + dashboard + CLI all read FleetState; nothing polls a worker directly.
  Borrow seer-agent's ADW *pattern* but **don't depend on it** — stay project-agnostic.

## 4. Phased build

- **Phase 0 — Foundations ✅** Windows-native confirmed; robot moves + mic + speaker + Claude
  Agent SDK on the Max plan all validated on hardware.
- **Phase 1 — Single-session assistant ✅ (shipped & committed)** OpenAI Realtime voice +
  personality + emotions/dances + `ask_claude_code` → Claude Code does real work, speaks results.
- **Phase 2 — Fleet core + observability + persistence — software shipped; on-robot E2E gated.**
  Built & tested (`reachy_fleet_supervisor/fleet/`, 176 tests green incl. real `claude --bg`
  sessions): typed fleet **config** (U1); **FleetManager** over `claude --bg` (U2); **SessionManager**
  track-N + reconnect-from-roster (U3); **FleetState** + poller pub/sub (U4); the **status convention**
  — managers emit, the fleet reads, never interrupts (U5, decision #21); the headless **`fleet` CLI**
  (U6); the read-only **web dashboard** (U7); the **body renderer** (U8, human-confirmed on hardware:
  torso turns toward the flagged manager, antennas droop on failure); per-agent **identity** name+color
  (U9). The **whole-fleet integration test** is green (U10) — CLI, dashboard and body all render one
  `FleetState` consistently; the **on-robot end-to-end** (talk → spawn → observe on body + dashboard)
  is the one remaining Phase-2 HUMAN_GATE. *Idle-breathing policy (U10):* breathing is never suppressed
  continuously (a still robot reads as "dead"); steady states relax into the breathing idle (a momentary
  glance), only an attention/failure signal lingers (optional renderer `hold` hook).
  - `SessionManager` = **track + route N persistent manager sessions** (generalize the existing
    single worker). It does NOT implement orchestration — each manager orchestrates its own
    subagents / Workflows / ralph-loops natively. Per-agent **identity** (name + color) so you can
    refer to one by voice ("how's the Unreal one doing?"); one git **worktree per manager**.
  - **Persistence (promoted to core; verified on 2.1.195):** managers run as `claude --bg`
    supervisor-hosted background agents — survive app/terminal close + sleep; app **reconnects on
    restart** via the daemon roster + `claude respawn`/`--resume`. FleetState polls `claude agents
    --json`; output via `claude logs <id>`. Auto-worktree isolation under `.claude/worktrees/`.
  - **Remote control (optional overlay):** steer a manager from **claude.ai/mobile** via RC
    **server mode** or an interactive `claude --remote-control` — **not** combinable with `--bg`
    (verified), needs full claude.ai OAuth, host must stay alive. Decide per deployment whether the
    fleet runs bg-durable, RC-steerable, or a mix.
  - `FleetState` (single source of truth, pub/sub) aggregates each manager's (possibly nested)
    events from `claude agents --json` (+ `logs`) **and each manager's own emitted status** (its
    drive-loop report line + a small `status.json`/journal it writes to its job dir or repo — see
    decision #21). **Read-only web dashboard** (FastAPI on `settings_app`, browser-reachable) + a
    **headless `fleet` CLI** render it; the **body** renders it too. Per agent: status, current tool,
    last spoken line, transcript ring buffer; a 2–4 card grid.
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
  - Worker model `{ project_path, environment, mcp_servers, plan, gate_policy, run_mode }` where
    **`run_mode` = `background` (default) | `remote-control`** — see decision #19; set by voice per
    task ("…in the background, give me updates" vs "…as a remote-control session I can watch from my
    phone"). Run-mode (how it runs) is independent of the manager's work pattern (what it does).
    **MCP-general:** if
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
16. **Detached survival vs remote control — they DON'T compose (verified 2.1.195):** `claude --bg`
    gives supervisor-hosted durability + non-interactive observability (`agents --json`, `logs`) +
    reconnect (roster + `respawn`/`--resume`); `--remote-control` (server or interactive) gives
    claude.ai/mobile steering but is a separate, must-stay-alive process needing full claude.ai
    OAuth. Passing both → `--bg` silently wins, RC off. So **bg = fleet backbone; RC = optional
    human-steering overlay (likely server mode)**. Both are CLI features → managers are NOT pure
    in-process SDK clients. *Open: non-interactive *steering* of a running bg session from the shell
    isn't cleanly exposed (reply via agent-view peek or `claude attach`) — verify `claude --resume
    <id> -p` injection.*
17. **Gate → voice escalation** → the ralph loop's **`HUMAN_GATE` sentinel** is the escalation
    channel: Reachy speaks the gate; the spoken answer resumes the loop. (Implements the earlier
    `ask_human` idea concretely.)
18. **Fleet skill** → a `drive-loop` variant installed into manager sessions teaching the fleet
    conventions: orchestrate via subagents/Workflows, keep spoken replies short, escalate
    `HUMAN_GATE` by voice. Doubles as our headless dev/test harness. Build it once the `fleet` CLI
    surface is real.
19. **Per-manager `run_mode` (resolves the bg-vs-RC tension from #16):** each manager runs in ONE of
    two modes, chosen by voice per task — they can't be combined on one session (verified):
    • **`background`** (default) → `claude --bg`: supervisor-durable, survives sleep/restart, observed
      via `agents --json`, **steered by Reachy's voice** (updates spoken; gates escalated). No
      claude.ai control. • **`remote-control`** → `claude --remote-control` (or server mode) kept
      alive by the self-healing launcher (+ ralph committed-state = effectively resumable); **watch/
      steer from claude.ai/mobile**. "All background," "one phone-steerable," or any mix all fall out
      of this single knob. Run-mode is **orthogonal** to the manager's work pattern (single goal /
      dynamic workflow loop / ralph loop / subagent team).
20. **Voice interaction contract for launching coding jobs (persona behavior):**
    (a) **Confirm before kickoff** — when asked to code, Reachy reads back the resolved settings
    (task summary + `location` + `run_mode` + any non-default gate policy) and waits for a spoken OK
    before spawning the manager; honor a quick "just do it" to skip the readback.
    (b) **Explain on request** — Reachy knows the available options (task, location, `background` vs
    `remote-control`, gates) and can explain what they mean and **how to ask for them, with spoken
    examples**, whenever the user wants help. Implement in the persona `instructions.txt` + the
    fleet-spawn tool schema (the generalized `ask_claude_code`); mirror in the fleet skill (#18).
21. **Status convention — Reachy reads FleetState, doesn't interrupt agents.** "How's X doing?" is
    answered by reading **FleetState** (`claude agents --json` state + `waitingFor`, `claude logs
    <id>`, and each manager's own drive-loop report line + a small `status.json`/journal it writes to
    its job dir or repo) — never by interrupting the agent, so it costs no agent turn and works the
    same in both run modes. Each manager therefore **emits status to a known place every iteration**;
    Reachy/dashboard/CLI all read it. Two-way follow-up ("why did you do X?") is *steering*: native in
    `remote-control`, the #16 to-verify path (`claude --resume <id> -p`) in `background`.

## 7. How to run

1. Robot powered + USB-C connected; `setx OPENAI_API_KEY "sk-..."` (Realtime access).
2. From `reachy_fleet_supervisor/`, in a terminal: `.\run.ps1`.
3. Talk to Reachy — chat, watch it emote, or ask it to do real work
   (*"create a file called hello.txt that says hi"* → `ask_claude_code` → Claude Code → spoken result).
