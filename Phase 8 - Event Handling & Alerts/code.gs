function doGet(e) {
  return ContentService
    .createTextOutput("Webhook ready")
    .setMimeType(ContentService.MimeType.TEXT);
}

function doPost(e) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Data");
  const now = new Date();

  // If no body -> behave like the simple Ping test
  if (!e || !e.postData || !e.postData.contents) {
    sheet.appendRow([ now.toISOString(), "Ping (no body)" ]);
    return ContentService
      .createTextOutput("OK (no body)")
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // Try to parse JSON body
  let payload;
  try {
    payload = JSON.parse(e.postData.contents);
  } catch (err) {
    // If body isn’t JSON, log raw and return error
    sheet.appendRow([ now.toISOString(), "BAD_JSON", e.postData.contents ]);
    return ContentService
      .createTextOutput("BAD_JSON")
      .setMimeType(ContentService.MimeType.TEXT);
  }

  // Pull fields we care about (matching our UNO Q alerts)
  const level    = payload.level    || "";
  const harness  = payload.harness  || "";
  const ruleName = payload.rule_name || "";
  const message  = payload.message  || "";
  const ecuA     = payload.ecu_a_v ?? "";
  const ecuB     = payload.ecu_b_v ?? "";
  const dcdc     = payload.dcdc_v  ?? "";
  const ts       = payload.ts      || now.toISOString();

  // Append one row per event
  sheet.appendRow([
    ts,
    level,
    harness,
    ruleName,
    message,
    ecuA,
    ecuB,
    dcdc
  ]);

  return ContentService
    .createTextOutput("OK")
    .setMimeType(ContentService.MimeType.TEXT);
}
