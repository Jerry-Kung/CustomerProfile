/* 渲染冒烟：把打包好的单文件页面装进一个最小 DOM 桩里跑一遍。
 *
 * 目的不是「看一眼像不像」，而是把三种可能的静默失败变成硬报错：
 * 1. 数据块没内联进去、或 JSON 解析失败——页面只剩一句错误提示；
 * 2. 渲染期抛异常——shell 里只有半截 HTML；
 * 3. 某些案/某些分段渲染出空内容——单看某个案「像是数据就长这样」。
 *
 * 覆盖：总览 → 逐案（39 案 × 三个分段）→ 每案 shell 非空且含预期段落标题。
 *
 * 用法（在本目录下执行）：
 *
 *     node scripts/smoke.mjs
 */
import fs from 'node:fs';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const HTML = path.resolve(HERE, '..', '..', '画像管线AB对照-v3.3.html');
const FALLBACK = path.resolve(HERE, '..', 'index.html');

const problems = [];
function fail(msg) { problems.push(msg); }

/* ---------- 取页面与数据 ---------- */
let src;
try {
  src = fs.readFileSync(HTML, 'utf8');
} catch (e) {
  console.error('找不到成品页：' + HTML);
  console.error('请先运行  python -X utf8 build.py');
  process.exit(2);
}

const dataMatch = src.match(/<script type="application\/json" id="d-payload">([\s\S]*?)<\/script>/);
if (!dataMatch) { console.error('[FAIL] 成品页里没有内联数据块 d-payload'); process.exit(1); }
const rawJson = dataMatch[1];

const appParts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (!appParts.length) { console.error('[FAIL] 成品页里没有内联脚本'); process.exit(1); }
const app = appParts.join('\n');

/* ---------- 最小 DOM 桩 ---------- */
function makeEl(tag, id, attrs) {
  return {
    tag, id, _html: '', _attrs: Object.assign({}, attrs || {}), dataset: {},
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    get textContent() { return this._text || ''; },
    set textContent(v) { this._text = String(v); },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    querySelector() { return null; },
    addEventListener() {},
    scrollTo() {},
  };
}

const ELS = new Map();
const segBtns = ['card', 'notes', 'machine'].map((s) => makeEl('button', null, { 'data-s': s }));
const viewBtns = ['overview', 'cases'].map((v) => makeEl('button', null, { 'data-v': v }));

/* 预置 39 个侧栏按钮：wire() 会把 onclick 挂上来，之后逐个点它们切案。 */
const railBtns = Array.from({ length: 39 }, (_, i) => makeEl('button', null, { 'data-k': String(i) }));

const Q = {
  '#views button': viewBtns,
  '#seg button': segBtns,
  '.rail-list button[data-k]': railBtns,
  'tr[data-jump]': [],
  'details[data-lazy]': [],
};

const document = {
  readyState: 'complete',
  getElementById(id) {
    if (!ELS.has(id)) ELS.set(id, makeEl('div', id));
    return ELS.get(id);
  },
  querySelectorAll(sel) { return Q[sel] || []; },
  addEventListener() {},
  querySelector() { return null; },
};
const window = { scrollTo() {} };

const shell = document.getElementById('shell');
document.getElementById('d-payload').textContent = rawJson;

/* ---------- 跑 ---------- */
try {
  new Function('document', 'window', app)(document, window);
} catch (e) {
  console.error('[FAIL] 启动期抛异常：' + e.message);
  console.error(e.stack);
  process.exit(1);
}

if (/err-box/.test(shell.innerHTML)) {
  fail('启动后 shell 里是错误提示框：' + shell.innerHTML.slice(0, 300));
}

/* 总览 */
const ov = shell.innerHTML;
if (!/关键读数/.test(ov)) fail('总览缺「关键读数」块');
if (!/逐案一览/.test(ov)) fail('总览缺「逐案一览」块');
if (!/analysis_result 十段/.test(ov)) fail('总览缺「十段覆盖」块');

const jumpCount = (ov.match(/data-jump="/g) || []).length;
if (jumpCount !== 39) fail('逐案一览行数应为 39，实际 ' + jumpCount);

const lede = document.getElementById('lede').innerHTML;
if (!/39 案/.test(lede)) fail('报头 lede 未含「39 案」：' + lede.slice(0, 200));

/* 逐案 × 三分段 */
viewBtns[1].onclick();               // 切到「逐案对照」
let rendered = 0;
const SECTION_HEADS = ['画像摘要', '推断标签', '用车场景', '沟通策略', '客户概览',
  '购车动机', '产品偏好', '抗性/顾虑', '情绪状态'];

for (let i = 0; i < railBtns.length; i++) {
  if (typeof railBtns[i].onclick !== 'function') { fail('侧栏第 ' + i + ' 个按钮没有挂上 onclick'); continue; }
  railBtns[i].onclick();
  for (const seg of segBtns) {
    if (typeof seg.onclick !== 'function') { fail('分段按钮 ' + seg.getAttribute('data-s') + ' 无 onclick'); continue; }
    seg.onclick();
    const html = shell.innerHTML;
    if (!html || html.length < 200) {
      fail('案 #' + i + ' 分段「' + seg.getAttribute('data-s') + '」渲染为空（' + html.length + ' 字节）');
      continue;
    }
    if (/err-box/.test(html)) { fail('案 #' + i + ' 分段「' + seg.getAttribute('data-s') + '」出现错误框'); continue; }
    if (seg.getAttribute('data-s') === 'card') {
      const missing = SECTION_HEADS.filter((t) => html.indexOf(t) < 0);
      if (missing.length) fail('案 #' + i + ' 画像卡缺段落小标题：' + missing.join('、'));
    }
    if (seg.getAttribute('data-s') === 'notes' && html.indexOf('nu-row') < 0) {
      fail('案 #' + i + ' 的 Notes 并集表没有任何行');
    }
    if (seg.getAttribute('data-s') === 'machine' && html.indexOf('异常条数') < 0) {
      fail('案 #' + i + ' 的机器读数缺「异常条数」行');
    }
    rendered++;
  }
}

/* ---------- 报结果 ---------- */
console.log('成品页   : ' + HTML);
console.log('内联数据 : ' + (rawJson.length / 1048576).toFixed(2) + ' MB');
console.log('渲染组合 : ' + rendered + ' 个（39 案 × 3 分段）');
console.log('总览行数 : ' + jumpCount + ' 案');

if (problems.length) {
  console.error('\n[FAIL] 发现 ' + problems.length + ' 个问题：');
  problems.slice(0, 20).forEach((p) => console.error('  - ' + p));
  if (problems.length > 20) console.error('  ... 还有 ' + (problems.length - 20) + ' 个');
  process.exit(1);
}
console.log('\n[OK] 冒烟通过：总览与 39 案 × 3 分段全部渲染出非空内容，无异常、无错误框。');
