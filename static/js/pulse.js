/**
 * GitHub-style contribution heatmap.
 * Usage: new PulseHeatmap(containerEl, {date: count}, {weeks: 26})
 */
class PulseHeatmap {
  constructor(container, data = {}, opts = {}) {
    this.container = container;
    this.data = data;
    this.opts = {
      weeks: opts.weeks || 26,
      cellSize: opts.cellSize || 11,
      cellGap: opts.cellGap || 2,
      showMonthLabels: opts.showMonthLabels !== false,
      showDayLabels: opts.showDayLabels !== false,
      colors: opts.colors || [
        'var(--pulse-0)',
        'var(--pulse-1)',
        'var(--pulse-2)',
        'var(--pulse-3)',
        'var(--pulse-4)',
      ],
    };
    this._tooltip = this._makeTooltip();
    this.render();
  }

  _makeTooltip() {
    let el = document.getElementById('heatmap-tooltip');
    if (!el) {
      el = document.createElement('div');
      el.id = 'heatmap-tooltip';
      el.className = 'tooltip';
      document.body.appendChild(el);
    }
    return el;
  }

  _colorIndex(count) {
    if (!count || count === 0) return 0;
    if (count <= 2) return 1;
    if (count <= 5) return 2;
    if (count <= 10) return 3;
    return 4;
  }

  _dateKey(d) {
    return d.toISOString().slice(0, 10);
  }

  _addDays(d, n) {
    const r = new Date(d);
    r.setDate(r.getDate() + n);
    return r;
  }

  _isoWeek(d) {
    return d.toISOString().slice(0, 10);
  }

  render() {
    this.container.innerHTML = '';

    const cs = this.opts.cellSize;
    const gap = this.opts.cellGap;
    const step = cs + gap;
    const weeks = this.opts.weeks;

    const dayLabels = ['', 'Пн', '', 'Ср', '', 'Пт', ''];
    const monthNames = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];

    // Start from weeks ago, aligned to Monday
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const todayDow = (today.getDay() + 6) % 7; // 0=Mon
    const startDay = this._addDays(today, -(weeks * 7 - 1 + todayDow));

    const dayLabelWidth = this.opts.showDayLabels ? 28 : 0;
    const monthLabelHeight = this.opts.showMonthLabels ? 18 : 0;

    const svgW = dayLabelWidth + weeks * step + gap;
    const svgH = monthLabelHeight + 7 * step + gap;

    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('width', svgW);
    svg.setAttribute('height', svgH);
    svg.style.display = 'block';

    // Day labels
    if (this.opts.showDayLabels) {
      dayLabels.forEach((label, i) => {
        if (!label) return;
        const t = document.createElementNS(ns, 'text');
        t.setAttribute('x', dayLabelWidth - 6);
        t.setAttribute('y', monthLabelHeight + i * step + cs - 1);
        t.setAttribute('text-anchor', 'end');
        t.setAttribute('dominant-baseline', 'middle');
        t.textContent = label;
        svg.appendChild(t);
      });
    }

    // Month labels + cells
    let prevMonth = -1;
    for (let w = 0; w < weeks; w++) {
      for (let d = 0; d < 7; d++) {
        const date = this._addDays(startDay, w * 7 + d);
        const key = this._dateKey(date);
        const count = this.data[key] || 0;
        const ci = this._colorIndex(count);
        const x = dayLabelWidth + w * step;
        const y = monthLabelHeight + d * step;

        // Month label
        if (this.opts.showMonthLabels && d === 0 && date.getMonth() !== prevMonth) {
          prevMonth = date.getMonth();
          const t = document.createElementNS(ns, 'text');
          t.setAttribute('x', x);
          t.setAttribute('y', monthLabelHeight - 4);
          t.textContent = monthNames[date.getMonth()];
          svg.appendChild(t);
        }

        const rect = document.createElementNS(ns, 'rect');
        rect.setAttribute('x', x);
        rect.setAttribute('y', y);
        rect.setAttribute('width', cs);
        rect.setAttribute('height', cs);
        rect.setAttribute('rx', 2);
        rect.setAttribute('ry', 2);
        rect.style.fill = this.opts.colors[ci];

        if (date > today) {
          rect.style.opacity = '0.2';
        }

        // Tooltip
        rect.addEventListener('mouseenter', (e) => {
          const dateStr = date.toLocaleDateString('ru-RU', {day:'numeric',month:'long',year:'numeric'});
          this._tooltip.innerHTML = `<strong>${count} коммит${this._plural(count)}</strong><span>${dateStr}</span>`;
          this._tooltip.style.display = 'block';
        });
        rect.addEventListener('mousemove', (e) => {
          this._tooltip.style.left = (e.clientX + 12) + 'px';
          this._tooltip.style.top  = (e.clientY - 40) + 'px';
        });
        rect.addEventListener('mouseleave', () => {
          this._tooltip.style.display = 'none';
        });

        svg.appendChild(rect);
      }
    }

    const wrap = document.createElement('div');
    wrap.className = 'heatmap-wrap';
    wrap.appendChild(svg);

    // Legend
    const legend = document.createElement('div');
    legend.className = 'heatmap-legend';
    legend.innerHTML = 'Меньше';
    this.opts.colors.forEach(c => {
      const cell = document.createElement('span');
      cell.className = 'heatmap-legend-cell';
      cell.style.background = c;
      // Resolve CSS var for inline style
      legend.appendChild(cell);
    });
    legend.innerHTML += '&nbsp;Больше';

    this.container.appendChild(wrap);
    this.container.appendChild(legend);
  }

  _plural(n) {
    if (n % 10 === 1 && n % 100 !== 11) return '';
    if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) return 'а';
    return 'ов';
  }

  update(data) {
    this.data = data;
    this.render();
  }
}
