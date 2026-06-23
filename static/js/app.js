/* ── Global state ── */
const State = {
  fromDate: null,
  toDate: null,
  onDateChange: [],
  selectedTeamId: 0,
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

  const today = new Date();
  const sixMonthsAgo = new Date();
  sixMonthsAgo.setMonth(today.getMonth() - 6);

  // Restore saved dates from localStorage, fall back to last 6 months
  const savedFrom = localStorage.getItem('pulse_dateFrom');
  const savedTo   = localStorage.getItem('pulse_dateTo');
  const initFrom  = savedFrom ? new Date(savedFrom) : sixMonthsAgo;
  const initTo    = savedTo   ? new Date(savedTo)   : today;

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

  function saveDates() {
    localStorage.setItem('pulse_dateFrom', State.fromDate);
    localStorage.setItem('pulse_dateTo',   State.toDate);
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
    defaultDate: initFrom,
    setDefaultDate: true,
    onSelect(date) {
      State.fromDate = dateToDisplay(date).split('.').reverse().join('-');
      pikaTo.setMinDate(date);
      saveDates();
      triggerDateChange();
    },
  });

  const pikaTo = new Pikaday({
    ...pikaOpts,
    field: toInput,
    defaultDate: initTo,
    setDefaultDate: true,
    onSelect(date) {
      State.toDate = dateToDisplay(date).split('.').reverse().join('-');
      pikaFrom.setMaxDate(date);
      saveDates();
      triggerDateChange();
    },
  });

  State.fromDate = initFrom.toISOString().slice(0,10);
  State.toDate   = initTo.toISOString().slice(0,10);

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
      saveDates();
      triggerDateChange();
    });
  });

  // Mark active quick button based on restored dates
  const diffDays = Math.round((initTo - initFrom) / 86400000);
  const matchBtn = document.querySelector(`.btn-quick[data-days="${diffDays}"]`);
  if (matchBtn) {
    matchBtn.classList.add('active');
  } else if (!savedFrom) {
    document.querySelector('.btn-quick[data-days="180"]')?.classList.add('active');
  }
}

function triggerDateChange() {
  State.onDateChange.forEach(fn => fn());
}

function onDateChange(fn) {
  State.onDateChange.push(fn);
}

/* ── Theme helpers ── */
window._chartRegistry = [];

function getCSSVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ── Chart.js defaults ── */
function applyChartDefaults() {
  if (!window.Chart) return;
  Chart.defaults.color = getCSSVar('--text-muted');
  Chart.defaults.borderColor = getCSSVar('--border');
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 12;
}

function refreshChartTheme() {
  applyChartDefaults();
  const gridColor = getCSSVar('--surface2');
  const borderColor = getCSSVar('--border');
  const textMuted = getCSSVar('--text-muted');
  const surface = getCSSVar('--surface');
  window._chartRegistry.forEach(chart => {
    if (!chart || chart.destroyed) return;
    const scales = chart.config.options.scales || {};
    Object.entries(scales).forEach(([key, scale]) => {
      if (scale.grid) scale.grid.color = key === 'r' ? borderColor : gridColor;
    });
    if (scales.r?.pointLabels) scales.r.pointLabels.color = textMuted;
    if (chart.config.type === 'doughnut') {
      chart.config.data.datasets.forEach(ds => { ds.borderColor = surface; });
    }
    chart.update('none');
  });
}

/* ── Chart factories ── */
function makeLineChart(canvas, labels, datasets, opts = {}) {
  const gridColor = getCSSVar('--surface2');
  const chart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      plugins: { legend: { display: datasets.length > 1 } },
      scales: {
        x: { grid: { color: gridColor }, ticks: { maxTicksLimit: 10 } },
        y: { grid: { color: gridColor }, beginAtZero: true, ...opts.y },
      },
      ...opts.extra,
    },
  });
  window._chartRegistry.push(chart);
  return chart;
}

function makeBarChart(canvas, labels, datasets, opts = {}) {
  const gridColor = getCSSVar('--surface2');
  const chart = new Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      indexAxis: opts.horizontal ? 'y' : 'x',
      plugins: { legend: { display: opts.legend !== false && datasets.length > 1 } },
      scales: {
        x: { grid: { color: gridColor }, stacked: opts.stacked, beginAtZero: true },
        y: { grid: { color: gridColor }, stacked: opts.stacked, ticks: { maxTicksLimit: 12 } },
      },
    },
  });
  window._chartRegistry.push(chart);
  return chart;
}

function makeDoughnutChart(canvas, labels, data, colors) {
  const chart = new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data, backgroundColor: colors, borderColor: getCSSVar('--surface'), borderWidth: 2 }],
    },
    options: {
      responsive: true,
      cutout: '65%',
      plugins: { legend: { position: 'bottom', labels: { padding: 16, boxWidth: 12 } } },
    },
  });
  window._chartRegistry.push(chart);
  return chart;
}

function makeRadarChart(canvas, labels, datasets) {
  const chart = new Chart(canvas, {
    type: 'radar',
    data: { labels, datasets },
    options: {
      responsive: true,
      scales: {
        r: {
          grid: { color: getCSSVar('--border') },
          pointLabels: { color: getCSSVar('--text-muted'), font: { size: 11 } },
          ticks: { display: false },
          beginAtZero: true,
        },
      },
      plugins: { legend: { position: 'bottom', labels: { padding: 16, boxWidth: 12 } } },
    },
  });
  window._chartRegistry.push(chart);
  return chart;
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

    this.input.addEventListener('focus', () => {
      this._renderDropdown();
      this.dropdown.classList.add('open');
    });
    this.input.addEventListener('input', () => this._renderDropdown());
    // mousedown fires before blur, so the dropdown stays open long enough for selection
    this.dropdown.addEventListener('mousedown', e => e.preventDefault());
    this.input.addEventListener('blur', () => {
      setTimeout(() => this.dropdown.classList.remove('open'), 150);
    });
  }

  _renderDropdown() {
    const q = (this.input.value || '').toLowerCase();
    const filtered = this.allDevs.filter(d =>
      (d.author_name || '').toLowerCase().includes(q) ||
      (d.author_email || '').toLowerCase().includes(q)
    ).slice(0, 50);

    if (!filtered.length) {
      this.dropdown.innerHTML = '<div class="empty" style="padding:12px">Никого не найдено</div>';
      return;
    }

    this.dropdown.innerHTML = filtered.map(d => `
      <div class="dev-option ${this.selected.includes(d.author_email) ? 'selected' : ''}"
           data-email="${d.author_email}" data-name="${d.author_name || d.author_email}">
        <div class="avatar" style="background:${avatarColor(d.author_email)}">${avatar(d.author_name)}</div>
        <div>
          <div class="dev-name">${d.author_name || d.author_email}</div>
          <div class="dev-email">${d.author_email}</div>
        </div>
        ${this.selected.includes(d.author_email) ? '<svg style="margin-left:auto;flex-shrink:0" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>' : ''}
      </div>
    `).join('');

    this.dropdown.querySelectorAll('.dev-option').forEach(el => {
      el.addEventListener('mousedown', e => {
        e.preventDefault();
        this._toggle(el.dataset.email, el.dataset.name);
        this.input.focus();
      });
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

/* ── Team filter ── */

/**
 * Renders a team dropdown inside `containerId` and wires it up.
 * `onSelect` is called (with no args) after team selection is saved server-side.
 */
async function initTeamFilter(containerId, onSelect) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;

  const [teamsJson, selJson] = await Promise.all([
    fetch('/api/teams').then(r => r.json()),
    fetch('/api/teams/select').then(r => r.json()),
  ]);

  const teams = teamsJson.data || [];
  State.selectedTeamId = (selJson.data?.team_id) || 0;

  if (!teams.length) {
    wrap.style.display = 'none';
    return;
  }

  wrap.innerHTML = `
    <div style="display:flex;align-items:center;gap:6px">
      <label style="font-size:.85rem;color:var(--text-muted);white-space:nowrap">Команда</label>
      <select id="team-select-dropdown" style="height:34px;padding:0 8px;border:1px solid var(--border);
              border-radius:var(--radius-sm);background:var(--surface2);color:var(--text);
              font-size:.85rem;cursor:pointer;min-width:140px">
        <option value="0">Все команды</option>
        ${teams.map(t => `<option value="${t.id}" ${t.id == State.selectedTeamId ? 'selected' : ''}>${t.name} (${t.members.length})</option>`).join('')}
      </select>
    </div>`;

  document.getElementById('team-select-dropdown').addEventListener('change', async e => {
    const teamId = parseInt(e.target.value) || 0;
    State.selectedTeamId = teamId;
    await fetch('/api/teams/select', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ team_id: teamId }),
    });
    onSelect();
  });
}

/* ── Theme toggle ── */
function initThemeToggle() {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  const sunIcon  = btn.querySelector('.icon-sun');
  const moonIcon = btn.querySelector('.icon-moon');

  function applyTheme(isLight) {
    if (isLight) {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    if (sunIcon)  sunIcon.style.display  = isLight ? 'none'  : '';
    if (moonIcon) moonIcon.style.display = isLight ? ''      : 'none';
    localStorage.setItem('theme', isLight ? 'light' : 'dark');
  }

  // Sync icon with current theme (set by anti-FOUC script)
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  if (sunIcon)  sunIcon.style.display  = isLight ? 'none' : '';
  if (moonIcon) moonIcon.style.display = isLight ? ''     : 'none';

  btn.addEventListener('click', () => {
    const nowLight = document.documentElement.getAttribute('data-theme') === 'light';
    applyTheme(!nowLight);
    refreshChartTheme();
  });
}

/* ── DOM ready ── */
document.addEventListener('DOMContentLoaded', () => {
  applyChartDefaults();
  setActiveNav();
  initDateFilter();
  initThemeToggle();
});
