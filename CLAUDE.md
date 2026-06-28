# Reachy Fleet Supervisor — repo guide

Embodied voice assistant for the **Reachy Mini Lite** that fronts **Claude Code**. OpenAI **Realtime**
provides voice + personality; an `ask_claude_code` tool delegates real coding to **Claude Code on the
Max plan** (no Anthropic API key). It is growing into a multi-session **fleet supervisor** over many
Claude Code sessions. Full design + decisions live in **`plan.md`** (§4 phases, §6 decisions #7–21).
Phases 0–1 are shipped; Phases 2–5 are being built autonomously (see *Build automation* below).

## Layout
- `reachy_fleet_supervisor/` — the Reachy Mini app (forked from Pollen's conversation app).
  - `src/reachy_fleet_supervisor/openai_realtime.py` — the active voice brain (unmodified).
  - `src/reachy_fleet_supervisor/claude_brain.py` — `WorkerSession` (persistent Claude Code worker;
    the seam the fleet generalizes).
  - `src/reachy_fleet_supervisor/profiles/_reachy_fleet_supervisor_locked_profile/` — persona
    (`instructions.txt`), enabled tools (`tools.txt`), and `ask_claude_code.py`.
  - `run.ps1` — self-healing launcher (daemon + app; gates on the **motor backend**, not just HTTP).
- `plan.md` — design + phased roadmap.  `docs/roadmap/` — the autonomous-build drive contract.
- `.phase0/` — feasibility spikes (incl. `vision_ref/` = reusable Claude+Reachy vision app).

## Environment (hard-won — see plan.md §5)
- **Windows-native host** (PowerShell). `reachy_mini` bundles GStreamer + pycaw + Rust kinematics;
  the Lite enumerates on **COM3**. Pin **`reachy-mini==1.8.4`** (1.8.0 mDNS-misresolves the local
  daemon on Windows).
- **Motors brown out on the front / USB-B port → use the rear USB-C port** (validated stable: 540
  motion frames, 0 errors). A powered USB hub is the fallback.
- App venv: `reachy_fleet_supervisor/.venv` (uv, Python 3.12). Claude Code `2.1.195` at `~/.local/bin`.
- Coding stays on the **Claude Max plan**; the OpenAI key (Realtime) is **voice-only**.

## Build automation (drive contract)
This roadmap is built by `/drive-loop` running `.claude/commands/drive-roadmap.md` — **one PR-sized
unit per iteration**, state in **`docs/roadmap/STATE.yaml`**, history in `docs/roadmap/JOURNAL.md`.
**Software units run autonomously; anything needing the physical robot (motion / voice / vision) or
publishing is a `HUMAN_GATE`** relayed to the user.

## Testing
- Software: `cd reachy_fleet_supervisor && uv run pytest` — write tests with each unit.
- Fleet integration may spawn **real trivial `claude --bg` sessions**; observe via
  `claude agents --json`, and ALWAYS `claude stop`/`claude rm` them — never leave stray agents.
- Hardware behavior requires the robot powered on the **rear USB-C** port and is verified by the
  human (gated). The agent cannot see/hear the robot.

## Verified Claude Code fleet mechanics (2.1.195)
`claude --bg` = supervisor-hosted, durable, reconnectable (roster + `respawn`/`--resume`), observed
non-interactively via `claude agents --json` (`state`, `waitingFor`) + `claude logs <id>`; auto-isolates
into `.claude/worktrees/`. **Remote control does NOT compose with `--bg`** (separate must-stay-alive
process; needs full claude.ai OAuth). So `--bg` = fleet backbone, remote-control = optional steering
overlay. See the project memory `ref-claude-code-fleet-mechanics` for the full verified reference.
