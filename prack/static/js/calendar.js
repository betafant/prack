// Month calendar with the number of flights per day (and weather availability).
import { state, emit, catColor } from './store.js';
import { api } from './api.js';
import { $, addDays, toast } from './util.js';

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

export class Calendar {
  constructor(el) {
    this.el = el;
    this.month = null; // 'YYYY-MM'
    el.addEventListener('click', (ev) => this.onClick(ev));
  }

  get open() {
    return !this.el.hidden;
  }

  toggle() {
    if (this.open) this.close();
    else this.show();
  }

  show() {
    this.month = (state.date || state.today).slice(0, 7);
    this.el.hidden = false;
    this.load();
  }

  close() {
    this.el.hidden = true;
  }

  gridStart() {
    const first = new Date(`${this.month}-01T12:00:00Z`);
    const dow = (first.getUTCDay() + 6) % 7; // Monday = 0
    return addDays(`${this.month}-01`, -dow);
  }

  async load() {
    const start = this.gridStart();
    const end = addDays(start, 41);
    this.render(null);
    try {
      const data = await api.days(start, end, state.region.id);
      if (this.open) this.render(data);
    } catch (err) {
      toast(`CALENDAR: ${err.message}`);
    }
  }

  render(data) {
    const [y, m] = this.month.split('-').map(Number);
    const byDate = new Map((data?.days || []).map((d) => [d.date, d]));
    const max = Math.max(1, ...[...byDate.values()].map((d) => d.total));
    const start = this.gridStart();
    const cells = [];
    for (let i = 0; i < 42; i++) {
      const iso = addDays(start, i);
      const d = byDate.get(iso);
      const classes = ['cal-day'];
      if (iso.slice(0, 7) !== this.month) classes.push('out');
      if (iso > state.today) classes.push('future');
      if (iso === state.today) classes.push('today');
      if (iso === state.date && state.mode === 'history') classes.push('selected');
      let inner = `<span>${Number(iso.slice(8))}</span>`;
      let style = '';
      if (d && d.total) {
        classes.push('has');
        style = ` style="--heat:${(Math.log1p(d.total) / Math.log1p(max)).toFixed(2)}"`;
        const bars = ['pg', 'hg', 'gl', 'ot']
          .filter((c) => d[c])
          .map((c) => `<i style="--c:${catColor(c)};flex:${d[c]}"></i>`)
          .join('');
        inner += `<span class="cal-count">${d.total}</span><span class="cal-bars">${bars}</span>`;
      }
      if (d?.weather) inner += '<span class="cal-wx" title="Weather stored"></span>';
      cells.push(`<button class="${classes.join(' ')}" data-date="${iso}"${style}${iso > state.today ? ' disabled' : ''}>${inner}</button>`);
    }
    const dows = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'].map((d) => `<div class="cal-dow">${d}</div>`).join('');
    this.el.innerHTML = `
      <div class="cal-head">
        <button class="key small icon-key" data-nav="-1">◀</button>
        <span class="cal-title">${MONTHS[m - 1]} ${y}</span>
        <button class="key small icon-key" data-nav="1"${this.month >= state.today.slice(0, 7) ? ' disabled' : ''}>▶</button>
      </div>
      <div class="cal-grid">${dows}${cells.join('')}</div>
      <div class="cal-foot">
        <span class="cal-note">${data ? 'FLIGHTS / DAY · <span style="color:var(--label)">●</span> WX STORED' : 'LOADING…'}</span>
        <button class="key small" data-act="today"><span class="led"></span>TODAY LIVE</button>
      </div>`;
  }

  onClick(ev) {
    const nav = ev.target.closest('[data-nav]');
    if (nav) {
      const [y, m] = this.month.split('-').map(Number);
      const d = new Date(Date.UTC(y, m - 1 + Number(nav.dataset.nav), 1));
      this.month = d.toISOString().slice(0, 7);
      this.load();
      return;
    }
    if (ev.target.closest('[data-act="today"]')) {
      this.close();
      emit('live');
      return;
    }
    const day = ev.target.closest('.cal-day');
    if (day && !day.disabled) {
      this.close();
      emit('date', day.dataset.date);
    }
  }
}
