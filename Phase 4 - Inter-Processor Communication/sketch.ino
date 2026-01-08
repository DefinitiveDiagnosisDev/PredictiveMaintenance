#include <Arduino_RouterBridge.h>
#include <SPI.h>
#include <mcp2515.h>

// ----- CAN + MCP2515 config -----
static const uint8_t PIN_CS = 10;
#define CAN_SPEED CAN_500KBPS
#define CAN_CLOCK MCP_16MHZ      // adjust if your MCP2515 board runs at 8 MHz

MCP2515 mcp2515(PIN_CS);

// ----- Bridge message format v0 -----
struct CanFrameMsgV0 {
  uint8_t  version;     // = 0
  uint32_t timestamp;   // millis() at send
  uint32_t can_id;      // 11 or 29 bit
  uint8_t  dlc;
  uint8_t  data[8];
  uint8_t  flags;       // bit0 = extended
};

// ----- Bridge rate limiting -----
// We know from tests that ~280 fps is fine,
// but we keep a conservative cap of ~200 fps.
static const uint32_t MIN_BRIDGE_INTERVAL_MS = 0;  // 5ms -> ~200 fps max

// ----- Stats -----
static uint32_t can_rx_count        = 0;
static uint32_t can_err_count       = 0;
static uint32_t bridge_sent_count   = 0;
static uint32_t bridge_skipped_rate = 0;  // skipped due to rate limit

static uint32_t last_bridge_send_ms = 0;
static uint32_t last_stats_ms       = 0;

// Map MCP2515 can_frame -> our message struct
bool build_msg_from_can_frame(CanFrameMsgV0 &msg, const struct can_frame &frame) {
  msg.version   = 0;
  msg.timestamp = millis();

  // ID + extended flag
  msg.can_id = frame.can_id & 0x1FFFFFFF;
  bool ext   = (frame.can_id & CAN_EFF_FLAG) != 0;

  msg.dlc = frame.can_dlc;
  for (uint8_t i = 0; i < 8; i++) {
    msg.data[i] = frame.data[i];
  }

  msg.flags = 0;
  if (ext) {
    msg.flags |= 0x01;  // bit0 = extended frame
  }

  return true;
}

void setup() {
  // Bridge first, then Monitor (this order worked reliably before)
  Bridge.begin();
  Monitor.begin();
  delay(300);

  Monitor.println("MCU: Phase 4.x REAL CAN → Bridge starting");
  Monitor.println("MCU: Initialising MCP2515...");

  SPI.begin();
  mcp2515.reset();
  mcp2515.setBitrate(CAN_SPEED, CAN_CLOCK);
  mcp2515.setNormalMode();

  Monitor.println("MCU: MCP2515 configured (NORMAL mode)");
  Monitor.print("MCU: MIN_BRIDGE_INTERVAL_MS = ");
  Monitor.println(MIN_BRIDGE_INTERVAL_MS);

  // Let Python start fully
  delay(5000);

  last_stats_ms = millis();
}

void loop() {
  uint32_t now = millis();

  // ---- 1) Poll CAN ----
  struct can_frame frame;
  MCP2515::ERROR err = mcp2515.readMessage(&frame);

  if (err == MCP2515::ERROR_OK) {
    can_rx_count++;

    // Build bridge message
    CanFrameMsgV0 msg;
    if (build_msg_from_can_frame(msg, frame)) {
      // Rate limit bridge notifications
      if (now - last_bridge_send_ms >= MIN_BRIDGE_INTERVAL_MS) {
        last_bridge_send_ms = now;

        Bridge.notify("can_frame_v0",
                      msg.version,
                      msg.timestamp,
                      msg.can_id,
                      msg.dlc,
                      msg.data[0],
                      msg.data[1],
                      msg.data[2],
                      msg.data[3],
                      msg.data[4],
                      msg.data[5],
                      msg.data[6],
                      msg.data[7],
                      msg.flags);

        bridge_sent_count++;
      } else {
        // Received a CAN frame but chose not to bridge it
        bridge_skipped_rate++;
      }
    }

  } else if (err != MCP2515::ERROR_NOMSG) {
    // Any error other than "no message"
    can_err_count++;
  }

  // ---- 2) Once per second, print stats ----
  if (now - last_stats_ms >= 1000) {
    uint32_t elapsed = now - last_stats_ms;
    uint32_t mcu_bridge_rate =
      (elapsed > 0) ? (bridge_sent_count * 1000UL / elapsed) : 0;

    Monitor.print("MCU[STATS] can_rx=");
    Monitor.print(can_rx_count);
    Monitor.print(" bridge_sent=");
    Monitor.print(bridge_sent_count);
    Monitor.print(" bridge_skipped=");
    Monitor.print(bridge_skipped_rate);
    Monitor.print(" approx_bridge_fps=");
    Monitor.println(mcu_bridge_rate);

    // Reset window counters (not the total lifetime counts)
    can_rx_count        = 0;
    bridge_sent_count   = 0;
    bridge_skipped_rate = 0;
    last_stats_ms       = now;
  }

  // Small delay to avoid hammering CPU if bus is idle
  delay(1);
}
