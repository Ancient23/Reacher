"""Discover the recorded-moves / emotions API and list the available named moves."""
import importlib


def find(cls):
    for path in ["reachy_mini.motion", "reachy_mini.io", "reachy_mini",
                 "reachy_mini.motion.move", "reachy_mini.motion.recorded_moves"]:
        try:
            m = importlib.import_module(path)
        except Exception:
            continue
        if hasattr(m, cls):
            return getattr(m, cls), path
    return None, None


for sub in ["motion", "io"]:
    try:
        m = importlib.import_module("reachy_mini." + sub)
        print(sub, "->", [x for x in dir(m) if not x.startswith("_")])
    except Exception as e:  # noqa: BLE001
        print(sub, "ERR", e)

RM, where = find("RecordedMoves")
print("RecordedMoves at:", where)
if RM is not None:
    for repo in ["pollen-robotics/reachy-mini-emotions-library"]:
        try:
            rm = RM(repo)
            moves = rm.list_moves() if hasattr(rm, "list_moves") else [
                x for x in dir(rm) if not x.startswith("_")
            ]
            print(f"{repo} => {moves}")
        except Exception as e:  # noqa: BLE001
            print(repo, "load err", repr(e))
