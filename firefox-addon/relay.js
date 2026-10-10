/* Draft Mantis Bridge - relay (extension content script).
 * Receives frames from tap.js, keeps only draft command lines (e.g. "SELECTED 7 3041969 1"),
 * and hands them to the background script. Chat and anything else in the feed is dropped here.
 */
(() => {
  const COMMAND = /^[A-Z][A-Z_]{2,24}(\s|$)/;
  let pending = [], timer = null, socket = "";

  const leagueFrom = () => {
    const q = new URLSearchParams(location.search);
    const fromSocket = (socket.match(/league-?(\d+)/i) || [])[1];
    return { leagueId: q.get("leagueId") || fromSocket || null, teamId: q.get("teamId") || null, seasonId: q.get("seasonId") || null };
  };

  const flush = () => {
    timer = null;
    if (!pending.length) return;
    const lines = pending; pending = [];
    browser.runtime.sendMessage({ type: "lines", lines, socket, page: location.host + location.pathname, ...leagueFrom() }).catch(() => {});
  };

  window.addEventListener("message", ev => {
    if (ev.source !== window || !ev.data || ev.data.__draftMantisTap !== true) return;
    const d = ev.data;
    if (d.socket) socket = String(d.socket);
    if (d.kind === "open" || d.kind === "close") {
      browser.runtime.sendMessage({ type: d.kind, socket, page: location.host + location.pathname, ...leagueFrom() }).catch(() => {});
      return;
    }
    if (d.kind !== "frame" || typeof d.data !== "string") return;
    for (const raw of d.data.split(/\r?\n/)) {
      const line = raw.trim();
      if (line && COMMAND.test(line)) pending.push(line.slice(0, 2000));
    }
    if (pending.length && !timer) timer = setTimeout(flush, 80);
  });
})();
