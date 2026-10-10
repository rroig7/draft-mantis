/* Draft Mantis Bridge - background.
 * Posts draft-room command lines to the local Draft Mantis server (python3 -m mantis serve).
 * Keeps every line per league so a server restart can be caught up in one request; the server
 * de-duplicates picks by player, so resending is harmless.
 */
const DEFAULT_PORT = 8765;
const MAX_LOG = 20000;

const state = {
  socketSeen: false, socket: "", page: "", leagueId: null, teamId: null,
  lines: 0, picks: 0, lastLineAt: null, lastSentAt: null, lastError: null, server: null,
};
const logs = new Map();      // leagueId -> all lines seen this session
let pending = [], resendAll = false, timer = null, sending = false;

async function port() {
  try { return (await browser.storage.local.get("port")).port || DEFAULT_PORT; } catch (e) { return DEFAULT_PORT; }
}

async function send() {
  timer = null;
  if (sending || !state.leagueId) return;
  const batch = resendAll ? (logs.get(state.leagueId) || []) : pending;
  if (!batch.length) return;
  sending = true;
  const taken = pending.length;
  try {
    const res = await fetch(`http://localhost:${await port()}/api/draftfeed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ leagueId: state.leagueId, teamId: state.teamId, lines: batch }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `Draft Mantis answered ${res.status}`);
    pending = pending.slice(taken);
    resendAll = false;
    state.lastSentAt = Date.now();
    state.lastError = null;
    state.server = body;
  } catch (e) {
    resendAll = true;
    state.lastError = /NetworkError|Failed to fetch/i.test(String(e))
      ? "Draft Mantis is not running. Start it with: python3 -m mantis serve"
      : String(e.message || e);
    timer = setTimeout(send, 3000);  // keep retrying; nothing is lost
  } finally {
    sending = false;
    if (pending.length && !timer) timer = setTimeout(send, 50);
  }
}

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "getState") return Promise.resolve(state);
  if (["open", "close", "lines"].includes(msg.type)) {
    state.socketSeen = true;
    state.socket = msg.socket || state.socket;
    state.page = msg.page || state.page;
    if (msg.leagueId && msg.leagueId !== state.leagueId) { state.leagueId = msg.leagueId; resendAll = true; }
    if (msg.teamId) state.teamId = msg.teamId;
  }
  if (msg.type === "lines" && Array.isArray(msg.lines)) {
    const log = logs.get(state.leagueId) || [];
    for (const line of msg.lines) {
      log.push(line);
      if (/^SELECTED\s/.test(line)) state.picks++;
    }
    if (log.length > MAX_LOG) log.splice(0, log.length - MAX_LOG);
    if (state.leagueId) logs.set(state.leagueId, log);
    pending.push(...msg.lines);
    state.lines += msg.lines.length;
    state.lastLineAt = Date.now();
    if (!timer) timer = setTimeout(send, 30);
  }
});
