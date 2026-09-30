// Classic round sailplane variometer (±5 m/s, zero at 9 o'clock, climb clockwise up).

const NS = 'http://www.w3.org/2000/svg';
const RANGE = 5;

function svg(tag, attrs, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

const angleFor = (v) => 180 + (Math.max(-RANGE, Math.min(RANGE, v)) / RANGE) * 180;

function polar(r, deg) {
  const a = (deg * Math.PI) / 180;
  return [r * Math.cos(a), r * Math.sin(a)];
}

function arc(r, fromDeg, toDeg) {
  const [x1, y1] = polar(r, fromDeg);
  const [x2, y2] = polar(r, toDeg);
  const large = Math.abs(toDeg - fromDeg) > 180 ? 1 : 0;
  return `M${x1} ${y1} A${r} ${r} 0 ${large} 1 ${x2} ${y2}`;
}

export function createVario(container) {
  const root = svg('svg', { viewBox: '-100 -100 200 200', role: 'img', 'aria-label': 'Variometer' }, container);
  svg('circle', { r: 97, fill: '#0b0f14', stroke: '#2b3542', 'stroke-width': 2 }, root);
  svg('circle', { r: 90, fill: '#020304', stroke: '#000', 'stroke-width': 2 }, root);
  svg('path', { d: arc(84, 180, 360), fill: 'none', stroke: '#39e58c', 'stroke-width': 3, opacity: 0.55 }, root);
  svg('path', { d: arc(84, 0, 180), fill: 'none', stroke: '#ffb020', 'stroke-width': 3, opacity: 0.45 }, root);
  for (let v = -RANGE; v <= RANGE + 1e-6; v += 0.5) {
    const major = Math.abs(v % 1) < 1e-6;
    const a = angleFor(v);
    const [x1, y1] = polar(major ? 66 : 72, a);
    const [x2, y2] = polar(80, a);
    svg('line', { x1, y1, x2, y2, stroke: '#dfe7ef', 'stroke-width': major ? 2.6 : 1.3, 'stroke-linecap': 'round' }, root);
    if (major && v !== -RANGE) {
      const [tx, ty] = polar(53, a);
      const t = svg('text', { x: tx, y: ty + 5, 'text-anchor': 'middle', fill: '#dfe7ef', 'font-size': 14, 'font-family': 'B612, monospace', 'font-weight': 700 }, root);
      t.textContent = String(Math.abs(v));
    }
  }
  const up = svg('text', { x: 0, y: -24, 'text-anchor': 'middle', fill: '#5cc8ec', 'font-size': 9, 'font-family': 'B612, sans-serif', 'letter-spacing': 2 }, root);
  up.textContent = 'CLB';
  const down = svg('text', { x: 0, y: 32, 'text-anchor': 'middle', fill: '#5cc8ec', 'font-size': 9, 'font-family': 'B612, sans-serif', 'letter-spacing': 2 }, root);
  down.textContent = 'm/s';
  const needle = svg('g', {}, root);
  needle.style.transformOrigin = '0px 0px';
  needle.style.transition = 'transform .45s cubic-bezier(.3,.7,.3,1)';
  svg('polygon', { points: '-80,0 -8,-5 10,0 -8,5', fill: '#f3f7fb', stroke: '#000', 'stroke-width': 1 }, needle);
  svg('circle', { r: 9, fill: '#1c232b', stroke: '#56606c', 'stroke-width': 2 }, root);
  const box = svg('rect', { x: 18, y: 38, width: 58, height: 22, rx: 3, fill: '#000', stroke: '#2b3542' }, root);
  box.setAttribute('opacity', 0.95);
  const readout = svg('text', { x: 71, y: 54, 'text-anchor': 'end', fill: '#39e58c', 'font-size': 15, 'font-family': 'B612, monospace', 'font-weight': 700 }, root);
  readout.textContent = '--';

  return {
    set(v) {
      const value = v == null || Number.isNaN(v) ? 0 : v;
      needle.style.transform = `rotate(${angleFor(value) - 180}deg)`;
      readout.textContent = v == null ? '--' : (v > 0 ? '+' : '') + v.toFixed(1);
      readout.setAttribute('fill', v != null && v < 0 ? '#ffb020' : '#39e58c');
    },
  };
}
