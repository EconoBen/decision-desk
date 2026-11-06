// Spectrum bridge for the decision desk's text-message line. sms.py starts it; it opens no ports.
// stdin:  {"id": 1, "to": "+14155550100", "text": "..."}            send a text
// stdout: {"id": 1, "ok": true} or {"id": 1, "ok": false, "error"}  the send result
//         {"inbound": {"from": "+14155550100", "text": "..."}}      a direct text someone sent the desk
//         {"fatal": "..."}                                          the SDK could not start (bad credentials, say)
// Inbound comes over the SDK's own stream, so no webhook or public URL is needed.
// Credentials come from SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET in the environment. SPECTRUM_LINE is
// optional: it pins one dedicated line (Business plan). Free and Pro projects text from Photon's shared pool.
import { createInterface } from "node:readline";
import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

const line = process.env.SPECTRUM_LINE || undefined;
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
let app;
try {
  app = await Spectrum({ providers: [imessage.config()], options: { logLevel: "warn" } });
} catch (e) {
  out({ fatal: String(e?.message ?? e).slice(0, 200) });
  process.exit(1);
}
const im = imessage(app);

createInterface({ input: process.stdin })
  .on("line", async (raw) => {
    let req;
    try { req = JSON.parse(raw); } catch { return; }
    try {
      const space = await im.space.create(await im.user(req.to), line ? { phone: line } : undefined);
      await space.send(req.text);
      out({ id: req.id, ok: true });
    } catch (e) {
      out({ id: req.id, ok: false, error: String(e?.message ?? e).slice(0, 200) });
    }
  })
  .on("close", () => app.stop().finally(() => process.exit(0)));  // the desk server stopped or let go

for await (const [space, message] of app.messages) {
  if (message.direction === "outbound" || message.content.type !== "text") continue;
  const s = imessage(space);
  if (s.type !== "dm" || (line && s.phone !== line)) continue;  // direct texts only, to the pinned line if there is one
  out({ inbound: { from: message.sender?.id ?? "", text: message.content.text } });
}
