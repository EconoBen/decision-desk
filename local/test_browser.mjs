// Browser checks for the page in local mode: node --test local/test_browser.mjs
// Needs `npm install` in local/ and Google Chrome. Each test starts local/server.py on a free port with its own data.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright-core";

const HERE = path.dirname(new URL(import.meta.url).pathname);
let browser;
before(async () => { browser = await chromium.launch({ channel: process.env.DESK_TEST_BROWSER || "chrome" }); });
after(async () => { await browser?.close(); });

const later = new Date(Date.now() + 400 * 864e5).toISOString();
const inDays = (n) => new Date(Date.now() + n * 864e5 + 36e5).toISOString();
const choices = (n) => Array.from({ length: n }, (_, i) => ({ id: `c${i + 1}`, label: `Option ${i + 1}`, why: `Reason ${i + 1}, explained in a sentence or two so the card wraps.` }));
const open = (i) => ({
  title: `Open question ${i}: pick the rollout path for component ${i}`, what: `Decide how component ${i} ships.`, status: "open", order: i, area: "Release", due: later,
  choices: choices(3), recommended: "c1", changes: [{ choice: "c1", effect: "Ships on the usual path." }, { choice: "c2", effect: "Waits a week." }],
  background: Array.from({ length: 6 }, (_, k) => `Background point ${k + 1} for question ${i}, long enough to wrap onto a second line in the reading column.`),
  risks: ["A risk worth knowing about.", "Another risk."], checked: [{ source: "Ticket tracker", at: later, note: "re-read" }], createdAt: "2026-01-01T00:00:00Z",
});
const row = (i, status) => ({
  title: `${status} item ${i}`, status, order: 100 + i, choices: choices(2), agentNote: `Agent note for item ${i}.`, agentAt: "2026-01-02T00:00:00Z",
  agent: { kind: "session", id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`, label: "Test session" }, trail: [{ at: "2026-01-02T00:00:00Z", actor: "agent", action: "did a thing" }],
});

function writeJSON(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file + ".tmp", JSON.stringify(value));
  fs.renameSync(file + ".tmp", file);  // the way agents write: temp file, then rename
}

async function freePort() {
  return new Promise((res) => { const s = net.createServer().listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); }); });
}

// A desk with 4 open decisions and 26 rows (12 waiting, 14 done), served by local/server.py.
async function desk(t, { width = 1000 } = {}) {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), "desk-")), db = path.join(state, "db");
  writeJSON(`${db}/meta/settings.json`, { name: "Ben", cutoffAt: inDays(3), onsiteAt: inDays(9) });
  for (let i = 1; i <= 4; i++) writeJSON(`${db}/decisions/open-${i}.json`, open(i));
  for (let i = 1; i <= 26; i++) {
    const status = i <= 12 ? "waiting" : "done", id = `row-${String(i).padStart(2, "0")}`;
    writeJSON(`${db}/decisions/${id}.json`, row(i, status));
    writeJSON(`${db}/responses/${id}.json`, { choice: "c1", note: "", submittedAt: "2026-01-01T12:00:00.000Z" });
  }
  const port = await freePort();
  const serve = () => spawn("python3", [path.join(HERE, "server.py")], { env: { ...process.env, DECISION_DESK_STATE: state, DECISION_DESK_PORT: String(port) }, stdio: "ignore" });
  let server = serve();
  t.after(() => { server.kill(); fs.rmSync(state, { recursive: true, force: true }); });
  const restart = async () => { server.kill(); await new Promise((r) => setTimeout(r, 1500)); server = serve(); };
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/db/meta`); break; } catch { await new Promise((r) => setTimeout(r, 100)); } }
  const page = await browser.newPage({ viewport: { width, height: 700 } });
  t.after(() => page.close());
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.waitForSelector("#tiles .tile");
  const edit = (id, change) => { const f = `${db}/decisions/${id}.json`; writeJSON(f, change(JSON.parse(fs.readFileSync(f)))); };
  const top = (id) => page.evaluate((id) => Math.round(document.querySelector(`[data-id="${id}"]`).getBoundingClientRect().top), id);
  const scrollItemTo = (id, y) => page.evaluate(([id, y]) => scrollBy(0, document.querySelector(`[data-id="${id}"]`).getBoundingClientRect().top - y), [id, y]);
  const shows = (text) => page.waitForFunction((s) => document.querySelector("main").innerText.includes(s), text, { timeout: 5000, polling: 20 });
  return { page, db, edit, top, scrollItemTo, shows, restart };
}

test("scroll stays put after accept, send, and the store's confirmation", async (t) => {
  // The counts in the header change when an answer goes out; at this width that rewraps the header.
  const { page, top, scrollItemTo } = await desk(t, { width: 900 });
  const hud = () => page.$eval(".hud", (h) => h.offsetHeight);
  await scrollItemTo("open-3", 250);
  const y0 = await top("open-3"), hud0 = await hud();
  await page.click("#a-open-3");
  assert.equal(await top("open-3"), y0, "accept moved the decision");
  await page.click("#s-open-3");
  await page.waitForFunction(() => /Sent/.test(document.querySelector('[data-id="open-3"] .badge')?.textContent || ""), null, { timeout: 5000 });
  await page.waitForTimeout(1800);  // longer than a poll, so the store's copy has come back
  assert.notEqual(await hud(), hud0, "precondition: the header should rewrap at this width");
  assert.equal(await top("open-3"), y0, "the answered decision moved");
});

test("sending from inside an open decision keeps it in view", async (t) => {
  const { page, top } = await desk(t);
  await page.click("#h-open-2");
  await page.evaluate(() => scrollBy(0, document.querySelector("#sb-open-2").getBoundingClientRect().top - 500));
  await page.click("#c-open-2-c2");
  await page.click("#sb-open-2");
  await page.waitForFunction(() => /Sent/.test(document.querySelector('[data-id="open-2"] .badge')?.textContent || ""), null, { timeout: 5000 });
  await page.waitForTimeout(1800);
  const y = await top("open-2");
  assert.ok(y >= 0 && y < 700, `the answered decision is off screen (top ${y})`);
});

test("scroll stays put after a background update", async (t) => {
  const { page, edit, top, shows } = await desk(t);
  // Leave row-03 hanging off the top edge, then let an agent add work to it.
  await page.evaluate(() => scrollBy(0, document.querySelector('[data-id="row-03"]').getBoundingClientRect().top + 40));
  const reader = await page.evaluate(() => [...document.querySelectorAll("main [data-id]")].find((e) => e.getBoundingClientRect().top >= 0).dataset.id);
  const y0 = await top(reader);
  edit("row-03", (d) => ({ ...d, handlers: [{ kind: "subagent", id: "abcdef12-0000", label: "helper", status: "running", startedAt: later }], ticketNote: { label: "Ticket 42" } }));
  await shows("Ticket 42");
  await page.waitForTimeout(100);
  const y1 = await top(reader);
  assert.ok(Math.abs(y1 - y0) <= 1, `${reader} moved from ${y0} to ${y1} while you were reading it`);  // 1 px: sub-pixel rounding
});

test("an open panel stays open through 10 poll cycles", async (t) => {
  const { page, edit, shows } = await desk(t);
  await page.click("#h-open-1");
  await page.click("#ds-open-1-bg");
  await page.waitForTimeout(400);  // let the panel's own opening animation finish
  await page.evaluate(() => {
    window.panel = document.querySelector("#ds-open-1-bg").parentElement;
    window.panelEvents = 0;
    new MutationObserver(() => { if (!window.panel.isConnected || !window.panel.open) window.panelEvents++; }).observe(document.body, { subtree: true, childList: true, attributes: true });
    window.replays = 0;
    document.addEventListener("animationstart", () => window.replays++);
  });
  for (let i = 1; i <= 10; i++) {
    edit("row-10", (d) => ({ ...d, agentNote: `Background tick ${i}` }));
    await shows(`Background tick ${i}`);
  }
  const r = await page.evaluate(() => ({ same: window.panel.isConnected, open: window.panel.open, closedOrReplaced: window.panelEvents, replays: window.replays }));
  assert.deepEqual(r, { same: true, open: true, closedOrReplaced: 0, replays: 0 });
});

test("a panel opened just as an update lands stays open", async (t) => {
  const { page } = await desk(t);
  await page.click("#h-open-1");
  const open = await page.evaluate(() => { document.querySelector("#ds-open-1-rk").click(); render(); return document.querySelector("#ds-open-1-rk").parentElement.open; });
  assert.equal(open, true);
});

test("click to visible is under 150 ms", async (t) => {
  const { page } = await desk(t);
  await page.click("#a-open-4");
  const ms = await page.evaluate(() => new Promise((done) => {
    const t0 = performance.now();
    document.querySelector("#s-open-4").click();
    const check = () => /Sent/.test(document.querySelector('[data-id="open-4"] .badge')?.textContent || "") ? done(performance.now() - t0) : requestAnimationFrame(check);
    check();
  }));
  t.diagnostic(`click to visible: ${Math.round(ms)} ms`);
  assert.ok(ms < 150, `took ${Math.round(ms)} ms`);
});

test("an answer written by the server (a text reply) shows within 400 ms", async (t) => {
  const { page, db, edit } = await desk(t);
  const worst = [];
  for (const id of ["open-1", "open-2", "open-3"]) {
    const ms = page.evaluate((id) => new Promise((done) => {
      const t0 = performance.now();
      const check = () => /Sent/.test(document.querySelector(`[data-id="${id}"] .badge`)?.textContent || "") ? done(performance.now() - t0) : requestAnimationFrame(check);
      check();
    }), id);
    writeJSON(`${db}/responses/${id}.json`, { choice: "c1", note: "by text", submittedAt: new Date().toISOString() });
    edit(id, (d) => ({ ...d, status: "answered" }));
    worst.push(await ms);
  }
  t.diagnostic(`store write to visible: ${worst.map(Math.round).join(", ")} ms`);
  assert.ok(Math.max(...worst) < 400, `took ${worst.map(Math.round).join(", ")} ms`);
});

test("every action still works, and typing survives live updates", async (t) => {
  const { page, edit, shows } = await desk(t);
  const errors = []; page.on("pageerror", (e) => errors.push(String(e)));
  const store = (p) => page.evaluate((p) => fetch("/db/" + p).then((r) => r.json()), p);
  const settle = (fn) => page.waitForFunction(fn, null, { timeout: 5000 });
  // Typing in a note keeps its text, focus and caret while an agent writes elsewhere.
  await page.click("#h-open-2");
  await page.click("#n-open-2");
  await page.keyboard.type("half a thought");
  edit("row-05", (d) => ({ ...d, agentNote: "Agent wrote while you typed" }));
  await shows("Agent wrote while you typed");
  assert.deepEqual(await page.evaluate(() => { const a = document.activeElement; return [a.id, a.value, a.selectionStart]; }), ["n-open-2", "half a thought", 14]);
  await page.keyboard.type(", finished");
  await page.click("#c-open-2-c3");
  await page.click("#sb-open-2");
  await settle(() => /Sent/.test(document.querySelector('[data-id="open-2"] .badge')?.textContent || ""));
  assert.deepEqual(((await store("responses/open-2")).data), { ...(await store("responses/open-2")).data, choice: "c3", note: "half a thought, finished" });
  // Need more info
  await page.click("#mi-open-1"); await page.fill("#r-open-1", "which region?"); await page.click("#xs-open-1");
  await settle(() => /Sent/.test(document.querySelector('[data-id="open-1"] .badge')?.textContent || ""));
  assert.equal((await store("responses/open-1")).data.note, "Need more info: which region?");
  assert.equal((await store("decisions/open-1")).data.status, "answered");
  // Reply on a waiting row keeps the earlier choice
  await page.click("#ry-row-01"); await page.fill("#r-row-01", "ping them again"); await page.click("#xs-row-01");
  await settle(() => /ping them again/.test(document.querySelector('[data-id="row-01"]')?.textContent || ""));
  assert.deepEqual([(await store("responses/row-01")).data.choice, (await store("decisions/row-01")).data.status], ["c1", "answered"]);
  // Change that answer
  await page.click("#e-row-01"); await page.click("#c-row-01-c2"); await page.click("#sb-row-01");
  await settle(() => !document.querySelector("#sb-row-01"));
  assert.equal((await store("responses/row-01")).data.choice, "c2");
  // Reopen a done one: it becomes a question again
  await page.click("#ro-row-20"); await page.click("#xs-row-20");
  await settle(() => !!document.querySelector('#h-row-20'));
  assert.equal((await store("decisions/row-20")).data.status, "open");
  // History, and the sort control
  await page.click("#hi-row-02");
  assert.equal(await page.getAttribute("#hi-row-02", "aria-expanded"), "true");
  assert.ok(await page.isVisible("#hl-row-02"));
  await page.selectOption("#sortSel", "number");
  assert.equal(await page.$eval("#tiles [data-id]", (e) => e.dataset.id), "open-1");
  assert.deepEqual(errors, []);
});

test("a save that fails is undone on the page", async (t) => {
  const { page } = await desk(t);
  await page.click("#a-open-1");
  // The answer saves, then the server goes away: the status write and every re-read fail.
  let gone = false;
  await page.route("**/db/**", (route) => {
    if (!gone && route.request().method() === "PUT") { gone = true; return route.continue(); }
    return gone ? route.abort() : route.continue();
  });
  await page.click("#s-open-1");
  await page.waitForFunction(() => /Couldn't send/.test(document.querySelector("#status")?.textContent || ""), null, { timeout: 5000 });
  assert.equal(await page.$eval('[data-id="open-1"]', (e) => e.classList.contains("row")), false, "the failed answer still shows as sent");
  assert.equal(await page.isVisible("#s-open-1"), true, "the pick is gone, so it can't be retried");
});

test("the page carries on after the server restarts", async (t) => {
  const { page, edit, shows, restart } = await desk(t);
  await restart();
  edit("row-07", (d) => ({ ...d, agentNote: "Written after the restart" }));
  await shows("Written after the restart");
  assert.equal(await page.$eval("#notice", (n) => n.hidden), true, "the page shows a connection error");
  await page.click("#a-open-1");
  await page.click("#s-open-1");
  await page.waitForFunction(() => /Sent/.test(document.querySelector('[data-id="open-1"] .badge')?.textContent || ""), null, { timeout: 5000 });
});
