# Roadmap build journal

Append-only. One line per completed unit or human gate, newest at the bottom. A fresh agent (or the
orchestrator) can read this for history without opening code or diffs. Format:

`YYYY-MM-DD · U<id> · <CONTINUE|HUMAN_GATE|COMPLETE|FAILED> · <one-line what happened / what's needed>`

---

<!-- entries below -->
2026-06-27 · U1 · CONTINUE · Fleet config schema+loader (pydantic): reachy_fleet_supervisor.fleet — FleetConfig/ProjectConfig/GatePolicy/McpServerConfig + load_fleet_config (toml/json, strict). fleet.example.toml + 24 tests green. (Pre-existing unrelated failures: torch-less vision tests, 2 profile-loading tests.) Next: U2 FleetManager over `claude --bg`.
2026-06-28 · U2 · CONTINUE · FleetManager over `claude --bg`: fleet/manager.py — build_spawn_argv/parse_spawn_output/parse_agents_json (pure) + FleetManager.spawn/info/is_running/stop/rm/stop_and_remove + AgentInfo + list_agents. Short id from stdout is authoritative; real sessionId resolved from `claude agents --json`. 16 tests green incl. a REAL bg-session lifecycle test (spawn→assert→stop+rm, cleaned up). (Recovered: prior iteration left this work uncommitted.) Next: U3 SessionManager (track N + reconnect).
2026-06-28 · U3 · CONTINUE · SessionManager (track N + reconnect): fleet/session_manager.py — registry keyed by short id, lookup by id-or-name (ambiguous-name guard), spawn (dup-name reject) / adopt / forget / stop / stop_and_remove(_all). reconnect() + from_roster() re-adopt background-only rows from `claude agents --json` (predicate-scoped, idempotent); FleetManager.from_agent_info() added as the reconnect builder. 17 new tests green incl. a REAL reconnect-after-restart test (spawn → fresh SessionManager re-adopts from live roster → cleaned up). Fleet suite 57 green. Next: U4 FleetState model + poller (pub/sub).
2026-06-28 · U4 · CONTINUE · FleetState model + poller (pub/sub): fleet/state.py — ManagerSnapshot/FleetSnapshot (frozen, value-comparable; needs_attention, last_line, lookup by id/session/name) + FleetState (thread-safe single source of truth: apply() folds a poll, merges per-manager transcript ring buffers via overlap-dedup, notifies subscribers ONLY on change; subscribe/unsubscribe, fire_immediately, bad-subscriber isolation) + tail_logs (best-effort `claude logs <id>`) + FleetPoller (background thread, injectable agents_source/logs_source, predicate scope, error-tolerant loop, ctx-mgr). 25 new tests green incl. a REAL poll-populates-FleetState integration test (spawn bg → real poll lands it + fires subs → cleaned up; 0 stray agents). Fleet suite 82 green. Next: U5 status convention (manager emits, fleet reads) — decision #21; will extend FleetState.apply to merge each manager's own status.json/report line.
