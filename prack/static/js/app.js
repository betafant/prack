// prack web app: wiring of state, live stream, map, list, details, calendar and weather.
import { api } from './api.js';
import { state, on, emit, persist, selectedLive, selectedFlightId, visibleFlights } from './store.js';
import { $, $$, isoInTz, addDays, fmtDateLong, fmtTime, toast, throttle } from './util.js';
import { MapView } from './map.js';
import { ProfileChart } from './chart.js';
import { createVario } from './vario.js';
import { renderList, sortOptions } from './list.js';
import { initDetails, renderDetails, renderReadouts } from './details.js';
import { Calendar } from './calendar.js';
import { renderWeather } from './weather.js';

const TRAIL_POINTS = 200;
const CURTAIN_MAX = 1500;

let mapView;
let chart;
let calendar;

// ------------------------------------------------------------------ rendering

let frame = null;
function scheduleRender() {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    mapView?.render();
    renderList();
    renderDetails();
    renderStatusLine();
  });
}

function renderStatusLine() {
  const node = $('#map-status');
  const parts = [];
  if (state.mode === 'live') parts.push('LIVE');
  else parts.push(fmtDateLong(state.date), `${visibleFlights().length} FLIGHTS`);
  if (state.view3d) parts.push('3D');
  if (state.follow) parts.push('FOLLOW');
  if (statusInfo.seeding) parts.push(`DEMO: ${statusInfo.seeding.toUpperCase()}`);
  node.textContent = parts.join(' · ');
}

function renderKeys() {
  $('#btn-live').classList.toggle('on', state.mode === 'live');
  $('#btn-date').classList.toggle('on', state.mode === 'history');
  $('#date-label').textContent = state.mode === 'live' ? `${fmtDateLong(state.today)}` : fmtDateLong(state.date);
  $('#btn-next').disabled = state.mode === 'live' || state.date >= state.today;
  $('#btn-3d').classList.toggle('on', state.view3d);
  $('#btn-follow').classList.toggle('on', state.follow);
  $('#btn-follow').disabled = state.mode !== 'live';
  $('#btn-trails').classList.toggle('on', state.trails);
  $('#btn-labels').classList.toggle('on', state.labels);
  $('#btn-hidden').classList.toggle('on', state.showHidden);
  $('#btn-ground').classList.toggle('on', state.showGround);
  $('#btn-wx').classList.toggle('on', state.wxOpen);
  $('#btn-map').classList.toggle('on', !$('#basemap-menu').hidden);
  for (const k of $$('.key.cat')) k.classList.toggle('on', !!state.cats[k.dataset.cat]);
}

function renderLegend() {
  $('#map-legend').innerHTML = state.config.categories
    .filter((c) => c.types.some((t) => state.config.tracked_types.includes(t)))
    .map((c) => `<span><i style="--c:${c.color}"></i>${c.name.toUpperCase()}</span>`)
    .join('') + '<span><i style="--c:#ff3ec9"></i>SELECTED</span>';
}

// ------------------------------------------------------------------ tracks

function prepareTrack(raw, cat) {
  const n = raw.t.length;
  const path3 = new Array(n);
  const path2 = new Array(n);
  let w = 180, s = 90, e = -180, nn = -90;
  for (let i = 0; i < n; i++) {
    const lon = raw.lon[i];
    const lat = raw.lat[i];
    path3[i] = [lon, lat, raw.alt[i]];
    path2[i] = [lon, lat];
    if (lon < w) w = lon; if (lon > e) e = lon;
    if (lat < s) s = lat; if (lat > nn) nn = lat;
  }
  const track = { ...raw, cat, path3, path2, bounds: n ? [w, s, e, nn] : null };
  buildCurtain(track);
  return track;
}

function buildCurtain(tr) {
  const n = tr.t.length;
  const step = Math.max(1, Math.ceil(n / CURTAIN_MAX));
  const quads = [];
  for (let i = 0; i + step < n; i += step) {
    const j = i + step;
    const gi = tr.gnd[i] ?? tr.alt[i] - 5000;
    const gj = tr.gnd[j] ?? tr.alt[j] - 5000;
    quads.push([
      [tr.lon[i], tr.lat[i], tr.alt[i]], [tr.lon[j], tr.lat[j], tr.alt[j]],
      [tr.lon[j], tr.lat[j], gj], [tr.lon[i], tr.lat[i], gi],
    ]);
  }
  tr.curtain = quads;
}

function appendLivePoint(a) {
  const tr = state.track;
  if (!tr || tr.id !== a.flight_id || !tr.t.length || a.t <= tr.t[tr.t.length - 1]) return false;
  tr.t.push(a.t); tr.lat.push(a.lat); tr.lon.push(a.lon); tr.alt.push(a.alt); tr.gnd.push(a.gnd);
  tr.spd.push(a.spd); tr.vs.push(a.vs); tr.hdg.push(a.hdg);
  tr.path3.push([a.lon, a.lat, a.alt]);
  tr.path2.push([a.lon, a.lat]);
  if (tr.t.length % 10 === 0) buildCurtain(tr);
  return true;
}

const updateChart = throttle(() => {
  const ok = chart.setData(state.track, state.tz);
  $('#chart-empty').hidden = ok;
}, 1500);

async function loadSelectedFlight(fit = false) {
  const fid = selectedFlightId();
  if (!fid) {
    state.flight = null;
    state.track = null;
    state.versions.track++;
    chart.setData(null);
    $('#chart-empty').hidden = false;
    scheduleRender();
    return;
  }
  try {
    const [flight, raw] = await Promise.all([api.flight(fid), api.track(fid)]);
    if (selectedFlightId() !== fid) return;
    state.flight = flight;
    state.track = prepareTrack(raw, flight.cat);
    state.versions.track++;
    const ok = chart.setData(state.track, state.tz);
    $('#chart-empty').hidden = ok;
    if (fit) {
      renderDetails(); // the panel changes the map size: lay it out before fitting
      fitTrack();
    }
    scheduleRender();
  } catch (err) {
    toast(`FLIGHT ${fid}: ${err.message}`);
  }
}

// ------------------------------------------------------------------ selection & modes

function select(sel, { fit = true } = {}) {
  const same = sel && state.selected && sel.kind === state.selected.kind && sel.id === state.selected.id;
  state.selected = sel;
  state.cursor = null;
  if (!same) {
    state.flight = null;
    state.track = null;
    state.versions.track++;
    chart.setData(null);
  }
  if (!sel) state.follow = false;
  renderList.scrollToSelection = true;
  updateHash();
  renderKeys();
  // Open / close the details panel now: it changes the map size, and a camera animation started
  // before the resize would keep aiming at the old centre pixel.
  renderDetails();
  mapView.map.resize();
  scheduleRender();
  if (!sel) return;
  if (sel.kind === 'live') {
    const a = selectedLive();
    if (a && fit) mapView.map.easeTo({ center: [a.lon, a.lat], duration: 700 });
  }
  loadSelectedFlight(fit && sel.kind === 'flight');
}

async function goLive() {
  state.mode = 'live';
  state.today = isoInTz(state.tz);
  state.date = state.today;
  if (state.selected?.kind === 'flight') select(null);
  sortOptions();
  renderKeys();
  updateHash();
  if (state.wxOpen) loadWeather();
  scheduleRender();
}

async function goDate(date, { keepSelection = false, fit = true } = {}) {
  state.today = isoInTz(state.tz);
  if (date > state.today) date = state.today;
  const changed = state.mode !== 'history' || state.date !== date;
  state.mode = 'history';
  state.date = date;
  state.follow = false;
  if (!keepSelection && state.selected) select(null);
  sortOptions();
  renderKeys();
  updateHash();
  await loadDay({ fit: fit && changed && !state.selected });
  if (state.wxOpen) loadWeather();
}

async function loadDay({ fit = false } = {}) {
  const date = state.date;
  try {
    const [fl, tr] = await Promise.all([api.flights(date, state.region.id), api.tracks(date, state.region.id)]);
    if (state.date !== date || state.mode !== 'history') return;
    state.flights = fl.flights;
    state.tracks = tr.tracks.map((t) => ({ ...t, path2: t.path.map((p) => [p[0], p[1]]) }));
    state.versions.day++;
    if (fit) fitDay();
    scheduleRender();
  } catch (err) {
    toast(`DAY ${date}: ${err.message}`);
  }
}

function trackAlt(tr) {
  if (!tr?.alt?.length) return null;
  let lo = Infinity, hi = -Infinity;
  for (const a of tr.alt) { if (a < lo) lo = a; if (a > hi) hi = a; }
  return (lo + hi) / 2;
}

function fitTrack() {
  if (state.track?.bounds) mapView.fitBounds(state.track.bounds, { alt: trackAlt(state.track) });
}

function fitDay() {
  const visible = new Set(visibleFlights().map((f) => f.id));
  let w = 180, s = 90, e = -180, n = -90, altSum = 0, count = 0;
  for (const t of state.tracks) {
    if (!visible.has(t.id)) continue;
    for (const [lon, lat, alt] of t.path) {
      if (lon < w) w = lon; if (lon > e) e = lon;
      if (lat < s) s = lat; if (lat > n) n = lat;
      altSum += alt; count++;
    }
  }
  if (e > w) mapView.fitBounds([w, s, e, n], { maxZoom: 11, alt: count ? altSum / count : null });
}

async function setHidden(flightId, hidden) {
  try {
    const f = await api.patchFlight(flightId, { hidden });
    for (const list of [state.flights]) {
      const item = list.find((x) => x.id === flightId);
      if (item) item.hidden = f.hidden;
    }
    const track = state.tracks.find((t) => t.id === flightId);
    if (track) track.hidden = f.hidden;
    for (const a of state.live.values()) if (a.flight_id === flightId) a.hidden = f.hidden;
    if (state.flight?.id === flightId) state.flight.hidden = f.hidden;
    state.versions.day++;
    state.versions.live++;
    toast(f.hidden ? `FLIGHT HIDDEN · ${f.name}` : `FLIGHT VISIBLE · ${f.name}`, 'ok');
    scheduleRender();
  } catch (err) {
    toast(`HIDE: ${err.message}`);
  }
}

// ------------------------------------------------------------------ live data

function applyLive(msg) {
  if (msg.full) {
    const next = new Map();
    for (const a of msg.aircraft) {
      a.trail = a.trail || state.live.get(a.id)?.trail || [];
      a.trail2d = a.trail.map((p) => [p[0], p[1]]);
      next.set(a.id, a);
    }
    state.live = next;
  } else {
    for (const a of msg.aircraft) {
      const prev = state.live.get(a.id);
      const trail = prev?.trail || [];
      if (!prev || prev.t !== a.t) trail.push([a.lon, a.lat, a.alt]);
      if (trail.length > TRAIL_POINTS) trail.splice(0, trail.length - TRAIL_POINTS);
      a.trail = trail;
      a.trail2d = trail.map((p) => [p[0], p[1]]);
      state.live.set(a.id, a);
    }
    for (const id of msg.removed || []) state.live.delete(id);
  }
  state.versions.live++;

  const sel = selectedLive();
  if (sel) {
    if (sel.flight_id && (!state.track || state.track.id !== sel.flight_id)) {
      if (!loadSelectedFlight.pending) {
        loadSelectedFlight.pending = true;
        loadSelectedFlight().finally(() => { loadSelectedFlight.pending = false; });
      }
    } else if (appendLivePoint(sel)) {
      state.versions.track++;
      updateChart();
    }
    if (state.follow) mapView.follow(sel);
  }
  if (state.mode === 'live') scheduleRender();
  else if (state.selected?.kind === 'live') renderReadouts();
}

function connectLive() {
  const es = api.stream();
  es.onmessage = (ev) => {
    try { applyLive(JSON.parse(ev.data)); } catch (err) { console.error(err); }
  };
  es.onerror = () => { /* EventSource reconnects by itself */ };
}

// ------------------------------------------------------------------ status, clock

const statusInfo = { seeding: null, lastLines: null, lastAt: null };

async function pollStatus() {
  try {
    const s = await api.status();
    const link = $('#ann-link');
    const state_ = s.link.state;
    const map = {
      connected: ['ok', 'CONN'], demo: ['info', 'SIM'], connecting: ['caution', 'CONN…'],
      error: ['warn', 'FAIL'], idle: ['caution', 'IDLE'], disabled: ['', 'OFF'],
    };
    const [cls, text] = map[state_] || ['caution', state_.toUpperCase()];
    link.className = `annun ${cls}`;
    link.querySelector('.annun-val').textContent = text;
    link.title = `OGN APRS link: ${state_}${s.link.server ? ` · ${s.link.server}` : ''}${s.link.last_error ? ` · ${s.link.last_error}` : ''}`;
    const ac = $('#ann-ac');
    ac.className = `annun ${s.live_aircraft ? 'ok' : ''}`;
    ac.querySelector('.annun-val').textContent = s.live_aircraft;
    const received = Object.entries(s.received_types || {}).map(([t, n]) => `${t} ${n}`).join(', ');
    ac.title = `Tracked aircraft (types ${s.tracked_types.join(', ')}) seen in the last minutes: ${s.live_aircraft}`
      + (received ? `\nMessages received since start by aircraft type: ${received}` : '');
    const hadStatus = !!state.status;
    state.status = s;
    if (!hadStatus && state.mode === 'live') scheduleRender();
    const now = Date.now();
    if (statusInfo.lastLines != null && now > statusInfo.lastAt) {
      const rate = ((s.link.lines - statusInfo.lastLines) / (now - statusInfo.lastAt)) * 60000;
      const rx = $('#ann-rx');
      rx.className = `annun ${rate > 0 ? 'ok' : state_ === 'connected' ? 'caution' : ''}`;
      rx.querySelector('.annun-val').textContent = Math.max(0, Math.round(rate));
    }
    statusInfo.lastLines = s.link.lines;
    statusInfo.lastAt = now;
    $('#ann-demo').hidden = !s.demo;
    $('#ann-demo').className = 'annun caution';
    const wasSeeding = statusInfo.seeding;
    statusInfo.seeding = s.seeding;
    if (wasSeeding && !s.seeding && state.mode === 'history') loadDay();
    renderStatusLine();
  } catch {
    const link = $('#ann-link');
    link.className = 'annun warn';
    link.querySelector('.annun-val').textContent = 'NO SRV';
  }
}

function tickClock() {
  const now = new Date();
  $('#clk-utc').textContent = now.toISOString().slice(11, 19);
  $('#clk-lcl').textContent = fmtTime(now.getTime() / 1000, state.tz, true);
  const today = isoInTz(state.tz, now);
  if (today !== state.today) {
    state.today = today;
    if (state.mode === 'live') { state.date = today; renderKeys(); }
  }
}

// ------------------------------------------------------------------ weather

async function loadWeather() {
  state.weather = null;
  renderWeather(null, true);
  const date = state.date;
  try {
    const report = await api.weather(date, state.region.id);
    if (date !== state.date) return;
    state.weather = report;
    renderWeather(report);
  } catch (err) {
    renderWeather(null);
    toast(`WX: ${err.message}`);
  }
  mapView.render();
}

async function fetchWeather() {
  renderWeather(null, true);
  try {
    state.weather = await api.fetchWeather(state.date, state.region.id);
    toast('WEATHER UPDATED', 'ok');
  } catch (err) {
    toast(`WX FETCH: ${err.message}`);
  }
  renderWeather(state.weather);
  mapView.render();
}

function toggleWeather(open = !state.wxOpen) {
  state.wxOpen = open;
  $('#wx-drawer').hidden = !open;
  if (open) loadWeather();
  renderKeys();
  mapView.render();
}

// ------------------------------------------------------------------ URL hash (#/live/<id> or #/<date>/<flight>)

function updateHash() {
  let hash = state.mode === 'live' ? '#/live' : `#/${state.date}`;
  if (state.selected) hash += `/${encodeURIComponent(state.selected.id)}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

function readHash() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (!parts.length) return { mode: 'live' };
  if (parts[0] === 'live') return { mode: 'live', id: parts[1] };
  if (/^\d{4}-\d{2}-\d{2}$/.test(parts[0])) return { mode: 'history', date: parts[0], id: parts[1] && Number(parts[1]) };
  return { mode: 'live' };
}

// ------------------------------------------------------------------ controls

function buildCategoryKeys() {
  const tracked = state.config.tracked_types;
  $('#cat-keys').innerHTML = state.config.categories
    .filter((c) => c.types.some((t) => tracked.includes(t)))
    .map((c) => `<button class="key small cat" data-cat="${c.id}" style="--c:${c.color}" title="${c.name}"><span class="led"></span>${c.label} <span class="cat-count">0</span></button>`)
    .join('');
}

function filtersChanged() {
  state.versions.filters++;
  persist();
  renderKeys();
  scheduleRender();
}

function bindControls() {
  $('#btn-live').addEventListener('click', () => goLive());
  $('#btn-date').addEventListener('click', () => { calendar.toggle(); });
  $('#btn-prev').addEventListener('click', () => goDate(addDays(state.mode === 'live' ? state.today : state.date, -1)));
  $('#btn-next').addEventListener('click', () => goDate(addDays(state.date, 1)));
  $('#btn-wx').addEventListener('click', () => toggleWeather());
  $('#btn-wx-close').addEventListener('click', () => toggleWeather(false));
  $('#btn-wx-fetch').addEventListener('click', () => fetchWeather());
  $('#btn-menu').addEventListener('click', () => {
    const open = $('#sidebar').classList.toggle('open');
    $('#btn-menu').classList.toggle('on', open);
  });

  $('#cat-keys').addEventListener('click', (ev) => {
    const key = ev.target.closest('.key.cat');
    if (!key) return;
    const cat = key.dataset.cat;
    if (ev.shiftKey || ev.altKey) {
      for (const c of Object.keys(state.cats)) state.cats[c] = c === cat; // solo
    } else state.cats[cat] = !state.cats[cat];
    filtersChanged();
  });
  $('#btn-hidden').addEventListener('click', () => { state.showHidden = !state.showHidden; filtersChanged(); });
  $('#btn-ground').addEventListener('click', () => { state.showGround = !state.showGround; filtersChanged(); });
  $('#sort').addEventListener('change', (ev) => { state.sort[state.mode] = ev.target.value; persist(); scheduleRender(); });
  $('#search').addEventListener('input', (ev) => { state.search = ev.target.value.trim(); state.versions.filters++; scheduleRender(); });

  $('#flight-list').addEventListener('click', (ev) => {
    const eye = ev.target.closest('[data-act="hide"]');
    if (eye) {
      ev.stopPropagation();
      setHidden(Number(eye.dataset.flight), eye.dataset.hidden !== '1');
      return;
    }
    const item = ev.target.closest('.fl-item');
    if (!item) return;
    const kind = item.dataset.kind;
    const id = kind === 'flight' ? Number(item.dataset.id) : item.dataset.id;
    select({ kind, id });
    $('#sidebar').classList.remove('open');
    $('#btn-menu').classList.remove('on');
  });

  $('#btn-3d').addEventListener('click', () => {
    state.view3d = !state.view3d;
    mapView.set3D(state.view3d);
    if (state.view3d) {
      // frame the selection at its altitude (the camera otherwise targets the ground)
      if (state.track?.bounds && state.selected?.kind === 'flight') fitTrack();
      else if (selectedLive()) mapView.follow(selectedLive());
    }
    renderKeys();
    renderStatusLine();
  });
  $('#btn-follow').addEventListener('click', () => emit('follow', !state.follow));
  $('#btn-fit').addEventListener('click', () => {
    if (state.track?.bounds) fitTrack();
    else if (state.mode === 'history') fitDay();
    else mapView.fitRegion();
  });
  $('#btn-trails').addEventListener('click', () => { state.trails = !state.trails; persist(); renderKeys(); mapView.render(); });
  $('#btn-labels').addEventListener('click', () => { state.labels = !state.labels; persist(); renderKeys(); mapView.render(); });
  $('#btn-zoom-in').addEventListener('click', () => mapView.map.zoomIn());
  $('#btn-zoom-out').addEventListener('click', () => mapView.map.zoomOut());
  $('#btn-north').addEventListener('click', () => mapView.resetNorth());
  $('#btn-map').addEventListener('click', () => {
    const menu = $('#basemap-menu');
    menu.hidden = !menu.hidden;
    if (!menu.hidden) renderBasemapMenu();
    renderKeys();
  });
  $('#basemap-menu').addEventListener('click', (ev) => {
    const item = ev.target.closest('[data-basemap]');
    if (!item) return;
    mapView.setBasemap(item.dataset.basemap);
    persist();
    renderBasemapMenu();
  });

  $('#btn-close').addEventListener('click', () => select(null));
  $('#btn-hide').addEventListener('click', () => {
    const fid = selectedFlightId();
    if (!fid) return;
    const hidden = state.flight?.hidden ?? selectedLive()?.hidden ?? false;
    setHidden(fid, !hidden);
  });
  $('#export-group').addEventListener('click', (ev) => {
    const key = ev.target.closest('[data-fmt]');
    const fid = selectedFlightId();
    if (!key || !fid) return;
    const a = document.createElement('a');
    a.href = api.exportUrl(fid, key.dataset.fmt);
    a.download = '';
    document.body.append(a);
    a.click();
    a.remove();
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.target.closest('input, select, textarea')) {
      if (ev.key === 'Escape') ev.target.blur();
      return;
    }
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    switch (ev.key) {
      case 'Escape':
        if (calendar.open) calendar.close();
        else if (!$('#basemap-menu').hidden) { $('#basemap-menu').hidden = true; renderKeys(); }
        else if (state.wxOpen) toggleWeather(false);
        else select(null);
        break;
      case '3': $('#btn-3d').click(); break;
      case 'l': case 'L': goLive(); break;
      case 'w': case 'W': toggleWeather(); break;
      case 'f': case 'F': if (state.mode === 'live') emit('follow', !state.follow); break;
      case 'h': case 'H': $('#btn-hide').click(); break;
      case 'ArrowLeft': $('#btn-prev').click(); break;
      case 'ArrowRight': if (!$('#btn-next').disabled) $('#btn-next').click(); break;
      default: return;
    }
    ev.preventDefault();
  });

  document.addEventListener('click', (ev) => {
    if (calendar.open && !ev.target.closest('#calendar, #btn-date')) calendar.close();
    const menu = $('#basemap-menu');
    if (!menu.hidden && !ev.target.closest('#basemap-menu, #btn-map')) { menu.hidden = true; renderKeys(); }
  });

  window.addEventListener('hashchange', () => {
    const h = readHash();
    if (h.mode === 'live' && state.mode !== 'live') goLive();
    else if (h.mode === 'history' && h.date !== state.date) goDate(h.date);
  });
}

function renderBasemapMenu() {
  const regional = state.region.basemaps.filter((b) => !['relief', 'topo', 'osm', 'satellite'].includes(b.id));
  const global = state.region.basemaps.filter((b) => ['relief', 'topo', 'osm', 'satellite'].includes(b.id));
  const item = (b) => `<button class="menu-item${state.basemap === b.id ? ' on' : ''}" data-basemap="${b.id}"><span class="led"></span>${b.name}</button>`;
  $('#basemap-menu').innerHTML =
    (regional.length ? `<div class="menu-sep">${state.region.name.toUpperCase()}</div>${regional.map(item).join('')}` : '')
    + `<div class="menu-sep">WORLD</div>${global.map(item).join('')}`;
}

// ------------------------------------------------------------------ boot

async function init() {
  try {
    state.config = await api.config();
  } catch (err) {
    document.body.innerHTML = `<div class="wx-empty">prack server not reachable: ${err.message}</div>`;
    return;
  }
  state.region = state.config.regions[0];
  state.tz = state.region.timezone;
  state.today = isoInTz(state.tz);
  state.date = state.today;
  const tzName = new Intl.DateTimeFormat('en-GB', { timeZone: state.tz, timeZoneName: 'short' })
    .formatToParts(new Date()).find((p) => p.type === 'timeZoneName')?.value;
  $('#clk-lcl-label').textContent = tzName ? `LCL ${tzName}` : 'LCL';

  buildCategoryKeys();
  renderLegend();
  mapView = new MapView($('#map'));
  chart = new ProfileChart($('#chart'), (idx) => {
    state.cursor = idx;
    mapView.render();
    renderReadouts();
  });
  initDetails(createVario($('#vario')));
  calendar = new Calendar($('#calendar'));
  bindControls();

  on('select', (sel) => select(sel));
  on('date', (date) => goDate(date));
  on('live', () => goLive());
  on('follow', (value) => {
    state.follow = value && state.mode === 'live' && !!selectedLive();
    if (state.follow) mapView.follow(selectedLive());
    renderKeys();
    renderStatusLine();
  });

  const h = readHash();
  sortOptions();
  renderKeys();
  try {
    const live = await api.live();
    applyLive({ ...live, full: true });
  } catch (err) {
    toast(`LIVE: ${err.message}`);
  }
  connectLive();
  if (h.mode === 'history' && h.date) {
    await goDate(h.date, { keepSelection: true, fit: !h.id });
    if (h.id) select({ kind: 'flight', id: h.id });
  } else {
    await goLive();
    if (h.id && state.live.has(h.id)) select({ kind: 'live', id: h.id });
  }

  tickClock();
  setInterval(tickClock, 1000);
  pollStatus();
  setInterval(pollStatus, 5000);
  // today's flight list grows during the day
  setInterval(() => { if (state.mode === 'history' && state.date === state.today) loadDay(); }, 60000);
  // re-sync the selected live flight (fills terrain elevation of new fixes)
  setInterval(() => { if (selectedLive()?.flight_id) loadSelectedFlight(); }, 90000);
  window.prack = { state, mapView };
}

init();
