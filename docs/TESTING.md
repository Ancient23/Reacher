# Reachy Fleet Supervisor — Testing Guide

Two layers of testing: an **automated software suite** (runs anywhere, no robot) and a set of
**hardware/voice checks** that a human runs on the physical robot (the agent cannot see or hear the
robot, so these are gated). This guide covers both.

For everyday usage see [`USER_GUIDE.md`](USER_GUIDE.md).

---

## 1. Software test suite (automated, no robot)

From `reachy_fleet_supervisor/`:

```powershell
uv run pytest
```

Write a test with every unit of work. The suite lives under `reachy_fleet_supervisor/tests/`:

- `tests/fleet/` — the fleet core: config, session manager, state/poller, control, drive loops,
  gate policy, voice/body renderers, vision, observer, resilience, dashboard, CLI, skill.
- `tests/tools/` — voice tools (`spawn_manager`, the background tool manager).
- `tests/` (top level) — packaging/licensing static checks, OpenAI Realtime handler.

### Run a subset

```powershell
uv run pytest tests/fleet/test_cli.py            # one file
uv run pytest tests/fleet/ -k voice              # by keyword
uv run pytest -q                                 # quiet
```

### Run pytest in the FOREGROUND — no shell timeout

Some fleet tests spawn **real** trivial `claude --bg` sessions and tear them down in a `finally`
block. If you wrap pytest in a shell `timeout` shorter than that teardown (or hard-kill it), Python
skips the `finally` and **orphans a background agent**. Always let the suite finish, or run the
specific files you need. After any run that touched real sessions, confirm none leaked:

```powershell
claude agents --json          # should show only your own interactive sessions
# clean up strays if any:
claude stop <id>; claude rm <id>
```

### Known pre-existing failures (not regressions)

These predate the current work and are unrelated to the fleet units — treat them as expected until
separately fixed:

- `tests/test_config_name_collisions.py` and `tests/test_external_loading.py` — 2 profile-loading
  tests (documented since fleet unit U1).
- `tests/vision/test_processors.py` — collection error when the optional `torch` extra isn't
  installed. Install with `uv sync --extra local_vision` (or `all_vision`) if you need the vision
  processors; otherwise it's out of scope.

A green run is currently **~560 passed** with only those exceptions.

### Lint / type-check

```powershell
uv run ruff check .
uv run mypy
```

---

## 2. Hardware & voice checks (human-run, gated)

The agent can't perceive the robot, so anything involving motion, audio/voice, or the camera is a
**HUMAN_GATE**: implemented and software-tested, then verified by a person. The authoritative,
copy-pasteable scripts live in `docs/roadmap/STATE.yaml` (`status.pending_gate`,
`status.pending_approval_gate`, and `status.deferred_gates`) — always check there for the current
gate. The recurring checklist:

### Setup

1. Power the robot on the **rear USB-C** port (front/USB-B browns out the motors).
2. From `reachy_fleet_supervisor/`: `./run.ps1`. Wait until voice is live and
   http://127.0.0.1:7860/fleet loads.

### A. Speak on completion + gate + voice-resume

1. By voice: *"Spawn a manager named greeter on C:/Source/reacher that creates hello.txt, then
   stop."* → **listen** for Reachy to speak the completion; **look** for the greeter card to remain
   on `/fleet` (not vanish).
2. By voice: *"Spawn a manager named builder on C:/Source/reacher that asks me a clarifying question
   and waits."* → Reachy should **speak the gate**, and because it's a *question*, phrase it as
   needing an **answer** (not "waiting on an action").
3. Answer by voice: *"Tell builder to use port 8080 and continue."* → Reachy calls `answer_gate`,
   confirms aloud, and the builder leaves the gated state on `/fleet`.

### B. Spawn confirm-before-kickoff (voice contract)

1. *"Spawn a manager called tester on C:/Source/reacher to <task>."* → Reachy reads back
   task/project (+ non-default modes) and asks to confirm — **must not spawn yet**. Say *"go ahead"*
   → it spawns and the card appears.
2. Skip: *"…, just do it"* → spawns immediately, no readback.
3. Correct: trigger a readback, then *"actually make it remote-control mode"* → it adjusts and reads
   back again (doesn't spawn on the uncorrected settings).
4. Explain: *"What are my options?"* → a short spoken explanation with an example request.

### C. Dashboard width + motion

- `/fleet`: all spawned managers show as cards, cards are **wide** (transcript readable) and expand
  and stay open across the ~2s polls.
- Watch the robot: at **idle** the breathing is small/calm; when it turns between managers or signals
  attention the gesture is **subtle and smooth** (small yaw, eases in/out) — not big or abrupt.

### D. Listens long enough

- Speak a slow sentence with a deliberate mid-sentence pause. Reachy should **wait** for you to
  finish before responding (turn-detection silence threshold ~1000ms).

### E. Vision (deferred robot re-tests)

- Screen source works without the robot: `uv run fleet look "what's on screen?" --source screen --keep`.
- Robot camera (needs the robot): `uv run fleet look "what do you see?" --source camera --keep` →
  expect a real captured frame + a plausible assessment. If it still reports the camera unavailable
  on a live robot, grab the console log lines containing `glance` (the observer logs
  `glance attempted` / `speaking glance`) so the failure stage is diagnosable.

**Reporting:** for each check, report **PASS**, or describe exactly what was/wasn't spoken/seen/felt
so the next fix pass can act on it.

---

## 3. Publish check (approval-gated)

Packaging/licensing/secrets are covered by `tests/test_packaging.py` (static checks of the
Apache-2.0 metadata, entry point, `NOTICE`, README frontmatter, and that `.env.example`/`.gitignore`
never leak secrets). The actual publish to Hugging Face is a **human approval gate** — never
automated. The exact publish commands live in `docs/roadmap/STATE.yaml`
`status.pending_approval_gate`.
