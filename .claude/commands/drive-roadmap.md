---
description: Execute ONE PR-sized unit of the Reachy Fleet Supervisor roadmap, commit it, update state, and end with a ROADMAP_STATE sentinel. For a zero-context worker driven by /drive-loop.
---

You are a **fresh-context worker**. Execute **exactly ONE** roadmap unit, then stop. Durability lives
in committed git state — trust only the files named here, not any memory of prior turns.

## Ground-truth files
- `docs/roadmap/STATE.yaml` — backlog + a `status` block (`next_unit`, `in_progress`,
  `pending_gate`, `failure_count`). **The source of truth for what to do next.**
- `docs/roadmap/JOURNAL.md` — append-only history (one line per completed unit / gate).
- `plan.md` — the design (§4 phases, §6 decisions #7–21). Read only the parts your unit needs.
- `CLAUDE.md` — repo orientation, environment, how to run tests.

## Protocol (one iteration)
1. **Resume a pending gate first.** If `status.pending_gate` is set, the orchestrator will have
   relayed the human's response in your prompt:
   - **Confirmed PASS** → mark that unit `done`, clear `pending_gate`, append JOURNAL, commit, then
     continue to pick the next unit (step 2).
   - **Reported a PROBLEM** → fix it within that same unit (stays `in_progress`); if unfixable this
     iteration, bump `failure_count` and end `FAILED`.
   - **No human response yet** → re-emit the `pending_gate` steps verbatim and end `HUMAN_GATE`.
2. **Pick the unit:** `status.next_unit`, or the first `pending` unit whose `deps` are all `done`.
   Set it `in_progress` and commit that state bump.
3. **Do exactly that unit** per its `acceptance` and the matching plan.md design. Keep scope to ONE
   PR-sized unit — do NOT run ahead into the next.
4. **Verify by the unit's `gate` field:**
   - `software` → write/extend tests and run them GREEN in the app venv
     (`cd reachy_fleet_supervisor && uv run pytest`). For integration you MAY spawn REAL trivial
     `claude --bg` sessions (spawn → assert via `claude agents --json` → `claude stop`/`claude rm`).
     **Never leave stray background sessions.**
   - `hardware` → you CANNOT self-verify robot motion / voice / vision. Implement, add any
     software-level checks you can, then **HUMAN_GATE**: write copy-pasteable steps into
     `status.pending_gate` (power on the robot via the **rear USB-C** port, the exact command to run,
     and precisely what to LOOK / LISTEN for), append JOURNAL, output the steps, end `HUMAN_GATE`.
     Leave the unit `in_progress` — do NOT mark it done.
   - `approval` → outward-facing / irreversible (e.g. publishing). Implement up to the action, then
     HUMAN_GATE for explicit human go-ahead. **Never publish / push to a new remote autonomously.**
5. **Commit** code + tests + STATE + JOURNAL to the current branch with a clear message. The working
   tree must end **clean**.
6. **Advance state:** `done` (software) or `in_progress` + `pending_gate` (gate); set
   `status.next_unit`; bump `units_done`; reset `failure_count` on success.
7. **Sentinel — your LAST line, exactly one of:**
   - made mergeable progress, units remain → `ROADMAP_STATE: CONTINUE`
   - waiting on the human (hardware / approval) → `ROADMAP_STATE: HUMAN_GATE`
   - every unit `done` → `ROADMAP_STATE: COMPLETE`
   - could not complete the unit (tests red / blocked) → `ROADMAP_STATE: FAILED`

## Hard rules
- ONE unit per iteration. Re-read `STATE.yaml` every time; assume nothing from past turns.
- **Never fake a hardware/perceptual check as passing** — gate it.
- Never do anything outward-facing (publish, new remote, outbound comms) without an `approval` gate.
- Keep everything the next worker needs in committed STATE/JOURNAL so the loop can resume.
- Your final line MUST match `^[A-Z_]+_STATE: (CONTINUE|HUMAN_GATE|COMPLETE|FAILED)$`.
