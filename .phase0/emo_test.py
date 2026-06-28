"""Offline check of the emotion library + move.evaluate output (no robot)."""
import sys

sys.path.insert(0, r"C:\Source\reacher\reachy_fleet_supervisor\src")
from reachy_fleet_supervisor.emotions import EmotionLibrary  # noqa: E402

lib = EmotionLibrary()
print("load:", lib.load())
print("count:", len(lib.names))
print("sample names:", lib.names[:8])
for nm in ["welcoming1", "thoughtful1", "success1"]:
    mv = lib.get(nm)
    if mv is None:
        print(nm, "-> MISSING")
        continue
    dur = float(getattr(mv, "duration", 0.0))
    h, a, b = mv.evaluate(min(0.1, dur))
    print(f"{nm}: dur={dur:.2f}s | head={type(h).__name__}{getattr(h, 'shape', '')} "
          f"| antennas={a} | body_yaw={b}")
