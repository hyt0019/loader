// HYT Container Load Planner —— 网页主逻辑。
// 输入（集装箱、清单、参数）与最近一次结果自动保存在浏览器 localStorage 中，刷新不丢失；
// 恢复出的旧结果会标明是上次的结果，开始新计算时旧结果立即撤下。
import { cancelOnUnload, downloadFrom, getJSON, postForm, postJSON, saveBlob } from './api.js';
import { categoryColor, computeEstimate, escapeHtml, fixed, g, isValidRow, typeLabel } from './format.js';
import { PlanViewer } from './viewer3d.js';

const INPUT_KEY = 'hyt-loader:inputs:v1';
const RESULT_KEY = 'hyt-loader:result:v1';
const CARGO_TYPES = ['木箱', '纸箱', '托盘'];
const POLL_MS = 1000;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  config: null,
  workmode: 'auto',          // auto | manual
  metric: 'none',            // none | weight | volumetric（自动模式下的底层优先指标）
  strict: false,             // 严格执行底层优先指标：重 / 密度大的不压在轻 / 密度小的上面
  threshold: 100,
  calcmode: 'standard',      // standard | enhanced
  budget: 600,
  shipNo: '',
  container: [null, null, null],
  rows: [],
  cargoFmt: 'txt',
  result: null,
  highlight: new Set(),
  region: null,
  job: null,
  restored: false,           // 当前显示的结果是否是上次打开网页时留下的
  skipDeleteConfirm: false,  // 「本次使用不再提示删除确认」，仅在当前页面有效
};
let nextRowId = 1;
let viewer = null;

// --------------------------------------------------------------------------- //
//                                  持久化                                     //
// --------------------------------------------------------------------------- //
let saveTimer = 0;
function saveInputs() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      localStorage.setItem(INPUT_KEY, JSON.stringify({
        workmode: state.workmode, metric: state.metric, strict: state.strict, threshold: state.threshold,
        calcmode: state.calcmode, budget: state.budget, shipNo: state.shipNo,
        container: state.container, rows: state.rows.map(stripRow), cargoFmt: state.cargoFmt,
      }));
    } catch { /* 隐私模式或存储已满：不影响使用 */ }
  }, 250);
}

function saveResult() {
  try {
    if (state.result) localStorage.setItem(RESULT_KEY, JSON.stringify(state.result));
    else localStorage.removeItem(RESULT_KEY);
  } catch {
    try { localStorage.removeItem(RESULT_KEY); } catch { /* ignore */ }
  }
}

function restoreState() {
  try {
    const saved = JSON.parse(localStorage.getItem(INPUT_KEY) || 'null');
    if (saved) {
      for (const key of ['workmode', 'metric', 'strict', 'threshold', 'calcmode', 'budget', 'shipNo', 'cargoFmt']) {
        if (saved[key] !== undefined) state[key] = saved[key];
      }
      if (Array.isArray(saved.container) && saved.container.length === 3) state.container = saved.container;
      if (Array.isArray(saved.rows)) state.rows = saved.rows.map(newRow);
    }
    const result = JSON.parse(localStorage.getItem(RESULT_KEY) || 'null');
    if (result && result.plan && Array.isArray(result.plan.packing_plan)) {
      state.result = result;
      state.restored = true;
    }
  } catch { /* 存储内容损坏时从空白开始 */ }
}

// --------------------------------------------------------------------------- //
//                                 货物行                                      //
// --------------------------------------------------------------------------- //
function newRow(src = {}) {
  const num = (v, d) => (Number.isFinite(Number(v)) && v !== '' && v !== null ? Number(v) : d);
  return {
    id: nextRowId++,
    l: num(src.l, 0), w: num(src.w, 0), h: num(src.h, 0),
    n: Math.max(0, Math.round(num(src.n, 1))), wt: num(src.wt, 0),
    type: CARGO_TYPES.includes(typeLabel(src.type)) ? typeLabel(src.type) : '木箱',
    no_flip: !!src.no_flip, prio: Math.round(num(src.prio, 0)), desc: String(src.desc ?? ''),
    no_stack: !!src.no_stack,
  };
}

const stripRow = ({ id, ...row }) => row;

const TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>';

function rowElement(row) {
  const tr = document.createElement('tr');
  tr.dataset.id = row.id;
  const numCell = (key, label, attrs) => `<td><input type="number" data-k="${key}" aria-label="${label}" ${attrs}></td>`;
  tr.innerHTML = `
    <td class="col-desc"><input type="text" data-k="desc" maxlength="200" placeholder="如：电机端盖 M8" aria-label="零件描述"></td>
    ${numCell('l', '长(m)', 'min="0" step="0.01" inputmode="decimal" placeholder="0"')}
    ${numCell('w', '宽(m)', 'min="0" step="0.01" inputmode="decimal" placeholder="0"')}
    ${numCell('h', '高(m)', 'min="0" step="0.01" inputmode="decimal" placeholder="0"')}
    ${numCell('n', '数量', 'min="1" step="1" inputmode="numeric"')}
    ${numCell('wt', '重量(kg)', 'min="0" step="1" inputmode="decimal" placeholder="0"')}
    <td><select data-k="type" aria-label="类型">${CARGO_TYPES.map((t) => `<option>${t}</option>`).join('')}</select></td>
    <td class="center"><input type="checkbox" data-k="no_flip" aria-label="禁止倒放"
      title="勾选后此类货物不可倒放：落地面固定为长×宽，高只能是原始高度"></td>
    <td class="center"><input type="checkbox" data-k="no_stack" aria-label="不可压"
      title="勾选后此类货物顶上不放任何货物，适合带仪表、怕压的设备"></td>
    <td class="col-prio"><input type="number" data-k="prio" step="1" aria-label="优先级" title="数值越大越优先放到底层"></td>
    <td class="center"><button type="button" class="icon-btn" data-act="del" title="删除此行" aria-label="删除此行">${TRASH_SVG}</button></td>`;
  const set = (key, value) => { $(`[data-k="${key}"]`, tr).value = value; };
  set('desc', row.desc);
  for (const key of ['l', 'w', 'h', 'wt']) set(key, row[key] ? String(row[key]) : '');
  set('n', String(row.n));
  set('prio', String(row.prio));
  set('type', row.type);
  $('[data-k="no_flip"]', tr).checked = row.no_flip;
  $('[data-k="no_stack"]', tr).checked = row.no_stack;
  return tr;
}

function renderCargoTable() {
  const tbody = $('#cargo-table tbody');
  tbody.replaceChildren(...state.rows.map(rowElement));
  $('#cargo-empty').hidden = state.rows.length > 0;
  onCargoChanged();
}

function onCargoChanged() {
  renderEstimate();
  renderExportButton();
  saveInputs();
}

function bindCargoTable() {
  const tbody = $('#cargo-table tbody');
  const findRow = (el) => state.rows.find((r) => r.id === Number(el.closest('tr').dataset.id));
  tbody.addEventListener('input', (event) => {
    const input = event.target;
    const key = input.dataset.k;
    const row = key && findRow(input);
    if (!row) return;
    if (key === 'desc') row.desc = input.value;
    else if (key === 'no_flip') row.no_flip = input.checked;
    else if (key === 'no_stack') row.no_stack = input.checked;
    else if (key === 'type') row.type = input.value;
    else {
      const value = input.value === '' ? 0 : Number(input.value);
      row[key] = Number.isFinite(value) ? (key === 'n' || key === 'prio' ? Math.round(value) : value) : 0;
    }
    onCargoChanged();
  });
  tbody.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-act="del"]');
    if (!btn) return;
    const row = findRow(btn);
    if (!state.skipDeleteConfirm) {
      const { ok, skip } = await confirmDialog('删除确认', '确定要删除这一行货物吗？删除后不可恢复。', '确认删除', true);
      if (!ok) return;
      if (skip) state.skipDeleteConfirm = true;
    }
    state.rows = state.rows.filter((r) => r !== row);
    btn.closest('tr').remove();
    $('#cargo-empty').hidden = state.rows.length > 0;
    onCargoChanged();
  });
}

function confirmDialog(title, text, okLabel, allowSkip = false) {
  const dialog = $('#confirm-dialog');
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  $('#confirm-ok').textContent = okLabel;
  $('#confirm-skip-wrap').hidden = !allowSkip;
  $('#confirm-skip').checked = false;
  dialog.returnValue = '';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve({
      ok: dialog.returnValue === 'ok', skip: $('#confirm-skip').checked,
    }), { once: true });
    dialog.showModal();
  });
}

// --------------------------------------------------------------------------- //
//                             集装箱与计算参数                                 //
// --------------------------------------------------------------------------- //
function renderPresets() {
  const box = $('#presets');
  const presets = state.config?.presets || {};
  box.replaceChildren(...Object.entries(presets).map(([name, dims]) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn preset';
    btn.dataset.preset = name;
    btn.innerHTML = `<span>${escapeHtml(name)}</span><small>${dims.map(g).join('×')} m</small>`;
    btn.addEventListener('click', () => {
      state.container = [...dims];
      renderContainerInputs();
      onContainerChanged();
    });
    return btn;
  }));
  markActivePreset();
}

function markActivePreset() {
  const presets = state.config?.presets || {};
  for (const btn of $$('#presets .preset')) {
    const dims = presets[btn.dataset.preset];
    const active = dims && dims.every((d, i) => Math.abs(d - Number(state.container[i])) < 1e-9);
    btn.classList.toggle('active', !!active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  }
}

function renderContainerInputs() {
  ['#c-l', '#c-w', '#c-h'].forEach((sel, i) => {
    const v = state.container[i];
    $(sel).value = v === null || v === undefined ? '' : String(v);
  });
}

function onContainerChanged() {
  markActivePreset();
  renderEstimate();
  saveInputs();
}

function renderParams() {
  $(`input[name="workmode"][value="${state.workmode}"]`).checked = true;
  $(`input[name="calcmode"][value="${state.calcmode}"]`).checked = true;
  $(`input[name="cargo-fmt"][value="${state.cargoFmt}"]`).checked = true;
  $('#m-weight').checked = state.metric === 'weight';
  $('#m-vol').checked = state.metric === 'volumetric';
  $('#m-strict').checked = state.strict;
  $('#threshold').value = String(state.threshold);
  $('#budget').value = String(state.budget);
  $('#ship-no').value = state.shipNo;
  renderModeDependent();
}

function renderModeDependent() {
  const manual = state.workmode === 'manual';
  $('#workmode-caption').textContent = manual
    ? '手动模式：在货物清单里为每类货物设置「优先级」，数值越大越靠底层。'
    : '自动模式：程序按所选「底层优先指标」自动摆放。';
  $('#metric-auto').hidden = manual;
  $('#metric-manual').hidden = !manual;
  $('#metric-none-hint').hidden = state.metric !== 'none';
  renderStrictOption();
  $('#cargo-table').classList.toggle('show-prio', manual);
  $('#cargo-caption').textContent = manual
    ? '直接在下表修改数值；勾选「禁止倒放」的货物不会被倒置摆放，勾选「不可压」的货物顶上不放其他货物；「优先级」数值越大越优先放到底层。每行最右侧可删除该行。'
    : '直接在下表修改数值，类型用下拉选择；勾选「禁止倒放」的货物不会被倒置摆放，勾选「不可压」的货物顶上不放其他货物；每行最右侧可删除该行。';
  $('#budget-field').hidden = state.calcmode !== 'enhanced';
  $('#budget-out').textContent = String(state.budget);
}

const STRICT_TEXT = {
  weight: ['严格执行：重的不压在轻的上面', '开启后，上层货物的重量不会大于它下面压着的任何一件货；限制越多，可能装得越少。'],
  volumetric: ['严格执行：密度大的不压在密度小的上面', '开启后，上层货物的密度不会大于它下面压着的任何一件货；限制越多，可能装得越少。'],
  none: ['严格执行', '先在上面选择一项指标，才能开启严格执行。'],
};

/** 严格执行只在自动模式下、且选了底层优先指标时可用。 */
const strictActive = () => state.workmode === 'auto' && state.metric !== 'none' && state.strict;

function renderStrictOption() {
  const [label, hint] = STRICT_TEXT[state.metric] || STRICT_TEXT.none;
  const input = $('#m-strict');
  input.disabled = state.metric === 'none';
  input.checked = state.strict && !input.disabled;
  input.closest('.switch').classList.toggle('off', input.disabled);
  $('#m-strict-text').textContent = label;
  $('#m-strict-hint').textContent = hint;
}

function bindParams() {
  for (const radio of $$('input[name="workmode"]')) {
    radio.addEventListener('change', () => { state.workmode = radio.value; renderModeDependent(); renderEstimate(); saveInputs(); });
  }
  for (const radio of $$('input[name="calcmode"]')) {
    radio.addEventListener('change', () => { state.calcmode = radio.value; renderModeDependent(); saveInputs(); });
  }
  // 两个底层优先指标互斥
  $('#m-weight').addEventListener('change', (e) => {
    state.metric = e.target.checked ? 'weight' : 'none';
    $('#m-vol').checked = false;
    renderModeDependent(); renderEstimate(); saveInputs();
  });
  $('#m-vol').addEventListener('change', (e) => {
    state.metric = e.target.checked ? 'volumetric' : 'none';
    $('#m-weight').checked = false;
    renderModeDependent(); renderEstimate(); saveInputs();
  });
  $('#m-strict').addEventListener('change', (e) => {
    state.strict = e.target.checked;
    renderEstimate(); saveInputs();
  });
  $('#threshold').addEventListener('input', (e) => {
    const v = Number(e.target.value);
    state.threshold = Number.isFinite(v) && e.target.value !== '' ? v : 0;
    renderEstimate(); saveInputs();
  });
  $('#budget').addEventListener('input', (e) => {
    state.budget = Number(e.target.value);
    $('#budget-out').textContent = e.target.value;
    saveInputs();
  });
  $('#ship-no').addEventListener('input', (e) => { state.shipNo = e.target.value; saveInputs(); renderShipCaption(); });
  ['#c-l', '#c-w', '#c-h'].forEach((sel, i) => {
    $(sel).addEventListener('input', (e) => {
      const v = Number(e.target.value);
      state.container[i] = e.target.value === '' || !Number.isFinite(v) ? null : v;
      onContainerChanged();
    });
  });
}

// --------------------------------------------------------------------------- //
//                                实时预估                                     //
// --------------------------------------------------------------------------- //
function metricHtml(label, value, cls = '') {
  return `<div class="metric"><div class="m-label">${label}</div><div class="m-value ${cls}">${value}</div></div>`;
}

function renderEstimate() {
  const container = state.container.map((v) => Number(v) || 0);
  const est = computeEstimate(container, state.rows, state.threshold);
  $('#estimate-metrics').innerHTML = metricHtml('货物件数', String(est.pieces))
    + metricHtml('货物总体积', `${fixed(est.cargoVolM3, 2)} m³`)
    + metricHtml('体积占集装箱', `${fixed(est.ratio * 100, 1)}%`)
    + metricHtml('货物总重', `${fixed(est.weight, 0)} kg`);
  const bar = $('#estimate-bar');
  bar.style.width = `${Math.min(est.ratio, 1) * 100}%`;
  bar.classList.toggle('over', est.overVolume);
  const alerts = [];
  if (est.overVolume) {
    alerts.push(`<div class="alert err">货物总体积 ${fixed(est.cargoVolM3, 2)} m³ 已超过集装箱 ${fixed(est.contVolM3, 2)} m³，必然装不下。</div>`);
  }
  if (est.oversized) {
    alerts.push(`<div class="alert warn">有 ${est.oversized} 类货物任意摆放都超过集装箱内尺寸，无法装入。</div>`);
  }
  if (est.heavy) {
    let how = '如需让重货优先压在底层，可开启「按重量优先」。';
    if (state.workmode === 'auto' && state.metric === 'weight') {
      how = strictActive()
        ? '已启用「按重量优先」并严格执行：重的不会压在轻的上面。'
        : '已启用「按重量优先」，将优先放到底层（实在放不下时仍允许叠放）。';
    }
    alerts.push(`<div class="alert info">有 ${est.heavy} 类为重货（&gt;${g(state.threshold)} kg）。${how}</div>`);
  }
  if (strictActive()) {
    const noWeight = state.rows.filter((r) => isValidRow(r) && !(Number(r.wt) > 0)).length;
    if (noWeight) {
      alerts.push(`<div class="alert warn">已开启严格执行，但有 ${noWeight} 类货物未填重量，它们不参与${state.metric === 'weight' ? '重量' : '密度'}比较。</div>`);
    }
  }
  $('#estimate-alerts').innerHTML = alerts.join('');
}

function renderExportButton() {
  const n = state.rows.filter(isValidRow).length;
  const btn = $('#btn-export-cargo');
  btn.disabled = n === 0;
  btn.textContent = n === 0 ? '清单为空，暂无可导出内容' : `导出货物清单（${n} 类 · ${state.cargoFmt}）`;
}

// --------------------------------------------------------------------------- //
//                                 计算任务                                    //
// --------------------------------------------------------------------------- //
function setMsg(sel, text, kind = 'err') {
  const el = $(sel);
  el.textContent = text || '';
  el.className = `form-msg ${kind}`;
}

let toastTimer = 0;
function toast(text, isError = false) {
  const el = $('#toast');
  el.textContent = text;
  el.classList.toggle('err', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 3500);
}

async function startCompute() {
  setMsg('#compute-msg', '');
  if (state.job) return;
  if (!state.container.every((v) => Number(v) > 0)) {
    setMsg('#compute-msg', '请填写有效的集装箱尺寸（长/宽/高均需大于 0）。');
    return;
  }
  if (!state.rows.some(isValidRow)) {
    setMsg('#compute-msg', '货物清单为空或没有有效行，请至少填写一行有效货物。');
    return;
  }
  const body = {
    container: state.container.map(Number),
    rows: state.rows.map(stripRow),
    ship_no: state.shipNo,
    mode: state.calcmode,
    budget: state.budget,
    bottom_metric: state.workmode === 'manual' ? 'manual' : state.metric,
    strict_stack: strictActive(),
    threshold: state.threshold,
  };
  setComputing(true);
  renderJob({ status: 'queued', mode: body.mode, budget: body.budget, position: 0 });
  try {
    const snap = await postJSON('/api/jobs', body);
    // 新任务已受理：旧结果作废，避免计算过程中被误当成本次结果
    state.job = { id: snap.id, failures: 0, timer: 0 };
    clearResult();
    handleSnapshot(snap);
  } catch (err) {
    setComputing(false);
    setMsg('#compute-msg', err.message);
  }
}

function schedulePoll(delay = POLL_MS) {
  if (!state.job) return;
  clearTimeout(state.job.timer);
  state.job.timer = setTimeout(pollJob, delay);
}

async function pollJob() {
  const job = state.job;
  if (!job) return;
  try {
    const snap = await getJSON(`/api/jobs/${job.id}`);
    if (state.job !== job) return;
    job.failures = 0;
    handleSnapshot(snap);
  } catch (err) {
    if (state.job !== job) return;
    job.failures += 1;
    if (job.failures >= 5 || /不存在|过期/.test(err.message)) {
      finishJob();
      setMsg('#compute-msg', `计算状态获取失败：${err.message}`);
    } else {
      schedulePoll(POLL_MS * 2);
    }
  }
}

function handleSnapshot(snap) {
  if (snap.status === 'queued' || snap.status === 'running') {
    renderJob(snap);
    schedulePoll();
    return;
  }
  finishJob();
  if (snap.status === 'done') {
    const result = { ...snap.result, elapsed: snap.elapsed, computed_at: Date.now() };
    applyResult(result);
    saveResult();
    toast(result.fits ? '计算完成，全部货物已装入。' : '计算完成，部分货物未能装入。');
    $('#result-area').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } else if (snap.status === 'error') {
    setMsg('#compute-msg', `计算失败：${snap.error || '未知错误'}`);
    toast('计算失败', true);
  } else {
    toast('已取消计算');
  }
}

function renderJob(snap) {
  const bar = $('#calc-bar');
  if (snap.status === 'queued') {
    const ahead = Math.max(0, (snap.position || 1) - 1);
    $('#calc-text').textContent = '正在排队…';
    $('#calc-sub').textContent = ahead > 0 ? `服务器正在处理其他计算，前面还有 ${ahead} 个任务` : '即将开始计算';
    bar.classList.add('indeterminate');
    return;
  }
  const elapsed = snap.elapsed || 0;
  $('#calc-text').textContent = '正在计算最优装箱方案…';
  if (snap.mode === 'enhanced') {
    $('#calc-sub').textContent = `已搜索 ${Math.round(elapsed)} 秒 / 上限 ${Math.round(snap.budget)} 秒（找到全部装入的方案会提前结束）`;
    bar.classList.remove('indeterminate');
    bar.style.width = `${Math.min(100, (elapsed / snap.budget) * 100)}%`;
  } else {
    $('#calc-sub').textContent = `正在尝试多种装载策略与货物朝向，已用 ${Math.round(elapsed)} 秒`;
    bar.classList.add('indeterminate');
  }
}

function setComputing(on) {
  $('#calc-card').hidden = !on;
  $('#btn-compute').disabled = on;
  $('#btn-load-plan').disabled = on;
  // 计算进行中不显示任何旧结果
  $('#result-area').hidden = on || !state.result;
  $('#empty-hint').hidden = on || !!state.result;
  if (on) {
    $('#calc-bar').style.width = '0';
    $('#calc-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function finishJob() {
  if (state.job) clearTimeout(state.job.timer);
  state.job = null;
  setComputing(false);
}

async function cancelCompute() {
  const job = state.job;
  if (!job) return;
  finishJob();
  toast('已取消计算');
  try { await postJSON(`/api/jobs/${job.id}/cancel`, {}); } catch { /* 任务可能刚好结束 */ }
}

// --------------------------------------------------------------------------- //
//                                  结果                                       //
// --------------------------------------------------------------------------- //
function categoryInfo(plan) {
  const placed = new Map();
  for (const item of plan.packing_plan) {
    const io = item.box_info.input_order;
    placed.set(io, (placed.get(io) || 0) + 1);
  }
  return plan.boxes.map((b, io) => ({
    io, category: io + 1, l: b[0], w: b[1], h: b[2], total: Number(b[3]), weight: Number(b[4]),
    type: typeLabel(b[5]), desc: String(b[6] ?? '').trim(), placed: placed.get(io) || 0,
  }));
}

function clearResult() {
  state.result = null;
  state.restored = false;
  saveResult();
  $('#result-area').hidden = true;
  $('#empty-hint').hidden = !!state.job;
}

function applyResult(result, { restored = false } = {}) {
  state.result = result;
  state.restored = restored;
  const cats = categoryInfo(result.plan);
  state.highlight = new Set(cats.map((c) => c.category));
  $('#result-area').hidden = false;
  $('#empty-hint').hidden = true;
  renderOverview();
  renderLegend(cats);
  renderTables(cats);
  if (viewer && viewer.supported) {
    viewer.setPlan(result.plan);
    viewer.setHighlight(state.highlight);
  }
  buildRanges();
}

function formatTime(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  const day = `${d.getMonth() + 1}月${d.getDate()}日`;
  const year = d.getFullYear() === new Date().getFullYear() ? '' : `${d.getFullYear()}年`;
  return `${year}${day} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const STACK_RULE_TEXT = { weight: '重的不压轻的', volumetric: '密度大的不压密度小的' };

/** 结果所用的叠放规则说明（来自方案本身，导入的旧方案没有这些字段）。 */
function stackRuleText(plan) {
  const parts = [];
  const strict = STACK_RULE_TEXT[plan.strict_stack];
  if (strict) parts.push(`严格执行（${strict}）`);
  const noStack = (plan.boxes || []).filter((box) => box[9]).length;
  if (noStack) parts.push(`${noStack} 类不可压`);
  return parts.join('，');
}

function renderShipCaption() {
  const { computed_at: at, plan } = state.result || {};
  const parts = [];
  if (state.shipNo) parts.push(`发货单号：${state.shipNo}`);
  if (at) parts.push(`计算时间：${formatTime(at)}`);
  const rule = plan ? stackRuleText(plan) : '';
  if (rule) parts.push(`叠放规则：${rule}`);
  const el = $('#result-ship');
  el.hidden = !parts.length;
  el.textContent = parts.join('　·　');
}

function renderOverview() {
  const { stats, fits, msg, note, mode } = state.result;
  renderShipCaption();
  let last;
  if (mode === 'enhanced') last = metricHtml('搜索评估 / 用时', `${stats.evaluations} 次 / ${stats.seconds}s`);
  else if (mode === 'imported') last = metricHtml('方案来源', '已保存方案');
  else last = metricHtml('计算用时', `${g(state.result.elapsed ?? stats.seconds ?? 0)} s`);
  $('#result-metrics').innerHTML = metricHtml('装入 / 总数', `${stats.placed} / ${stats.total}`)
    + metricHtml('空间利用率', `${fixed(stats.utilization * 100, 1)}%`)
    + metricHtml('是否全部装入', fits ? '是' : '否', fits ? 'good' : 'bad')
    + last;
  const alerts = [];
  if (state.restored) {
    const at = state.result.computed_at;
    alerts.push(`<div class="alert info stale">
      <span>这是上次${at ? `（${escapeHtml(formatTime(at))}）` : ''}留下的结果，不是本次新算的；修改清单后请重新点击「开始计算」。</span>
      <button type="button" class="btn small" data-act="clear-result">清除此结果</button></div>`);
  }
  if (!fits) {
    const why = msg || `装入 ${stats.placed}/${stats.total} 件`;
    alerts.push(`<div class="alert warn">未能全部装入：${escapeHtml(why)}</div>`);
  }
  if (note) alerts.push(`<div class="alert info">${escapeHtml(note)}</div>`);
  $('#result-alerts').innerHTML = alerts.join('');
}

function renderLegend(cats) {
  const legend = $('#legend');
  legend.replaceChildren(...cats.map((c) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip';
    btn.dataset.category = c.category;
    const lead = c.desc || `类别${c.category}`;
    const full = c.placed >= c.total;
    // 数量为本方案实际装入的件数；未全部装入时附上应装总数，避免与清单数量混淆
    const qty = full ? `已装${c.placed}件` : `已装${c.placed}/${c.total}件`;
    btn.innerHTML = `<span class="sw" style="background:${categoryColor(c.io)}"></span>`
      + `<span class="chip-text">${escapeHtml(lead)}: ${g(c.l)}×${g(c.w)}×${g(c.h)}m (${escapeHtml(c.type)}, `
      + `<span class="qty${full ? '' : ' short'}">${qty}</span>)</span>`;
    btn.title = `${lead}：应装 ${c.total} 件，本方案装入 ${c.placed} 件`;
    if (c.placed === 0) btn.disabled = true;
    btn.setAttribute('aria-pressed', state.highlight.has(c.category) ? 'true' : 'false');
    btn.addEventListener('click', () => {
      if (state.highlight.has(c.category)) state.highlight.delete(c.category);
      else state.highlight.add(c.category);
      syncHighlight();
    });
    return btn;
  }));
}

function syncHighlight() {
  for (const chip of $$('#legend .chip')) {
    chip.setAttribute('aria-pressed', state.highlight.has(Number(chip.dataset.category)) ? 'true' : 'false');
  }
  if (viewer) viewer.setHighlight(state.highlight);
}

// ---------------- 分段查看（三个方向的双端滑块） ---------------- //
const AXES = [
  { key: 'x', label: '长度方向 Length（装货方向）' },
  { key: 'y', label: '宽度方向 Width' },
  { key: 'z', label: '高度方向 Height' },
];

function buildRanges() {
  const dims = state.result.plan.container.map(Number);
  const box = $('#ranges');
  box.replaceChildren(...AXES.map((axis, i) => {
    const max = dims[i];
    const step = max / 100; // 100 档，保证滑块能精确停在两端
    const wrap = document.createElement('div');
    wrap.className = 'range';
    wrap.innerHTML = `
      <div class="range-head"><span>${axis.label}</span><output></output></div>
      <div class="dual"><div class="fill"></div>
        <input type="range" class="lo" min="0" max="${max}" step="${step}" value="0" aria-label="${axis.label} 起点">
        <input type="range" class="hi" min="0" max="${max}" step="${step}" value="${max}" aria-label="${axis.label} 终点">
      </div>`;
    const lo = $('.lo', wrap);
    const hi = $('.hi', wrap);
    const update = (moved) => {
      let a = Number(lo.value);
      let b = Number(hi.value);
      if (a > b) {
        if (moved === lo) { a = b; lo.value = String(b); } else { b = a; hi.value = String(a); }
      }
      lo.style.zIndex = a > max - step * 1.5 ? '3' : '1';
      $('.fill', wrap).style.left = `${(a / max) * 100}%`;
      $('.fill', wrap).style.width = `${((b - a) / max) * 100}%`;
      $('output', wrap).textContent = `${g(a)} ~ ${g(b)} m`;
      scheduleRegion();
    };
    lo.addEventListener('input', () => update(lo));
    hi.addEventListener('input', () => update(hi));
    wrap.reset = () => { lo.value = '0'; hi.value = String(max); update(hi); };
    wrap.read = () => {
      // 滑块位于两端时视为不限制，避免步长取整漏掉边缘货物
      const a = Number(lo.value);
      const b = Number(hi.value);
      return [a <= step / 2 ? -Infinity : a, b >= max - step / 2 ? Infinity : b];
    };
    update(hi);
    return wrap;
  }));
}

let regionFrame = 0;
function scheduleRegion() {
  if (regionFrame) return;
  regionFrame = requestAnimationFrame(() => {
    regionFrame = 0;
    const ranges = $$('#ranges .range');
    if (ranges.length !== 3 || !viewer) return;
    const r = ranges.flatMap((el) => el.read());
    const full = r.every((v, i) => (i % 2 === 0 ? v === -Infinity : v === Infinity));
    viewer.setRegion(full ? null : r);
  });
}

// ---------------- 汇总 / 明细表 ---------------- //
function renderTables(cats) {
  const head = (cols) => `<thead><tr>${cols.map(([t, cls]) => `<th class="${cls || ''}">${t}</th>`).join('')}</tr></thead>`;
  $('#summary-table').innerHTML = head([
    ['类别'], ['零件描述'], ['类型'], ['规格(长×宽×高 m)'], ['应装', 'num'], ['已装', 'num'], ['未装', 'num'],
    ['单件重量(kg)', 'num'], ['已装重量(kg)', 'num'], ['已装体积(m³)', 'num'],
  ]) + `<tbody>${cats.map((c) => `<tr>
      <td><span class="sw" style="background:${categoryColor(c.io)}"></span>${c.category}</td>
      <td>${escapeHtml(c.desc)}</td><td>${escapeHtml(c.type)}</td>
      <td>${g(c.l)}×${g(c.w)}×${g(c.h)}</td>
      <td class="num">${c.total}</td><td class="num">${c.placed}</td>
      <td class="num ${c.total - c.placed > 0 ? 'short' : ''}">${c.total - c.placed}</td>
      <td class="num">${g(c.weight)}</td><td class="num">${fixed(c.weight * c.placed, 1)}</td>
      <td class="num">${fixed(c.l * c.w * c.h * c.placed, 3)}</td></tr>`).join('')}</tbody>`;

  const items = [...state.result.plan.packing_plan].sort((a, b) => a.box_info.number - b.box_info.number);
  const m = (v) => fixed(v, 3);
  $('#detail-table').innerHTML = head([
    ['装载顺序', 'num'], ['类别', 'num'], ['零件描述'], ['类型'], ['原始规格(长×宽×高 m)'],
    ['摆放长(m)', 'num'], ['摆放宽(m)', 'num'], ['摆放高(m)', 'num'],
    ['位置X起点(m)', 'num'], ['位置Y起点(m)', 'num'], ['位置Z起点(m)', 'num'], ['重量(kg)', 'num'],
  ]) + `<tbody>${items.map(({ position: p, dimensions: d, box_info: info }) => {
    const [ol, ow, oh] = info.original_dimensions;
    return `<tr><td class="num">${info.number}</td><td class="num">${info.input_order + 1}</td>
      <td>${escapeHtml(info.desc || '')}</td><td>${escapeHtml(typeLabel(info.type))}</td>
      <td>${g(ol)}×${g(ow)}×${g(oh)}</td>
      <td class="num">${m(d[0])}</td><td class="num">${m(d[1])}</td><td class="num">${m(d[2])}</td>
      <td class="num">${m(p[0])}</td><td class="num">${m(p[1])}</td><td class="num">${m(p[2])}</td>
      <td class="num">${g(info.weight)}</td></tr>`;
  }).join('')}</tbody>`;
}

// --------------------------------------------------------------------------- //
//                                导入 / 导出                                   //
// --------------------------------------------------------------------------- //
function bindFilePick(input) {
  const label = input.closest('.file-pick');
  const name = $('.file-name', label);
  input.addEventListener('change', () => {
    const file = input.files[0];
    name.textContent = file ? file.name : name.dataset.empty;
    label.classList.toggle('has-file', !!file);
  });
}

function resetFilePick(input) {
  input.value = '';
  input.dispatchEvent(new Event('change'));
}

function applyImportedInputs(data) {
  state.container = data.container.map(Number);
  state.shipNo = data.ship_no || '';
  state.rows = data.rows.map(newRow);
  renderContainerInputs();
  $('#ship-no').value = state.shipNo;
  markActivePreset();
  renderCargoTable();
}

async function importCargo() {
  const fileInput = $('#cargo-file');
  const text = $('#cargo-text').value;
  const fd = new FormData();
  if (fileInput.files[0]) fd.append('file', fileInput.files[0]);
  else if (text.trim()) fd.append('text', text);
  else { setMsg('#import-msg', '请先选择文件或粘贴文本。'); return; }
  setMsg('#import-msg', '正在导入…', 'ok');
  try {
    applyImportedInputs(await postForm('/api/cargo/import', fd));
    resetFilePick(fileInput);
    setMsg('#import-msg', '已导入，请在上方核对后点击“开始计算”。', 'ok');
  } catch (err) {
    setMsg('#import-msg', err.message);
  }
}

async function exportCargo() {
  const body = {
    container: state.container.map((v) => Number(v) || 0),
    ship_no: state.shipNo,
    rows: state.rows.filter(isValidRow).map(stripRow),
  };
  try {
    await downloadFrom(`/api/cargo/export?fmt=${state.cargoFmt}`, body, `货物清单.${state.cargoFmt}`);
  } catch (err) {
    toast(`导出失败：${err.message}`, true);
  }
}

async function loadSavedPlan() {
  const input = $('#plan-file');
  const file = input.files[0];
  if (!file) { setMsg('#plan-msg', '请先选择 JSON 或 XLSX 方案文件。'); return; }
  const fd = new FormData();
  fd.append('file', file);
  setMsg('#plan-msg', '正在载入…', 'ok');
  try {
    const data = await postForm('/api/plan/import', fd);
    applyImportedInputs(data);
    applyResult(data.result);
    saveResult();
    resetFilePick(input);
    setMsg('#plan-msg', '方案已载入，集装箱尺寸、货物清单和装箱结果均已恢复。', 'ok');
    $('#result-area').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    setMsg('#plan-msg', err.message);
  }
}

async function downloadPlanXlsx() {
  const { plan, stats } = state.result;
  try {
    await downloadFrom('/api/plan/xlsx', { plan, stats, ship_no: state.shipNo }, 'packing_plan.xlsx');
  } catch (err) {
    toast(`导出失败：${err.message}`, true);
  }
}

function downloadPlanJson() {
  // 与 Python 版 json.dumps(indent=4, ensure_ascii=False) 的结构一致，可直接再导入
  const plan = { ...state.result.plan, ship_no: state.shipNo };
  saveBlob(new Blob([JSON.stringify(plan, null, 4)], { type: 'application/json' }), 'packing_plan.json');
}

// --------------------------------------------------------------------------- //
//                                   启动                                      //
// --------------------------------------------------------------------------- //
function bindActions() {
  $('#btn-add-row').addEventListener('click', () => {
    const row = newRow();
    state.rows.push(row);
    $('#cargo-table tbody').append(rowElement(row));
    $('#cargo-empty').hidden = true;
    onCargoChanged();
    $(`#cargo-table tr[data-id="${row.id}"] [data-k="desc"]`).focus();
  });
  $('#btn-clear').addEventListener('click', async () => {
    if (!state.rows.length) return;
    const { ok } = await confirmDialog('清空清单', `确定要清空全部 ${state.rows.length} 行货物吗？`, '确认清空');
    if (!ok) return;
    state.rows = [];
    renderCargoTable();
  });
  $('#btn-sample').addEventListener('click', () => {
    if (!state.config) { toast('示例数据尚未加载，请稍后再试', true); return; }
    state.rows = state.config.sample.rows.map(newRow);
    state.container = [...state.config.sample.container];
    renderContainerInputs();
    markActivePreset();
    renderCargoTable();
  });
  for (const radio of $$('input[name="cargo-fmt"]')) {
    radio.addEventListener('change', () => { state.cargoFmt = radio.value; renderExportButton(); saveInputs(); });
  }
  $('#btn-export-cargo').addEventListener('click', exportCargo);
  $('#btn-import').addEventListener('click', importCargo);
  $('#btn-compute').addEventListener('click', startCompute);
  $('#btn-cancel').addEventListener('click', cancelCompute);
  $('#btn-load-plan').addEventListener('click', loadSavedPlan);
  bindFilePick($('#cargo-file'));
  bindFilePick($('#plan-file'));

  $('#btn-hl-all').addEventListener('click', () => {
    state.highlight = new Set(categoryInfo(state.result.plan).map((c) => c.category));
    syncHighlight();
  });
  $('#btn-hl-none').addEventListener('click', () => { state.highlight = new Set(); syncHighlight(); });
  $('#btn-reset-range').addEventListener('click', () => {
    for (const el of $$('#ranges .range')) el.reset();
  });
  $('#opt-translucent').addEventListener('change', (e) => viewer && viewer.setTranslucent(e.target.checked));
  $('#opt-edges').addEventListener('change', (e) => viewer && viewer.setEdges(e.target.checked));
  for (const btn of $$('.viewer-toolbar [data-view]')) {
    btn.addEventListener('click', () => viewer && viewer.setView(btn.dataset.view));
  }
  $('#btn-shot').addEventListener('click', async () => {
    if (!viewer || !viewer.supported) return;
    const blob = await viewer.snapshot();
    if (blob) saveBlob(blob, `装箱方案${state.shipNo ? `-${state.shipNo}` : ''}.png`);
  });
  $('#btn-full').addEventListener('click', () => {
    const wrap = $('#viewer-wrap');
    if (document.fullscreenElement) document.exitFullscreen();
    else if (wrap.requestFullscreen) wrap.requestFullscreen().catch(() => toast('浏览器不允许全屏', true));
  });
  document.addEventListener('fullscreenchange', () => {
    $('#btn-full').textContent = document.fullscreenElement ? '退出全屏' : '全屏';
  });
  for (const tab of $$('.tabs [data-tab]')) {
    tab.addEventListener('click', () => {
      for (const t of $$('.tabs [data-tab]')) t.setAttribute('aria-selected', t === tab ? 'true' : 'false');
      $('#tab-summary').hidden = tab.dataset.tab !== 'summary';
      $('#tab-detail').hidden = tab.dataset.tab !== 'detail';
    });
  }
  $('#result-alerts').addEventListener('click', (e) => {
    if (e.target.closest('[data-act="clear-result"]')) clearResult();
  });
  $('#btn-dl-xlsx').addEventListener('click', downloadPlanXlsx);
  $('#btn-dl-json').addEventListener('click', downloadPlanJson);

  // 页面回到前台时立即刷新计算状态；页面关闭时通知服务器停止计算
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.job) schedulePoll(0);
  });
  window.addEventListener('pagehide', () => { if (state.job) cancelOnUnload(state.job.id); });
}

async function init() {
  restoreState();
  renderParams();
  renderContainerInputs();
  bindParams();
  bindCargoTable();
  bindActions();
  renderCargoTable();

  viewer = new PlanViewer($('#viewer'), { tooltip: $('#viewer-tip'), info: $('#viewer-info') });
  if (!viewer.supported) {
    $('#viewer-fallback').hidden = false;
    $('.viewer-toolbar').hidden = true;
  }
  if (state.result) {
    try { applyResult(state.result, { restored: true }); } catch { clearResult(); }
  }

  try {
    state.config = await getJSON('/api/config');
    $('#budget').max = String(Math.min(600, state.config.max_budget));
    renderPresets();
  } catch (err) {
    toast(`无法连接服务器：${err.message}`, true);
  }
}

init();
