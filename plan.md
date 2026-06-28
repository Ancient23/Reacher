# Reachy Mini — Claude Code Fleet Supervisor

> Status: **DRAFT — awaiting approval.** Per `reachy_mini/AGENTS.md`, no code/scaffolding
> until this plan is approved. Created 2026-06-27.

## 1. What we're building (understanding of requirements)

An **embodied supervisor** for Claude Code. Reachy Mini becomes the voice and body of a
"Reachy LLM" that can **see, observe, and control multiple Claude Code sessions at once** —
a fleet of autonomous coding agents working in parallel, surfaced physically through the robot.

You talk to Reachy; Reachy watches the fleet, steers it, narrates status, and physically
signals which session needs attention. The actual coding is done by independent Claude Code
worker sessions; Reachy is the orchestrator/avatar, not a coder itself.

### Core behavior — "plan-gated autonomy"
- A worker session runs **fully autonomously while its plan is solid and unambiguous.**
- **Gates defined in the plan** (e.g. "pause before pushing", "ask before deleting data")
  → the worker pauses and **Reachy asks you out loud** to approve/deny.
- **Ambiguity not covered by the plan** → the worker stops and **Reachy asks you** for a
  decision; your spoken answer is routed back to that session.
- Everything else proceeds without interruption.

### Scope
- **Start with 1 session, architect for N.** Build the single-session voice loop first, but
  put the session-manager / fleet abstraction in from the start so adding workers is trivial.

## 2. Key decisions (locked)

| Decision | Choice | Why |
|---|---|---|
| App model | **Python app** (runs on the Lite's host laptop) | Only Python can spawn/observe local Claude Code processes; JS apps are sandboxed on HF Spaces. |
| Supervisor brain | **Claude-native** (a Claude Agent SDK session) | "Assistant for talking to Claude Code" + runs entirely on the Max plan. No OpenAI. |
| Workers | **N × `ClaudeSDKClient`** (Agent SDK), one per git worktree | Independent persistent sessions we can stream, steer, interrupt, and gate. |
| Auth | **Claude Max plan** via `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` | No API key, no per-token billing; one subscription covers supervisor + all workers. |
| Voice | **Local** — Whisper (STT) + Piper (TTS) + mic DoA | Keeps the whole stack on the Max plan with no extra paid AI account. STT/TTS are not LLMs. |
| Autonomy | **Plan-gated** (see §1) | User's chosen model. |
| Hardware | **Reachy Mini Lite** (USB → laptop) | Laptop runs workers + supervisor + voice + robot control. |

## 3. Architecture

```
You ⇄ (local STT/TTS) ⇄  Supervisor Agent (Claude, Max plan)
                                │  custom fleet-control tools (in-process MCP)
              ┌─────────────────┼──────────────────┐
        WorkerSession      WorkerSession      WorkerSession      ← Claude Code (Max plan)
        repoA/worktree-1   repoB              repoA/feature-x
              │                 │                  │
              └──── Event bus → FleetState (status, action, gate/question) ──┘
                                │
                       Embodiment mapper → Reachy motion (yaw / antennas / emotions)
```

### Components
1. **Reachy app shell** — `ReachyMiniApp.run(reachy_mini, stop_event)` hosts one asyncio loop
   running: voice I/O, session manager, supervisor agent, and the motion mapper.
   Plan: **fork the `conversation` template** to reuse its audio pipeline (16 kHz mic capture,
   DoA, speaker out) and its **LLM→queue→control-loop** motion decoupling + safe-return-to-pose,
   then **replace its OpenAI-Realtime brain module** with our Claude supervisor + local voice.
2. **Voice layer** — VAD-gated Whisper STT; Piper TTS to the robot speaker; head-turn toward
   the speaker via direction-of-arrival.
3. **SessionManager** — owns a dict of `WorkerSession`s. Each wraps a `ClaudeSDKClient` with its
   own `cwd` (worktree), system prompt, permission config, and an async task that consumes the
   session stream + `PreToolUse`/`PostToolUse` hooks to update status and emit events.
4. **FleetState** — *summarized* per session (id, label, cwd, phase ∈
   {planning, working, blocked, awaiting-approval, awaiting-clarification, done, error},
   last_action, last_message, pending question/permission). Summary-not-firehose keeps the
   supervisor's context small and the design scalable to N.
5. **Supervisor agent** — a `ClaudeSDKClient` whose custom tools are the fleet API:
   `list_sessions`, `get_status`, `spawn_session(plan, cwd)`, `send_to_session`,
   `interrupt_session`, `approve_gate` / `deny_gate`, `answer_clarification`, `summarize_all`.
   Mediates your voice commands and narrates back.
6. **Gate / escalation engine** — per-session `can_use_tool` callback consults the session's
   plan-derived gate policy; gated actions + ambiguity questions go on an **escalation queue**
   that Reachy voices; your answer is routed back to the originating session.
7. **Embodiment mapper** — translates FleetState → motion via the queue→control-loop pattern:
   `body_yaw` points at the active session (sessions laid out on a virtual arc); antennas as
   status/attention indicators (wiggle on awaiting-approval/clarification); emotion on all-green;
   "thinking" idle while working. `safelyReturnToPose` on leave.

## 4. Phased build (each phase demoable)

- **Phase 0 — Foundations.** Scaffold app via `reachy-mini-app-assistant create`; wire
  `CLAUDE_CODE_OAUTH_TOKEN`; robot connects, safe pose, basic motion test.
- **Phase 1 — Single worker voice loop.** Voice → one Claude Code session in a worktree →
  spoken responses + working/thinking motion. Plan-gated: give it a plan, it runs, asks on
  ambiguity by voice.
- **Phase 2 — Fleet abstraction.** Route the single session through SessionManager + FleetState
  + event bus; drive status motion from FleetState (still 1 session).
- **Phase 3 — N sessions + supervisor.** Supervisor agent with fleet-control tools; spawn
  multiple workers; body-yaw arc; approval/clarification escalation by voice.
- **Phase 4 — Polish.** Emotions/profiles, rate-limit handling, reconnect, error states,
  robustness on the gate engine.

## 5. Risks & honest caveats

- **Windows feasibility (needs verification first).** You're on Windows 11. Two things to
  confirm before committing: (a) Claude Code + `claude-agent-sdk` running cleanly on Windows
  (native vs WSL2); (b) the Reachy Lite **local media backend** (GStreamer mic/speaker) on
  Windows — the SDK notes WebRTC client support is Linux-first, so local audio on Windows is an
  open question. **Mitigation:** validate a "hello, robot moves + mic captures + speaker plays"
  spike on this exact machine in Phase 0 before building anything else. WSL2 or a Linux box may
  end up the better host.
- **Rate limits compound.** N concurrent Max-plan sessions share one subscription rate pool
  (rolling 5-hour / weekly caps). A few sessions is fine; a large heavy fleet will throttle.
  No dollar cost, but a throughput ceiling — surface it in FleetState.
- **Worktree isolation.** Parallel sessions on one repo collide → one git worktree (or clone)
  per worker. One-feature-per-worktree.
- **Supervisor latency.** An LLM turn per utterance adds latency to voice UX; keep the
  supervisor prompt lean and let it pull detail on demand rather than ingesting every token.

## 6. Clarifying questions (please fill in)

1. **Template base** — fork the `conversation` template (reuse audio + motion-queue, swap the
   brain) as proposed, or start from the `default` template and build voice fresh?
   → _Answer:_ ____________________ (recommend: fork `conversation`)

2. **Where do worker plans come from?** This defines the "solid plan, no ambiguity" gate. Do you
   author a `plan.md` per task and point a session at it, or should Reachy help you **draft and
   approve the plan by voice first**, then launch the worker?
   → _Answer:_ ____________________

3. **Voice engines** — local Whisper + Piper confirmed? Any preferred models, languages, or a
   wake word vs. always-listening (DoA) turn-taking?
   → _Answer:_ ____________________

4. **On app exit**, should running workers **keep going (detached)** or **pause/stop**?
   → _Answer:_ ____________________

5. **Host OS** — target **Windows native**, **WSL2**, or a **separate Linux machine** for the
   Lite + Claude Code? (Drives the Phase-0 feasibility spike.)
   → _Answer:_ ____________________

6. **Publish to HF?** This app is local-machine-specific (drives local Claude Code), so default
   is **keep local / unpublished**. Publish a sanitized version later? (yes/no)
   → _Answer:_ ____________________
