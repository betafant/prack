// Weather drawer: stored daily weather of the region (Open-Meteo) for the selected day.
import { state } from './store.js';
import { $, escapeHtml, fmtNum, fmtSigned, fmtDateLong, fmtTime } from './util.js';

let gradientChart = null;

function wind(w) {
  if (!w || w.speed == null) return '<span class="muted">--</span>';
  return `<span class="wx-arrow" style="transform:rotate(${(w.dir ?? 0) + 180}deg)">↑</span> ${fmtNum(w.dir)}°/${fmtNum(w.speed)}`;
}

function nearestPointId(points) {
  const f = state.flight;
  const pos = f?.takeoff_pos;
  if (!pos || !points.length) return null;
  let best = null;
  let bestD = Infinity;
  for (const p of points) {
    const d = (p.lat - pos[1]) ** 2 + ((p.lon - pos[0]) * Math.cos((pos[1] * Math.PI) / 180)) ** 2;
    if (d < bestD) { bestD = d; best = p.id; }
  }
  return best;
}

export function renderWeather(report, loading = false) {
  const body = $('#wx-body');
  $('#wx-date').textContent = fmtDateLong(state.date);
  if (gradientChart) { gradientChart.destroy(); gradientChart = null; }
  if (loading) {
    body.innerHTML = '<div class="wx-empty">LOADING…</div>';
    return;
  }
  if (!report || !report.points?.length) {
    body.innerHTML = `<div class="wx-empty">NO WEATHER STORED FOR THIS DAY<br><br>Press FETCH to download it from Open-Meteo.<br>
      <span class="muted">Today and yesterday are stored automatically.</span></div>`;
    return;
  }
  const demo = (report.source || '').startsWith('demo');
  const fetched = report.fetched_at ? new Date(report.fetched_at + 'Z') : null;
  const meta = `SOURCE ${escapeHtml(report.source || '?')}${demo ? ' <span class="demo">(SYNTHETIC DEMO DATA)</span>' : ''}`
    + ` · ${report.final ? 'FINAL' : 'PRELIMINARY'}`
    + (fetched ? ` · FETCHED ${fetched.toISOString().slice(0, 16).replace('T', ' ')} UTC` : '')
    + ' · VALUES AT 13 LT UNLESS NOTED';
  const grads = report.gradients.map((g) => `
    <div class="wx-grad">
      <span>${escapeHtml(g.name)}</span>
      <b>${fmtSigned(g.value)} hPa</b>${g.label ? `<em>${escapeHtml(g.label.toUpperCase())}</em>` : ''}
      <small>DAY RANGE ${fmtSigned(g.min)} … ${fmtSigned(g.max)} hPa · THRESHOLD ±${fmtNum(g.threshold, 0)}</small>
    </div>`).join('');
  const near = nearestPointId(report.points);
  const rows = report.points.map((p) => {
    const s = p.summary || {};
    return `<tr class="${p.id === near ? 'near' : ''}" title="${escapeHtml(p.name)} · ${fmtNum(p.elevation)} m">
      <td>${escapeHtml(p.name)}</td>
      <td>${fmtNum(s.t_max, 0)}°</td>
      <td>${fmtNum(s.cloud_mean)}%</td>
      <td>${fmtNum(s.cloudbase_14)}</td>
      <td>${fmtNum(s.blh_max)}</td>
      <td>${fmtNum(s.cape_max)}</td>
      <td>${wind(s.wind_850)}</td>
      <td>${wind(s.wind_700)}</td>
      <td>${fmtNum(s.gust_max)}</td>
      <td>${fmtNum(s.sunshine_h, 1)}</td>
      <td>${fmtNum(s.precip_mm, 1)}</td>
    </tr>`;
  }).join('');
  body.innerHTML = `
    <div class="wx-meta">${meta}</div>
    <div class="wx-gradients">${grads}</div>
    <table class="wx-table">
      <thead><tr><th>POINT</th><th>TMAX</th><th>CLD</th><th>BASE m</th><th>BLH m</th><th>CAPE</th><th>850 hPa</th><th>700 hPa</th><th>GUST</th><th>SUN h</th><th>RR mm</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="wx-chart" id="wx-chart"></div>
    <div class="wx-meta">BASE = cumulus base estimate (Espy) at 14 LT · BLH / CAPE = daytime max · winds dir°/km/h · pressure indices = hourly difference of mean sea level pressure</div>`;
  drawGradients(report);
}

function drawGradients(report) {
  const series = report.gradients.filter((g) => g.series?.length);
  const el = $('#wx-chart');
  if (!series.length || !el) return;
  const times = series[0].series.map(([t]) => Date.parse(t + ':00Z') / 1000); // local wall time shown as-is
  const data = [times, ...series.map((g) => g.series.map(([, v]) => v))];
  const colors = ['#ff3ec9', '#5cc8ec', '#ffb020'];
  gradientChart = new uPlot({
    width: Math.max(300, el.clientWidth || 480),
    height: 150,
    legend: { show: true, live: false },
    cursor: { y: false },
    scales: { x: { time: true } },
    axes: [
      { stroke: '#7b8ba0', grid: { stroke: '#16202a' }, font: '10px "B612"', values: (u, s) => s.map((t) => fmtTime(t, 'UTC')) },
      { stroke: '#7b8ba0', grid: { stroke: '#16202a' }, font: '10px "B612"', size: 44, values: (u, s) => s.map((v) => `${v}`) },
    ],
    series: [{}, ...series.map((g, i) => ({ label: `${g.id.toUpperCase()} hPa`, stroke: colors[i % 3], width: 2, points: { show: false } }))],
  }, data, el);
}
