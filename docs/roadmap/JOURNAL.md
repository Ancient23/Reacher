# Roadmap build journal

Append-only. One line per completed unit or human gate, newest at the bottom. A fresh agent (or the
orchestrator) can read this for history without opening code or diffs. Format:

`YYYY-MM-DD · U<id> · <CONTINUE|HUMAN_GATE|COMPLETE|FAILED> · <one-line what happened / what's needed>`

---

<!-- entries below -->
2026-06-27 · U1 · CONTINUE · Fleet config schema+loader (pydantic): reachy_fleet_supervisor.fleet — FleetConfig/ProjectConfig/GatePolicy/McpServerConfig + load_fleet_config (toml/json, strict). fleet.example.toml + 24 tests green. (Pre-existing unrelated failures: torch-less vision tests, 2 profile-loading tests.) Next: U2 FleetManager over `claude --bg`.
