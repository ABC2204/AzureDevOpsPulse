/* ── Global state ── */
const State = {
  fromDate: null,
  toDate: null,
  onDateChange: [],
};

/* ── Utils ── */
function fmt(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('ru-RU');
}

function fmtDate(s) {
  if (!s) return '—';
  return new Date(s).toLocaleDateString('ru-RU', {day:'2-digit', month:'2-digit', year:'numeric'});
}

function fmtDateTime(s) {
  if (!s) return '—';
  return new Date(s).toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit'});
}

function avatar(name) {
  const parts = (name || '?').split(' ');
  return (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase();
}

function avatarColor(email) {
  let h = 0;
  for (const c of (email || '')) h = ((h << 5) - h) + c.charCodeAt(0);
  const colors = ['#1f6feb','#388bfd','#3fb950','#d29922','#f85149','#8957e5','#db61a2','#39d353'];
  return colors[Math.abs(h) % colors.length];
}

function loading(el) {
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Загрузка...</div>';
}

function empty(el, msg = 'Нет данных за выбранный период') {
  el.innerHTML = `<div class="empty">${msg}</div>`;
}

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  Object.entries(params).forEach(([k, v]) => v != null && url.searchParams.set(k, v));
  if (State.fromDate) url.searchParams.set('from_date', State.fromDate);
  if (State.toDate)   url.searchParams.set('to_date',   State.toDate);
  const resp = await fetch(url);
  const json = await resp.json();
  if (!json.success) throw new Error(json.error || 'Ошибка API');
  return json.data;
}

/* ── Date filter ── */
function initDateFilter() {
  const fromInput = document.getElementById('date-from');
  const toInput   = document.getElementById('date-to');
  if (!fromInput || !toInput) return;

  // Default: last 6 months
  const today = new Date();
  const sixMonthsAgo = new Date();
  sixMonthsAgo.setMonth(today.getMonth() - 6);

  const ruLocale = {
    previousMonth: 'Предыдущий месяц',
    nextMonth: 'Следующий месяц',
    months: ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'],
    weekdays: ['Воскресенье','Понедельник','Вторник','Среда','Четверг','Пятница','Суббота'],
    weekdaysShort: ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'],
  };

  function dateToDisplay(d) {
    return [
      String(d.getDate()).padStart(2,'0'),
      String(d.getMonth()+1).padStart(2,'0'),
      d.getFullYear(),
    ].join('.');
  }

  function displayToISO(s) {
    const [d, m, y] = s.split('.');
    return `${y}-${m}-${d}`;
  }

  const pikaOpts = {
    i18n: ruLocale,
    firstDay: 1,
    format: 'DD.MM.YYYY',
    maxDate: today,
    toString(date) { return dateToDisplay(date); },
    parse(str) {
      const [d, m, y] = str.split('.');
      return new Date(parseInt(y), parseInt(m)-1, parseInt(d));
    },
  };

  const pikaFrom = new Pikaday({
    ...pikaOpts,
    field: fromInput,
    defaultDate: sixMonthsAgo,
    setDefaultDate: true,
    onSelect(date) {
      State.fromDate = dateToDisplay(date).split('.').reverse().join('-');
      pikaTo.setMinDate(date);
      triggerDateChange();
    },
  });

  const pikaTo = new Pikaday({
    ...pikaOpts,
    field: toInput,
    defaultDate: today,
    setDefaultDate: true,
    onSelect(date) {
      State.toDate = dateToDisplay(date).split('.').reverse().join('-');
      pikaFrom.setMaxDate(date);
      triggerDateChange();
    },
  });

  State.fromDate = [
    sixMonthsAgo.getFullYear(),
    String(sixMonthsAgo.getMonth()+1).padStart(2,'0'),
    String(sixMonthsAgo.getDate()).padStart(2,'0'),
  ].join('-');
  State.toDate = today.toISOString().slice(0,10);

  // Quick buttons
  document.querySelectorAll('.btn-quick').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.btn-quick').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const days = parseInt(btn.dataset.days);
      const t = new Date();
      const f = new Date();
      f.setDate(t.getDate() - days);
      pikaFrom.setDate(f);
      pikaTo.setDate(t);
      State.fromDate = f.toISOString().slice(0,10);
      State.toDate   = t.toISOString().slice(0,10);
      triggerDateChange();
    });
  });

  // Mark default active button
  document.querySelector('.btn-quick[data-days="180"]')?.classList.add('active');
}

function triggerDateChange() {
  State.onDateChange.forEach(fn => fn());
}

function onDateChange(fn) {
  State.onDateChange.push(fn);
}

/* ── Chart.js defaults ── */
function applyChartDefaults() {
  if (!window.Chart) return;
  Chart.defaults.color = '#8b949e';
  Chart.defaults.borderColor = '#30363d';
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 12;
}

/* ── Chart factories ── */
function makeLineChart(canvas, labels, datasets, opts = {}) {
  return new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: { legend: { display: datasets.length > 1 } },
      scales: {
        x: { grid: { color: '#21262d' }, ticks: { maxTicksLimit: 10 } },
        y: { grid: { color: '#21262d' }, beginAtZero: true, ...opts.y },
      },
      ...opts.extra,
    },
  });
}

function makeBarChart(canvas, labels, datasets, opts = {}) {
  return new Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      indexAxis: opts.horizontal ? 'y' : 'x',
      plugins: { legend: { display: opts.legend !== false && datasets.length > 1 } },
      scales: {
        x: { grid: { color: '#21262d' }, stacked: opts.stacked, beginAtZero: true },
        y: { grid: { color: '#21262d' }, stacked: opts.stacked, ticks: { maxTicksLimit: 12 } },
      },
    },
  });
}

function makeDoughnutChart(canvas, labels, data, colors) {
  return new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderColor: '#161b22', borderWidth: 2 }],
    },
    options: {
      responsive: true,
      cutout: '65%',
      plugins: { legend: { position: 'bottom', labels: { padding: 16, boxWidth: 12 } } },
    },
  });
}

function makeRadarChart(canvas, labels, datasets) {
  return new Chart(canvas, {
    type: 'radar',
    data: { labels, datasets },
    options: {
      responsive: true,
      scales: {
        r: {
          grid: { color: '#30363d' },
          pointLabels: { color: '#8b949e', font: { size: 11 } },
          ticks: { display: false },
          beginAtZero: true,
        },
      },
      plugins: { legend: { position: 'bottom', labels: { padding: 16, boxWidth: 12 } } },
    },
  });
}

/* ── Heatmap builder ── */
function buildHeatmap(container, dailyData, weeks = 26) {
  const map = {};
  (dailyData || []).forEach(r => { map[r.day] = r.count; });
  return new PulseHeatmap(container, map, { weeks });
}

/* ── Developer picker (compare page) ── */
class DevPicker {
  constructor(container, onChange) {
    this.container = container;
    this.onChange = onChange;
    this.selected = [];
    this.allDevs = [];
    this._build();
  }

  async load() {
    this.allDevs = await api('/api/developers');
    this._renderDropdown();
  }

  _build() {
    this.input = this.container.querySelector('.dev-picker-input');
    this.dropdown = this.container.querySelector('.dev-picker-dropdown');
    this.tags = this.container.querySelector('.selected-devs');

    this.input.addEventListener('focus', () => this.dropdown.classList.add('open'));
    this.input.addEventListener('input', () => this._renderDropdown());
    document.addEventListener('click', e => {
      if (!this.container.contains(e.target)) this.dropdown.classList.remove('open');
    });
  }

  _renderDropdown() {
    const q = (this.input.value || '').toLowerCase();
    const filtered = this.allDevs.filter(d =>
      (d.author_name || '').toLowerCase().includes(q) ||
      (d.author_email || '').toLowerCase().includes(q)
    ).slice(0, 30);

    this.dropdown.innerHTML = filtered.map(d => `
      <div class="dev-option ${this.selected.includes(d.author_email) ? 'selected' : ''}"
           data-email="${d.author_email}" data-name="${d.author_name || d.author_email}">
        <div class="avatar" style="background:${avatarColor(d.author_email)}">${avatar(d.author_name)}</div>
        <div>
          <div class="dev-name">${d.author_name || d.author_email}</div>
          <div class="dev-email">${d.author_email}</div>
        </div>
      </div>
    `).join('');

    this.dropdown.querySelectorAll('.dev-option').forEach(el => {
      el.addEventListener('click', () => this._toggle(el.dataset.email, el.dataset.name));
    });
  }

  _toggle(email, name) {
    if (this.selected.includes(email)) {
      this.selected = this.selected.filter(e => e !== email);
    } else if (this.selected.length < 5) {
      this.selected.push(email);
    }
    this._renderTags();
    this._renderDropdown();
    this.onChange(this.selected);
  }

  _renderTags() {
    this.tags.innerHTML = this.selected.map(email => {
      const dev = this.allDevs.find(d => d.author_email === email);
      const name = dev?.author_name || email;
      return `<span class="dev-tag" data-email="${email}">
        <div class="avatar" style="width:18px;height:18px;font-size:.6rem;background:${avatarColor(email)}">${avatar(name)}</div>
        ${name}
        <span class="dev-tag-remove" data-email="${email}">×</span>
      </span>`;
    }).join('');
    this.tags.querySelectorAll('.dev-tag-remove').forEach(el => {
      el.addEventListener('click', e => {
        e.stopPropagation();
        this._toggle(el.dataset.email, '');
      });
    });
  }
}

/* ── Nav active state ── */
function setActiveNav() {
  const path = location.pathname;
  document.querySelectorAll('.nav-item').forEach(el => {
    const href = el.dataset.href;
    if (href && (path === href || (href !== '/' && path.startsWith(href)))) {
      el.classList.add('active');
    }
  });
  document.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => {
      const href = el.dataset.href;
      if (href) location.href = href;
    });
  });
}

/* ── DOM ready ── */
document.addEventListener('DOMContentLoaded', () => {
  applyChartDefaults();
  setActiveNav();
  initDateFilter();
});
