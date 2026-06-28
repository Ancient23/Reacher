"""Power-stability test: ~22s of continuous gentle motion, counting control errors.
Uses os._exit to avoid any hanging SDK teardown (we only care about whether the
daemon survives the motion load = stable power)."""
import os
import time

import numpy as np

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

errors = 0
frames = 0
last_err = ""
try:
    mini = ReachyMini(connection_mode="localhost_only", media_backend="no_media")
except Exception as e:  # noqa: BLE001
    print(f"RESULT connect_error={e!r}", flush=True)
    os._exit(1)

try:
    mini.wake_up()
except Exception as e:  # noqa: BLE001
    print("wake_up:", e, flush=True)

t0 = time.time()
while time.time() - t0 < 22.0:
    t = time.time() - t0
    try:
        mini.set_target(
            head=create_head_pose(yaw=8.0 * np.sin(2 * np.pi * 0.4 * t),
                                   pitch=5.0 * np.sin(2 * np.pi * 0.3 * t), degrees=True),
            antennas=np.deg2rad([30.0 * np.sin(2 * np.pi * 0.5 * t),
                                 -30.0 * np.sin(2 * np.pi * 0.5 * t)]),
        )
        frames += 1
    except Exception as e:  # noqa: BLE001
        errors += 1
        last_err = str(e)
    time.sleep(0.04)

try:
    mini.set_target(head=create_head_pose(yaw=0, pitch=0, roll=0, degrees=True),
                    antennas=np.deg2rad([0.0, 0.0]))
except Exception:  # noqa: BLE001
    pass
print(f"RESULT frames={frames} errors={errors} last_err={last_err!r}", flush=True)
os._exit(0)
