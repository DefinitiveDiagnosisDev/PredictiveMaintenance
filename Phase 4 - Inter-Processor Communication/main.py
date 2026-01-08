import time
from arduino.app_utils import App, Bridge

print("Python: Phase 4.4 CAN frame THROUGHPUT receiver starting", flush=True)

# Stats
recv_count = 0
last_stats_time = time.time()
last_frame = None  # store last frame for sample display


def can_frame_v0(version,
                 timestamp,
                 can_id,
                 dlc,
                 b0, b1, b2, b3, b4, b5, b6, b7,
                 flags):
    """Handler called from MCU via Bridge.notify('can_frame_v0', ...)."""
    global recv_count, last_frame

    ext = bool(flags & 0x01)
    data = [b0, b1, b2, b3, b4, b5, b6, b7]

    recv_count += 1
    last_frame = (version, timestamp, can_id, dlc, data, ext)


Bridge.provide("can_frame_v0", can_frame_v0)


def loop():
    global recv_count, last_stats_time, last_frame

    now = time.time()
    if now - last_stats_time >= 1.0:
        elapsed = now - last_stats_time
        rate = int(recv_count / elapsed) if elapsed > 0 else 0

        print(
            f"Linux[STATS] recv_count={recv_count} approx_rate_fps={rate}",
            flush=True,
        )

        if last_frame is not None:
            version, timestamp, can_id, dlc, data, ext = last_frame
            print(
                f"Linux[LAST] v{version} ts={timestamp}ms "
                f"id=0x{can_id:X} "
                f"{'EXT' if ext else 'STD'} "
                f"dlc={dlc} "
                f"data={[hex(b) for b in data[:dlc]]}",
                flush=True,
            )

        recv_count = 0
        last_stats_time = now

    time.sleep(0.05)


App.run(user_loop=loop)
