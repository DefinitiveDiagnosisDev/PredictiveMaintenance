#include <Arduino_RouterBridge.h>
#include <SPI.h>
#include <mcp2515.h>

// --------------------------------------------------
// CAN + MCP2515 config
// --------------------------------------------------
static const uint8_t PIN_CS = 10;
#define CAN_SPEED CAN_500KBPS
#define CAN_CLOCK MCP_16MHZ   // change to MCP_8MHZ if your board is 8MHz

MCP2515 mcp2515(PIN_CS);

// --------------------------------------------------
// Bridge CAN message v0 format
// --------------------------------------------------
struct CanFrameMsgV0 {
  uint8_t  version;     // = 0
  uint32_t timestamp;   // millis() at send
  uint32_t can_id;      // 11 or 29 bit
  uint8_t  dlc;
  uint8_t  data[8];
  uint8_t  flags;       // bit0 = extended
};

// --------------------------------------------------
// Bridge throughput limit (safe default)
// --------------------------------------------------
static const uint32_t MIN_BRIDGE_INTERVAL_MS = 0;   // 0ms = send every frame

// --------------------------------------------------
// Stats
// --------------------------------------------------
static uint32_t can_rx_count        = 0;
static uint32_t can_err_count       = 0;
static uint32_t bridge_sent_count   = 0;
static uint32_t bridge_skipped_rate = 0;

static uint32_t last_bridge_send_ms = 0;
static uint32_t last_stats_ms       = 0;

// --------------------------------------------------
// Convert MCP2515 frame -> Bridge msg
// --------------------------------------------------
bool build_msg_from_can_frame(CanFrameMsgV0 &msg, const struct can_frame &frame) {
  msg.version   = 0;
  msg.timestamp = millis();

  msg.can_id = frame.can_id & 0x1FFFFFFF;
  bool ext   = (frame.can_id & CAN_EFF_FLAG) != 0;

  msg.dlc = frame.can_dlc;
  for (uint8_t i = 0; i < 8; i++) {
    msg.data[i] = frame.data[i];
  }

  msg.flags = 0;
  if (ext) msg.flags |= 0x01;

  return true;
}

void setup() {
  Bridge.begin();     // must be first for Python bridge
  Monitor.begin();    // serial monitor
  delay(300);

  Monitor.println("MCU: Phase 5 baseline CAN→Bridge sender starting...");
  Monitor.print("MCU: MIN_BRIDGE_INTERVAL_MS=");
  Monitor.println(MIN_BRIDGE_INTERVAL_MS);

  // Init CAN
  SPI.begin();
  mcp2515.reset();
  mcp2515.setBitrate(CAN_SPEED, CAN_CLOCK);
  mcp2515.setNormalMode();
  Monitor.println("MCU: MCP2515 NORMAL mode");

  // Let Python App start fully
  delay(3000);

  last_stats_ms = millis();
}

void loop() {
  uint32_t now = millis();

  // ---- CAN Read ----
  struct can_frame frame;
  MCP2515::ERROR err = mcp2515.readMessage(&frame);

  if (err == MCP2515::ERROR_OK) {
    can_rx_count++;

    CanFrameMsgV0 msg;
    if (build_msg_from_can_frame(msg, frame)) {
      if (now - last_bridge_send_ms >= MIN_BRIDGE_INTERVAL_MS) {
        last_bridge_send_ms = now;

        // --- BRIDGE SEND ---
        Bridge.notify("can_frame_v0",
          msg.version,
          msg.timestamp,
          msg.can_id,
          msg.dlc,
          msg.data[0], msg.data[1], msg.data[2], msg.data[3],
          msg.data[4], msg.data[5], msg.data[6], msg.data[7],
          msg.flags
        );

        bridge_sent_count++;
      } else {
        bridge_skipped_rate++;
      }
    }

  } else if (err != MCP2515::ERROR_NOMSG) {
    can_err_count++;
  }

  // ---- Stats once per second ----
  if (now - last_stats_ms >= 1000) {
    uint32_t elapsed = now - last_stats_ms;
    uint32_t fps = (elapsed > 0) ? (bridge_sent_count * 1000UL / elapsed) : 0;

    Monitor.print("MCU[STATS] can_rx=");
    Monitor.print(can_rx_count);
    Monitor.print(" bridge_sent=");
    Monitor.print(bridge_sent_count);
    Monitor.print(" skipped=");
    Monitor.print(bridge_skipped_rate);
    Monitor.print(" fps=");
    Monitor.println(fps);

    can_rx_count        = 0;
    bridge_sent_count   = 0;
    bridge_skipped_rate = 0;
    last_stats_ms       = now;
  }

  delay(1);
}
