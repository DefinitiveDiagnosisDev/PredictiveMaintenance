#!/usr/bin/env python3
"""
Phase 8 – Live rules + Google Sheets alerts
Running on Arduino UNO Q Minima’s Linux side (UNO Q environment).

- Receives CAN frames from MCU via Bridge (can_frame_v0).
- Decodes using harness_demo.dbc (ECU_A_STATUS, ECU_B_STATUS, DCDC_STATUS).
- Applies simple harness rules:
    * HARNESS_A / HARNESS_B / HARNESS_C absolute voltage deltas vs DCDC
    * HARNESS_A_DRIFT early warning based on EWMA + trend of ΔA
- Pushes alerts & early warnings to a Google Sheet via Apps Script webhook.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cantools
from arduino.app_utils import App, Bridge
from urllib import request, error as urlerror


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path to our DBC file
DBC_PATH = Path(__file__).with_name("harness_demo.dbc")

# Google Apps Script web app URL (your working one)
GSHEETS_WEBHOOK_URL = (
    "gsheetURLgoeshere"  <--------------------------------------------!!!!!!!!!!!!
)

# How often to print stats
STATS_INTERVAL_SEC = 1.0

# Minimum time between pushes of the same (level, harness, rule_name)
SHEET_MIN_INTERVAL_SEC = 10.0

# Rule thresholds
DELTA_ALERT_V = 0.8          # V difference vs DCDC to flag a harness ALERT
DCDC_OK_MIN_V = 13.5
DCDC_OK_MAX_V = 14.7

# Drift (early warning) thresholds
EWMA_ALPHA = 0.2             # smoothing for ΔA EWMA
DRIFT_WINDOW_SEC = 20.0      # lookback window for trend
EARLY_DRIFT_DV_MIN = 0.3     # V – EWMA of ΔA
EARLY_DRIFT_TREND_MIN = 0.01 # V/s – slope of ΔA vs time


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# DBC / message references
db = None
msg_a = None
msg_b = None
msg_dcdc = None
SIG_NAME_A = "ECUA_Supply_Voltage"
SIG_NAME_B = "ECUB_Supply_Voltage"
SIG_NAME_DCDC = "DCDC_Output_Voltage"

# Map CAN ID -> cantools message object
id_to_msg: Dict[int, object] = {}

# Latest decoded voltages
latest_voltages: Dict[str, Optional[float]] = {
    "ECU_A": None,
    "ECU_B": None,
    "DCDC": None,
}

# Stats (per-second window)
frames_seen_window = 0
frames_decoded_window = 0

last_stats_time = time.time()

# Drift state for ECU A (ΔA = DCDC - ECU_A)
ewma_delta_a: Optional[float] = None
drift_history: List[Tuple[float, float]] = []  # list of (t_sec, delta_a)

# Sheet throttling: (level, harness, rule) -> last_sent_time
last_sheet_send: Dict[Tuple[str, str, str], float] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso_utc_now() -> str:
    """Return ISO8601 UTC string with 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fmt_v(v: Optional[float]) -> str:
    if v is None:
        return "nan"
    return f"{v:.2f}"


@dataclass
class RuleEvent:
    level: str      # "ALERT" or "EARLY"
    harness: str    # "HARNESS_A" / "HARNESS_B" / "HARNESS_C"
    rule_name: str  # e.g. "HARNESS_A_LOW_VS_DCDC" or "HARNESS_A_DRIFT"
    message: str
    ecu_a_v: float
    ecu_b_v: float
    dcdc_v: float


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

def update_drift(ts_mcu_ms: int, ecu_a_v: float, dcdc_v: float) -> Tuple[Optional[float], Optional[float]]:
    """
    Update EWMA + trend for ΔA = DCDC - ECU_A.

    Returns (ewma_delta_a, trendA) where trendA is V/s over the last DRIFT_WINDOW_SEC.
    """
    global ewma_delta_a, drift_history

    t_sec = ts_mcu_ms / 1000.0
    delta_a = dcdc_v - ecu_a_v

    # Update EWMA
    if ewma_delta_a is None:
        ewma_delta_a = delta_a
    else:
        ewma_delta_a = EWMA_ALPHA * delta_a + (1.0 - EWMA_ALPHA) * ewma_delta_a

    # Update history for trend
    drift_history.append((t_sec, delta_a))

    # Drop old points
    cutoff = t_sec - DRIFT_WINDOW_SEC
    drift_history = [(t, d) for (t, d) in drift_history if t >= cutoff]

    trend = None
    if len(drift_history) >= 2:
        t0, d0 = drift_history[0]
        t1, d1 = drift_history[-1]
        dt = t1 - t0
        if dt > 0:
            trend = (d1 - d0) / dt  # V/s

    return ewma_delta_a, trend


def evaluate_rules(ts_mcu_ms: int) -> List[RuleEvent]:
    """Apply harness rules to latest_voltages and return any triggered RuleEvent(s)."""
    a = latest_voltages["ECU_A"]
    b = latest_voltages["ECU_B"]
    d = latest_voltages["DCDC"]

    if a is None or b is None or d is None:
        return []

    events: List[RuleEvent] = []

    dA = d - a
    dB = d - b

    # -------------------------------------------------------------------
    # 1) Absolute delta rules vs DCDC (only if DCDC is in reasonable range)
    # -------------------------------------------------------------------
    if DCDC_OK_MIN_V <= d <= DCDC_OK_MAX_V:
        # HARNESS_A: ECU_A low vs DCDC, ECU_B OK
        if dA > DELTA_ALERT_V and dB <= DELTA_ALERT_V:
            msg = (
                f"ECU_A low vs DCDC (ΔA=+{dA:.2f} V, ΔB=+{dB:.2f} V, DCDC≈{d:.1f} V)"
            )
            events.append(RuleEvent(
                level="ALERT",
                harness="HARNESS_A",
                rule_name="HARNESS_A_LOW_VS_DCDC",
                message=msg,
                ecu_a_v=a,
                ecu_b_v=b,
                dcdc_v=d,
            ))

        # HARNESS_B: ECU_B low vs DCDC, ECU_A OK
        if dB > DELTA_ALERT_V and dA <= DELTA_ALERT_V:
            msg = (
                f"ECU_B low vs DCDC (ΔA=+{dA:.2f} V, ΔB=+{dB:.2f} V, DCDC≈{d:.1f} V)"
            )
            events.append(RuleEvent(
                level="ALERT",
                harness="HARNESS_B",
                rule_name="HARNESS_B_LOW_VS_DCDC",
                message=msg,
                ecu_a_v=a,
                ecu_b_v=b,
                dcdc_v=d,
            ))

        # HARNESS_C: both ECUs low vs DCDC
        if dA > DELTA_ALERT_V and dB > DELTA_ALERT_V:
            msg = (
                f"Both ECUs low vs DCDC (ΔA=+{dA:.2f} V, ΔB=+{dB:.2f} V, DCDC≈{d:.1f} V)"
            )
            events.append(RuleEvent(
                level="ALERT",
                harness="HARNESS_C",
                rule_name="HARNESS_C_BOTH_LOW_VS_DCDC",
                message=msg,
                ecu_a_v=a,
                ecu_b_v=b,
                dcdc_v=d,
            ))

    # -------------------------------------------------------------------
    # 2) Early drift rule for HARNESS_A based on EWMA + trend
    # -------------------------------------------------------------------
    ewma, trend = update_drift(ts_mcu_ms, a, d)

    if ewma is not None and trend is not None:
        if ewma >= EARLY_DRIFT_DV_MIN and trend >= EARLY_DRIFT_TREND_MIN:
            msg = (
                f"ΔA_ewma={ewma:.2f} V, trendA={trend:.4f} V/s, ΔB_ewma=0.00 V"
            )
            events.append(RuleEvent(
                level="EARLY",
                harness="HARNESS_A",
                rule_name="HARNESS_A_DRIFT",
                message=msg,
                ecu_a_v=a,
                ecu_b_v=b,
                dcdc_v=d,
            ))

    return events


# ---------------------------------------------------------------------------
# Google Sheets upload
# ---------------------------------------------------------------------------

def send_event_to_sheets(event: RuleEvent) -> None:
    """POST a single event to Google Apps Script, with simple throttling."""
    global last_sheet_send

    if not GSHEETS_WEBHOOK_URL:
        return

    # Throttle on key
    key = (event.level, event.harness, event.rule_name)
    now = time.time()
    last = last_sheet_send.get(key, 0.0)
    if now - last < SHEET_MIN_INTERVAL_SEC:
        return

    payload = {
        "level": event.level,
        "harness": event.harness,
        "rule_name": event.rule_name,
        "message": event.message,
        "ecu_a_v": event.ecu_a_v,
        "ecu_b_v": event.ecu_b_v,
        "dcdc_v": event.dcdc_v,
        "timestamp": iso_utc_now(),
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            GSHEETS_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as resp:
            status = resp.getcode()
            if status == 200:
                print(
                    f"[GSHEETS] Logged {event.level} {event.harness} {event.rule_name} to sheet",
                    flush=True,
                )
                last_sheet_send[key] = now
            else:
                body = resp.read(200).decode("utf-8", errors="ignore")
                print(
                    f"[GSHEETS] Non-200 response: {status} {body}",
                    flush=True,
                )
    except urlerror.HTTPError as e:
        body = e.read(200).decode("utf-8", errors="ignore")
        print(
            f"[GSHEETS] HTTPError {e.code}: {body}",
            flush=True,
        )
    except Exception as e:
        print(f"[GSHEETS] Error sending to sheet: {e}", flush=True)


# ---------------------------------------------------------------------------
# Bridge callback – from MCU
# ---------------------------------------------------------------------------

def can_frame_v0(version: int,
                 ts_mcu_ms: int,
                 can_id: int,
                 dlc: int,
                 b0: int, b1: int, b2: int, b3: int,
                 b4: int, b5: int, b6: int, b7: int,
                 flags: int) -> None:
    """
    Handler called by MCU via Bridge.notify('can_frame_v0', ...).

    Fields:
        version:   message version (we use 0)
        ts_mcu_ms: MCU timestamp from millis()
        can_id:    11-bit ID
        dlc:       Data length (0-8)
        b0..b7:    Data bytes
        flags:     bit0 = extended flag (unused here)
    """
    global frames_seen_window, frames_decoded_window, latest_voltages

    frames_seen_window += 1

    # Reconstruct data bytes (always 8 from the MCU, but dlc tells how many valid)
    data_bytes = bytes([b0, b1, b2, b3, b4, b5, b6, b7])

    msg = id_to_msg.get(can_id)
    if msg is None:
        # Unknown CAN ID (not in DBC)
        return

    try:
        # IMPORTANT: decode with full payload; DBC will only use bits it needs.
        decoded = msg.decode(data_bytes)
    except Exception:
        # Decoding failed, ignore this frame
        return

    frames_decoded_window += 1

    # Update latest voltages based on which message this is
    if msg is msg_a:
        latest_voltages["ECU_A"] = float(decoded[SIG_NAME_A])
    elif msg is msg_b:
        latest_voltages["ECU_B"] = float(decoded[SIG_NAME_B])
    elif msg is msg_dcdc:
        latest_voltages["DCDC"] = float(decoded[SIG_NAME_DCDC])
    else:
        # Shouldn't happen, but safe-guard
        return

    # Evaluate rules and print / push events
    events = evaluate_rules(ts_mcu_ms)
    for ev in events:
        prefix = f"[{ev.level}][{ev.harness}]"
        print(
            f"{prefix} ECU_A={ev.ecu_a_v:.2f} V ECU_B={ev.ecu_b_v:.2f} V DCDC={ev.dcdc_v:.2f} V | {ev.message}",
            flush=True,
        )
        send_event_to_sheets(ev)


# Register the handler with the Bridge
Bridge.provide("can_frame_v0", can_frame_v0)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def user_loop():
    """Periodic stats printer; rules run inside the callback."""
    global frames_seen_window, frames_decoded_window, last_stats_time

    now = time.time()
    if now - last_stats_time >= STATS_INTERVAL_SEC:
        print(
            f"[STATS] frames_seen={frames_seen_window} decoded={frames_decoded_window} "
            f"ECU_A={fmt_v(latest_voltages['ECU_A'])} V "
            f"ECU_B={fmt_v(latest_voltages['ECU_B'])} V "
            f"DCDC={fmt_v(latest_voltages['DCDC'])} V",
            flush=True,
        )
        frames_seen_window = 0
        frames_decoded_window = 0
        last_stats_time = now

    # Small sleep to keep CPU usage low
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_dbc():
    global db, msg_a, msg_b, msg_dcdc, id_to_msg

    print("=== PYTHON: PHASE 8 – Live rules + Google Sheets ===", flush=True)
    print(f"[INFO] Google Webhook URL set? {bool(GSHEETS_WEBHOOK_URL)}", flush=True)

    print(f"[DBC] Loading {DBC_PATH} ...", flush=True)
    db = cantools.database.load_file(str(DBC_PATH))

    # Find messages by name
    msg_a = db.get_message_by_name("ECU_A_STATUS")
    msg_b = db.get_message_by_name("ECU_B_STATUS")
    msg_dcdc = db.get_message_by_name("DCDC_STATUS")

    for m in db.messages:
        print(f"[DBC] - {m.name} (0x{m.frame_id:X}), signals={len(m.signals)}", flush=True)

    # Map CAN IDs
    id_to_msg = {
        msg_a.frame_id: msg_a,
        msg_b.frame_id: msg_b,
        msg_dcdc.frame_id: msg_dcdc,
    }


if __name__ == "__main__":
    init_dbc()
    App.run(user_loop=user_loop)

