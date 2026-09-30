// Sidebar: category keys, sorting and the traffic / flight list.
import { state, visibleLive, visibleFlights, selectedFlightId, catColor } from './store.js';
import { $, escapeHtml, fmtNum, fmtSigned, fmtTime, fmtDuration, fmtAgo, EYE, EYE_OFF } from './util.js';

export const SORTS = {
  live: [
    ['alt', 'ALT ▼'],
    ['vs', 'VARIO ▼'],
    ['speed', 'GS ▼'],
    ['name', 'NAME'],
    ['recent', 'LAST RX'],
    ['type', 'TYPE'],
  ],
  history: [
    ['takeoff', 'T/O TIME'],
    ['duration', 'DURATION ▼'],
    ['distance', 'DIST ▼'],
    ['max_alt', 'MAX ALT ▼'],
    ['name', 'NAME'],
    ['type', 'TYPE'],
  ],
};

const byName = (a, b) => (a.name || '').localeCompare(b.name || '');
const desc = (key) => (a, b) => (b[key] ?? -1e9) - (a[key] ?? -1e9);

const LIVE_SORT = {
  alt: desc('alt'),
  vs: desc('vs'),
  speed: desc('spd'),
  name: byName,
  recent: desc('t'),
  type: (a, b) => a.cat.localeCompare(b.cat) || byName(a, b),
};
const FLIGHT_SORT = {
  takeoff: (a, b) => (a.takeoff ?? 0) - (b.takeoff ?? 0),
  duration: desc('duration_s'),
  distance: desc('distance_km'),
  max_alt: desc('max_alt'),
  name: byName,
  type: (a, b) => a.cat.localeCompare(b.cat) || (a.takeoff ?? 0) - (b.takeoff ?? 0),
};

export function sortOptions() {
  const select = $('#sort');
  const current = state.sort[state.mode];
  select.innerHTML = SORTS[state.mode]
    .map(([id, label]) => `<option value="${id}"${id === current ? ' selected' : ''}>${label}</option>`)
    .join('');
}

function catLabel(cat) {
  return state.config.categories.find((c) => c.id === cat)?.label ?? 'OTH';
}

function eye(item, flightId) {
  if (!flightId) return '<span></span>';
  const hidden = !!item.hidden;
  return `<button class="eye${hidden ? ' off' : ''}" data-act="hide" data-flight="${flightId}" data-hidden="${hidden ? 1 : 0}" title="${hidden ? 'Unhide' : 'Hide'} flight">${hidden ? EYE_OFF : EYE}</button>`;
}

function liveItem(a, selId) {
  const vsClass = a.vs > 0.2 ? 'up' : a.vs < -0.2 ? 'down' : '';
  const sub = [a.src, a.flying ? `${fmtNum(a.spd)} km/h` : 'ON GROUND', fmtAgo(a.t)].filter(Boolean).join(' · ');
  const stale = Date.now() / 1000 - a.t > 120;
  return `<li class="fl-item${a.id === selId ? ' selected' : ''}${a.hidden ? ' is-hidden' : ''}${stale ? ' stale' : ''}" data-kind="live" data-id="${escapeHtml(a.id)}">
    <span class="fl-cat" style="--c:${catColor(a.cat)}">${catLabel(a.cat)}</span>
    <div class="fl-main"><div class="fl-name">${escapeHtml(a.name)}</div><div class="fl-sub">${escapeHtml(sub)}</div></div>
    <div class="fl-vals"><div class="fl-alt">${fmtNum(a.alt)}<small>m</small></div><div class="fl-vs ${vsClass}">${fmtSigned(a.vs)}<small>m/s</small></div></div>
    ${eye(a, a.flight_id)}
  </li>`;
}

function flightItem(f, selId) {
  const tz = state.tz;
  const times = `${fmtTime(f.takeoff, tz)}–${f.status === 'active' ? 'now' : fmtTime(f.landing ?? f.end, tz)}`;
  const sub = [times, fmtDuration(f.duration_s), `${fmtNum(f.distance_km, 0)} km`].join(' · ');
  const state_ = f.status === 'active' ? ' <span class="up">●</span>' : '';
  return `<li class="fl-item${f.id === selId ? ' selected' : ''}${f.hidden ? ' is-hidden' : ''}" data-kind="flight" data-id="${f.id}">
    <span class="fl-cat" style="--c:${catColor(f.cat)}">${catLabel(f.cat)}</span>
    <div class="fl-main"><div class="fl-name">${escapeHtml(f.name)}${state_}</div><div class="fl-sub">${escapeHtml(sub)}</div></div>
    <div class="fl-vals"><div class="fl-alt">${fmtNum(f.max_alt)}<small>m</small></div><div class="fl-vs">${escapeHtml(f.src)}</div></div>
    ${eye(f, f.id)}
  </li>`;
}

function emptyLiveMessage() {
  if (state.live.size) return 'NO TRAFFIC<br>CHECK FILTERS (PG / HG / GL, GND, SEARCH)';
  const s = state.status;
  if (!s || !['connected', 'demo'].includes(s.link.state)) return 'NO TRAFFIC<br>WAITING FOR OGN DATA…';
  const other = s.received_types_untracked || 0;
  return 'NO PARAGLIDERS, HANG GLIDERS OR GLIDERS<br>RECEIVED IN THE LAST MINUTES'
    + (other ? `<br><br><span class="muted">${other} messages from other aircraft types ignored<br>(airliners, powered aircraft, …)</span>` : '');
}

export function renderList() {
  const ul = $('#flight-list');
  const live = state.mode === 'live';
  const selId = live ? (state.selected?.kind === 'live' ? state.selected.id : null) : selectedFlightId();
  let items;
  if (live) {
    items = visibleLive().sort(LIVE_SORT[state.sort.live] || LIVE_SORT.alt);
    ul.innerHTML = items.map((a) => liveItem(a, selId)).join('')
      || `<li class="list-empty">${emptyLiveMessage()}</li>`;
  } else {
    items = visibleFlights().sort(FLIGHT_SORT[state.sort.history] || FLIGHT_SORT.takeoff);
    ul.innerHTML = items.map((f) => flightItem(f, selId)).join('')
      || `<li class="list-empty">NO FLIGHTS${state.flights.length ? '<br>CHECK FILTERS' : ' RECORDED'}</li>`;
  }
  $('#list-title').textContent = live ? 'LIVE TRAFFIC' : 'FLIGHTS';
  $('#list-count').textContent = items.length;

  // category counts (ignoring the category filter itself)
  const counts = { pg: 0, hg: 0, gl: 0, ot: 0 };
  if (live) {
    for (const a of state.live.values()) if (a.flying || state.showGround) counts[a.cat] = (counts[a.cat] || 0) + 1;
  } else {
    for (const f of state.flights) if (f.airborne || state.showGround) counts[f.cat] = (counts[f.cat] || 0) + 1;
  }
  for (const [cat, n] of Object.entries(counts)) {
    const node = document.querySelector(`.key.cat[data-cat="${cat}"] .cat-count`);
    if (node) node.textContent = n;
  }

  const foot = $('#list-foot');
  if (live) {
    const flying = [...state.live.values()].filter((a) => a.flying).length;
    foot.innerHTML = `<span>${flying} AIRBORNE · ${state.live.size - flying} GND</span><span>OGN LIVE</span>`;
  } else {
    const hidden = state.flights.filter((f) => f.hidden).length;
    const zip = `/api/days/${state.date}/export.zip?region=${state.region.id}`;
    foot.innerHTML = `<span>${state.flights.filter((f) => f.airborne).length} FLIGHTS · ${hidden} HIDDEN</span><a href="${zip}" download>⬇ DAY ZIP (IGC)</a>`;
  }
  const sel = ul.querySelector('.selected');
  if (sel && renderList.scrollToSelection) {
    sel.scrollIntoView({ block: 'nearest' });
    renderList.scrollToSelection = false;
  }
}
renderList.scrollToSelection = false;
