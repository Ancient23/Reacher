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

### Target architecture (Phase 2/3 — the fleet)
Generalize `ask_claude_code` (one worker) → a **SessionManager** of N worktree-isolated
`WorkerSession`s + a **FleetState** the Realtime host can query/steer, with the body signalling
which session needs attention (body-yaw arc, antenna status, approval/clarification by voice).
Borrow seer-agent's ADW *pattern* (worktree + isolated ports + state JSON) but **don't depend on
it** — the supervisor must be project-agnostic.

## 4. Phased build

- **Phase 0 — Foundations ✅** Windows-native confirmed; robot moves + mic + speaker + Claude
  Agent SDK on the Max plan all validated on hardware.
- **Phase 1 — Single-session assistant ✅ (shipped & committed)** OpenAI Realtime voice +
  personality + emotions/dances + `ask_claude_code` → Claude Code does real work, speaks results.
- **Phase 2 — Fleet abstraction.** SessionManager + FleetState + event bus; one→N worktree
  workers; status-driven body motion.
- **Phase 3 — N sessions + steering.** Realtime host observes/steers many workers; per-target
  worker environments (seer-agent → WSL2; Unreal/RoadRage → Windows); plan-gated approval/
  clarification by voice.
- **Phase 4 — Polish.** Rate-limit handling, robustness, profiles, and a 5090-native local mode
  (CUDA Whisper + neural TTS) as an OpenAI-free option.

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

## 7. How to run

1. Robot powered + USB-C connected; `setx OPENAI_API_KEY "sk-..."` (Realtime access).
2. From `reachy_fleet_supervisor/`, in a terminal: `.\run.ps1`.
3. Talk to Reachy — chat, watch it emote, or ask it to do real work
   (*"create a file called hello.txt that says hi"* → `ask_claude_code` → Claude Code → spoken result).
