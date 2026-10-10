const ORIGINS = ["https://fantasy.espn.com/*", "https://fantasydraft.espn.com/*", "http://localhost/*", "http://127.0.0.1/*"];
const $ = id => document.getElementById(id);
const ago = t => { if (!t) return "–"; const s = Math.round((Date.now() - t) / 1000); return s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`; };
const setDot = (id, cls) => { $(id).className = "dot " + cls; };

async function port() { return (await browser.storage.local.get("port")).port || 8765; }

async function refresh() {
  const granted = await browser.permissions.contains({ origins: ORIGINS });
  $("permRow").hidden = granted;
  const p = await port();
  $("port").value = p;
  $("openBoard").href = `http://localhost:${p}/`;

  let status = null;
  if (granted) {
    try {
      const res = await fetch(`http://localhost:${p}/api/status`, { cache: "no-store" });
      status = await res.json();
      setDot("srvDot", "ok");
      $("srvHead").textContent = "Draft Mantis board running";
      $("srvSub").textContent = status.league ? `Connected to ESPN league ${status.league.id}.` : "Running, but no ESPN league connected yet. Connect one on the board.";
    } catch (e) {
      setDot("srvDot", "bad");
      $("srvHead").textContent = "Draft Mantis board not running";
      $("srvSub").textContent = "In the project folder run: python3 -m mantis serve";
    }
  } else {
    setDot("srvDot", "wait"); $("srvSub").textContent = "Waiting for access.";
  }

  const st = await browser.runtime.sendMessage({ type: "getState" }).catch(() => null);
  const warn = [];
  if (st && st.socketSeen) {
    const live = st.lastLineAt && Date.now() - st.lastLineAt < 5 * 60e3;
    setDot("roomDot", live ? "ok" : "wait");
    $("roomHead").textContent = live ? "Draft room connected" : "Draft room seen";
    $("roomSub").textContent = st.leagueId ? `League ${st.leagueId}${st.teamId ? ", team " + st.teamId : ""}. Listening only.` : "Could not tell which league this is. Open the draft room from your league page.";
    $("stats").hidden = false;
    $("sPicks").textContent = st.picks;
    $("sLines").textContent = st.lines;
    $("sSent").textContent = ago(st.lastSentAt);
    if (st.lastError) warn.push(st.lastError);
    if (status?.league && st.leagueId && String(status.league.id) !== String(st.leagueId))
      warn.push(`This draft room is league ${st.leagueId}, but the board is connected to league ${status.league.id}. Picks are stored, but the board only shows its connected league.`);
  } else {
    setDot("roomDot", "wait");
    $("roomHead").textContent = "No draft room yet";
    $("roomSub").textContent = "Open your ESPN draft room in this browser. If it was already open, reload it so the bridge loads first.";
  }
  $("warn").hidden = !warn.length;
  $("warn").textContent = warn.join(" ");
}

$("grant").onclick = async () => { await browser.permissions.request({ origins: ORIGINS }); refresh(); };
$("port").onchange = async e => {
  const v = Math.min(65535, Math.max(1024, +e.target.value || 8765));
  await browser.storage.local.set({ port: v });
  refresh();
};
refresh();
setInterval(refresh, 2000);
