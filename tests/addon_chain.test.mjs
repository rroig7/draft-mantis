// End-to-end test of the Firefox add-on scripts against a running Draft Mantis server, with a fake
// ESPN draft-room socket. Run the server first (python3 -m mantis serve --no-open), then:
//   node tests/addon_chain.test.mjs [port]
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const PORT = process.argv[2] || "8765";
const src = f => readFileSync(new URL(`../firefox-addon/${f}`, import.meta.url), "utf8");
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));

// ---- shared fake page window: one target for page-world postMessage, seen by tap.js and relay.js
const listeners = [];
const pageWindow = {
  location: { origin: "https://fantasy.espn.com", href: "https://fantasy.espn.com/basketball/draft?leagueId=777&seasonId=2027&teamId=4", search: "?leagueId=777&seasonId=2027&teamId=4", host: "fantasy.espn.com", pathname: "/basketball/draft" },
  addEventListener: (t, fn) => t === "message" && listeners.push(fn),
  postMessage(data, origin) { assert.equal(origin, "https://fantasy.espn.com"); setTimeout(() => listeners.forEach(fn => fn({ source: pageWindow, data })), 0); },
};
const sentFrames = [];
class FakeSocket extends EventTarget {
  constructor(url) { super(); this.url = url; FakeSocket.last = this; }
  send(x) { sentFrames.push(x); }
}
FakeSocket.OPEN = 1;
pageWindow.WebSocket = FakeSocket;

// ---- page world: tap.js
vm.runInNewContext(src("tap.js"), { window: pageWindow, URL });
assert.notEqual(pageWindow.WebSocket, FakeSocket, "tap wraps WebSocket");
assert.equal(pageWindow.WebSocket.OPEN, 1, "static constants kept");

// ---- extension: background.js with real fetch to the local server
let onMessage;
const browser = {
  runtime: { onMessage: { addListener: fn => (onMessage = fn) }, sendMessage: msg => Promise.resolve(onMessage(msg, {})) },
  storage: { local: { get: async () => ({ port: +PORT }) } },
};
const fetchLog = [];
const loggedFetch = async (url, opts) => { fetchLog.push({ url, body: JSON.parse(opts.body) }); return fetch(url, opts); };
vm.runInNewContext(src("background.js"), { browser, fetch: loggedFetch, setTimeout, clearTimeout, console, Date, JSON, String, Promise, Map, Array });
// ---- extension: relay.js (isolated world, same page window)
vm.runInNewContext(src("relay.js"), { window: pageWindow, location: pageWindow.location, browser, setTimeout, URLSearchParams });

// ESPN's code opens the draft socket (with a secret code in the query) and an unrelated socket
const ws = new pageWindow.WebSocket("wss://fantasydraft.espn.com/game-3/league-777/JOIN?securityCode=SECRET123");
assert.ok(ws instanceof FakeSocket, "socket is the real class");
new pageWindow.WebSocket("wss://other.example.com/feed");
const other = FakeSocket.last;

const frame = data => ws.dispatchEvent(Object.assign(new Event("message"), { data }));
frame("SELECTING 3 30000\nSELECTED 3 3112335 1\nhey everyone good luck\nSELECTED 1 4278073 1");
other.dispatchEvent(Object.assign(new Event("message"), { data: "SELECTED 9 999 1" }));
await tick(400);
frame("SELECTED 3 3112335 1");          // replay: de-duplicated by the server
frame("SELECTED 4 3945274 1\nSELECTING 2 30000");
await tick(400);

assert.equal(sentFrames.length, 0, "the tap never sends on ESPN's socket");
const allLines = fetchLog.flatMap(f => f.body.lines);
assert.ok(!allLines.some(l => /good luck/.test(l)), "chat lines dropped");
assert.ok(!allLines.some(l => /SELECTED 9 999/.test(l)), "other sockets ignored");
assert.ok(!JSON.stringify(fetchLog).includes("SECRET123"), "security code never forwarded");
assert.ok(fetchLog.every(f => f.url === `http://localhost:${PORT}/api/draftfeed` && f.body.leagueId === "777"));

const state = await browser.runtime.sendMessage({ type: "getState" });
assert.equal(state.lastError, null, "server accepted the lines");
const snap = await (await fetch(`http://localhost:${PORT}/api/league`)).json();
assert.equal(snap.feed.active, true);
assert.deepEqual(snap.draft.picks.map(p => p.playerId), [3112335, 4278073, 3945274]);
assert.equal(snap.draft.clock.teamId, 2);
console.log(`addon chain ok: ${fetchLog.length} posts, ${allLines.length} lines, picks ${snap.draft.picks.length}, on clock team ${snap.draft.clock.teamId}`);
process.exit(0);
