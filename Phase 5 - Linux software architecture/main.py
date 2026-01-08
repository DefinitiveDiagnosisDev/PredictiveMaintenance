import os
import time

from arduino.app_utils import App, Bridge
import cantools

print("=== PYTHON: PHASE5 – CONTINUOUS DBC DECODE (ALL 3 ECUs) ===", flush=True)

# ------------------------------------------------------------
# 1) Load DBC once at startup
# ------------------------------------------------------------
HERE = os.path.dirname(__file__)
DBC_PATH = os.path.join(HERE, "harness_demo.dbc")

print(f"DBC PATH = {DBC_PATH} exists={os.path.exists(DBC_PATH)}", flush=True)
if not os.path.exists(DBC_PATH):
    raise SystemExit("FATAL: DBC file not found next to main.py")

dbc = cantools.database.load_file(DBC_PATH)

print("Loaded DBC OK:", flush=True)
for msg in dbc.messages:
    print(f"- {msg.name} (0x{msg.frame_id:X}), signals={len(msg.signals)}", flush=True)


# ------------------------------------------------------------
# 2) State for decoded voltages + stats
# ------------------------------------------------------------
# Store (timestamp_ms, decoded_dict) for each message
last_values = {
    "ECU_A_STATUS": None,
    "ECU_B_STATUS": None,
    "DCDC_STATUS": None,
}

recv_count = 0
last_stats_time = time.time()


# ------------------------------------------------------------
# 3) Bridge handler: decode ANY of the 3 messages
# ------------------------------------------------------------
def can_frame_v0(
    version: int,
    timestamp: int,
    can_id: int,
    dlc: int,
    b0: int, b1: int, b2: int, b3: int,
    b4: int, b5: int, b6: int, b7: int,
    flags: int,
):
    """Called from MCU via Bridge.notify('can_frame_v0', ...)."""
    global recv_count, last_values

    recv_count += 1

    # Rebuild bytes payload
    data_bytes = bytes([b0, b1, b2, b3, b4, b5, b6, b7])

    # Ignore messages we don't know in the DBC
    try:
        msg = dbc.get_message_by_frame_id(can_id)
    except KeyError:
        return

    try:
        decoded = msg.decode(data_bytes)
    except Exception as e:
        # If something goes wrong decoding, don't kill the app
        print(f"[WARN] decode failed for id=0x{can_id:X}: {e}", flush=True)
        return

    # Only track our 3 demo messages
    if msg.name in last_values:
        last_values[msg.name] = (timestamp, decoded)


# Register the handler with the Bridge
Bridge.provide("can_frame_v0", can_frame_v0)


# ------------------------------------------------------------
# 4) Main loop: once per second, print latest voltages
# ------------------------------------------------------------
def loop():
    global recv_count, last_stats_time, last_values

    now = time.time()
    if now - last_stats_time >= 1.0:
        elapsed = now - last_stats_time
        fps = int(recv_count / elapsed) if elapsed > 0 else 0

        print(f"[STATS] recv_count={recv_count} approx_rate_fps={fps}", flush=True)

        def fmt(name: str, key: str) -> str:
            entry = last_values.get(key)
            if entry is None:
                return f"{name}: (no data yet)"

            ts_ms, decoded = entry

            if key == "ECU_A_STATUS":
                v = decoded.get("ECUA_Supply_Voltage")
            elif key == "ECU_B_STATUS":
                v = decoded.get("ECUB_Supply_Voltage")
            else:  # DCDC_STATUS
                v = decoded.get("DCDC_Output_Voltage")

            if v is None:
                return f"{name}: (no value decoded) ts={ts_ms} ms"

            return f"{name}: {v:.2f} V (ts={ts_ms} ms)"

        print("Latest voltages:", flush=True)
        print("  " + fmt("ECU_A", "ECU_A_STATUS"), flush=True)
        print("  " + fmt("ECU_B", "ECU_B_STATUS"), flush=True)
        print("  " + fmt("DCDC", "DCDC_STATUS"), flush=True)
        print("--------------------------------------------------", flush=True)

        recv_count = 0
        last_stats_time = now

    # Small sleep to keep CPU + UI happy
    time.sleep(0.05)


App.run(user_loop=loop)
