#!/usr/bin/env python3
"""
Phase 6 – Offline CSV replay + manual decode (NO external libraries)

Usage:
    python3 replay_decode.py logs/phase6_log_YYYY-MM-DD_HH-MM-SS.csv
"""

import csv
import sys
from datetime import datetime

# --------------------------------------------------------------------
# Message & signal definitions (manual, instead of using cantools)
# --------------------------------------------------------------------

MESSAGES = {
    0x111: {
        "name": "ECU_A_STATUS",
        "signal_name": "ECUA_Supply_Voltage",
    },
    0x112: {
        "name": "ECU_B_STATUS",
        "signal_name": "ECUB_Supply_Voltage",
    },
    0x113: {
        "name": "DCDC_STATUS",
        "signal_name": "DCDC_Output_Voltage",
    },
}

def decode_voltage_from_bytes(b0: int, b1: int) -> float:
    raw = (b1 << 8) | b0      # little endian 16-bit
    return raw * 0.1          # factor = 0.1

def parse_int(field: str) -> int:
    field = field.strip()
    if field.startswith("0x") or field.startswith("0X"):
        return int(field, 16)
    try:
        return int(field, 10)
    except ValueError:
        return int(field, 16)

# --------------------------------------------------------------------
# Main replay logic
# --------------------------------------------------------------------

def replay(csv_path: str) -> None:
    print(f"[REPLAY] Opening CSV: {csv_path}")

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)

        total_rows = 0
        decoded_rows = 0

        for row in reader:
            total_rows += 1

            # timestamp
            ts_host_ms = int(row["ts_host_ms"])
            ts = datetime.fromtimestamp(ts_host_ms / 1000.0)

            # *** PATCH HERE: support hex CAN IDs ***
            can_id = parse_int(row["can_id"])

            if can_id not in MESSAGES:
                continue

            msg_def = MESSAGES[can_id]
            msg_name = msg_def["name"]
            sig_name = msg_def["signal_name"]

            b0 = parse_int(row["b0"])
            b1 = parse_int(row["b1"])

            voltage = decode_voltage_from_bytes(b0, b1)

            print(
                f"[REPLAY][DECODE] t={ts} "
                f"id=0x{can_id:03X} {msg_name:<14} "
                f"{sig_name} = {voltage:.1f} V"
            )

            decoded_rows += 1

    print()
    print(f"[REPLAY] Done. total_rows={total_rows}, decoded_rows={decoded_rows}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 replay_decode.py <csv_path>")
        sys.exit(1)

    csv_path = sys.argv[1]
    replay(csv_path)
