// Details panel of the selected aircraft / flight: instruments, barogram and statistics.
import { state, selectedLive, selectedFlightId, catColor } from './store.js';
import { $, $$, escapeHtml, fmtNum, fmtSigned, fmtTime, fmtDuration, compass } from './util.js';

let vario = null;
export function initDetails(varioGauge) {
  vario = varioGauge;
}

function summary() {
  if (state.flight) return state.flight;
  const id = selectedFlightId();
  return id ? state.flights.find((f) => f.id === id) || null : null;
}

/** The point shown by the instruments: chart cursor, else the live position, else the last fix. */
function currentPoint() {
  const tr = state.track;
  const a = selectedLive();
  if (tr && state.cursor != null && state.cursor < tr.t.length) return pointAt(tr, state.cursor);
  if (a) return { t: a.t, alt: a.alt, gnd: a.gnd, spd: a.spd, vs: a.vs, hdg: a.hdg };
  if (tr && tr.t.length) return pointAt(tr, tr.t.length - 1);
  return null;
}

function pointAt(tr, i) {
  return { t: tr.t[i], alt: tr.alt[i], gnd: tr.gnd[i], spd: tr.spd[i], vs: tr.vs[i], hdg: tr.hdg[i] };
}

function readout(label, value, unit = '', cls = '') {
  return `<div class="readout ${cls}"><span>${label}</span><b>${value}${unit ? `<small>${unit}</small>` : ''}</b></div>`;
}

export function renderReadouts() {
  const p = currentPoint();
  const agl = p && p.gnd != null ? p.alt - p.gnd : null;
  $('#readouts').innerHTML = [
    readout('ALT', fmtNum(p?.alt), 'm'),
    readout('AGL', fmtNum(agl), 'm'),
    readout('GS', fmtNum(p?.spd), 'km/h'),
    readout('HDG', p?.hdg == null ? '---' : String(Math.round(p.hdg)).padStart(3, '0'), '°'),
    readout('TIME', p ? fmtTime(p.t, state.tz, true) : '--:--:--', '', 'wide time'),
  ].join('');
  vario?.set(p?.vs ?? null);
  const cursor = $('#chart-cursor');
  if (state.cursor != null && p) {
    cursor.textContent = `${fmtTime(p.t, state.tz, true)}  ${fmtNum(p.alt)} m  AGL ${fmtNum(agl)}  ${fmtSigned(p.vs)} m/s`;
  } else cursor.textContent = '';
}

function stat(label, value, cls = '') {
  return `<div class="stat ${cls}"><span>${label}</span><b>${value}</b></div>`;
}

function weatherText(w) {
  if (!w) return null;
  const v = w.values || {};
  const wind = (lvl) => (v[`wind_speed_${lvl}`] != null ? `${fmtNum(v[`wind_direction_${lvl}`])}°/${fmtNum(v[`wind_speed_${lvl}`])}` : '--');
  const parts = [
    `${w.point.name} ${w.time.slice(11)} LT`,
    `SFC ${fmtNum(v.wind_direction_10m)}°/${fmtNum(v.wind_speed_10m)} km/h`,
    `850 ${wind('850hPa')} · 700 ${wind('700hPa')}`,
    `T ${fmtNum(v.temperature_2m, 1)}° · CLD ${fmtNum(v.cloud_cover)}%`,
    `BLH ${fmtNum(v.boundary_layer_height)} m · CAPE ${fmtNum(v.cape)}`,
    w.summary?.cloudbase_14 != null ? `BASE≈${fmtNum(w.summary.cloudbase_14)} m` : null,
  ];
  return parts.filter(Boolean).join('<br>');
}

function renderStats(f) {
  const tz = state.tz;
  const a = selectedLive();
  if (!f) {
    $('#stats').innerHTML = a
      ? [stat('SOURCE', escapeHtml(a.src)), stat('RX', escapeHtml(a.rx || '--')), stat('STATUS', a.flying ? 'AIRBORNE' : 'ON GROUND', 'wide')].join('')
      : '';
    return;
  }
  const rows = [
    stat('T/O', `${fmtTime(f.takeoff, tz)} LT`, 'hl'),
    stat('LDG', f.status === 'active' ? 'IN FLIGHT' : `${fmtTime(f.landing ?? f.end, tz)} LT`, 'hl'),
    stat('DURATION', fmtDuration(f.duration_s)),
    stat('TRACK', `${fmtNum(f.distance_km, 1)} km`),
    stat('MAX ALT', `${fmtNum(f.max_alt)} m`),
    stat('MAX AGL', `${fmtNum(f.max_agl)} m`),
    stat('MAX CLB', `${fmtSigned(f.max_climb)} m/s`),
    stat('MAX SNK', `${fmtSigned(f.max_sink)} m/s`),
    stat('ALT GAIN', `${fmtNum(f.gain)} m`),
    stat('MAX GS', `${fmtNum(f.max_speed)} km/h`),
    stat('T/O→LDG', `${fmtNum(f.straight_km, 1)} km`),
    stat('MAX FROM T/O', `${fmtNum(f.max_from_start_km, 1)} km`),
    stat('FIXES', fmtNum(f.fixes)),
    stat('SOURCE', escapeHtml(f.src)),
  ];
  const wx = weatherText(f.weather);
  rows.push(stat('WX @ T/O', wx || 'no weather stored for this day', 'wide wx'));
  if (f.notes) rows.push(stat('NOTES', escapeHtml(f.notes), 'wide'));
  $('#stats').innerHTML = rows.join('');
}

export function renderDetails() {
  const panel = $('#details');
  const a = selectedLive();
  const f = summary();
  if (!state.selected || (!a && !f)) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const cat = a?.cat ?? f?.cat ?? 'ot';
  const badge = $('#d-cat');
  badge.textContent = state.config.categories.find((c) => c.id === cat)?.label ?? 'OTH';
  badge.style.setProperty('--c', catColor(cat));
  $('#d-name').textContent = a?.name ?? f?.name ?? '--';
  const typeName = state.config.aircraft_types[a?.type ?? f?.type] || '';
  const reg = a?.reg ?? f?.reg;
  const cn = a?.cn ?? f?.cn;
  const model = a?.model ?? f?.model;
  $('#d-sub').textContent = [a?.id ?? f?.callsign, a?.src ?? f?.src, typeName, reg, cn && `CN ${cn}`, model]
    .filter(Boolean).join(' · ');

  const annun = $('#d-state');
  let text = 'GND';
  let cls = 'info';
  if (a) {
    text = a.flying ? 'AIRBORNE' : 'ON GND';
    cls = a.flying ? 'ok' : 'info';
  } else if (f) {
    if (f.status === 'active') { text = 'IN FLIGHT'; cls = 'ok'; }
    else if (!f.airborne) { text = 'GND TRACK'; cls = 'caution'; }
    else if (f.close_reason === 'gap') { text = 'SIGNAL LOST'; cls = 'caution'; }
    else { text = 'LANDED'; cls = 'info'; }
  }
  annun.className = `annun small ${cls}`;
  annun.querySelector('.annun-val').textContent = text;

  const flightId = selectedFlightId();
  const hidden = !!(f?.hidden ?? a?.hidden);
  const hideBtn = $('#btn-hide');
  hideBtn.disabled = !flightId;
  hideBtn.classList.toggle('on', hidden);
  hideBtn.classList.add('caution');
  hideBtn.lastChild.textContent = hidden ? 'HIDDEN' : 'HIDE';
  for (const b of $$('#export-group .key')) b.disabled = !flightId;

  renderStats(f);
  renderReadouts();
}
