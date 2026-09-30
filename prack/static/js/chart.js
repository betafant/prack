// Barogram: altitude over time with the terrain below the aircraft as a filled area.
import { fmtTime } from './util.js';

const AXIS = {
  stroke: '#7b8ba0',
  grid: { stroke: '#16202a', width: 1 },
  ticks: { stroke: '#1d2833', width: 1, size: 4 },
  font: '10px "B612", monospace',
};

export class ProfileChart {
  constructor(el, onCursor) {
    this.el = el;
    this.onCursor = onCursor;
    this.u = null;
    this.tz = 'UTC';
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(el);
  }

  size() {
    return { width: Math.max(200, this.el.clientWidth), height: Math.max(90, this.el.clientHeight) };
  }

  resize() {
    if (this.u) this.u.setSize(this.size());
  }

  clear() {
    if (this.u) { this.u.destroy(); this.u = null; }
  }

  setData(track, tz) {
    if (!track || track.t.length < 2) { this.clear(); return false; }
    const data = [track.t, track.gnd, track.alt];
    if (this.u && this.tz === tz) {
      this.u.setData(data);
      return true;
    }
    this.clear();
    this.tz = tz;
    const opts = {
      ...this.size(),
      pxAlign: 0,
      legend: { show: false },
      cursor: {
        points: { size: 9, fill: '#ff3ec9', stroke: '#fff', width: 2 },
        drag: { x: true, y: false, setScale: true },
        y: false,
      },
      scales: {
        x: { time: true },
        y: {
          range: (u, min, max) => {
            const lo = Math.floor((Math.max(-400, min) - 150) / 100) * 100;
            const hi = Math.ceil((max + 200) / 100) * 100;
            return [lo, hi];
          },
        },
      },
      axes: [
        { ...AXIS, space: 70, values: (u, splits, _i, _space, incr) => splits.map((ts) => fmtTime(ts, this.tz, incr < 60)) },
        { ...AXIS, size: 54, values: (u, splits) => splits.map((v) => `${v} m`) },
      ],
      series: [
        {},
        {
          label: 'Ground',
          stroke: '#9b774c',
          width: 1,
          fill: (u) => {
            const g = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
            g.addColorStop(0, 'rgba(138,106,69,.95)');
            g.addColorStop(1, 'rgba(46,34,22,.95)');
            return g;
          },
          fillTo: (u) => u.scales.y.min,
          points: { show: false },
          spanGaps: true,
        },
        {
          label: 'Altitude',
          stroke: '#ff3ec9',
          width: 2,
          points: { show: false },
        },
      ],
      hooks: {
        setCursor: [(u) => this.onCursor(u.cursor.idx ?? null)],
      },
    };
    this.u = new uPlot(opts, data, this.el);
    return true;
  }
}
