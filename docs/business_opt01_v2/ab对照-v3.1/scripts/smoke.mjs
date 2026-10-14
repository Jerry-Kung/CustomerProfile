/* 渲染冒烟测试（v3.1 版）：加载单文件成品，用最小 DOM 垫片跑通两个视图、逐案分段与懒加载。
 * 只读；不修改成品。node smoke.mjs
 *
 * 与 v3 版的核对点差异：新增 **v3.1 四项整改读数**（确定性注入 / 顾问词拒收 / 噪音锚点 /
 * 推演同集率）与逐案的**注入层 / 合并层**两块的断言。
 */
import fs from 'node:fs';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const TARGET = path.resolve(HERE, '..', '..', '画像管线AB对照-v3.1.html');
const html = fs.readFileSync(TARGET, 'utf8');
const CLOSE = '</scr' + 'ipt>';

function blockText(id) {
  const tag = '<script type="application/json" id="' + id + '">';
  const s0 = html.indexOf(tag);
  if (s0 < 0) throw new Error('missing block ' + id);
  const s = s0 + tag.length;
  return html.slice(s, html.indexOf(CLOSE, s));
}

const payload = JSON.parse(blockText('d-payload'));
const si = html.lastIndexOf('<script>');
const js = html.slice(si + 8, html.indexOf(CLOSE, si + 8));

console.log('=== 0. 内联数据 ===');
console.log('  cases=' + payload.cases.length + ' sections=' + payload.sections.length);
console.log('  中文还原 ' + (JSON.stringify(payload).includes('客户') ? 'OK' : '未命中'));
console.log('  脚本体 ' + js.length.toLocaleString() + ' 字节');
console.log('  单边字段合计 ' + payload.cases.reduce((n, c) => n + c.notes_diff.only_base.length + c.notes_diff.only_v3.length, 0));
console.log('  事实层条目合计 ' + payload.cases.reduce((n, c) => n + ((c.v3.facts && c.v3.facts.records) || []).length, 0));

/* ---------------- 最小 DOM 垫片 ---------------- */
const CACHE = new Map();
function node(sig, init) {
  if (!CACHE.has(sig)) {
    CACHE.set(sig, Object.assign({
      textContent: '', dataset: {}, open: false, className: '', style: {}, _a: {},
      setAttribute(k, v) { this._a[k] = v; },
      getAttribute(k) { return this._a[k] === undefined ? null : this._a[k]; },
      appendChild() {}, addEventListener() {},
      querySelector() { return this._box || null; },
      querySelectorAll() { return []; },
    }, init || {}));
  }
  return CACHE.get(sig);
}

const STATIC = html.slice(0, html.indexOf('<script type="application/json"'));

const ELS = {
  'd-payload': node('d-payload', { textContent: JSON.stringify(payload) }),
  lede: node('lede'),
  'mh-meta': node('mh-meta'),
  shell: node('shell'),
  foot: node('foot'),
  app: node('app'),
};

Object.defineProperty(ELS.shell, 'innerHTML', {
  get() { return this._html || ''; },
  set(v) { this._html = String(v); CURRENT = this._html; },
});
['lede', 'mh-meta', 'foot'].forEach((k) => {
  Object.defineProperty(ELS[k], 'innerHTML', { get() { return this._html || ''; }, set(v) { this._html = String(v); } });
});

const doc = {
  readyState: 'complete',
  _listeners: [],
  addEventListener(k, fn) { this._listeners.push(fn); },
  getElementById(id) { return ELS[id] || null; },
  querySelectorAll(sel) { return qsa(sel); },
  querySelector(sel) { return qsa(sel)[0] || null; },
};

let CURRENT = '';
function qsa(sel) {
  const hay = sel.indexOf('#views') === 0 ? STATIC : CURRENT;
  const out = [];
  if (sel.indexOf('#views') === 0) {
    const re = /<button[^>]*data-v="([^"]+)"[^>]*>/g; let m;
    while ((m = re.exec(hay)) !== null) out.push(mkBtn('data-v', m[1], m[0]));
  } else if (sel.indexOf('.rail-list') === 0) {
    const re = /<button[^>]*data-k="(\d+)"[^>]*>/g; let m;
    while ((m = re.exec(hay)) !== null) out.push(mkBtn('data-k', m[1], m[0]));
  } else if (sel.indexOf('#seg') === 0) {
    const re = /<button[^>]*data-s="([^"]+)"[^>]*>/g; let m;
    while ((m = re.exec(hay)) !== null) out.push(mkBtn('data-s', m[1], m[0]));
  } else if (sel.indexOf('tr[data-jump]') === 0) {
    const re = /<tr[^>]*data-jump="(\d+)"[^>]*>/g; let m;
    while ((m = re.exec(hay)) !== null) out.push(mkBtn('data-jump', m[1], m[0]));
  } else if (sel.indexOf('details[data-lazy]') === 0) {
    const re = /<details[^>]*data-lazy="([^"]+)"[^>]*>/g; let m;
    while ((m = re.exec(hay)) !== null) {
      const sig = 'lazy|' + m[1];
      out.push(node(sig, {
        onclick: null, ontoggle: null, open: false, dataset: {}, _a: { 'data-lazy': m[1] },
        getAttribute: function (k) { return this._a[k] === undefined ? null : this._a[k]; },
        querySelector: () => node(sig + '|bd', { innerHTML: '' }),
      }));
    }
  }
  return out;
}
function mkBtn(attr, val, tag) {
  const sig = 'btn|' + attr + '|' + val;
  return node(sig, {
    onclick: null, _a: { [attr]: val },
    getAttribute: function (k) { return this._a[k] === undefined ? null : this._a[k]; },
    setAttribute: function (k, v) { this._a[k] = v; },
  });
}
globalThis.document = doc;
globalThis.window = { scrollTo() {} };
globalThis.Blob = class { constructor(parts) { this.size = parts.join('').length; } };

try { eval(js); } catch (e) { console.log('  脚本体执行异常：' + e.message); }

function grab(sel) { const el = ELS[sel]; return (el && el.innerHTML) || ''; }

console.log('\n=== 1. 启动 ===');
const shell0 = grab('shell');
console.log('  lede 有内容 ' + (grab('lede').length > 40 ? 'OK' : '空'));
console.log('  总览渲染 ' + (shell0.indexOf('关键读数') >= 0 ? 'OK' : '未命中关键读数'));
console.log('  旧「单边差异独立模块」已移除 ' + (shell0.indexOf('Notes 单边字段') < 0 ? 'OK' : '仍在（不符预期）'));
console.log('  逐案一览含事实列 ' + (shell0.indexOf('事实') >= 0 ? 'OK' : '未命中'));

CURRENT = shell0;

console.log('\n=== 2. 切到逐案对照 ===');
const tabs = qsa('#views button');
console.log('  视图按钮 ' + tabs.length + ' 个');
const caseTab = tabs.find((b) => b.getAttribute('data-v') === 'cases');
if (caseTab && caseTab.onclick) caseTab.onclick();
const shell1 = grab('shell');
console.log('  进入即为 Notes 并集对照 ' + (shell1.indexOf('Notes 并集对照') >= 0 ? 'OK' : '缺失'));
console.log('  并集大表 .nunion ' + (shell1.indexOf('class="nunion"') >= 0 ? 'OK' : '缺失'));
console.log('  缺侧高亮 .miss ' + (shell1.indexOf('nu-c miss') >= 0 ? 'OK' : '未出现（该案可能无缺侧）'));
console.log('  旧「Notes 单边差异」模块 ' + (shell1.indexOf('Notes 单边差异') < 0 ? 'OK（已移除）' : '仍在'));

CURRENT = shell1;
console.log('\n=== 3. 逐案遍历 ===');
const rail = qsa('.rail-list button');
console.log('  左栏用例 ' + rail.length + ' 个');
let ok = 0, missRows = 0;
payload.cases.forEach((c, i) => {
  const b = rail.find((x) => x.getAttribute('data-k') === String(i));
  if (b && b.onclick) { b.onclick(); ok++; }
  else console.log('  案 ' + i + ' ' + c.phone + '：左栏无按钮');
});
payload.cases.forEach((c, i) => {
  const b = rail.find((x) => x.getAttribute('data-k') === String(i));
  if (b && b.onclick) b.onclick();
  CURRENT = grab('shell');
  const miss = (CURRENT.match(/nu-c miss/g) || []).length;
  missRows += miss;
  const expectMiss = c.notes_diff.only_base.length + c.notes_diff.only_v3.length;
  console.log('  案 ' + c.phone + ' 缺侧单元格 ' + miss + '（应≈' + expectMiss + '）' + (miss >= expectMiss ? ' OK' : ' 偏少'));
});
console.log('  可切换案数 ' + ok + ' / ' + payload.cases.length + ' · 缺侧单元格合计 ' + missRows);

console.log('\n=== 4. 逐案分段 ===');
const segs = ['notes', 'card', 'pipeline', 'raw'];
const rail0 = (() => { CURRENT = grab('shell'); return qsa('.rail-list button'); })();
if (rail0[0] && rail0[0].onclick) rail0[0].onclick();
segs.forEach((s) => {
  CURRENT = grab('shell');
  const b = qsa('#seg button').find((x) => x.getAttribute('data-s') === s);
  if (!b || !b.onclick) { console.log('  分段 ' + s + '：按钮缺失'); return; }
  b.onclick();
  const body = grab('shell');
  const marker = { notes: 'Notes · 并集对照', card: '画像摘要', pipeline: 'L1-1a 事实提取层', raw: '录音转写' }[s];
  console.log('  分段 ' + s + ' ' + (body.indexOf(marker) >= 0 ? 'OK' : '未命中「' + marker + '」'));
  if (s === 'pipeline') {
    console.log('    人设层块 ' + (body.indexOf('L1-1b 人设画像层') >= 0 ? 'OK' : '缺失'));
    console.log('    审查层块 ' + (body.indexOf('L2 审查核验层') >= 0 ? 'OK' : '缺失'));
    console.log('    当归锚点呈现 ' + (body.indexOf('当归') >= 0 || body.indexOf('逐字') >= 0 ? 'OK' : '未命中'));
  }
});

console.log('\n=== 5. 失败案呈现 ===');
const failed = payload.cases.filter((c) => c.v3.status && c.v3.status !== 'succeeded');
console.log('  失败案 ' + failed.length + ' 个' + (failed.length ? '（' + failed.map((c) => c.phone).join('、') + '）' : ''));

console.log('\n=== 6. v3.1 四项整改读数 ===');
const injectPhone = payload.cases.filter((c) => c.v3.inject && c.v3.inject.phone_injected).length;
const injectLicense = payload.cases.filter((c) => c.v3.inject && c.v3.inject.license_injected).length;
const salesperson = payload.cases.reduce((n, c) => n + (((c.v3.v31 || {}).salesperson) || []).length, 0);
const noisy = payload.cases.reduce((n, c) => n + (((c.v3.v31 || {}).noisy_anchors) || []).length, 0);
const stepsFields = payload.cases.reduce((n, c) => n + (((c.v3.v31 || {}).steps || {}).fields || 0), 0);
const stepsSame = payload.cases.reduce((n, c) => n + (((c.v3.v31 || {}).steps || {}).same || 0), 0);
const stepsChain = payload.cases.reduce((n, c) => n + (((c.v3.v31 || {}).steps || {}).chain || 0), 0);
console.log('  改造四 注入：phone ' + injectPhone + '/' + payload.cases.length + '，驾照 ' + injectLicense);
console.log('  改造二 顾问词拒收 ' + salesperson + ' 条 · 改造三 噪音锚点 ' + noisy + ' 条');
console.log('  改造一 同集率 ' + (stepsFields ? (stepsSame / stepsFields * 100).toFixed(1) + '%' : '—') + '（' + stepsSame + '/' + stepsFields + '），真推导链 ' + stepsChain);
/* 第 4 节收尾停在分段 raw，读的是逐案页；断言总览块前必须先切回总览。 */
const ovTab = tabs.find((b) => b.getAttribute('data-v') === 'overview');
if (ovTab && ovTab.onclick) ovTab.onclick();
const ovBody = grab('shell');
console.log('  总览含 v3.1 读数块 ' + (ovBody.indexOf('v3.1 四项整改') >= 0 ? 'OK' : '缺失'));

console.log('\n完成。');
