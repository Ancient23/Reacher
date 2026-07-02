"""Read-only web dashboard — the browser renderer of FleetState (U7).

One source of truth, many renderers (plan.md §3): the robot body, the headless
``fleet`` CLI and this web dashboard are all just *subscribers* to a single
:class:`~reachy_fleet_supervisor.fleet.state.FleetState`. This module exposes that
state over HTTP as a small **FastAPI** app — a 2–4 card grid plus a JSON poll
endpoint — so a browser can watch the fleet without ever interrupting a manager
(decision #21: read status, never steer the agent here; steering arrives in
Phase 3).

The app is a *factory* (:func:`create_dashboard_app`) so it can either run
standalone (``uvicorn``) or be mounted onto the Reachy Mini settings app
(``settings_app.mount("/fleet", create_dashboard_app(state))``). It reads from a
:class:`FleetState`; whoever owns the state runs the :class:`FleetPoller` that
keeps it fresh. For a standalone server with no background poller, pass an
``on_request`` hook (e.g. ``poller.poll_once``) to refresh on demand.

Routes:

- ``GET /``            — the HTML dashboard (server-rendered grid + live JS poll).
- ``GET /api/fleet``   — the current :class:`FleetSnapshot` as JSON (what the page
  polls, and what tests assert against).
- ``GET /healthz``     — liveness probe (``{"ok": true}``).
"""

from __future__ import annotations
import html
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .cli import _snapshot_dict  # single serialization source shared with the CLI
from .state import FleetState, FleetSnapshot, ManagerSnapshot
from .control import CONTROL_ACTIONS, FleetController


# Default browser poll cadence (ms). Matches the FleetPoller's ~2 s server cadence.
DEFAULT_POLL_MS = 2000

# A hook the dashboard calls before reading state (e.g. a one-shot poll on demand).
RefreshHook = Callable[[], None]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def snapshot_payload(snapshot: FleetSnapshot) -> dict[str, object]:
    """JSON-ready dict for *snapshot* (includes per-manager transcript buffers).

    Thin wrapper over the CLI's serializer so the dashboard JSON and the
    ``fleet status --json`` output never drift apart.
    """
    return _snapshot_dict(snapshot, include_transcript=True)


# ---------------------------------------------------------------------------
# HTML rendering (server-side; the page also re-renders live from /api/fleet)
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    """HTML-escape any value to safe text (``None`` → empty string)."""
    return html.escape("" if value is None else str(value))


def _render_controls(ident: str) -> str:
    """Render the per-card interactive control row (U12).

    A message box (send / approve a gate / reassign) plus pause / resume / kill.
    All buttons carry ``data-action``/``data-key`` so a single delegated grid
    listener handles them — surviving the live re-render that rebuilds the grid.
    """
    return f"""        <div class="controls" data-key="{ident}">
          <input class="msg" type="text" placeholder="message · approve · reassign…">
          <div class="btns">
            <button data-action="send" data-key="{ident}">send</button>
            <button data-action="pause" data-key="{ident}">pause</button>
            <button data-action="resume" data-key="{ident}">resume</button>
            <button data-action="kill" data-key="{ident}" class="danger">kill</button>
          </div>
          <div class="ctl-result" data-key="{ident}"></div>
        </div>"""


def _render_card(m: ManagerSnapshot, *, controls: bool = False) -> str:
    """Render one manager as a status card (with control row if *controls*)."""
    name = _esc(m.name or "<unnamed>")
    ident = _esc(m.id or m.session_id)
    state = _esc(m.state or "unknown")
    tool = _esc(m.status or "—")
    waiting = (
        f'<div class="waiting">waiting: {_esc(m.waiting_for)}</div>'
        if m.waiting_for
        else ""
    )
    report = ""
    if m.report is not None and m.report.normalized_state:
        report = f'<span class="report">{_esc(m.report.normalized_state)}</span>'
    headline = _esc(m.headline) if m.headline else "<em>no output yet</em>"
    transcript = "\n".join(_esc(line) for line in m.transcript) or "(no transcript)"
    attn = " attention" if m.needs_attention else ""
    state_slug = _esc((m.state or "unknown").lower())
    color_hex = _esc(m.color.hex) if m.color is not None else "transparent"
    color_name = _esc(m.color.name) if m.color is not None else ""
    control_row = ("\n" + _render_controls(ident)) if controls else ""
    return f"""      <article class="card{attn}" data-key="{ident}" style="--agent:{color_hex}">
        <header>
          <span class="name"><span class="dot" title="{color_name}"></span>{name}</span>
          <span class="id">{ident}</span>
        </header>
        <div class="row"><span class="badge state-{state_slug}">{state}</span>{report}
          <span class="tool">tool: {tool}</span></div>
        {waiting}
        <div class="headline">{headline}</div>
        <details class="transcript"><summary>transcript</summary><pre>{transcript}</pre></details>{control_row}
      </article>"""


def render_cards(snapshot: FleetSnapshot, *, controls: bool = False) -> str:
    """Render the full card grid (or an empty-state message)."""
    if not snapshot.managers:
        return '      <p class="empty">No managers running.</p>'
    return "\n".join(_render_card(m, controls=controls) for m in snapshot.managers)


def render_page(
    snapshot: FleetSnapshot, *, title: str, poll_ms: int, controls: bool = False
) -> str:
    """Render the complete dashboard HTML document for *snapshot*.

    When *controls* is true each card gains an interactive control row (send /
    pause / resume / kill, U12) and the page wires the buttons to
    ``POST /api/control/<action>``. When false, NO control markup or wiring is
    shipped (the Phase-2 read-only dashboard).
    """
    # The JS card() builder injects this per card. Single (not doubled) braces:
    # it is interpolated as a VALUE into the f-string, so `${id}` is the runtime
    # JS template var. Empty string in read-only mode → no control UI is emitted.
    card_controls_js = (
        '`\n'
        '        <div class="controls" data-key="${id}">\n'
        '          <input class="msg" type="text" placeholder="message · approve · reassign…">\n'
        '          <div class="btns">\n'
        '            <button data-action="send" data-key="${id}">send</button>\n'
        '            <button data-action="pause" data-key="${id}">pause</button>\n'
        '            <button data-action="resume" data-key="${id}">resume</button>\n'
        '            <button data-action="kill" data-key="${id}" class="danger">kill</button>\n'
        '          </div>\n'
        '          <div class="ctl-result" data-key="${id}"></div>\n'
        '        </div>`'
    ) if controls else '""'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    /* Full-bleed body: no max-width container squeezing the cards (human
       feedback 2026-07-01: cards must be WIDE). */
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1rem;
            max-width: none; width: auto; }}
    h1 {{ font-size: 1.2rem; margin: 0 0 .75rem; }}
    .muted {{ color: #888; font-size: .8rem; }}
    /* WIDE cards: a SINGLE full-width column by default so every card uses the
       whole viewport width (the transcript finally has room). Only on very wide
       screens (>= 1400px) do we allow a SECOND column, and even then each card is
       at least 640px — never the narrow ~400px packing that made cards small. */
    #grid {{ display: grid; gap: 1rem; width: 100%;
             grid-template-columns: minmax(0, 1fr); }}
    @media (min-width: 1400px) {{
      #grid {{ grid-template-columns: repeat(2, minmax(640px, 1fr)); }}
    }}
    .card {{ border: 1px solid #8884; border-left: 4px solid var(--agent, #8884);
             border-radius: 10px; padding: .75rem; }}
    .card.attention {{ border-color: #e0a400; border-left-color: var(--agent, #e0a400);
                       box-shadow: 0 0 0 2px #e0a40066; }}
    .card header {{ display: flex; justify-content: space-between; align-items: baseline; }}
    .name {{ font-weight: 600; }}
    .dot {{ display: inline-block; width: .6rem; height: .6rem; border-radius: 50%;
            margin-right: .35rem; background: var(--agent, #8888); vertical-align: baseline; }}
    .id {{ font-family: monospace; font-size: .75rem; color: #888; }}
    .row {{ display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin: .4rem 0; }}
    .badge {{ font-size: .7rem; text-transform: uppercase; padding: .1rem .4rem;
              border-radius: 6px; background: #8883; }}
    .state-working {{ background: #2e7d3266; }}
    .state-blocked, .state-failed {{ background: #c6282866; }}
    .state-done {{ background: #1565c066; }}
    .report {{ font-size: .7rem; font-weight: 600; }}
    .tool {{ font-size: .75rem; color: #888; }}
    .waiting {{ font-size: .8rem; color: #e0a400; }}
    .headline {{ margin: .4rem 0; }}
    .transcript pre {{ max-height: 24rem; overflow: auto; font-size: .72rem;
                       white-space: pre-wrap; word-break: break-word;
                       background: #8881; padding: .4rem; border-radius: 6px; }}
    .controls {{ margin-top: .5rem; border-top: 1px dashed #8884; padding-top: .5rem; }}
    .controls .msg {{ width: 100%; box-sizing: border-box; font-size: .78rem;
                      padding: .25rem .4rem; border: 1px solid #8886; border-radius: 6px; }}
    .controls .btns {{ display: flex; gap: .35rem; flex-wrap: wrap; margin-top: .35rem; }}
    .controls button {{ font-size: .72rem; padding: .2rem .5rem; border-radius: 6px;
                        border: 1px solid #8886; cursor: pointer; background: #8882; }}
    .controls button.danger {{ border-color: #c62828aa; color: #c62828; }}
    .controls button:disabled {{ opacity: .5; cursor: default; }}
    .ctl-result {{ font-size: .72rem; margin-top: .35rem; white-space: pre-wrap;
                   color: #888; max-height: 8rem; overflow: auto; }}
    .ctl-result.err {{ color: #c62828; }}
    .empty {{ color: #888; }}
  </style>
</head>
<body>
  <h1>{_esc(title)} <span class="muted" id="meta"></span></h1>
  <div id="grid">
{render_cards(snapshot, controls=controls)}
  </div>
  <script>
    const POLL_MS = {poll_ms};
    const CONTROLS = {str(controls).lower()};
    const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g,
      (c) => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}}[c]));
    function card(m) {{
      const id = esc(m.id || m.session_id);
      const state = esc(m.state || "unknown");
      const report = (m.report && m.report.state)
        ? `<span class="report">${{esc(String(m.report.state).toUpperCase())}}</span>` : "";
      const waiting = m.waiting_for ? `<div class="waiting">waiting: ${{esc(m.waiting_for)}}</div>` : "";
      const headline = m.headline ? esc(m.headline) : "<em>no output yet</em>";
      const transcript = (m.transcript && m.transcript.length)
        ? m.transcript.map(esc).join("\\n") : "(no transcript)";
      const colorHex = (m.color && m.color.hex) ? esc(m.color.hex) : "transparent";
      const colorName = (m.color && m.color.name) ? esc(m.color.name) : "";
      const controls = {card_controls_js};
      return `<article class="card${{m.needs_attention ? " attention" : ""}}" data-key="${{id}}" style="--agent:${{colorHex}}">
        <header><span class="name"><span class="dot" title="${{colorName}}"></span>${{esc(m.name || "<unnamed>")}}</span>
          <span class="id">${{id}}</span></header>
        <div class="row"><span class="badge state-${{esc((m.state||"unknown").toLowerCase())}}">${{state}}</span>${{report}}
          <span class="tool">tool: ${{esc(m.status || "\\u2014")}}</span></div>
        ${{waiting}}
        <div class="headline">${{headline}}</div>
        <details class="transcript"><summary>transcript</summary><pre>${{transcript}}</pre></details>${{controls}}
      </article>`;
    }}
    // Snapshot per-card UI state (open transcript + scroll) keyed by the STABLE
    // manager id (data-key), so the live re-render below never snaps an opened
    // <details> shut or jumps the scroll position the user is reading.
    function captureUi(grid) {{
      const ui = {{}};
      grid.querySelectorAll(".card").forEach((el) => {{
        const key = el.getAttribute("data-key");
        if (!key) return;
        const det = el.querySelector("details.transcript");
        const pre = el.querySelector("details.transcript pre");
        const msg = el.querySelector(".controls .msg");
        const res = el.querySelector(".ctl-result");
        ui[key] = {{
          open: det ? det.open : false,
          scroll: pre ? pre.scrollTop : 0,
          // Preserve a half-typed message + the last action result across polls.
          msg: msg ? msg.value : "",
          result: res ? res.textContent : "",
          resultErr: res ? res.classList.contains("err") : false,
        }};
      }});
      return ui;
    }}
    function restoreUi(grid, ui) {{
      grid.querySelectorAll(".card").forEach((el) => {{
        const key = el.getAttribute("data-key");
        const saved = key ? ui[key] : null;
        if (!saved) return;
        const det = el.querySelector("details.transcript");
        if (det) det.open = saved.open;
        const pre = el.querySelector("details.transcript pre");
        if (pre) pre.scrollTop = saved.scroll;
        const msg = el.querySelector(".controls .msg");
        if (msg && saved.msg) msg.value = saved.msg;
        const res = el.querySelector(".ctl-result");
        if (res && saved.result) {{ res.textContent = saved.result; res.classList.toggle("err", saved.resultErr); }}
      }});
    }}
    // Interactive controls (U12): one delegated listener on the stable #grid
    // element, so it keeps working after each live innerHTML re-render.
    async function control(action, key, card) {{
      const res = card.querySelector(`.ctl-result[data-key="${{key}}"]`);
      const msgEl = card.querySelector(".controls .msg");
      const message = msgEl ? msgEl.value : "";
      if (action === "send" && !message.trim()) {{
        if (res) {{ res.textContent = "type a message first"; res.classList.add("err"); }}
        return;
      }}
      if (action === "kill" && !confirm(`Kill ${{key}}? This stops and removes it.`)) return;
      const btns = card.querySelectorAll(".controls button");
      btns.forEach((b) => (b.disabled = true));
      if (res) {{ res.textContent = action + "…"; res.classList.remove("err"); }}
      try {{
        const r = await fetch(`api/control/${{action}}`, {{
          method: "POST", headers: {{"content-type": "application/json"}},
          body: JSON.stringify({{key, message}}),
        }});
        const data = await r.json();
        if (res) {{ res.textContent = data.detail || (data.ok ? "ok" : "failed"); res.classList.toggle("err", !data.ok); }}
        if (data.ok && action === "send" && msgEl) msgEl.value = "";
      }} catch (e) {{
        if (res) {{ res.textContent = "request failed"; res.classList.add("err"); }}
      }} finally {{
        btns.forEach((b) => (b.disabled = false));
        refresh();
      }}
    }}
    if (CONTROLS) {{
      document.getElementById("grid").addEventListener("click", (ev) => {{
        const btn = ev.target.closest("button[data-action]");
        if (!btn) return;
        const card = btn.closest(".card");
        control(btn.getAttribute("data-action"), btn.getAttribute("data-key"), card);
      }});
    }}
    async function refresh() {{
      try {{
        const r = await fetch("api/fleet", {{cache: "no-store"}});
        if (!r.ok) return;
        const data = await r.json();
        const grid = document.getElementById("grid");
        const ui = captureUi(grid);
        grid.innerHTML = data.managers.length
          ? data.managers.map(card).join("") : '<p class="empty">No managers running.</p>';
        restoreUi(grid, ui);
        const when = data.updated_at ? new Date(data.updated_at * 1000).toLocaleTimeString() : "never";
        document.getElementById("meta").textContent =
          `${{data.count}} manager(s) · updated ${{when}}`;
      }} catch (e) {{ /* keep last render on transient error */ }}
    }}
    refresh();
    setInterval(refresh, POLL_MS);
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_dashboard_app(
    state: FleetState,
    *,
    title: str = "Reachy Fleet",
    poll_ms: int = DEFAULT_POLL_MS,
    on_request: Optional[RefreshHook] = None,
    controller: Optional[FleetController] = None,
) -> FastAPI:
    """Build the FleetState dashboard as a :class:`FastAPI` app.

    *state* is the single source of truth this dashboard renders. *on_request*,
    if given, is called before every read (handy for a standalone server with no
    background poller — pass ``poller.poll_once`` to refresh on demand); when an
    external :class:`FleetPoller` already keeps *state* fresh, leave it ``None``.
    A failing ``on_request`` is swallowed so the dashboard still serves the last
    known snapshot.

    If *controller* is given the dashboard becomes **interactive** (U12): each
    card gets send / pause / resume / kill controls and a ``POST
    /api/control/<action>`` endpoint drives them through the shared
    :class:`~reachy_fleet_supervisor.fleet.control.FleetController`. Without a
    controller the dashboard stays read-only (decision #21 — the Phase-2
    behavior). Mount onto the Reachy Mini settings app or run via uvicorn.
    """
    app = FastAPI(title=title)
    controls_enabled = controller is not None

    def _current() -> FleetSnapshot:
        if on_request is not None:
            try:
                on_request()
            except Exception:  # noqa: BLE001 — never let a poll error break a read
                pass
        return state.snapshot

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:  # pragma: no cover - exercised via TestClient
        return HTMLResponse(
            render_page(_current(), title=title, poll_ms=poll_ms, controls=controls_enabled)
        )

    @app.get("/api/fleet")
    def api_fleet() -> JSONResponse:  # pragma: no cover - exercised via TestClient
        return JSONResponse(snapshot_payload(_current()))

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:  # pragma: no cover - exercised via TestClient
        return {"ok": True}

    if controller is not None:

        @app.post("/api/control/{action}")
        async def api_control(action: str, request: Request) -> JSONResponse:  # pragma: no cover - via TestClient
            if action not in CONTROL_ACTIONS:
                return JSONResponse(
                    {"ok": False, "action": action, "detail": f"unknown action {action!r}"},
                    status_code=404,
                )
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 — tolerate a missing/garbled body
                body = {}
            key = str(body.get("key", "")) if isinstance(body, dict) else ""
            message = str(body.get("message", "")) if isinstance(body, dict) else ""
            result = controller.apply(action, key, message)
            return JSONResponse(result.to_dict(), status_code=200 if result.ok else 400)

    return app
