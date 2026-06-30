"""Headless ``fleet`` CLI — the dev/test harness over the fleet core (U6).

A thin, scriptable front-end over the same building blocks the robot body and the
web dashboard use: :class:`~reachy_fleet_supervisor.fleet.session_manager.SessionManager`
(spawn / reconnect / stop) and :class:`~reachy_fleet_supervisor.fleet.state.FleetState`
+ :class:`~reachy_fleet_supervisor.fleet.state.FleetPoller` (the single source of
truth, polled once per invocation). Because background managers are durable and
supervisor-hosted, every invocation is stateless: it *reconnects* to the live
``claude agents --json`` roster, acts, and exits — there is no long-running
process to keep alive (plan.md §3/§4, decision #21: read status, never interrupt).

Commands::

    fleet start              # reconnect to durable bg managers and show the fleet
    fleet spawn <task> --name N [--cwd … --model … --mcp-config … -w]
    fleet status [key]       # one poll of FleetState (text or --json)
    fleet logs <key>         # recent `claude logs` tail for one manager
    fleet stop [key] [--rm]  # stop (and optionally remove) a manager, or --all
    fleet send <key> <msg>   # steer: message / approve a gate / reassign (U12)
    fleet pause <key>        # pause a manager (claude stop; conversation kept)
    fleet resume <key>       # resume a paused/exited manager (claude respawn)

``key`` is a manager's short id or its voice name. ``--json`` makes any read
command emit machine-readable output (what tests assert against). The read
commands (``status``/``logs``) never interrupt a running agent (decision #21);
the U12 control commands (``send``/``pause``/``resume``/``stop``) are the
explicit human *write* side.
"""

from __future__ import annotations
import sys
import json
import argparse
from typing import IO, Optional, Sequence

from .state import FleetState, FleetPoller, FleetSnapshot, ManagerSnapshot, tail_logs
from .control import ControlResult, FleetController
from .manager import DEFAULT_RUN_MODE, DEFAULT_PERMISSION_MODE, FleetManagerError
from .session_manager import SessionManager


# ---------------------------------------------------------------------------
# Rendering helpers (pure)
# ---------------------------------------------------------------------------


def _manager_dict(m: ManagerSnapshot, *, include_transcript: bool = False) -> dict[str, object]:
    """Serialize one :class:`ManagerSnapshot` to a JSON-ready dict."""
    data: dict[str, object] = {
        "id": m.id,
        "session_id": m.session_id,
        "name": m.name,
        "cwd": m.cwd,
        "kind": m.kind,
        "state": m.state,
        "status": m.status,
        "waiting_for": m.waiting_for,
        "needs_attention": m.needs_attention,
        "headline": m.headline,
        "last_line": m.last_line,
        "report": m.report.to_dict() if m.report is not None else None,
        "color": m.color.to_dict() if m.color is not None else None,
    }
    if include_transcript:
        data["transcript"] = list(m.transcript)
    return data


def _snapshot_dict(snap: FleetSnapshot, *, include_transcript: bool = False) -> dict[str, object]:
    """Serialize a whole :class:`FleetSnapshot` to a JSON-ready dict."""
    return {
        "updated_at": snap.updated_at,
        "count": len(snap),
        "managers": [_manager_dict(m, include_transcript=include_transcript) for m in snap.managers],
    }


def _format_manager(m: ManagerSnapshot, *, include_transcript: bool = False) -> str:
    """Render one manager as a short human-readable block."""
    flag = " (!) needs attention" if m.needs_attention else ""
    swatch = f"{m.color.swatch()} " if m.color is not None else ""
    color_tag = f" ·{m.color.name}" if m.color is not None else ""
    head = f"{swatch}{m.name or '<unnamed>'} [{m.id or m.session_id}]{color_tag}"
    bits = []
    if m.state:
        bits.append(f"state={m.state}")
    if m.status:
        bits.append(f"status={m.status}")
    if m.waiting_for:
        bits.append(f"waiting={m.waiting_for}")
    if m.report is not None and m.report.normalized_state:
        bits.append(f"report={m.report.normalized_state}")
    lines = [f"{head}  {' '.join(bits)}{flag}".rstrip()]
    if m.cwd:
        lines.append(f"  cwd: {m.cwd}")
    if m.headline:
        lines.append(f"  {m.headline}")
    if include_transcript and m.transcript:
        lines.append("  --- transcript ---")
        lines.extend(f"  | {line}" for line in m.transcript)
    return "\n".join(lines)


def _print_snapshot(
    snap: FleetSnapshot,
    out: IO[str],
    *,
    as_json: bool,
    include_transcript: bool = False,
) -> None:
    """Print a fleet snapshot to *out* as JSON or human-readable text."""
    if as_json:
        json.dump(_snapshot_dict(snap, include_transcript=include_transcript), out, indent=2)
        out.write("\n")
        return
    if not snap.managers:
        print("fleet: no managers", file=out)
        return
    print(f"fleet: {len(snap)} manager(s)", file=out)
    for m in snap.managers:
        print(_format_manager(m, include_transcript=include_transcript), file=out)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _poll_once(args: argparse.Namespace) -> FleetSnapshot:
    """Run one poll of a fresh FleetState over the live roster."""
    state = FleetState()
    poller = FleetPoller(
        state,
        claude_bin=args.claude_bin,
        include_all=getattr(args, "include_all", False),
        fetch_logs=getattr(args, "with_logs", False),
        status_dir=args.status_dir,
    )
    return poller.poll_once()


def _load_session_manager(args: argparse.Namespace) -> SessionManager:
    """Build a SessionManager reconnected to the live roster (durable sessions)."""
    return SessionManager.from_roster(claude_bin=args.claude_bin, include_all=True)


# ---------------------------------------------------------------------------
# Command handlers — each returns a process exit code
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Reconnect to durable background managers and show the current fleet."""
    sm = _load_session_manager(args)
    if not args.as_json:
        print(f"fleet: reconnected to {len(sm)} background manager(s)", file=out)
    snap = _poll_once(args)
    _print_snapshot(snap, out, as_json=args.as_json, include_transcript=args.with_logs)
    return 0


def cmd_spawn(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Spawn a new background manager and report its identifiers."""
    sm = _load_session_manager(args)  # so the unique-name check sees existing managers
    mgr = sm.spawn(
        args.task,
        name=args.name,
        cwd=args.cwd,
        run_mode=args.run_mode,
        model=args.model,
        permission_mode=args.permission_mode,
        mcp_configs=args.mcp_configs,
        worktree=args.worktree,
    )
    if args.as_json:
        json.dump(
            {
                "id": mgr.id,
                "name": mgr.name,
                "session_id": mgr.session_id,
                "cwd": mgr.cwd,
                "run_mode": mgr.run_mode,
            },
            out,
            indent=2,
        )
        out.write("\n")
    else:
        mode_note = "" if mgr.run_mode == "background" else f" [{mgr.run_mode}]"
        print(f"spawned '{mgr.name}' [{mgr.id}]{mode_note} (session {mgr.session_id})", file=out)
    return 0


def cmd_status(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Show the fleet (one poll), optionally filtered to a single manager."""
    snap = _poll_once(args)
    if args.key is not None:
        m = snap.get(args.key)
        if m is None:
            print(f"fleet: no manager matching {args.key!r}", file=err)
            return 2
        snap = FleetSnapshot(managers=(m,), updated_at=snap.updated_at)
    _print_snapshot(snap, out, as_json=args.as_json, include_transcript=args.with_logs)
    return 0


def cmd_logs(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Print a recent ``claude logs`` tail for one manager (read-only)."""
    sm = _load_session_manager(args)
    try:
        mgr = sm.get(args.key)
    except FleetManagerError as exc:  # ambiguous name
        print(f"fleet: {exc}", file=err)
        return 2
    if mgr is None:
        print(f"fleet: no manager matching {args.key!r}", file=err)
        return 2
    lines = tail_logs(mgr.id, lines=args.lines, claude_bin=args.claude_bin)
    if args.as_json:
        json.dump({"id": mgr.id, "name": mgr.name, "lines": lines}, out, indent=2)
        out.write("\n")
    else:
        for line in lines:
            print(line, file=out)
    return 0


def cmd_stop(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Stop (and optionally remove) one manager, or every tracked one with --all."""
    sm = _load_session_manager(args)
    if args.stop_all:
        targets = sm.managers
    else:
        if not args.key:
            print("fleet: stop needs a manager key or --all", file=err)
            return 2
        try:
            mgr = sm.get(args.key)
        except FleetManagerError as exc:  # ambiguous name
            print(f"fleet: {exc}", file=err)
            return 2
        if mgr is None:
            print(f"fleet: no manager matching {args.key!r}", file=err)
            return 2
        targets = [mgr]
    stopped: list[str] = []
    for mgr in targets:
        if args.remove:
            mgr.stop_and_remove()
        else:
            mgr.stop(check=False)
        stopped.append(mgr.id)
    verb = "stopped+removed" if args.remove else "stopped"
    if args.as_json:
        json.dump({"action": verb, "ids": stopped}, out, indent=2)
        out.write("\n")
    else:
        print(f"{verb} {len(stopped)} manager(s): {', '.join(stopped) or '(none)'}", file=out)
    return 0


def _emit_control_result(result: ControlResult, out: IO[str], err: IO[str], *, as_json: bool) -> int:
    """Print a :class:`ControlResult` and return its process exit code."""
    if as_json:
        json.dump(result.to_dict(), out, indent=2)
        out.write("\n")
    elif result.ok:
        print(result.detail or f"{result.action}: ok", file=out)
    else:
        print(f"fleet: {result.detail}", file=err)
    return 0 if result.ok else 2


def cmd_send(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Steer one manager: send a message / approve a gate / reassign it.

    Uses the decision #16 path (``claude --resume <sessionId> -p <message>``) for
    background managers; prints the manager's reply.
    """
    sm = _load_session_manager(args)
    controller = FleetController(sm)
    result = controller.send(args.key, args.message)
    return _emit_control_result(result, out, err, as_json=args.as_json)


def cmd_pause(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Pause one manager (``claude stop``; conversation kept, resumable)."""
    sm = _load_session_manager(args)
    result = FleetController(sm).pause(args.key)
    return _emit_control_result(result, out, err, as_json=args.as_json)


def cmd_resume(args: argparse.Namespace, out: IO[str], err: IO[str]) -> int:
    """Resume one paused/exited manager (``claude respawn``)."""
    sm = _load_session_manager(args)
    result = FleetController(sm).resume(args.key)
    return _emit_control_result(result, out, err, as_json=args.as_json)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the ``fleet`` argument parser."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--claude-bin", default="claude", help="Claude Code CLI binary (default: claude).")
    parent.add_argument(
        "--status-dir",
        default=None,
        help="Fleet status directory (manager status.json files); default ~/.reachy_fleet/status.",
    )
    parent.add_argument("--json", dest="as_json", action="store_true", help="Emit machine-readable JSON.")

    parser = argparse.ArgumentParser(prog="fleet", description="Headless Reachy fleet supervisor CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", parents=[parent], help="Reconnect to durable managers and show the fleet.")
    p_start.add_argument("--all", dest="include_all", action="store_true", help="Include completed/stopped sessions.")
    p_start.add_argument("--logs", dest="with_logs", action="store_true", help="Include transcript ring buffers.")
    p_start.set_defaults(func=cmd_start)

    p_spawn = sub.add_parser("spawn", parents=[parent], help="Spawn a new background manager.")
    p_spawn.add_argument("task", help="The task/prompt for the manager.")
    p_spawn.add_argument("--name", required=True, help="Unique voice/handle name for the manager.")
    p_spawn.add_argument("--cwd", default=None, help="Working directory to spawn in.")
    p_spawn.add_argument("--model", default=None, help="Model override.")
    p_spawn.add_argument(
        "--run-mode",
        dest="run_mode",
        choices=["background", "remote-control"],
        default=DEFAULT_RUN_MODE,
        help=(
            f"How the manager is hosted (default: {DEFAULT_RUN_MODE}). "
            "'background' = durable claude --bg (roster-visible); 'remote-control' = a "
            "claude.ai/mobile-steerable session (separate must-stay-alive process)."
        ),
    )
    p_spawn.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help=f"Permission mode (default: {DEFAULT_PERMISSION_MODE}).",
    )
    p_spawn.add_argument(
        "--mcp-config",
        dest="mcp_configs",
        action="append",
        default=[],
        help="MCP config path (repeatable).",
    )
    p_spawn.add_argument(
        "-w",
        "--worktree",
        nargs="?",
        const=True,
        default=None,
        help="Run in a git worktree (optionally named).",
    )
    p_spawn.set_defaults(func=cmd_spawn)

    p_status = sub.add_parser("status", parents=[parent], help="Show fleet status (one poll).")
    p_status.add_argument("key", nargs="?", default=None, help="Optional manager id/name to filter to.")
    p_status.add_argument("--all", dest="include_all", action="store_true", help="Include completed/stopped sessions.")
    p_status.add_argument("--logs", dest="with_logs", action="store_true", help="Include transcript ring buffers.")
    p_status.set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", parents=[parent], help="Print recent logs for a manager.")
    p_logs.add_argument("key", help="Manager id or name.")
    p_logs.add_argument("--lines", type=int, default=40, help="How many recent lines to show (default: 40).")
    p_logs.set_defaults(func=cmd_logs)

    p_stop = sub.add_parser("stop", parents=[parent], help="Stop (and optionally remove) a manager.")
    p_stop.add_argument("key", nargs="?", default=None, help="Manager id or name (omit with --all).")
    p_stop.add_argument("--all", dest="stop_all", action="store_true", help="Stop every tracked manager.")
    p_stop.add_argument("--rm", dest="remove", action="store_true", help="Also remove from the roster (claude rm).")
    p_stop.set_defaults(func=cmd_stop)

    p_send = sub.add_parser(
        "send",
        parents=[parent],
        help="Steer a manager: send a message / approve a gate / reassign (claude --resume -p).",
    )
    p_send.add_argument("key", help="Manager id or name.")
    p_send.add_argument("message", help="The message/instruction to inject into the manager's conversation.")
    p_send.set_defaults(func=cmd_send)

    p_pause = sub.add_parser("pause", parents=[parent], help="Pause a manager (claude stop; conversation kept).")
    p_pause.add_argument("key", help="Manager id or name.")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", parents=[parent], help="Resume a paused/exited manager (claude respawn).")
    p_resume.add_argument("key", help="Manager id or name.")
    p_resume.set_defaults(func=cmd_resume)

    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    out: Optional[IO[str]] = None,
    err: Optional[IO[str]] = None,
) -> int:
    """Parse *argv* and dispatch one command; return a process exit code.

    *out*/*err* default to ``sys.stdout``/``sys.stderr`` but are injectable so
    tests can capture output without touching the process streams.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args, out, err))
    except FleetManagerError as exc:
        print(f"fleet: {exc}", file=err)
        return 1
    except (KeyError, ValueError) as exc:
        print(f"fleet: {exc}", file=err)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
