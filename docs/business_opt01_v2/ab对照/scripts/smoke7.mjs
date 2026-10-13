/* 渲染冒烟测试：加载单文件成品，用最小 DOM 垫片跑通四个主视图、逐案子视图与懒加载。
 * 只读；不修改成品。node smoke7.mjs
 */
import fs from 'node:fs';

const html = fs.readFileSync('画像管线AB对照.html', 'utf8');
const CLOSE = '</scr' + 'ipt>';

function blockText(id) {
  const tag = '<script type="application/json" id="' + id + '">';
  const s0 = html.indexOf(tag);
  if (s0 < 0) throw new Error('missing block ' + id);
  const s = s0 + tag.length;
  const e = html.indexOf(CLOSE, s);
  return html.slice(s, e);
}
const RAW = {};
['d-payload', 'd-findings', 'd-oldprob'].forEach(function (id) { RAW[id] = blockText(id); });
const payload = JSON.parse(RAW['d-payload']);
const findings = JSON.parse(RAW['d-findings']);
const oldprob = JSON.parse(RAW['d-oldprob']);

const si = html.lastIndexOf('<script>');
const js = html.slice(si + 8, html.indexOf(CLOSE, si + 8));

console.log('=== 0. 内联数据 ===');
console.log('  cases=' + payload.cases.length + ' findings=' + findings.length + ' oldprob=' + oldprob.length);
console.log('  中文还原 ' + (JSON.stringify(payload).includes('客户画像') ? 'OK' : '未命中'));
console.log('  脚本体 ' + js.length.toLocaleString() + ' 字节');

/* ---------------- 最小 DOM 垫片（同 selector 同 key 复用同一对象） ---------------- */
const CACHE = new Map();
function node(sig, init) {
  if (!CACHE.has(sig)) {
    CACHE.set(sig, Object.assign({
      textContent: '', dataset: {}, open: false, className: '', style: {}, _a: {},
      setAttribute(k, v) { this._a[k] = v; },
      getAttribute(k) { return this._a[k] === undefined ? null : this._a[k]; },
      appendChild() {}, addEventListener() {},
      querySelector() { return this._box || null; },
      querySelectorAll(sel) { return nodes(sel, this._h || APP._h); }
    }, init || {}));
  }
  return CACHE.get(sig);
}
function reset(prefix) {
  Array.from(CACHE.keys()).forEach(function (k) { if (k.indexOf(prefix) === 0) CACHE.delete(k); });
}
const APP = node('app');
Object.defineProperty(APP, 'innerHTML', { get() { return this._h || ''; }, set(v) { this._h = String(v); } });
const REG = { app: APP, 'brand-sub': node('bs'), foot: node('ft') };
['d-payload', 'd-findings', 'd-oldprob'].forEach(function (id) { REG[id] = node(id, { textContent: RAW[id] }); });

function nodes(sel, src) {
  const out = [];
  const s = String(src || '');
  if (sel.indexOf('#tabs button') >= 0) {
    ['overview', 'problems', 'cases', 'method'].forEach(function (v) {
      out.push(node('#tabs|' + v, { _a: { 'data-v': v } }));
    });
  } else if (sel.indexOf('.chip') >= 0) {
    (s.match(/data-k="(\d+)"/g) || []).forEach(function (m) {
      const k = m.replace(/\D/g, '');
      out.push(node('.chip|' + k, { _a: { 'data-k': k } }));
    });
  } else if (sel.indexOf('#subtabs') >= 0) {
    (s.match(/data-s="([a-z]+)"/g) || []).forEach(function (m) {
      const v = m.replace(/data-s="|"/g, '');
      out.push(node('#subtabs|' + v, { _a: { 'data-s': v } }));
    });
  } else if (sel.indexOf('details') >= 0) {
    (s.match(/data-lazy="([^"]+)"/g) || []).forEach(function (m) {
      const id = m.replace('data-lazy="', '').replace('"', '');
      out.push(node('details|' + id, { _a: { 'data-lazy': id }, dataset: {}, _box: node('box|' + id) }));
    });
  }
  return out;
}

globalThis.document = {
  readyState: 'complete',
  getElementById(id) { return REG[id] || (REG[id] = node(id)); },
  querySelectorAll(sel) { return nodes(sel, APP._h); },
  addEventListener() {}
};
globalThis.window = { scrollTo() {}, matchMedia() { return { matches: false }; } };
globalThis.Blob = class { constructor(a) { this.size = Buffer.byteLength(String(a[0] === undefined ? '' : a[0])); } };

let boom = null;
try { new Function(js)(); } catch (e) { boom = e.message; }
await new Promise(function (r) { setTimeout(r, 250); });

console.log('\n=== 1. 启动 ===');
console.log('  脚本执行 ' + (boom ? '** 抛错: ' + boom + ' **' : 'OK'));
console.log('  副标题   ' + String(REG['brand-sub'].textContent).trim().slice(0, 90));
console.log('  页脚     ' + String(REG['foot'].innerHTML).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 70));

function click(sel, pred) {
  const el = nodes(sel, APP._h).find(pred);
  if (!el) return '未找到控件';
  if (typeof el.onclick !== 'function') return '无 onclick 处理器';
  el.onclick();
  return null;
}
const clickTab = function (v) { return click('#tabs button', function (n) { return n.getAttribute('data-v') === v; }); };
const clickSub = function (v) { return click('#subtabs button', function (n) { return n.getAttribute('data-s') === v; }); };
const clickChip = function (v) { return click('.chip', function (n) { return n.getAttribute('data-k') === v; }); };
const miss = function (arr) { return arr.filter(function (x) { return APP._h.indexOf(x) < 0; }); };

console.log('\n=== 2. 主视图 ===');
['overview', 'problems', 'cases', 'method'].forEach(function (v) {
  const e = clickTab(v);
  console.log('  ' + v.padEnd(9) + String(APP._h.length).padStart(9) + ' 字符  ' + (e || 'ok'));
});
clickTab('overview');
console.log('  总览串 ' + (miss(['十类老问题', 'LLM 调用', '确实修好了', '新出现', '逐案成本', '等级字段打架']).join(' / ') || '全部命中'));
clickTab('problems');
console.log('  复检串 ' + (miss(['购买类型与旧车矛盾', 'age 跨节格式', '枚举取值', '证据锚点引用画像', '姓名/微信号脏值', '模板外 key', 'summary 与 steps']).join(' / ') || '全部命中'));
clickTab('method');
console.log('  口径串 ' + (miss(['可比性', '已知缺陷', '数据来源', 'phones_sha256', '口径差']).join(' / ') || '全部命中'));

console.log('\n=== 3. 逐案 x 子视图 ===');
clickTab('cases');
console.log('  案件 chip ' + (APP._h.match(/data-k="/g) || []).length + ' 个');
['0', '4', '7'].forEach(function (k) {
  const e0 = clickChip(k);
  if (e0) { console.log('  案 ' + k + ' ' + e0); return; }
  const out = [payload.cases[Number(k)].phone.padEnd(12)];
  ['raw', 'notes', 'card', 'pipeline'].forEach(function (s) {
    const e = clickSub(s);
    if (e) { out.push(s + ':' + e); return; }
    out.push(s + ' ' + String(APP._h.length).padStart(8) + (APP._h.indexOf('err-box') >= 0 ? '!' : ''));
  });
  console.log('  ' + out.join(' | '));
});

console.log('\n=== 4. 子视图内容验证（案索引 1）===');
clickChip('1');
clickSub('notes');
console.log('  Notes 对照 表头 ' + ((APP._h.indexOf('基线 Notes') >= 0 && APP._h.indexOf('v2 Notes') >= 0) ? 'OK' : '缺') +
  ' · 「未知渠道」出现 ' + (APP._h.match(/未知渠道/g) || []).length + ' 次');
clickSub('card');
const SEC = ['画像摘要', '推断标签', '用车场景', '沟通策略', '客户概览', '购车动机', '产品偏好', '抗性/顾虑', '情绪状态'];
console.log('  画像卡对照 9 节标题命中 ' + SEC.filter(function (x) { return APP._h.indexOf(x) >= 0; }).length + '/9' +
  ' · 空段占位 ' + (APP._h.indexOf('该 section 在本臂为空') >= 0 ? '有' : '无'));
clickSub('pipeline');
console.log('  L1/投影明细 basis 徽章 ' + (APP._h.match(/inferred_low|inferred_high|>fact</g) || []).length +
  ' · 降级原因行 ' + (APP._h.match(/unknown_channel|anchor_not_found|anchor_too_short|inferred_without_basis/g) || []).length +
  ' · 未降级行 ' + (APP._h.match(/未降级/g) || []).length);

console.log('\n=== 5. 原始输入懒加载 ===');
clickSub('raw');
const ids = (APP._h.match(/data-lazy="([^"]+)"/g) || []).map(function (m) { return m.replace('data-lazy="', '').replace('"', ''); });
let ok = 0;
const bad = [];
ids.forEach(function (id) {
  const el = nodes('details', APP._h).find(function (n) { return n.getAttribute('data-lazy') === id; });
  if (!el) { bad.push(id + ':缺'); return; }
  el.open = true; el.dataset = {};
  try { el.ontoggle(); } catch (e) { bad.push(id + ':' + e.message); return; }
  const box = el.querySelector('.dt-bd');
  const n = box && box.innerHTML ? box.innerHTML.length : 0;
  if (n > 0) ok++; else bad.push(id + ':空');
});
console.log('  折叠块 ' + ids.length + ' 个 · 展开 ' + ok + ' 个 · ' + (bad.length ? '问题: ' + bad.join(' ') : '全部有内容'));

console.log('\n=== 6. 数据完备性 ===');
const SECK = ['profile_summary', 'inferred_tags', 'usage_scenarios', 'communication_strategies',
  'customer_overview', 'purchase_motivations', 'product_preferences', 'resistances', 'emotion_state'];
payload.cases.forEach(function (c) {
  const bs = SECK.filter(function (k) { return c.base.sections[k] && JSON.stringify(c.base.sections[k]).length > 2; }).length;
  const vs = SECK.filter(function (k) { return c.v2.sections[k] && JSON.stringify(c.v2.sections[k]).length > 2; }).length;
  console.log('  ' + c.phone + '  段 ' + bs + '/9 → ' + vs + '/9   Notes ' +
    String(c.base.notes.length).padStart(2) + ' → ' + String(c.v2.notes.length).padStart(2) +
    '   原始 ' + ((Buffer.byteLength(JSON.stringify(c.raw)) / 1024) | 0) + 'KB');
});
let v2llm = 0, v2tok = 0, bllm = 0, btok = 0;
payload.cases.forEach(function (c) {
  v2llm += c.v2.metrics.llm_calls; v2tok += c.v2.metrics.tokens;
  bllm += c.base.metrics.llm_calls; btok += c.base.metrics.tokens;
});
console.log('\n=== 7. 页面汇总数字 ===');
console.log('  findings ' + findings.length + ' 条（error ' + findings.filter(function (f) { return f.severity === 'error'; }).length +
  ' / warn ' + findings.filter(function (f) { return f.severity === 'warn'; }).length + '）');
console.log('  LLM ' + bllm + ' → ' + v2llm + ' · Token ' + btok.toLocaleString() + ' → ' + v2tok.toLocaleString());
console.log('  逐案 Token 基线 ' + Math.round(btok / 8).toLocaleString() + ' → v2 ' + Math.round(v2tok / 8).toLocaleString());
