"""answer_gate — resume a gated fleet manager with the human's spoken answer (U15).

Decision #17: a manager's ``HUMAN_GATE`` is the escalation channel — Reachy
*speaks* the gate (the :class:`~reachy_fleet_supervisor.fleet.voice.FleetVoiceRenderer`
says "the tester needs you: …"), and **the spoken answer resumes the loop**. This
tool is that resume seam by voice: when the user answers a gate out loud ("tell
the tester to use port 8080", "yes, go ahead and push"), Reachy calls this tool
with the manager's name and the answer, and it steers that ``claude --bg`` manager
via the shared :class:`~reachy_fleet_supervisor.fleet.control.FleetController`
(``claude --resume <sessionId> -p <answer>`` — the U12 / decision #16 path), which
continues the manager's own conversation so its drive loop picks back up.

It is the voice counterpart of the dashboard's "send / approve a gate" control:
same :class:`FleetController`, same shared :class:`SessionManager`, so a gate
answered by voice and one answered in the browser behave identically.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, Dict

from reachy_fleet_supervisor.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class AnswerGate(Tool):
    """Resume a gated background manager with the user's spoken answer."""

    name = "answer_gate"
    description = (
        "Answer a manager that is waiting on you (a HUMAN_GATE) and RESUME its work, "
        "using the user's spoken reply. Use this whenever the user responds to a gate "
        "Reachy announced — e.g. Reachy said 'the tester needs you: which port?' and the "
        "user says 'use 8080', or 'yes, go ahead'. Provide the manager's name and the "
        "answer in plain language. This steers the durable background manager so its loop "
        "continues. Returns a short confirmation you then say out loud."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "The name (or short id) of the manager to answer — the one Reachy "
                    "said needs you, e.g. 'tester'."
                ),
            },
            "answer": {
                "type": "string",
                "description": (
                    "The user's answer to the gate, in plain language — what to tell the "
                    "manager so it can continue (e.g. 'use port 8080', 'yes, push it')."
                ),
            },
        },
        "required": ["name", "answer"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        name = (kwargs.get("name") or "").strip()
        answer = (kwargs.get("answer") or "").strip()
        if not name:
            return {"error": "the manager's name is required to answer its gate"}
        if not answer:
            return {"error": "an answer is required to resume the manager"}

        try:
            from reachy_fleet_supervisor.fleet.runtime import get_fleet_runtime
            from reachy_fleet_supervisor.fleet.control import FleetController

            runtime = get_fleet_runtime()
            controller = FleetController(runtime.session_manager)
            # send() shells out (claude --resume <id> -p) — blocking — so keep it
            # off the event loop.
            result = await asyncio.to_thread(controller.send, name, answer)

            # Nudge an immediate poll so the dashboard/body/voice reflect the
            # manager's resumed state without waiting for the next interval.
            try:
                await asyncio.to_thread(runtime.poller.poll_once)
            except Exception:  # noqa: BLE001
                logger.debug("post-answer poll_once failed; poller will catch up", exc_info=True)

            if not result.ok:
                logger.warning("answer_gate failed for %r: %s", name, result.detail)
                return {"error": result.detail or f"could not answer manager {name!r}"}

            logger.info("answer_gate: resumed manager '%s' (id=%s)", name, result.manager_id)
            return {
                "status": "resumed",
                "name": name,
                "id": result.manager_id,
                "reply": result.detail,
                "result": f"Answered {name} and resumed its work.",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("answer_gate failed")
            return {"error": str(e)}
