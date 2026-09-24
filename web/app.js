// The radio's web page: station cards built from /api/status (see
// web_server.py), refreshed every few seconds, with one audio player shared
// by all stations.

const REFRESH_SECONDS = 15;
const TEXT = {
  listen: "Słuchaj",
  stop: "Zatrzymaj",
  onAir: "Na antenie",
  offAir: "Stacja nie nadaje",
  nothing: "—",
  noHistory: "Nic jeszcze nie grało.",
  listeners: (n) => `${n} ${n === 1 ? "słuchacz" : "słuchaczy"}`,
  copied: "Skopiowano link do schowka",
  copyFailed: "Nie udało się skopiować — link: ",
  loadFailed: "Nie udało się pobrać stanu stacji. Ponawiam…",
  playFailed: "Nie udało się włączyć stacji.",
  updated: (time) => `Zaktualizowano o ${time}`,
  news: "Wiadomości co godzinę",
  programs: "Ramówka",
  days: { mon: "pon", tue: "wt", wed: "śr", thu: "czw", fri: "pt", sat: "sob", sun: "nd" },
  daily: "codziennie",
};

const stationsEl = document.getElementById("stations");
const statusEl = document.getElementById("status-message");
const template = document.getElementById("station-template");
const player = document.getElementById("player");
const cards = new Map();
let playingId = null;
let streamBase = null;

function hueFor(id) {
  let hash = 0;
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return hash;
}

function streamUrl(station) {
  const base = streamBase || `${location.protocol}//${location.hostname}:${state.icecastPort}`;
  return base + station.mount;
}

function formatTime(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
}

function formatSchedule(schedule) {
  return schedule.map((slot) => {
    const days = !slot.days || slot.days === "daily" || slot.days.length === 7
      ? TEXT.daily
      : slot.days.map((day) => TEXT.days[day.slice(0, 3).toLowerCase()] || day).join(", ");
    return `${days} ${slot.start}–${slot.end}`;
  }).join("; ");
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2500);
}

async function copyLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    showToast(TEXT.copied);
  } catch {
    // Clipboard access needs HTTPS (or localhost); fall back to a prompt.
    window.prompt(TEXT.copyFailed, url);
  }
}

function setPlaying(id, loading = false) {
  playingId = id;
  for (const [stationId, card] of cards) {
    const active = stationId === id;
    card.el.classList.toggle("playing", active && !loading);
    card.el.classList.toggle("loading", active && loading);
    card.play.setAttribute("aria-label", active ? TEXT.stop : TEXT.listen);
  }
}

function togglePlay(station) {
  if (playingId === station.id) {
    player.pause();
    player.removeAttribute("src");
    player.load();
    setPlaying(null);
    return;
  }
  // A fresh URL each time, so the stream starts live instead of from a
  // stale buffer.
  player.src = `${streamUrl(station)}?t=${Date.now()}`;
  setPlaying(station.id, true);
  player.play().catch(() => {
    setPlaying(null);
    showToast(TEXT.playFailed);
  });
}

player.addEventListener("playing", () => { if (playingId) setPlaying(playingId); });
player.addEventListener("error", () => {
  if (playingId) {
    setPlaying(null);
    showToast(TEXT.playFailed);
  }
});

function createCard(station) {
  const el = template.content.firstElementChild.cloneNode(true);
  el.style.setProperty("--hue", hueFor(station.id));
  const card = {
    el,
    play: el.querySelector(".play"),
    name: el.querySelector(".station-name"),
    genre: el.querySelector(".genre"),
    listeners: el.querySelector(".listeners"),
    description: el.querySelector(".description"),
    nowState: el.querySelector(".now-state"),
    nowTitle: el.querySelector(".now-title"),
    history: el.querySelector(".history-list"),
    extras: el.querySelector(".extras"),
    copy: el.querySelector(".copy"),
    m3u: el.querySelector(".m3u"),
    station,
  };
  card.play.addEventListener("click", () => togglePlay(card.station));
  card.copy.addEventListener("click", () => copyLink(streamUrl(card.station)));
  stationsEl.appendChild(el);
  cards.set(station.id, card);
  return card;
}

function renderExtras(card, station) {
  const items = [];
  if (station.news) items.push(`<li>${TEXT.news}</li>`);
  for (const program of station.programs) {
    const li = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = program.title;
    li.append(title, ` · ${formatSchedule(program.schedule)}`);
    items.push(li.outerHTML);
  }
  card.extras.innerHTML = items.length ? `<h3>${TEXT.programs}</h3><ul>${items.join("")}</ul>` : "";
}

function renderHistory(card, history) {
  card.history.replaceChildren();
  if (!history.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = TEXT.noHistory;
    card.history.append(li);
    return;
  }
  for (const song of history) {
    const li = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = new Date(song.played_at * 1000).toISOString();
    time.textContent = formatTime(song.played_at);
    const track = document.createElement("span");
    track.className = "track";
    if (song.artist) {
      const artist = document.createElement("span");
      artist.className = "artist";
      artist.textContent = `${song.artist} – `;
      track.append(artist);
    }
    track.append(song.title);
    li.append(time, track);
    card.history.append(li);
  }
}

function render(station) {
  const card = cards.get(station.id) || createCard(station);
  card.station = station;
  card.name.textContent = station.name;
  card.genre.textContent = station.genre;
  card.description.textContent = station.description;
  card.listeners.textContent = station.on_air ? TEXT.listeners(station.listeners) : "";
  card.el.classList.toggle("offline", !station.on_air);
  card.play.disabled = !station.on_air && playingId !== station.id;
  card.nowState.textContent = station.on_air ? TEXT.onAir : TEXT.offAir;
  card.nowTitle.textContent = station.now_playing || TEXT.nothing;
  const url = streamUrl(station);
  card.m3u.href = `data:audio/x-mpegurl;charset=utf-8,${encodeURIComponent(`#EXTM3U\n#EXTINF:-1,${station.name}\n${url}\n`)}`;
  card.m3u.download = `${station.id}.m3u`;
  renderHistory(card, station.history);
  renderExtras(card, station);
}

const state = { icecastPort: 8000 };

async function refresh() {
  try {
    const response = await fetch("api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(response.statusText);
    const data = await response.json();
    streamBase = data.stream_base;
    state.icecastPort = data.icecast_port;
    document.title = data.title;
    document.getElementById("site-title").textContent = data.title;
    data.stations.forEach(render);
    statusEl.textContent = "";
    document.getElementById("updated-at").textContent = TEXT.updated(formatTime(data.generated_at));
  } catch {
    statusEl.textContent = TEXT.loadFailed;
  }
}

refresh();
setInterval(refresh, REFRESH_SECONDS * 1000);
