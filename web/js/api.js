// 与后端通信的小工具。所有接口出错时都抛出带中文说明的 Error。

async function parseError(res) {
  try {
    const data = await res.json();
    if (data && data.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
  } catch { /* 非 JSON 响应 */ }
  if (res.status === 413) return '文件过大';
  if (res.status >= 500) return `服务器出错（${res.status}），请稍后重试`;
  return `请求失败（${res.status}）`;
}

async function request(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    throw new Error('无法连接服务器，请检查网络后重试');
  }
  if (!res.ok) throw new Error(await parseError(res));
  return res;
}

export async function getJSON(url) {
  return (await request(url)).json();
}

export async function postJSON(url, body) {
  const res = await request(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return res.json();
}

export async function postForm(url, formData) {
  return (await request(url, { method: 'POST', body: formData })).json();
}

/** POST JSON 并把返回的文件交给浏览器下载。 */
export async function downloadFrom(url, body, filename) {
  const res = await request(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  saveBlob(await res.blob(), filename);
}

export function saveBlob(blob, filename) {
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

/** 页面关闭时通知服务器取消计算（sendBeacon 在页面卸载期间也能发出）。 */
export function cancelOnUnload(jobId) {
  const url = `/api/jobs/${jobId}/cancel`;
  if (!navigator.sendBeacon || !navigator.sendBeacon(url)) {
    fetch(url, { method: 'POST', keepalive: true }).catch(() => {});
  }
}
