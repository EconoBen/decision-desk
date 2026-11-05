// Spectrum bridge for the decision desk's text-message line. sms.py starts it; it opens no ports.
// stdin:  {"id": 1, "to": "+14155550100", "text": "..."}            send a text from SPECTRUM_LINE
// stdout: {"id": 1, "ok": true} or {"id": 1, "ok": false, "error"}  the send result
//         {"inbound": {"from": "+14155550100", "text": "..."}}      a text someone sent to SPECTRUM_LINE
// Inbound comes over the SDK's own stream, so no webhook or public URL is needed.
// Credentials come from SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET in the environment.
import { createInterface } from "node:readline";
import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

const line = process.env.SPECTRUM_LINE;
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const app = await Spectrum({ providers: [imessage.config()], options: { logLevel: "warn" } });
const im = imessage(app);

createInterface({ input: process.stdin })
  .on("line", async (raw) => {
    let req;
    try { req = JSON.parse(raw); } catch { return; }
    try {
      const space = await im.space.create(await im.user(req.to), { phone: line });
      await space.send(req.text);
      out({ id: req.id, ok: true });
    } catch (e) {
      out({ id: req.id, ok: false, error: String(e?.message ?? e).slice(0, 200) });
    }
  })
  .on("close", () => app.stop().finally(() => process.exit(0)));  // the desk server went away

for await (const [space, message] of app.messages) {
  if (message.direction === "outbound" || message.content.type !== "text") continue;
  const s = imessage(space);
  if (s.type !== "dm" || s.phone !== line) continue;  // only direct texts to the desk's own line
  out({ inbound: { from: message.sender?.id ?? "", text: message.content.text } });
}
