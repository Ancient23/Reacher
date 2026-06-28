# Roadmap build journal

Append-only. One line per completed unit or human gate, newest at the bottom. A fresh agent (or the
orchestrator) can read this for history without opening code or diffs. Format:

`YYYY-MM-DD · U<id> · <CONTINUE|HUMAN_GATE|COMPLETE|FAILED> · <one-line what happened / what's needed>`

---

<!-- entries below -->
2026-06-27 · U1 · CONTINUE · Fleet config schema+loader (pydantic): reachy_fleet_supervisor.fleet — FleetConfig/ProjectConfig/GatePolicy/McpServerConfig + load_fleet_config (toml/json, strict). fleet.example.toml + 24 tests green. (Pre-existing unrelated failures: torch-less vision tests, 2 profile-loading tests.) Next: U2 FleetManager over `claude --bg`.
2026-06-28 · U2 · CONTINUE · FleetManager over `claude --bg`: fleet/manager.py — build_spawn_argv/parse_spawn_output/parse_agents_json (pure) + FleetManager.spawn/info/is_running/stop/rm/stop_and_remove + AgentInfo + list_agents. Short id from stdout is authoritative; real sessionId resolved from `claude agents --json`. 16 tests green incl. a REAL bg-session lifecycle test (spawn→assert→stop+rm, cleaned up). (Recovered: prior iteration left this work uncommitted.) Next: U3 SessionManager (track N + reconnect).
