// 数字格式化、类别配色与实时预估（与 planner_core.compute_estimate 逻辑一致）。

export const TYPE_LABELS = { 0: '木箱', 1: '纸箱', 2: '托盘' };
export const PALETTE = ['#3151CB', '#B9693F', '#2F7D68', '#A84A5B', '#6B5CA5',
  '#8A6A4A', '#4F7897', '#69727D', '#8C7836', '#2F7480'];
export const categoryColor = (io) => PALETTE[io % PALETTE.length];

/** 等价于 Python 的 f"{x:g}"：6 位有效数字，去掉多余的 0。 */
export function g(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return String(Number(n.toPrecision(6)));
}

export const fixed = (x, digits) => Number(x).toLocaleString('zh-CN',
  { minimumFractionDigits: digits, maximumFractionDigits: digits });

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export const typeLabel = (t) => TYPE_LABELS[String(t)] ?? String(t);

/** 米 -> 整数毫米（与后端 to_mm 一致，用于判断有效行与超尺寸）。 */
export const toMM = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? Math.round(n * 1000) : 0;
};

export const isValidRow = (r) => toMM(r.l) > 0 && toMM(r.w) > 0 && toMM(r.h) > 0 && Math.round(Number(r.n)) > 0;

export function computeEstimate(container, rows, threshold) {
  const c = container.map(toMM);
  const contVol = c[0] * c[1] * c[2];
  const valid = rows.filter(isValidRow);
  let cargoVol = 0, pieces = 0, weight = 0, oversized = 0, heavy = 0;
  const cd = [...c].sort((a, b) => a - b);
  for (const r of valid) {
    const dims = [toMM(r.l), toMM(r.w), toMM(r.h)];
    const n = Math.round(Number(r.n));
    const wt = Number(r.wt) || 0;
    cargoVol += dims[0] * dims[1] * dims[2] * n;
    pieces += n;
    weight += wt * n;
    if (contVol > 0) {
      const bd = [...dims].sort((a, b) => a - b);
      if (!bd.every((d, i) => d <= cd[i])) oversized += 1;
    }
    if (wt > threshold) heavy += 1;
  }
  return {
    pieces,
    cargoVolM3: cargoVol / 1e9,
    contVolM3: contVol / 1e9,
    ratio: contVol > 0 ? cargoVol / contVol : 0,
    weight,
    overVolume: contVol > 0 && cargoVol > contVol,
    oversized,
    heavy,
  };
}
