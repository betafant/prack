// Application state and a tiny event bus.
import { storageGet, storageSet } from './util.js';

const bus = new EventTarget();
export const on = (name, fn) => bus.addEventListener(name, (e) => fn(e.detail));
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

export const state = {
  config: null,
  region: null,
  tz: 'UTC',
  mode: 'live', // live | history
  today: null,
  date: null,
  live: new Map(), // callsign -> aircraft (with trail)
  flights: [], // flights of the selected day
  tracks: [], // simplified tracks of the selected day
  selected: null, // { kind: 'live', id } | { kind: 'flight', id }
  flight: null, // details of the selected flight
  track: null, // columnar track of the selected flight
  cursor: null, // index into track under the chart cursor
  cats: storageGet('cats', { pg: true, hg: true, gl: true, ot: true }),
  showHidden: storageGet('showHidden', false),
  showGround: storageGet('showGround', false),
  sort: storageGet('sort', { live: 'alt', history: 'takeoff' }),
  search: '',
  view3d: false,
  follow: false,
  trails: storageGet('trails', true),
  labels: storageGet('labels', true),
  basemap: storageGet('basemap', null),
  weather: null,
  wxOpen: false,
  status: null, // last /api/status response
  // bumped whenever the corresponding data changes (lets the map reuse layer data)
  versions: { live: 0, filters: 0, day: 0, track: 0 },
};

export function persist() {
  storageSet('cats', state.cats);
  storageSet('showHidden', state.showHidden);
  storageSet('showGround', state.showGround);
  storageSet('sort', state.sort);
  storageSet('trails', state.trails);
  storageSet('labels', state.labels);
  storageSet('basemap', state.basemap);
}

export function catOf(type) {
  for (const c of state.config.categories) if (c.types.includes(type)) return c.id;
  return 'ot';
}

export function catColor(cat) {
  const c = state.config.categories.find((x) => x.id === cat);
  return c ? c.color : '#c084fc';
}

export function passesFilters(item) {
  if (!state.cats[item.cat]) return false;
  if (item.hidden && !state.showHidden) return false;
  if (state.search) {
    const q = state.search.toLowerCase();
    const hay = [item.name, item.callsign, item.id, item.reg, item.cn, item.pilot, item.model, item.address]
      .filter(Boolean).join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

/** Live aircraft that should be shown (map + list). */
export function visibleLive() {
  const out = [];
  for (const a of state.live.values()) {
    if (!a.flying && !state.showGround) continue;
    if (passesFilters(a)) out.push(a);
  }
  return out;
}

/** Flights of the selected day that should be shown. */
export function visibleFlights() {
  return state.flights.filter((f) => (f.airborne || state.showGround) && passesFilters(f));
}

export function selectedLive() {
  return state.selected?.kind === 'live' ? state.live.get(state.selected.id) : null;
}

export function selectedFlightId() {
  if (!state.selected) return null;
  if (state.selected.kind === 'flight') return state.selected.id;
  return selectedLive()?.flight_id ?? null;
}
