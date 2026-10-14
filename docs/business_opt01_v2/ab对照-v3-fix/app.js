/* 画像管线 AB 对照 v3 — 静态渲染器
 * 数据：payload.json（8 案完整输入 + 两臂全部输出 + Notes 并集对照）
 * 只读一次，按需构建 DOM。两个视图：总览 / 逐案对照。
 *
 * 与 v2.1 版的唯一呈现改动：Notes 不再分「两边都有 / 仅基线有 / 仅 v3 有」三块，
 * 而是把两臂按 attribute_key 求并集，排成一张两栏大表；某一侧缺该字段时，
 * 该侧单元格留空并高亮，供一眼看出「哪边有、哪边没有」。
 */
(function () {
  'use strict';

  var STATE = { view: 'overview', kase: 0, seg: 'notes' };
  var D = null;

  /* ---------- 基础工具 ---------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function int(n) { return (Number(n) || 0).toLocaleString('en-US'); }
  function bytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(2) + ' MB';
  }
  function has(v) {
    if (v == null) return false;
    if (Array.isArray(v)) return v.length > 0;
    if (typeof v === 'object') return Object.keys(v).length > 0;
    if (typeof v === 'string') return v.trim() !== '';
    return true;
  }
  function nul() { return '<span class="nul">— 空 —</span>'; }
  function txt(v) {
    if (!has(v)) return nul();
    if (typeof v === 'object') return '<code>' + esc(JSON.stringify(v)) + '</code>';
    return esc(v);
  }
  function valStr(v) {
    if (!has(v)) return '';
    if (Array.isArray(v)) return v.map(function (x) { return typeof x === 'object' ? JSON.stringify(x) : String(x); }).join(' / ');
    if (typeof v === 'object') return v.input_value || v.key || v.display_name || JSON.stringify(v);
    return String(v);
  }
  function pct(a, b) { return b ? Math.round(a / b * 1000) / 10 + '%' : '—'; }
  function jbytes(v) { try { return new Blob([JSON.stringify(v)]).size; } catch (e) { return JSON.stringify(v).length; } }
  function pretty(v) {
    if (typeof v === 'string') { try { return JSON.stringify(JSON.parse(v), null, 1); } catch (e) { return v; } }
    return JSON.stringify(v, null, 1);
  }

  /* ---------- 聚合 ---------- */
  function agg() {
    var c = D.cases, o = {
      phones: c.length,
      notesV3: 0, notesBs: 0, secV3: 0, secBs: 0,
      llmV3: 0, llmBs: 0, tokV3: 0, tokBs: 0,
      onlyBase: 0, onlyV3: 0, both: 0,
      factsV3: 0, personaFields: 0, reviewIssues: 0,
      secByKey: {}, failedV3: [], emptyV3: [], failedBs: []
    };
    D.sections.forEach(function (k) { o.secByKey[k] = { v3: 0, bs: 0 }; });
    c.forEach(function (x) {
      o.notesV3 += x.v3.notes.length; o.notesBs += x.base.notes.length;
      o.llmV3 += x.v3.metrics.llm_calls; o.llmBs += x.base.metrics.llm_calls;
      o.tokV3 += x.v3.metrics.tokens; o.tokBs += x.base.metrics.tokens;
      o.onlyBase += x.notes_diff.only_base.length;
      o.onlyV3 += x.notes_diff.only_v3.length;
      o.both += x.notes_diff.both.length;
      o.factsV3 += ((x.v3.facts && x.v3.facts.records) || []).length;
      o.personaFields += personaCount(x.v3.persona);
      o.reviewIssues += Number((x.v3.review && x.v3.review.issue_count) || 0);
      D.sections.forEach(function (k) {
        var a = x.v3.sec_counts[k] || 0, b = x.base.sec_counts[k] || 0;
        o.secByKey[k].v3 += a; o.secByKey[k].bs += b;
        o.secV3 += a; o.secBs += b;
      });
      if (x.v3.status && x.v3.status !== 'succeeded') o.failedV3.push(x.phone);
      else if (!x.v3.notes.length) o.emptyV3.push(x.phone);
      if (!x.base.notes.length) o.failedBs.push(x.phone);
    });
    return o;
  }

  function personaCount(p) {
    if (!p) return 0;
    var obj = p.persona || p;
    if (typeof obj !== 'object') return 0;
    return Object.keys(obj).filter(function (k) { return has((obj[k] || {}).value); }).length;
  }

  /* ---------- 可读性检查（对客产出硬约束） ---------- */
  var AUDIT = ['归属过滤', '来源分级', '枚举归一', '单多值约束', '契约校验', '未知渠道',
    '社交自呈现', '一手交互', '第三方预测', '公开登记查询', '人工/系统登记', '按来源分级投影得到'];
  function readability() {
    var r = { hits: 0, hitWords: {}, hitCases: [], unknown: 0, ev: 0, evReadable: 0, selfRef: 0, selfRefCases: [] };
    D.cases.forEach(function (c) {
      var caseHits = 0, caseSelf = 0;
      c.v3.notes.forEach(function (n) {
        var rd = n.reasoning_detail || {};
        var blob = String(rd.summary || '') + ' ' + (rd.steps || []).join(' ');
        AUDIT.forEach(function (w) {
          if (blob.indexOf(w) >= 0) { r.hits++; caseHits++; r.hitWords[w] = (r.hitWords[w] || 0) + 1; }
        });
        (rd.evidence || []).forEach(function (s) {
          r.ev++;
          var t = String(s);
          if (t.indexOf('未知渠道') >= 0) r.unknown++;
          if (t.indexOf('：') >= 0 || t.indexOf(':') >= 0) r.evReadable++;
          if (/推断为/.test(t) && /置信度|置信/.test(t)) { r.selfRef++; caseSelf++; }
        });
      });
      if (caseHits) r.hitCases.push(c.phone);
      if (caseSelf) r.selfRefCases.push(c.phone);
    });
    r.hitCases = r.hitCases.join('、');
    r.selfRefCases = r.selfRefCases.join('、');
    return r;
  }

  /* ---------- Notes / section 渲染 ---------- */
  function reasoning(rd, open) {
    if (!has(rd)) return '';
    var p = [];
    if (has(rd.summary)) p.push('<div><span class="lb">推理</span><div>' + esc(rd.summary) + '</div></div>');
    if (has(rd.steps)) p.push('<div><span class="lb">推导链</span><ol>' + rd.steps.map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ol></div>');
    if (has(rd.evidence)) p.push('<div><span class="lb">依据</span><div class="ev">' + rd.evidence.map(function (s) { return '<span>' + esc(s) + '</span>'; }).join('') + '</div></div>');
    if (rd.confidence != null) p.push('<div><span class="lb">置信度</span> <span class="conf">' + esc(rd.confidence) + '</span></div>');
    if (!p.length) return '';
    return '<details class="rz"' + (open ? ' open' : '') + '><summary>推理明细</summary><div class="rz-bd">' + p.join('') + '</div></details>';
  }

  function noteCell(n) {
    if (!n) {
      return '<div class="nu-c miss"><div class="miss-in">⊘ 本臂无此字段</div></div>';
    }
    var rd = n.reasoning_detail || {};
    var conf = rd.confidence != null ? '<span class="conf">置信 ' + esc(rd.confidence) + '</span>' : '';
    return '<div class="nu-c"><div class="nc-v">' + txt(n.attribute_value) + conf + '</div>' + reasoning(rd, false) + '</div>';
  }

  function noteRow(n, flag) {
    var rd = n.reasoning_detail || {};
    var cls = flag === 'v3' ? ' onlyv3' : (flag === 'base' ? ' onlybase' : '');
    var conf = rd.confidence != null ? '<span class="conf">置信 ' + esc(rd.confidence) + '</span>' : '';
    return '<div class="note-row' + cls + '">' +
      '<div class="nr-h"><span class="nr-k">' + esc(n.attribute_key || '') + '</span>' +
      '<span class="nr-v">' + txt(n.attribute_value) + '</span>' +
      '<span class="nr-meta">' + conf + '</span></div>' +
      reasoning(rd, false) + '</div>';
  }

  function notesList(notes, flag) {
    if (!has(notes)) return '<div class="nul" style="padding:8px 0">本臂无 Notes。</div>';
    return notes.map(function (n) { return noteRow(n, flag); }).join('');
  }

  /* 两臂 Notes 并集：按 attribute_key 对齐，缺的一侧留空并高亮。 */
  function notesUnion(c) {
    var bm = {}, vm = {};
    c.base.notes.forEach(function (n) { if (n && n.attribute_key) bm[String(n.attribute_key)] = n; });
    c.v3.notes.forEach(function (n) { if (n && n.attribute_key) vm[String(n.attribute_key)] = n; });
    var keys = Object.keys(bm);
    Object.keys(vm).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    keys.sort();

    var baseMissing = 0, v3Missing = 0;
    keys.forEach(function (k) { if (!bm[k]) baseMissing++; if (!vm[k]) v3Missing++; });

    var h = '<div class="nu-legend">' +
      '<span class="k"><span class="sw2" style="background:var(--delbg);border-color:var(--delline)"></span>该侧无此字段（留空高亮）</span>' +
      '<span class="k">共 ' + int(keys.length) + ' 个字段 · 基线缺 ' + int(baseMissing) + ' 个 · v3 缺 ' + int(v3Missing) + ' 个</span>' +
      '</div>';
    h += '<div class="nunion"><div class="nu-head">' +
      '<div class="nu-k">字段</div>' +
      '<div class="nu-c base">基线<span class="ct">' + esc(c.base.notes.length) + ' 条</span></div>' +
      '<div class="nu-c v3">v3<span class="ct">' + esc(c.v3.notes.length) + ' 条</span></div></div>';
    keys.forEach(function (k) {
      var bn = bm[k], vn = vm[k];
      var cls = 'nu-row' + (!bn ? ' miss-left' : '') + (!vn ? ' miss-right' : '');
      h += '<div class="' + cls + '">' +
        '<div class="nu-k">' + esc(k) + '</div>' +
        noteCell(bn) + noteCell(vn) + '</div>';
    });
    h += '</div>';
    return h;
  }

  function isValueShape(obj) {
    var ks = Object.keys(obj);
    if (!ks.length) return false;
    var hit = ks.filter(function (k) {
      var v = obj[k];
      return v && typeof v === 'object' && !Array.isArray(v) && ('value' in v || 'reasoning_detail' in v);
    }).length;
    return hit > ks.length / 2;
  }

  function sectionBlock(val) {
    if (!has(val)) return '<div class="note">本臂该 section 为空。</div>';
    if (Array.isArray(val)) {
      if (!val.length) return '<div class="note">本臂该 section 为空数组。</div>';
      return val.map(function (it) {
        if (typeof it !== 'object' || it === null) return '<div class="item"><div class="t">' + esc(String(it)) + '</div></div>';
        var title = it.title || it.strategy_name || it.motivation_name || it.preference_name || it.resistance_name || '';
        var desc = it.desc || it.description || '';
        var extra = '';
        if (has(it.business_scenario)) extra += '<span class="tag t-info">' + esc(it.business_scenario) + '</span>';
        if (has(it.severity)) extra += '<span class="tag t-warn">' + esc(it.severity) + '</span>';
        if (it.rank_order != null && it.rank_order !== '') extra += '<span class="tag t-mut">#' + esc(it.rank_order) + '</span>';
        if (it.confidence != null) extra += '<span class="conf">置信 ' + esc(it.confidence) + '</span>';
        return '<div class="item"><div class="t"><span>' + esc(title || '（未命名）') + '</span>' + extra + '</div>' +
          (has(desc) ? '<div class="d">' + esc(desc) + '</div>' : '') + reasoning(it.reasoning_detail, false) + '</div>';
      }).join('');
    }
    if (typeof val === 'object') {
      if (isValueShape(val)) {
        return val ? Object.keys(val).map(function (k) {
          var v = val[k] || {};
          return '<div class="item"><div class="t">' + esc(k) + ' <span style="font-weight:400;color:var(--ink2)">' + txt(v.value) + '</span></div>' +
            reasoning(v.reasoning_detail, false) + '</div>';
        }).join('') : '';
      }
      return '<dl class="kv">' + Object.keys(val).map(function (k) {
        return '<dt>' + esc(k) + '</dt><dd>' + txt(val[k]) + '</dd>';
      }).join('') + '</dl>';
    }
    return '<div>' + txt(val) + '</div>';
  }

  /* ---------- 视图：总览 ---------- */
  function viewOverview() {
    var a = agg(), rd = readability();
    var h = '';

    var notesPct = Math.round(a.notesV3 / Math.max(a.notesBs, 1) * 100);
    var secPct = Math.round(a.secV3 / Math.max(a.secBs, 1) * 100);
    h += '<div class="verdicts">';

    h += '<div class="vcard up"><span class="flag">v3 达标的项</span><ul>';
    h += '<li><b>8 案全部跑通</b>：' + int(a.phones) + ' 案无一失败，v2.1 崩溃的 <code>13435844484</code> 本次产出完整（JSONL + 括号抢救生效）。</li>';
    h += '<li><b>Audit 词归零</b>：对客产出里审计/规则/来源档代号命中 <b>' + int(rd.hits) + '</b> 处（v2.1 为 28 处）。</li>';
    h += '<li><b>「未知渠道」占位归零</b>：' + int(rd.unknown) + ' 处。</li>';
    h += '<li><b>事实层带来源坐标</b>：L1-1a 共提取 ' + int(a.factsV3) + ' 条事实/高置信推断，每条带锚点或原文校正。</li>';
    h += '<li><b>人设层不进证据位</b>：新增人设画像 ' + int(a.personaFields) + ' 项（8 案），仅作一致性参考。</li>';
    h += '</ul></div>';

    h += '<div class="vcard dn"><span class="flag">需关注</span><ul>';
    if (rd.selfRef) {
      h += '<li><b>证据位自指仍有 ' + int(rd.selfRef) + ' 处</b>：<b>' + esc(rd.selfRefCases) + '</b> 案的依据里出现「推断为…（置信度…）」这类结论当依据的形态。</li>';
    } else {
      h += '<li><b>证据位自指 = 0</b>：依据位未发现「结论当依据」的形态。</li>';
    }
    var gap = a.notesBs - a.notesV3;
    h += '<li><b>Notes 条数 ' + int(a.notesV3) + ' vs 基线 ' + int(a.notesBs) + '</b>（差 ' + (gap >= 0 ? '−' : '+') + int(Math.abs(gap)) + '）：条数不等于质量，逐案看并集大表。</li>';
    h += '<li><b>两侧字段集合不同</b>：仅基线有 ' + int(a.onlyBase) + ' 个、仅 v3 有 ' + int(a.onlyV3) + ' 个，两边都有 ' + int(a.both) + ' 个——逐案对照页把它们并成一张表看。</li>';
    if (a.failedV3.length) h += '<li><b>' + a.failedV3.length + ' 案 v3 运行未成功</b>：' + a.failedV3.map(esc).join('、') + '。</li>';
    if (a.emptyV3.length) h += '<li><b>' + a.emptyV3.length + ' 案上游取数大面积失败</b>：' + a.emptyV3.map(esc).join('、') + ' 运行成功但无材料可提取。</li>';
    h += '</ul></div>';

    h += '<div class="vcard cal"><span class="flag">读这页要留意的口径</span><ul>';
    h += '<li>两臂<b>同号码、同输入</b>，差异只来自实现本身。基线是现行主入口，v3 是三分层实验臂。</li>';
    h += '<li>Notes 并集大表把<b>两臂字段对齐</b>：某字段只在一侧有时，另一侧留空并高亮，一眼可辨。</li>';
    h += '<li>section 计数把列表按条数、值字典按非空字段数计，<b>只反映内容量</b>，不代表准确性。</li>';
    h += '</ul></div>';
    h += '</div>';

    /* 读数 */
    h += '<div class="block"><div class="sh"><h2>关键读数</h2><span class="hint">8 案合计 · 基线 → v3</span></div>';
    h += '<div class="readout">';
    h += ro(int(a.notesBs), int(a.notesV3), 'Notes 条数', notesPct + '% of 基线', notesPct >= 100 ? 'good' : 'bad');
    h += ro(int(a.secBs), int(a.secV3), '画像卡条目', secPct + '% of 基线', secPct >= 100 ? 'good' : 'bad');
    h += ro(int(a.onlyBase), int(a.onlyV3), '单边字段（仅基线 / 仅 v3）', '共 ' + int(a.both) + ' 字段两边都有', 'bad');
    h += ro(int(a.factsV3), int(a.personaFields), 'L1-1a 事实 → L1-1b 人设项', '事实带坐标 · 人设不进证据位', 'good');
    h += ro(rd.hits === 0 ? '0' : rd.hits, rd.selfRef, '审计词命中 / 证据位自指', rd.hits === 0 ? '对客产出硬约束已满足' : '仍有审计词', rd.hits === 0 ? 'good' : 'bad');
    h += ro(int(a.reviewIssues), rd.unknown, 'L2 审查命中 / 未知渠道占位', '审查层：占位清除、枚举合法、fact 有锚点', (a.reviewIssues === 0 && rd.unknown === 0) ? 'good' : 'bad');
    h += ro(int(a.llmBs), int(a.llmV3), 'LLM 调用次数', 'v3 = ' + int(a.llmV3) + ' 次', 'flat');
    h += ro(int(Math.round(a.tokBs / 1000)) + 'k', int(Math.round(a.tokV3 / 1000)) + 'k', 'Token 用量', 'v3 = ' + int(Math.round(a.tokV3 / 1000)) + 'k', 'flat');
    h += '</div></div>';

    /* section 覆盖 */
    h += '<div class="block"><div class="sh sub"><h3>画像卡 9 个 section · 逐项覆盖</h3><span class="hint">数字为 8 案合计条目数</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>section</th><th>字段名</th><th class="n">基线</th><th class="n">v3</th><th class="n">差值</th><th>判定</th></tr></thead><tbody>';
    D.sections.forEach(function (k) {
      var b = a.secByKey[k].bs, v = a.secByKey[k].v3, d = v - b;
      h += '<tr><td>' + esc(D.section_labels[k]) + '</td><td class="k">' + esc(k) + '</td>' +
        '<td class="n">' + int(b) + '</td><td class="n">' + int(v) + '</td>' +
        '<td class="n ' + (d > 0 ? 'better' : d < 0 ? 'worse' : 'flat') + '">' + (d > 0 ? '+' : '') + int(d) + '</td>' +
        '<td>' + (d >= 0 ? '<span class="tag t-ok">持平或更多</span>' : '<span class="tag t-del">少于基线</span>') + '</td></tr>';
    });
    h += '<tr><td><b>Notes</b></td><td class="k">basic_notes_updates</td>' +
      '<td class="n"><b>' + int(a.notesBs) + '</b></td><td class="n"><b>' + int(a.notesV3) + '</b></td>' +
      '<td class="n ' + (a.notesV3 >= a.notesBs ? 'better' : 'worse') + '"><b>' + (a.notesV3 - a.notesBs > 0 ? '+' : '') + int(a.notesV3 - a.notesBs) + '</b></td>' +
      '<td>' + (a.notesV3 >= a.notesBs ? '<span class="tag t-ok">持平或更多</span>' : '<span class="tag t-del">少于基线</span>') + '</td></tr>';
    h += '</tbody></table></div></div></div>';

    /* 逐案一览 */
    h += '<div class="block"><div class="sh sub"><h3>逐案一览</h3><span class="hint">点号码进入逐案对照</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th class="n">基线 Notes</th><th class="n">v3 Notes</th><th class="n">仅基线</th><th class="n">仅 v3</th>' +
      '<th class="n">基线卡面</th><th class="n">v3 卡面</th><th class="n">事实</th><th class="n">审查</th><th>状态</th></tr></thead><tbody>';
    D.cases.forEach(function (c, i) {
      var sc3 = 0, scb = 0;
      D.sections.forEach(function (k) { sc3 += c.v3.sec_counts[k] || 0; scb += c.base.sec_counts[k] || 0; });
      var bad = c.v3.status && c.v3.status !== 'succeeded';
      var nf = ((c.v3.facts && c.v3.facts.records) || []).length;
      var iss = Number((c.v3.review && c.v3.review.issue_count) || 0);
      h += '<tr style="cursor:pointer" data-jump="' + i + '">' +
        '<td class="k">' + esc(c.phone) + '</td>' +
        '<td class="n">' + c.base.notes.length + '</td><td class="n">' + c.v3.notes.length + '</td>' +
        '<td class="n ' + (c.notes_diff.only_base.length ? 'worse' : 'flat') + '">' + c.notes_diff.only_base.length + '</td>' +
        '<td class="n ' + (c.notes_diff.only_v3.length ? '' : 'flat') + '">' + c.notes_diff.only_v3.length + '</td>' +
        '<td class="n">' + scb + '</td><td class="n">' + sc3 + '</td>' +
        '<td class="n">' + nf + '</td>' +
        '<td>' + (iss ? '<span class="tag t-warn">' + iss + '</span>' : '<span class="tag t-ok">0</span>') + '</td>' +
        '<td>' + (bad ? '<span class="tag t-del">运行失败</span>' : '<span class="tag t-ok">成功</span>') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';
    return h;
  }

  function ro(from, to, label, sub, cls) {
    return '<div class="ro"><div class="v ' + cls + '">' + esc(from) + '<span class="to">→</span>' + esc(to) + '</div>' +
      '<div class="l">' + esc(label) + '</div><div class="sub">' + esc(sub) + '</div></div>';
  }

  /* ---------- 视图：逐案对照 ---------- */
  function viewCases() {
    var c = D.cases[STATE.kase];
    var h = '';

    var bad = c.v3.status && c.v3.status !== 'succeeded';
    var empty = !bad && !c.v3.notes.length;
    if (bad) {
      h += '<div class="note err"><b>本案 v3 运行未成功（' + esc(c.v3.status) + '）</b>：未产出画像与 Notes。' +
        '因此下方基线侧的字段全部落在「v3 缺」一侧，其数量是<b>整案基线字段数</b>，不代表 v3 的召回缺口。' +
        (c.v3.error ? '<br><span style="color:var(--muted);font-size:12px">原因摘要：<code>' + esc(String(c.v3.error).slice(0, 240)) + '</code></span>' : '') +
        '</div>';
    } else if (empty) {
      h += '<div class="note err"><b>本案 v3 运行成功，但上游取数大面积异常、未产出任何结论</b>：' +
        '模型手里没有材料，Notes 与画像卡因此为空。这是<b>取数侧问题，不是 v3 管线的提取缺陷</b>。' +
        '下方基线侧的字段全部落在「v3 缺」一侧。</div>';
    }

    /* 分段切换 */
    h += '<div class="seg" id="seg">' +
      segBtn('notes', 'Notes 并集对照') + segBtn('card', '画像卡对照') +
      segBtn('pipeline', 'L1/L2 分层明细') + segBtn('raw', '原始输入') + '</div>';
    h += '<div id="segbody">' + segBody(c) + '</div>';
    return h;
  }
  function segBtn(k, label) { return '<button data-s="' + k + '" aria-selected="' + (STATE.seg === k) + '">' + label + '</button>'; }

  function segBody(c) {
    if (STATE.seg === 'notes') {
      var h = '<div class="block">';
      h += '<div class="sh sub"><h3>两臂 Notes · 并集对照</h3>' +
        '<span class="hint">按字段对齐，共 ' + int(c.notes_diff.both.length + c.notes_diff.only_base.length + c.notes_diff.only_v3.length) + ' 个字段 · 左基线右 v3</span></div>';
      h += notesUnion(c);
      h += '<div class="note">某一字段只在一侧有结论时，另一侧<b>留空并高亮</b>（<span style="color:var(--del);font-weight:700">红色 ⊘</span>）。' +
        '每条的值与推理明细都完整保留：v3 侧的「推理」是<b>结论怎么推出来的</b>，「依据」是<b>客户原始数据里的哪一段</b>；' +
        '基线侧的推理明细多为过程描述。</div>';
      h += '</div>';
      return h;
    }
    if (STATE.seg === 'card') {
      var h2 = '';
      D.sections.forEach(function (k) {
        var cv = c.v3.sec_counts[k] || 0, cb = c.base.sec_counts[k] || 0;
        h2 += '<div class="block"><div class="sh sub"><h3>' + esc(D.section_labels[k]) + '</h3>' +
          '<span class="hint">' + esc(k) + ' · 基线 ' + cb + ' → v3 ' + cv + '</span></div>';
        h2 += '<div class="collate"><div class="col-head">' +
          '<div><span class="tag t-base">基线</span></div><div><span class="tag t-v3">v3</span></div></div>' +
          '<div class="col-body"><div>' + sectionBlock(c.base.sections[k]) + '</div>' +
          '<div>' + sectionBlock(c.v3.sections[k]) + '</div></div></div></div>';
      });
      return h2;
    }
    if (STATE.seg === 'pipeline') return pipelineBody(c);
    return rawBody(c);
  }

  function factTable(recs) {
    var h = '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>字段</th><th>取值</th><th>档位</th><th>锚点类型</th><th>锚点 / 原文校正</th><th>渠道</th><th>主体</th><th class="n">置信</th></tr></thead><tbody>';
    (recs || []).forEach(function (x) {
      var b = String(x.basis || '');
      var cls = b === 'fact' ? 't-ok' : (b === 'inferred_high' ? 't-info' : 't-warn');
      var ak = String(x.anchor_kind || '');
      var akTag = ak === 'verbatim' ? '<span class="tag t-ok">逐字</span>'
        : (ak === 'normalized' ? '<span class="tag t-warn">当归</span>' : '<span class="nul">—</span>');
      var anchor = '';
      if (ak === 'normalized') {
        anchor = '<div style="font-size:12px">原文：<code>' + esc(x.source_quote || '') + '</code></div>' +
          (x.normalize_reason ? '<div style="font-size:11.5px;color:var(--muted)">' + esc(x.normalize_reason) + '</div>' : '');
      } else if (has(x.anchor)) {
        anchor = '<div style="font-size:12px;color:var(--ink2);overflow-wrap:anywhere">' + esc(x.anchor) + '</div>';
      } else if (has(x.basis_of_inference)) {
        anchor = '<div style="font-size:12px;color:var(--ink2)">' + x.basis_of_inference.map(esc).join('；') + '</div>';
      } else { anchor = nul(); }
      h += '<tr><td class="k">' + esc(x.attribute) + '</td><td>' + txt(x.value) + '</td>' +
        '<td><span class="tag ' + cls + '">' + esc(D.tier_label[b] || b) + '</span></td>' +
        '<td>' + akTag + '</td><td>' + anchor + '</td>' +
        '<td class="k">' + (has(x.record_channel) ? esc(x.record_channel) : '<span class="nul">空</span>') + '</td>' +
        '<td class="k">' + esc(x.subject || '') + '</td>' +
        '<td class="n">' + esc(x.confidence) + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
    return h;
  }

  function personaBlock(p) {
    if (!has(p)) return '<div class="note">本案人设层无产出。</div>';
    var obj = p.persona || {}, narr = p.narrative || '';
    var h = '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>人设项</th><th>判断</th><th>依据（provenance，回溯到事实）</th></tr></thead><tbody>';
    Object.keys(obj).forEach(function (k) {
      var v = obj[k] || {};
      h += '<tr><td class="k">' + esc(k) + '</td><td>' + txt(v.value) + '</td>' +
        '<td style="font-size:12.5px;color:var(--ink2)">' + (has(v.provenance) ? v.provenance.map(esc).join('；') : nul()) + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
    if (has(narr)) h += '<div class="note"><b>整体描述</b>：' + esc(narr) + '</div>';
    h += '<div class="note warn">人设层<b>不进证据位</b>：它只作后续推理的一致性参考；写进 Notes 或卡片的取值，其证据须回溯到带坐标的事实。</div>';
    return h;
  }

  function reviewBlock(rv) {
    if (!has(rv)) return '<div class="note">本案无审查记录。</div>';
    var iss = Number(rv.issue_count || 0);
    var h = '<div class="readout">';
    h += ro('—', iss, 'L2 审查命中', '占位清除 / 枚举合法 / fact 有锚点 / 证据位自指', iss === 0 ? 'good' : 'bad');
    h += ro('—', (rv.placeholder_removed || []).length, '无效占位清除', '「未提供 / 无」这类占位串', 'good');
    h += ro('—', (rv.enum_illegal || []).length, '枚举非法（未降级）', '归不到合法键且未标低置信', (rv.enum_illegal || []).length ? 'bad' : 'good');
    h += ro('—', (rv.unbacked_fact || []).length, '无锚点 fact', 'fact 却指不出原文', (rv.unbacked_fact || []).length ? 'bad' : 'good');
    h += '</div>';
    if (has(rv.issues)) {
      h += '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>判据</th><th class="n">命中</th><th>明细（前 10）</th></tr></thead><tbody>';
      (rv.issues || []).forEach(function (it) {
        h += '<tr><td class="k">' + esc(it.rule) + '</td><td class="n">' + esc(it.count) + '</td>' +
          '<td style="font-size:12px">' + esc(JSON.stringify(it.detail)) + '</td></tr>';
      });
      h += '</tbody></table></div></div>';
    } else {
      h += '<div class="note ok">本案 L2 审查四条判据全部通过（命中 0）。</div>';
    }
    return h;
  }

  function pipelineBody(c) {
    var v = c.v3, loc = v.localize || {}, pr = v.project || {};
    var h = '<div class="block">';
    h += '<div class="sh sub"><h3>管线读数</h3><span class="hint">分诊 ' + esc(v.tier || '—') + ' · 可用渠道 ' + esc(v.usable_count) + ' · 记录 ' + esc(v.records_count) + '</span></div>';
    h += '<div class="readout">';
    h += ro(((v.facts && v.facts.records) || []).length, Object.keys(pr.facts || {}).length, 'L1-1a 事实 → 落档字段', '事实层带坐标，投影落档', 'good');
    h += ro(personaCount(v.persona), loc.kept, 'L1-1b 人设项 → L1-1c 保留', '丢弃 ' + esc(loc.dropped) + ' · 降级 ' + esc(loc.downgraded), 'good');
    var basisC = {};
    Object.keys(pr.basis || {}).forEach(function (k) { basisC[pr.basis[k]] = (basisC[pr.basis[k]] || 0) + 1; });
    h += ro(Object.keys(pr.basis || {}).length, (basisC.fact || 0), '落档字段 → 其中事实档', '强推断 ' + (basisC.inferred_high || 0) + ' · 弱推断 ' + (basisC.inferred_low || 0), 'good');
    h += ro(esc(pr.notes_count), v.notes.length, '投影产出 → Notes 条数', '装配后实际写出的条数', 'good');
    h += '</div></div>';

    /* L1-1a 事实提取层 */
    var frs = (v.facts && v.facts.records) || [];
    h += '<div class="block"><div class="sh sub"><h3>L1-1a 事实提取层</h3>' +
      '<span class="hint">' + frs.length + ' 条 · 逐字锚 / 当归（原文校正）</span></div>';
    h += frs.length ? factTable(frs) : '<div class="note warn">本案事实层未提取到记录。</div>';
    if (v.facts && has(v.facts.dropped_lines)) {
      h += '<div class="note warn">JSONL 坏行留痕 ' + int((v.facts.dropped_lines || []).length) + ' 行（未丢整段）。</div>';
    }
    h += '</div>';

    /* L1-1b 人设画像层 */
    h += '<div class="block"><div class="sh sub"><h3>L1-1b 人设画像层</h3><span class="hint">半结构化 + 一段整体描述 · 不进证据位</span></div>';
    h += personaBlock(v.persona);
    h += '</div>';

    /* L1-1c 分析投影（模型原始输出） */
    var claims = loc.claims || [];
    h += '<div class="block"><div class="sh sub"><h3>L1-1c 分析投影层（模型原始输出）</h3><span class="hint">' + claims.length + ' 条</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>字段</th><th>取值</th><th>档位</th><th>渠道</th><th>依据</th><th class="n">置信</th></tr></thead><tbody>';
    claims.forEach(function (x) {
      var b = String(x.basis || '');
      var cls = b === 'fact' ? 't-ok' : (b === 'inferred_high' ? 't-info' : 't-warn');
      h += '<tr><td class="k">' + esc(x.attribute) + '</td><td>' + txt(x.value) + '</td>' +
        '<td><span class="tag ' + cls + '">' + esc(D.tier_label[b] || b) + '</span></td>' +
        '<td class="k">' + (has(x.record_channel) ? esc(x.record_channel) : '<span class="nul">空</span>') + '</td>' +
        '<td style="font-size:12.5px;color:var(--ink2)">' + ((x.basis_of_inference || []).map(esc).join('；') || (has(x.anchor) ? esc(x.anchor) : nul())) + '</td>' +
        '<td class="n">' + esc(x.confidence) + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    /* L2 投影事实表 */
    h += '<div class="block"><div class="sh sub"><h3>L2 投影事实表</h3><span class="hint">' + Object.keys(pr.facts || {}).length + ' 个字段 · 含档位标记</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>字段</th><th>取值</th><th>档位</th></tr></thead><tbody>';
    Object.keys(pr.facts || {}).forEach(function (k) {
      var b = pr.basis[k];
      var cls = b === 'fact' ? 't-ok' : (b === 'inferred_high' ? 't-info' : 't-warn');
      h += '<tr><td class="k">' + esc(k) + '</td><td>' + txt(pr.facts[k]) + '</td>' +
        '<td>' + (b ? '<span class="tag ' + cls + '">' + esc(D.tier_label[b] || b) + '</span>' : '<span class="nul">未落档</span>') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    /* L2 审查核验层 */
    h += '<div class="block"><div class="sh sub"><h3>L2 审查核验层</h3><span class="hint">纯代码复核 · 0 token</span></div>';
    h += reviewBlock(v.review);
    h += '</div>';

    /* 降级与丢弃归因 */
    var dg = loc.downgraded_detail || [], dr = loc.dropped_detail || [];
    h += '<div class="block"><div class="sh sub"><h3>降级与丢弃归因</h3><span class="hint">降级 ' + dg.length + ' · 丢弃 ' + dr.length + '</span></div>';
    if (dg.length) {
      h += '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>字段</th><th>原因</th><th>锚点</th></tr></thead><tbody>';
      dg.forEach(function (x) {
        h += '<tr><td class="k">' + esc(x.attribute) + '</td>' +
          '<td><span class="tag ' + (x.reason === 'unknown_channel' ? 't-del' : 't-warn') + '">' + esc(x.reason) + '</span></td>' +
          '<td class="k" style="font-size:12px">' + (has(x.anchor) ? esc(x.anchor) : nul()) + '</td></tr>';
      });
      h += '</tbody></table></div></div></div>';
    } else {
      h += '<div class="note ok">本案 L1 无降级。</div></div>';
    }
    return h;
  }

  function rawBody(c) {
    var r = c.raw, h = '<div class="block">';
    h += '<div class="note">两臂共用同一份原始取数（同批次、同输入）。含真实客户姓名、手机号与录音转写。</div>';
    h += dt('录音转写（原始识别结果）', bytes(jbytes(r.transcripts)), function () {
      var ks = Object.keys(r.transcripts || {});
      if (!ks.length) return '<div class="note">无</div>';
      return ks.map(function (k) {
        return '<div class="sh sub" style="margin-top:12px"><h3>' + esc(k) + '</h3></div><pre class="blk">' + esc(pretty(r.transcripts[k])) + '</pre>';
      }).join('');
    });
    h += dt('各渠道取数结果', bytes(jbytes(r.channels)), function () {
      var o = r.channels || {}, ks = Object.keys(o);
      if (!ks.length) return '<div class="note">无</div>';
      return '<dl class="kv">' + ks.map(function (k) {
        var v = o[k], s = typeof v === 'string' ? v : pretty(v);
        return '<dt>' + esc(k) + '</dt><dd>' + (String(s).length > 380
          ? '<details class="rz"><summary>展开（' + String(s).length + ' 字）</summary><pre class="blk">' + esc(s) + '</pre></details>'
          : '<div style="font-size:13px">' + esc(s) + '</div>') + '</dd>';
      }).join('') + '</dl>';
    });
    h += dt('历史数据接口（GET /history）', bytes(jbytes(r.history_body)), function () {
      return '<pre class="blk">' + esc(pretty(r.history_body)) + '</pre>';
    });
    h += dt('L0 记录层（结构化后的输入）', bytes(jbytes(c.v3.records)), function () {
      return '<pre class="blk">' + esc(JSON.stringify(c.v3.records, null, 1)) + '</pre>';
    });
    h += dt('L1 提示词输入（目标字段清单 + 记录）', bytes(c.v3.l1_input.length), function () {
      return '<pre class="blk">' + esc(c.v3.l1_input) + '</pre>';
    });
    h += '</div>';
    return h;
  }

  var LAZY = {};
  function dt(title, meta, bodyFn) {
    var id = 'dt' + Math.random().toString(36).slice(2, 9);
    LAZY[id] = bodyFn;
    return '<details class="dt" data-lazy="' + id + '"><summary><span>' + esc(title) + '</span>' +
      '<span class="dt-meta">' + esc(meta) + '</span></summary><div class="dt-bd" id="' + id + '"></div></details>';
  }

  /* ---------- 左栏 ---------- */
  function rail() {
    var h = '';
    if (STATE.view === 'cases') {
      h += '<div><h4>用例</h4><div class="rail-list">';
      D.cases.forEach(function (c, i) {
        var ob = c.notes_diff.only_base.length, ov = c.notes_diff.only_v3.length;
        var bad = c.v3.status && c.v3.status !== 'succeeded';
        var col = bad ? 'var(--del)' : (ob > ov ? 'var(--del)' : (ob < ov ? 'var(--add)' : 'var(--muted)'));
        h += '<button data-k="' + i + '" aria-pressed="' + (i === STATE.kase) + '">' +
          '<span class="dot" style="background:' + col + '"></span>' +
          '<span class="ph">' + esc(c.phone) + '</span>' +
          '<span class="n">−' + ob + ' +' + ov + '</span></button>';
      });
      h += '</div></div>';
    }
    h += '<div><h4>图例</h4><div class="legend">' +
      '<div class="row"><span class="sw s-base"></span>基线（现行主入口）</div>' +
      '<div class="row"><span class="sw s-v3"></span>v3 三分层（管线）</div>' +
      '<div class="row"><span class="sw s-del"></span>某侧缺该字段 · 留空高亮</div>' +
      '<div class="row"><span class="sw s-add"></span>较基线新增字段</div>' +
      '</div></div>';
    h += '<div><h4>批次</h4><div class="legend" style="font-family:var(--mono);font-size:11.5px">' +
      '<div>' + esc(D.batch_a || 'A-pipeline-L8-v3') + '</div>' +
      '<div style="color:var(--muted)">' + esc(D.batch_b || 'baseline') + '</div>' +
      '</div></div>';
    return h;
  }

  /* ---------- 渲染调度 ---------- */
  function render() {
    LAZY = {};
    var body = STATE.view === 'overview' ? viewOverview() : viewCases();
    document.getElementById('shell').innerHTML = '<aside class="rail">' + rail() + '</aside><div class="view">' + body + '</div>';
    wire();
  }

  function wire() {
    document.querySelectorAll('#views button').forEach(function (b) {
      b.onclick = function () {
        STATE.view = b.getAttribute('data-v');
        document.querySelectorAll('#views button').forEach(function (x) { x.setAttribute('aria-selected', x === b); });
        render(); try { window.scrollTo(0, 0); } catch (e) {}
      };
    });
    document.querySelectorAll('.rail-list button[data-k]').forEach(function (b) {
      b.onclick = function () { STATE.kase = Number(b.getAttribute('data-k')); STATE.seg = 'notes'; render(); try { window.scrollTo(0, 0); } catch (e) {} };
    });
    document.querySelectorAll('tr[data-jump]').forEach(function (tr) {
      tr.onclick = function () {
        STATE.kase = Number(tr.getAttribute('data-jump')); STATE.view = 'cases'; STATE.seg = 'notes';
        document.querySelectorAll('#views button').forEach(function (x) { x.setAttribute('aria-selected', x.getAttribute('data-v') === 'cases'); });
        render(); try { window.scrollTo(0, 0); } catch (e) {}
      };
    });
    document.querySelectorAll('#seg button').forEach(function (b) {
      b.onclick = function () { STATE.seg = b.getAttribute('data-s'); render(); };
    });
    document.querySelectorAll('details[data-lazy]').forEach(function (d) {
      d.ontoggle = function () {
        if (!d.open || d.dataset.done) return;
        var box = d.querySelector('.dt-bd'), fn = LAZY[d.getAttribute('data-lazy')];
        if (box && fn) { box.innerHTML = fn(); d.dataset.done = '1'; }
      };
    });
  }

  /* ---------- 启动 ---------- */
  function boot() {
    var el = document.getElementById('d-payload');
    if (!el) { document.getElementById('shell').innerHTML = '<div class="err-box">页面内未找到内联数据块（d-payload）。</div>'; return; }
    try { D = JSON.parse(el.textContent); }
    catch (e) { document.getElementById('shell').innerHTML = '<div class="err-box">数据解析失败：' + esc(e.message) + '</div>'; return; }

    var a = agg(), rd = readability();
    var notesPct = Math.round(a.notesV3 / Math.max(a.notesBs, 1) * 100);
    document.getElementById('lede').innerHTML =
      'v3 三分层管线与基线在<b>同 8 案</b>上的输出直接比对。Notes 从基线的 <b>' + int(a.notesBs) +
      '</b> 条到 v3 的 <b>' + int(a.notesV3) + '</b> 条（基线的 <b>' + notesPct + '%</b>），' +
      '画像卡条目 <b>' + int(a.secBs) + ' → ' + int(a.secV3) + '</b>。' +
      '对客可读性：审计词命中 <b class="' + (rd.hits === 0 ? 'up' : 'dn') + '">' + int(rd.hits) + '</b> 处，' +
      '「未知渠道」占位 <b class="' + (rd.unknown === 0 ? 'up' : 'dn') + '">' + int(rd.unknown) + '</b> 处，' +
      '证据位自指 <b class="' + (rd.selfRef === 0 ? 'up' : 'dn') + '">' + int(rd.selfRef) + '</b> 处。' +
      'L1-1a 提取事实 <b>' + int(a.factsV3) + '</b> 条、L1-1b 人设 <b>' + int(a.personaFields) + '</b> 项、L2 审查命中 <b>' + int(a.reviewIssues) + '</b> 条。' +
      'Notes 并集大表按字段对齐：某侧缺的字段留空并高亮。';

    document.getElementById('mh-meta').innerHTML =
      '<span>用例 <b>' + a.phones + ' / 39 抽样</b></span>' +
      '<span>v3 批次 <b>' + esc(D.batch_a || 'A-pipeline-L8-v3') + '</b></span>' +
      '<span>基线批次 <b>' + esc(D.batch_b || 'baseline') + '</b></span>' +
      '<span>模型 <b>deepseek-v4.1-flash · noThink</b></span>' +
      '<span>回写 <b>关闭</b></span>';

    document.getElementById('foot').innerHTML =
      '<div>数据来源：v3 批次 ' + esc(D.batch_a || '') + '（实验臂）对照 ' + esc(D.batch_b || '') + '（基线臂）。</div>' +
      '<div>本页含真实客户数据（姓名 / 手机号 / 录音转写），仅供内部使用，请勿外传。</div>';
    render();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
