"""Phase-1 worker timing test: measure connect + per-turn latency (Max plan)."""
import sys
import time

import anyio

sys.path.insert(0, r"C:\Source\reacher\reachy_fleet_supervisor\src")
from reachy_fleet_supervisor.claude_brain import WorkerSession  # noqa: E402


async def main() -> None:
    worker = WorkerSession(r"C:\Source\reacher\.phase0\worker_sandbox", name="t")
    t0 = time.time()
    await worker.start()
    print(f"connect: {time.time() - t0:.2f}s")
    for q in ["Say hello in one short sentence.",
              "What is 2 plus 2? Answer in one short sentence."]:
        t0 = time.time()
        parts = []
        async for ev in worker.ask(q):
            if ev.kind == "text":
                parts.append(ev.text)
        print(f"[{time.time() - t0:.2f}s] {' '.join(parts).strip()}")
    await worker.stop()


if __name__ == "__main__":
    anyio.run(main)
