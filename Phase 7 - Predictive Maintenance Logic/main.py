import os
import time
import collections
from datetime import datetime

import cantools
from arduino.app_utils import App, Bridge

print("=== PYTHON: PHASE 7 – Live rules + stats on UNO Q ===", flush=True)

# ---------------------------------------------------------------------------
# 1. DBC loading (robust path handling for harness_demo.dbc)
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
DBC_CANDIDATES = [
    os.path.join(HERE, "harness_demo.dbc"),
    os.path.join(HERE, "demo.dbc"),
    os.path.join(os.path.dirname(HERE), "harness_demo.dbc"),
]

dbc_path = None
for p in DBC_CANDIDATES:
    if os.path.exists(p):
        dbc_path = p
        break

if not dbc_path:
    raise FileNotFoundError(
        f"Could not find harness_demo.dbc; tried: {DBC_CANDIDATES}"
    )

print(f"[DBC] Using DBC: {dbc_path}", flush=True)
dbc = cantools.database.load_file(dbc_path)

# Build lookup from frame ID -> message
MESSAGE_BY_ID = {msg.frame_id: msg for msg in dbc.messages}

# IDs of interest from the DBC (should be 0x111, 0x112, 0x113)
ID_ECU_A = dbc.get_message_by_name("ECU_A_STATUS").frame_id
ID_ECU_B = dbc.get_message_by_name("ECU_B_STATUS").frame_id
ID_DCDC  = dbc.get_message_by_name("DCDC_STATUS").frame_id

# ---------------------------------------------------------------------------
# 2. Runtime state: decoded voltages and counters
# ---------------------------------------------------------------------------

# Last known voltages (None until seen)
V_A = None  # ECUA_Supply_Voltage
V_B = None  # ECUB_Supply_Voltage
V_D = None  # DCDC_Output_Voltage

frames_seen   = 0
frames_decoded = 0

# ---------------------------------------------------------------------------
# 3. Statistical layer: sliding window + EWMA for deltas
# ---------------------------------------------------------------------------

WINDOW_SECONDS = 60.0      # how much history we keep for trend analysis
STATS_PERIOD   = 1.0       # how often we print stats (seconds)
EWMA_ALPHA     = 0.1       # smoothing factor (0 < alpha <= 1)

# Keep tuples: (timestamp_s, deltaA, deltaB, v_dcdc)
window = collections.deque()

ewma_deltaA = None
ewma_deltaB = None

# Simple helper for time
def now_s() -> float:
    return time.time()

# ---------------------------------------------------------------------------
# 4. Rule-based thresholds
# ---------------------------------------------------------------------------

# Band of "normal" for DCDC
DCDC_NOMINAL_V = 14.0
DCDC_TOLERANCE = 0.5        # DCDC considered "about 14V" if within ±0.5

# Harness deltas
DELTA_WARN_V  = 0.5         # where we start to care
DELTA_ALERT_V = 1.0         # full harness fault

# Statistical thresholds
DRIFT_MIN_V   = 0.3         # EWMA above this means noticeable drift
TREND_MIN_VPS = 0.002       # minimal positive slope to call it "drifting"

# ---------------------------------------------------------------------------
# 5. CAN frame handler from the bridge
# ---------------------------------------------------------------------------

def can_frame_v0(version,
                 ts_mcu_ms,
                 can_id,
                 dlc,
                 b0, b1, b2, b3, b4, b5, b6, b7,
                 flags):
    """
    Handler called from MCU via Bridge.notify('can_frame_v0', ...).
    """
    global frames_seen, frames_decoded, V_A, V_B, V_D
    global ewma_deltaA, ewma_deltaB, window

    frames_seen += 1

    # Extended/standard flag (not critical here, but kept for completeness)
    ext = bool(flags & 0x01)

    # Data bytes
    data = bytes([b0, b1, b2, b3, b4, b5, b6, b7])

    # Find message in DBC (only decode if we know it)
    msg = MESSAGE_BY_ID.get(can_id)
    if msg is None:
        return  # ignore unknown frames

    try:
        decoded = msg.decode(data)
    except Exception as e:
        print(f"[DECODE][ERROR] id=0x{can_id:X}: {e}", flush=True)
        return

    frames_decoded += 1

    # Update voltages
    if msg.frame_id == ID_ECU_A:
        V_A = float(decoded.get("ECUA_Supply_Voltage"))
    elif msg.frame_id == ID_ECU_B:
        V_B = float(decoded.get("ECUB_Supply_Voltage"))
    elif msg.frame_id == ID_DCDC:
        V_D = float(decoded.get("DCDC_Output_Voltage"))

    # Once we have all three, update statistical state and run rules
    if V_A is not None and V_B is not None and V_D is not None:
        update_stats_and_rules()


Bridge.provide("can_frame_v0", can_frame_v0)

# ---------------------------------------------------------------------------
# 6. Core logic: rules + stats
# ---------------------------------------------------------------------------

def update_stats_and_rules():
    """
    Called each time we have all three voltages.
    Updates statistical state (window + EWMA) and runs:
      - Rule-based harness detection
      - Statistical early warnings
    """
    global ewma_deltaA, ewma_deltaB, window

    t = now_s()

    # Compute deltas
    deltaA = V_D - V_A
    deltaB = V_D - V_B

    # Maintain sliding window
    window.append((t, deltaA, deltaB, V_D))

    # Drop old entries
    cutoff = t - WINDOW_SECONDS
    while window and window[0][0] < cutoff:
        window.popleft()

    # Update EWMA
    if ewma_deltaA is None:
        ewma_deltaA = deltaA
        ewma_deltaB = deltaB
    else:
        ewma_deltaA = EWMA_ALPHA * deltaA + (1.0 - EWMA_ALPHA) * ewma_deltaA
        ewma_deltaB = EWMA_ALPHA * deltaB + (1.0 - EWMA_ALPHA) * ewma_deltaB

    # Run rule-based harness checks
    run_rule_based_harness_checks(deltaA, deltaB)

    # Run statistical early warnings
    run_statistical_checks(deltaA, deltaB)


def run_rule_based_harness_checks(deltaA, deltaB):
    """
    Rule-based harness localisation:
      - Harness A: ECU A low vs DCDC, ECU B OK
      - Harness B: ECU B low vs DCDC, ECU A OK
      - Harness C: both ECU A and ECU B low vs DCDC
    """
    # Check DCDC is near nominal so comparisons make sense
    dcdc_ok = abs(V_D - DCDC_NOMINAL_V) <= DCDC_TOLERANCE

    if not dcdc_ok:
        return  # skip localisation if reference itself is dodgy

    # Helper for "low compared to DCDC"
    low_A = deltaA >= DELTA_ALERT_V
    low_B = deltaB >= DELTA_ALERT_V

    # Harness A: only ECU A significantly lower than DCDC
    if low_A and not low_B:
        print(
            f"[ALERT][HARNESS_A] ECU_A={V_A:.2f} V ECU_B={V_B:.2f} V DCDC={V_D:.2f} V | "
            f"ECU_A low vs DCDC (ΔA={deltaA:+.2f} V, ΔB={deltaB:+.2f} V, DCDC≈{V_D:.1f} V)",
            flush=True,
        )

    # Harness B: only ECU B significantly lower than DCDC
    elif low_B and not low_A:
        print(
            f"[ALERT][HARNESS_B] ECU_A={V_A:.2f} V ECU_B={V_B:.2f} V DCDC={V_D:.2f} V | "
            f"ECU_B low vs DCDC (ΔA={deltaA:+.2f} V, ΔB={deltaB:+.2f} V, DCDC≈{V_D:.1f} V)",
            flush=True,
        )

    # Harness C: both ECUs low compared to DCDC
    elif low_A and low_B:
        print(
            f"[ALERT][HARNESS_C] ECU_A={V_A:.2f} V ECU_B={V_B:.2f} V DCDC={V_D:.2f} V | "
            f"Both ECUs low vs DCDC (ΔA={deltaA:+.2f} V, ΔB={deltaB:+.2f} V, DCDC≈{V_D:.1f} V)",
            flush=True,
        )


def run_statistical_checks(deltaA, deltaB):
    """
    Statistical early warnings using sliding window + EWMA + simple trend.
    We run three checks:
      - Harness A drift
      - Harness B drift
      - Harness C (both drifting) early warning
    """
    if len(window) < 2:
        return  # not enough data for slope

    t_first, dA_first, dB_first, vD_first = window[0]
    t_last,  dA_last,  dB_last,  vD_last  = window[-1]
    dt = t_last - t_first
    if dt <= 0:
        return

    trendA = (dA_last - dA_first) / dt
    trendB = (dB_last - dB_first) / dt

    # Simple DCDC stability check
    dcdc_stable = abs(vD_last - DCDC_NOMINAL_V) <= DCDC_TOLERANCE

    # --- Harness A drift: ECU A gradually dropping vs DCDC ---
    if ewma_deltaA is not None and dcdc_stable:
        if ewma_deltaA > DRIFT_MIN_V and trendA > TREND_MIN_VPS and abs(trendB) < TREND_MIN_VPS * 0.5:
            print(
                f"[EARLY][HARNESS_A_DRIFT] "
                f"ECU_A={V_A:.2f} V ECU_B={V_B:.2f} V DCDC={V_D:.2f} V | "
                f"ΔA_ewma={ewma_deltaA:.2f} V, trendA={trendA:.4f} V/s, ΔB_ewma={ewma_deltaB:.2f} V",
                flush=True,
            )

    # --- Harness B drift: ECU B gradually dropping vs DCDC ---
    if ewma_deltaB is not None and dcdc_stable:
        if ewma_deltaB > DRIFT_MIN_V and trendB > TREND_MIN_VPS and abs(trendA) < TREND_MIN_VPS * 0.5:
            print(
                f"[EARLY][HARNESS_B_DRIFT] "
                f"ECU_A={V_A:.2f} V ECU_B={V_B:.2f} V DCDC={V_D:.2f} V | "
                f"ΔB_ewma={ewma_deltaB:.2f} V, trendB={trendB:.4f} V/s, ΔA_ewma={ewma_deltaA:.2f} V",
                flush=True,
            )

    # --- Harness C drift: both gradually dropping vs DCDC ---
    if ewma_deltaA is not None and ewma_deltaB is not None and dcdc_stable:
        if (
            ewma_deltaA > DRIFT_MIN_V
            and ewma_deltaB > DRIFT_MIN_V
            and trendA > TREND_MIN_VPS
            and trendB > TREND_MIN_VPS
        ):
            print(
                f"[EARLY][HARNESS_C_DRIFT] "
                f"ECU_A={V_A:.2f} V ECU_B={V_B:.2f} V DCDC={V_D:.2f} V | "
                f"ΔA_ewma={ewma_deltaA:.2f} V, trendA={trendA:.4f} V/s; "
                f"ΔB_ewma={ewma_deltaB:.2f} V, trendB={trendB:.4f} V/s",
                flush=True,
            )


# ---------------------------------------------------------------------------
# 7. App loop: stats logging at low rate
# ---------------------------------------------------------------------------

last_stats_time = time.time()


def loop():
    global frames_seen, frames_decoded, last_stats_time

    now = time.time()
    if now - last_stats_time >= STATS_PERIOD:
        last_stats_time = now

        # Prepare simple status line
        va = f"{V_A:.2f} V" if V_A is not None else "N/A"
        vb = f"{V_B:.2f} V" if V_B is not None else "N/A"
        vd = f"{V_D:.2f} V" if V_D is not None else "N/A"

        print(
            f"[STATS] frames_seen={frames_seen} decoded={frames_decoded} "
            f"ECU_A={va} ECU_B={vb} DCDC={vd}",
            flush=True,
        )

        # Optionally reset counters for per-second fps view:
        frames_seen = 0
        frames_decoded = 0

    # Small sleep to avoid spinning
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# 8. Run app
# ---------------------------------------------------------------------------

App.run(user_loop=loop)
