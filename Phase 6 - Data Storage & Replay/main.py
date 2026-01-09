import os
import time
import csv
from datetime import datetime

import cantools
from arduino.app_utils import App, Bridge

# ============================================================
#  PHASE 7 – LIVE DBC DECODE + HARNESS A/B/C PREDICTIVE RULES
# ============================================================

print("=== PYTHON: LIVE HARNESS PREDICTIVE MONITOR ===", flush=True)

# ---------------------------
# 1. Load the DBC definition
# ---------------------------
HERE = os.path.dirname(__file__)
DBC_PATH = os.path.join(HERE, "harness_demo.dbc")

if not os.path.exists(DBC_PATH):
  raise SystemExit(f"FATAL: DBC not found at {DBC_PATH}")

dbc = cantools.database.load_file(DBC_PATH)

msg_ecu_a = dbc.get_message_by_name("ECU_A_STATUS")
msg_ecu_b = dbc.get_message_by_name("ECU_B_STATUS")
msg_dcdc  = dbc.get_message_by_name("DCDC_STATUS")

# Map CAN ID -> message
MSG_BY_ID = {
  msg_ecu_a.frame_id: msg_ecu_a,
  msg_ecu_b.frame_id: msg_ecu_b,
  msg_dcdc.frame_id:  msg_dcdc,
}

print("Loaded DBC messages:")
for m in (msg_ecu_a, msg_ecu_b, msg_dcdc):
  print(f" - {m.name} (0x{m.frame_id:X}), signals={len(m.signals)}", flush=True)

# -------------------------------------
# 2. Live state & predictive thresholds
# -------------------------------------

# Latest decoded voltages (V) – None until we see a frame
latest_volts = {
  "ECU_A": None,
  "ECU_B": None,
  "DCDC":  None,
}

# Some simple thresholds for the demo
NOMINAL_VOLT = 14.0     # "expected" system voltage
GOOD_DELTA_V = 0.3      # within this => considered OK / same
FAULT_DELTA_V = 0.8     # above this => considered suspicious

# Alert spam control
LAST_ALERT_TS = 0.0
ALERT_COOLDOWN_S = 1.0  # min seconds between printed alerts

# Stats for the console
frames_seen = 0
decoded_frames = 0
last_stats_ts = time.time()

# -------------------------------------
# 3. Simple CSV logging (raw + decoded)
# -------------------------------------

LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

log_filename = f"live_harness_log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv"
LOG_PATH = os.path.join(LOG_DIR, log_filename)

log_file = open(LOG_PATH, "w", newline="")
log_writer = csv.writer(log_file)

log_writer.writerow([
  "ts_iso",
  "ts_mcu_ms",
  "can_id",
  "is_extended",
  "dlc",
  "b0", "b1", "b2", "b3", "b4", "b5", "b6", "b7",
  "msg_name",
  "ECUA_Supply_Voltage",
  "ECUB_Supply_Voltage",
  "DCDC_Output_Voltage",
  "alert_code",
])
log_file.flush()

print(f"[INIT] Logging to {LOG_PATH}", flush=True)

# -------------------------------------
# 4. Harness health evaluation function
# -------------------------------------

def evaluate_harness_health():
  """
  Use latest_volts to infer harness health.

  Rules (simplified demo logic):

  - If DCDC ~ NOMINAL and:
      ECU_A far from DCDC, ECU_B near DCDC  => suspect Harness A
      ECU_B far from DCDC, ECU_A near DCDC  => suspect Harness B
      ECU_A and ECU_B far from DCDC        => suspect Harness C
  """
  global LAST_ALERT_TS

  vA = latest_volts["ECU_A"]
  vB = latest_volts["ECU_B"]
  vD = latest_volts["DCDC"]

  # Need all three for meaningful diagnostics
  if vA is None or vB is None or vD is None:
    return None  # no alert

  dA = vD - vA   # positive if ECU_A sees LOWER voltage than DCDC
  dB = vD - vB

  # How close is DCDC to nominal?
  dD_nom = abs(vD - NOMINAL_VOLT)

  # Helper: absolute differences
  abs_dA = abs(dA)
  abs_dB = abs(dB)

  alert_code = None
  reason = ""

  # Harness A suspect: A deviates, B matches DCDC
  if abs_dA > FAULT_DELTA_V and abs_dB <= GOOD_DELTA_V and dD_nom <= GOOD_DELTA_V:
    alert_code = "HARNESS_A"
    reason = (
      f"ECU_A low vs DCDC (ΔA={dA:+.2f} V, ΔB={dB:+.2f} V, DCDC≈{vD:.1f} V)"
    )

  # Harness B suspect: B deviates, A matches DCDC
  elif abs_dB > FAULT_DELTA_V and abs_dA <= GOOD_DELTA_V and dD_nom <= GOOD_DELTA_V:
    alert_code = "HARNESS_B"
    reason = (
      f"ECU_B low vs DCDC (ΔA={dA:+.2f} V, ΔB={dB:+.2f} V, DCDC≈{vD:.1f} V)"
    )

  # Harness C suspect: both ECUs off, DCDC still nominal
  elif abs_dA > FAULT_DELTA_V and abs_dB > FAULT_DELTA_V and dD_nom <= GOOD_DELTA_V:
    alert_code = "HARNESS_C"
    reason = (
      f"Both ECUs low vs DCDC (ΔA={dA:+.2f} V, ΔB={dB:+.2f} V, DCDC≈{vD:.1f} V)"
    )

  # No specific harness suspicion
  if not alert_code:
    return None

  # Throttle console spam
  now = time.time()
  if now - LAST_ALERT_TS >= ALERT_COOLDOWN_S:
    LAST_ALERT_TS = now
    print(
      f"[ALERT][{alert_code}] "
      f"ECU_A={vA:.2f} V ECU_B={vB:.2f} V DCDC={vD:.2f} V | {reason}",
      flush=True,
    )

  return alert_code

# -------------------------------------
# 5. Bridge handler: called per CAN frame
# -------------------------------------

def can_frame_v0(version,
                 ts_mcu_ms,
                 can_id,
                 dlc,
                 b0, b1, b2, b3, b4, b5, b6, b7,
                 flags):
  """
  Handler called from MCU via Bridge.notify('can_frame_v0', ...).

  - Logs the raw frame to CSV
  - Decodes voltages using the DBC
  - Updates latest_volts
  - Runs harness rules
  """
  global frames_seen, decoded_frames

  frames_seen += 1

  ts_iso = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
  is_extended = bool(flags & 0x01)
  data_bytes = bytes([b0, b1, b2, b3, b4, b5, b6, b7])

  msg = MSG_BY_ID.get(can_id)
  msg_name = ""
  ECUA_val = ""
  ECUB_val = ""
  DCDC_val = ""
  alert_code = ""

  if msg is not None:
    try:
      decoded = msg.decode(data_bytes)
      decoded_frames += 1

      if msg is msg_ecu_a:
        v = float(decoded["ECUA_Supply_Voltage"])
        latest_volts["ECU_A"] = v
        ECUA_val = f"{v:.3f}"
        msg_name = "ECU_A_STATUS"

      elif msg is msg_ecu_b:
        v = float(decoded["ECUB_Supply_Voltage"])
        latest_volts["ECU_B"] = v
        ECUB_val = f"{v:.3f}"
        msg_name = "ECU_B_STATUS"

      elif msg is msg_dcdc:
        v = float(decoded["DCDC_Output_Voltage"])
        latest_volts["DCDC"] = v
        DCDC_val = f"{v:.3f}"
        msg_name = "DCDC_STATUS"

      # Run the rules after any update
      alert_code = evaluate_harness_health() or ""

    except Exception as e:
      print(f"[DECODE ERROR] id=0x{can_id:X} err={e}", flush=True)

  # Log raw + decoded
  log_writer.writerow([
    ts_iso,
    ts_mcu_ms,
    f"0x{can_id:X}",
    int(is_extended),
    dlc,
    f"0x{b0:02X}", f"0x{b1:02X}", f"0x{b2:02X}", f"0x{b3:02X}",
    f"0x{b4:02X}", f"0x{b5:02X}", f"0x{b6:02X}", f"0x{b7:02X}",
    msg_name,
    ECUA_val,
    ECUB_val,
    DCDC_val,
    alert_code,
  ])
  log_file.flush()

Bridge.provide("can_frame_v0", can_frame_v0)

# -------------------------------------
# 6. Main loop – light stats only
# -------------------------------------

def loop():
  global last_stats_ts

  now = time.time()
  if now - last_stats_ts >= 1.0:
    last_stats_ts = now

    vA = latest_volts["ECU_A"]
    vB = latest_volts["ECU_B"]
    vD = latest_volts["DCDC"]

    def fmt(v):
      return "None" if v is None else f"{v:.2f} V"

    print(
      f"[STATS] frames_seen={frames_seen} decoded={decoded_frames} "
      f"ECU_A={fmt(vA)} ECU_B={fmt(vB)} DCDC={fmt(vD)}",
      flush=True,
    )

  # Small sleep to avoid busy-wait
  time.sleep(0.05)


App.run(user_loop=loop)
