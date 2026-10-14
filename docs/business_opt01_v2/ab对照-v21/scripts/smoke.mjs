/* 渲染冒烟测试：加载单文件成品，用最小 DOM 垫片跑通两个视图、逐案分段与懒加载。
 * 只读；不修改成品。node smoke.mjs
 */
import fs from 'node:fs';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const TARGET = path.resolve(HERE, '..', '..', '画像管线AB对照-v2.1.html');
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
console.log('  单边差异字段合计 ' + payload.cases.reduce((n, c) => n + c.notes_diff.only_base.length + c.notes_diff.only_v2.length, 0));

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

const REG = [];
function scan(haySelector, hay, re) {
  // 把 innerHTML 里匹配到的元素登记成可查询节点
  let m;
  while ((m = re.exec(hay)) !== null) {
    const sig = haySelector + '|' + (m[1] || REG.length);
    REG.push({ sig, attrs: m[0], html: m.groups ? m.groups.body || '' : '' });
  }
}

const ELS = {
  'd-payload': node('d-payload', { textContent: JSON.stringify(payload) }),
  lede: node('lede'),
  'mh-meta': node('mh-meta'),
  shell: node('shell'),
  foot: node('foot'),
  app: node('app'),
};

/* shell.innerHTML 的写入即「一次渲染」，把它记为当前文档体 */
Object.defineProperty(ELS.shell, 'innerHTML', {
  get() { return this._html || ''; },
  set(v) { this._html = String(v); CURRENT = this._html; },
});
Object.defineProperty(ELS.lede, 'innerHTML', { get() { return this._html || ''; }, set(v) { this._html = String(v); } });
Object.defineProperty(ELS['mh-meta'], 'innerHTML', { get() { return this._html || ''; }, set(v) { this._html = String(v); } });
Object.defineProperty(ELS.foot, 'innerHTML', { get() { return this._html || ''; }, set(v) { this._html = String(v); } });

const doc = {
  readyState: 'complete',
  _listeners: [],
  addEventListener(k, fn) { this._listeners.push(fn); },
  getElementById(id) { return ELS[id] || null; },
  querySelectorAll(sel) { return qsa(sel); },
  querySelector(sel) { return qsa(sel)[0] || null; },
};

/* 查询：视图标签在静态 HTML 里；其余（左栏 / 分段 / 可跳转行 / 懒加载）在渲染产物里 */
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

/* 运行脚本体：它能把 innerHTML 写进 shell，我们据此核对 */
const origSet = Object.getOwnPropertyDescriptor(node('shell'), 'innerHTML');
try { eval(js); } catch (e) { console.log('  脚本体执行异常：' + e.message); }

function grab(sel) {
  const el = ELS[sel];
  return (el && el.innerHTML) || '';
}

console.log('\n=== 1. 启动 ===');
const shell0 = grab('shell');
console.log('  lede 有内容 ' + (grab('lede').length > 40 ? 'OK' : '空'));
console.log('  总览渲染 ' + (shell0.indexOf('关键读数') >= 0 ? 'OK' : '未命中关键读数'));
console.log('  单边差异汇总块 ' + (shell0.indexOf('Notes 单边字段') >= 0 ? 'OK' : '缺失'));

CURRENT = shell0;

console.log('\n=== 2. 切到逐案对照 ===');
const tabs = qsa('#views button');
console.log('  视图按钮 ' + tabs.length + ' 个');
const caseTab = tabs.find((b) => b.getAttribute('data-v') === 'cases');
if (caseTab && caseTab.onclick) caseTab.onclick();
const shell1 = grab('shell');
console.log('  单边差异板块 ' + (shell1.indexOf('Notes 单边差异') >= 0 ? 'OK' : '缺失'));
console.log('  基线栏 ' + (shell1.indexOf('仅基线有') >= 0 ? 'OK' : '缺失'));
console.log('  v2.1 栏 ' + (shell1.indexOf('仅 v2.1 有') >= 0 ? 'OK' : '缺失'));

CURRENT = shell1;
console.log('\n=== 3. 逐案遍历 ===');
const rail = qsa('.rail-list button');
console.log('  左栏用例 ' + rail.length + ' 个');
let ok = 0;
payload.cases.forEach((c, i) => {
  const b = rail.find((x) => x.getAttribute('data-k') === String(i));
  if (b && b.onclick) { b.onclick(); ok++; }
  else console.log('  案 ' + i + ' ' + c.phone + '：左栏无按钮');
});
console.log('  可切换案数 ' + ok + ' / ' + payload.cases.length);

console.log('\n=== 4. 逐案分段 ===');
const segs = ['notes', 'card', 'pipeline', 'raw'];
const first = payload.cases[0].phone;
const rail0 = (() => { CURRENT = grab('shell'); return qsa('.rail-list button'); })();
if (rail0[0] && rail0[0].onclick) rail0[0].onclick();
segs.forEach((s) => {
  CURRENT = grab('shell');
  const b = qsa('#seg button').find((x) => x.getAttribute('data-s') === s);
  if (!b || !b.onclick) { console.log('  分段 ' + s + '：按钮缺失'); return; }
  b.onclick();
  const body = grab('shell');
  const marker = { notes: '两臂 Notes 全量对照', card: '画像摘要', pipeline: 'L2 投影事实表', raw: '录音转写' }[s];
  console.log('  分段 ' + s + ' ' + (body.indexOf(marker) >= 0 ? 'OK' : '未命中「' + marker + '」'));
});

console.log('\n=== 5. 失败案呈现 ===');
const failed = payload.cases.filter((c) => c.v2.status && c.v2.status !== 'succeeded');
console.log('  失败案 ' + failed.length + ' 个' + (failed.length ? '（' + failed.map((c) => c.phone).join('、') + '）' : ''));
if (failed.length) {
  const idx = payload.cases.findIndex((c) => c === failed[0]);
  CURRENT = grab('shell');
  const r = qsa('.rail-list button').find((x) => x.getAttribute('data-k') === String(idx));
  if (r && r.onclick) r.onclick();
  const body = grab('shell');
  console.log('  失败提示 ' + (body.indexOf('运行未成功') >= 0 ? 'OK' : '缺失'));
}

console.log('\n完成。');
