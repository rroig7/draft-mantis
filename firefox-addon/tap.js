/* Draft Mantis Bridge - page tap (runs in the page, before ESPN's code).
 *
 * Wraps window.WebSocket so that connections to fantasydraft.espn.com get a passive "message" listener.
 * It only listens: it never calls send(), never opens a connection of its own, and leaves every other
 * socket untouched. ESPN keeps a reference to the WebSocket class from startup and reuses it to reconnect,
 * which is why this must load first (run_at: document_start). Frames are handed to relay.js via postMessage.
 */
(() => {
  if (window.__draftMantisTap) return;
  window.__draftMantisTap = true;

  const Native = window.WebSocket;
  if (typeof Native !== "function") return;
  const DRAFT_HOST = /(^|\.)fantasydraft\.espn\.com$/i;

  const post = (kind, extra) => {
    try { window.postMessage({ __draftMantisTap: true, kind, ...extra }, window.location.origin); } catch (e) {}
  };

  function Tapped(url, protocols) {
    const ws = protocols === undefined ? new Native(url) : new Native(url, protocols);
    try {
      const u = new URL(String(url), window.location.href);
      if (DRAFT_HOST.test(u.hostname)) {
        // Never forward the query string: ESPN puts the member's security code there.
        const socket = u.host + u.pathname;
        post("open", { socket });
        ws.addEventListener("message", ev => {
          if (typeof ev.data === "string") post("frame", { socket, data: ev.data.slice(0, 200000) });
        });
        ws.addEventListener("close", () => post("close", { socket }));
      }
    } catch (e) { /* never break ESPN's connection */ }
    return ws;
  }
  Tapped.prototype = Native.prototype;
  Object.setPrototypeOf(Tapped, Native);  // keeps WebSocket.OPEN etc.
  window.WebSocket = Tapped;
})();
