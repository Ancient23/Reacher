"""Phase-0 robot motion + audio smoke test.

*** THIS PHYSICALLY MOVES THE ROBOT AND PLAYS SOUND ***  (gentle, small range)

What it does:
  1. Spawns the daemon itself (spawn_daemon=True) and connects.
  2. wake_up() to enable motors.
  3. Gentle antenna wiggle + a small (+/-15 deg) body-yaw turn. Head is NOT moved.
  4. Records ~2s from the Reachy mic, then plays it back through the Reachy speaker.
  5. Returns antennas/body to neutral and goes to sleep.

Run:
  C:\\Source\\reacher\\.phase0\\.venv\\Scripts\\python.exe C:\\Source\\reacher\\.phase0\\robot_test.py
"""
import time
import numpy as np
from reachy_mini import ReachyMini


def main() -> None:
    print("Connecting to daemon on localhost...")
    with ReachyMini(media_backend="default", localhost_only=True) as mini:
        mini.wake_up()
        time.sleep(0.5)

        # --- gentle motion (antennas + small body yaw; head untouched) ---
        print("Antenna wiggle + small body-yaw turn...")
        mini.goto_target(antennas=np.deg2rad([25.0, -25.0]), body_yaw=np.deg2rad(15.0), duration=1.0)
        time.sleep(0.3)
        mini.goto_target(antennas=np.deg2rad([-25.0, 25.0]), body_yaw=np.deg2rad(-15.0), duration=1.0)
        time.sleep(0.3)
        mini.goto_target(antennas=np.deg2rad([0.0, 0.0]), body_yaw=0.0, duration=1.0)

        # --- audio loopback: record ~2s, then play it back ---
        try:
            sr = mini.media.get_input_audio_samplerate()
            ch = mini.media.get_input_channels()
            print(f"Mic: {sr} Hz, {ch} ch. Recording ~2s — say something...")
            mini.media.start_recording()
            mini.media.start_playing()
            frames = []
            t0 = time.time()
            while time.time() - t0 < 2.0:
                s = mini.media.get_audio_sample()
                if s is not None and len(s) > 0:
                    frames.append(np.asarray(s))
                time.sleep(0.005)
            mini.media.stop_recording()

            if frames:
                audio = np.concatenate(frames, axis=0)
                print(f"Captured {audio.shape} samples. Playing back...")
                mini.media.push_audio_sample(audio)
                time.sleep(audio.shape[0] / float(sr) + 0.5)
            else:
                print("WARNING: no audio captured.")
            mini.media.stop_playing()
        except Exception as e:  # audio is best-effort; motion is the critical check
            print(f"Audio step error (non-fatal): {e!r}")

        print("Returning to sleep pose...")
        mini.goto_sleep()
    print("Done.")


if __name__ == "__main__":
    main()
