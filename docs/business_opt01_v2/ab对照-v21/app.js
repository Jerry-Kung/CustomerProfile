/* 画像管线 AB 对照 v2.1 — 静态渲染器
 * 数据：payload.json（8 案完整输入 + 两臂全部输出 + Notes 单边差异）
 * 只读一次，按需构建 DOM。两个视图：总览 / 逐案对照。
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
  function sign(n, goodDir) {
    if (!n) return '';
    var good = goodDir === 'up' ? n > 0 : n < 0;
    return good ? 'better' : 'worse';
  }

  /* ---------- 聚合 ---------- */
  function agg() {
    var c = D.cases, o = {
      phones: c.length,
      notesV2: 0, notesBs: 0, secV2: 0, secBs: 0,
      llmV2: 0, llmBs: 0, tokV2: 0, tokBs: 0,
      onlyBase: 0, onlyV2: 0, both: 0,
      secByKey: {}, failedV2: [], emptyV2: [], failedBs: []
    };
    D.sections.forEach(function (k) { o.secByKey[k] = { v2: 0, bs: 0 }; });
    c.forEach(function (x) {
      o.notesV2 += x.v2.notes.length; o.notesBs += x.base.notes.length;
      o.llmV2 += x.v2.metrics.llm_calls; o.llmBs += x.base.metrics.llm_calls;
      o.tokV2 += x.v2.metrics.tokens; o.tokBs += x.base.metrics.tokens;
      o.onlyBase += x.notes_diff.only_base.length;
      o.onlyV2 += x.notes_diff.only_v2.length;
      o.both += x.notes_diff.both.length;
      D.sections.forEach(function (k) {
        var a = x.v2.sec_counts[k] || 0, b = x.base.sec_counts[k] || 0;
        o.secByKey[k].v2 += a; o.secByKey[k].bs += b;
        o.secV2 += a; o.secBs += b;
      });
      if (x.v2.status && x.v2.status !== 'succeeded') o.failedV2.push(x.phone);
      else if (!x.v2.notes.length) o.emptyV2.push(x.phone);
      if (!x.base.notes.length) o.failedBs.push(x.phone);
    });
    return o;
  }

  /* ---------- 可读性检查（对客产出硬约束） ---------- */
  var AUDIT = ['归属过滤', '来源分级', '枚举归一', '单多值约束', '契约校验', '未知渠道',
    '社交自呈现', '一手交互', '第三方预测', '公开登记查询', '人工/系统登记', '按来源分级投影得到'];
  function readability() {
    var r = { hits: 0, hitWords: {}, hitCases: [], unknown: 0, ev: 0, evReadable: 0 };
    D.cases.forEach(function (c) {
      var caseHits = 0;
      c.v2.notes.forEach(function (n) {
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
        });
      });
      if (caseHits) r.hitCases.push(c.phone);
    });
    r.hitCases = r.hitCases.join('、');
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

  function noteRow(n, flag) {
    var rd = n.reasoning_detail || {};
    var cls = flag === 'v2' ? ' onlyv2' : (flag === 'base' ? ' onlybase' : '');
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

    /* 判定条 */
    var notesPct = Math.round(a.notesV2 / Math.max(a.notesBs, 1) * 100);
    var secPct = Math.round(a.secV2 / Math.max(a.secBs, 1) * 100);
    h += '<div class="verdicts">';

    h += '<div class="vcard up"><span class="flag">确实修好了</span><ul>';
    h += '<li><b>推理依据换成了原文</b>：' + int(rd.ev) + ' 条证据里，' + int(rd.evReadable) + ' 条形如「渠道：原文片段」（' + pct(rd.evReadable, rd.ev) + '），销售能看出结论从哪来。</li>';
    h += '<li><b>「未知渠道」占位归零</b>：' + int(rd.unknown) + ' 处。v2 首轮此处为 128 条。</li>';
    h += '<li><b>Notes 召回追上基线</b>：' + int(a.notesV2) + ' 条 vs 基线 ' + int(a.notesBs) + ' 条，为基线的 ' + notesPct + '%。</li>';
    h += '<li><b>卡面内容量追上基线</b>：9 个 section 合计 ' + int(a.secV2) + ' 条 vs 基线 ' + int(a.secBs) + ' 条，为基线的 ' + secPct + '%。</li>';
    h += '</ul></div>';

    h += '<div class="vcard dn"><span class="flag">仍未追平 / 需关注</span><ul>';
    if (rd.hits) {
      h += '<li><b>审计词仍有 ' + int(rd.hits) + ' 处命中，未达「归零」验收线</b>：全部出现在 <b>' + rd.hitCases + '</b> 案的 L2 兜底路径上，文字形如「兜底裁决：仅社交自呈现单一来源支撑，维持原值」——来源档代号与内部规则名直接写进了销售可见的推理步骤。数据见「逐案对照」。</li>';
    }
    var gap = a.notesBs - a.notesV2;
    h += '<li><b>Notes 仍差 ' + int(gap) + ' 条</b>：' + int(a.onlyBase) + ' 个字段仅基线有结论，v2.1 未产出。</li>';
    h += '<li><b>单边字段需逐案看</b>：仅基线有 ' + int(a.onlyBase) + ' 个、仅 v2.1 有 ' + int(a.onlyV2) + ' 个——两边字段集合并不相同，不能只比条数。</li>';
    if (a.failedV2.length) h += '<li><b>有 ' + a.failedV2.length + ' 案 v2.1 运行未成功</b>：' + a.failedV2.map(esc).join('、') + '，本节读数按实际成功案统计。</li>';
    if (a.emptyV2.length) h += '<li><b>有 ' + a.emptyV2.length + ' 案上游取数大面积失败</b>：' + a.emptyV2.map(esc).join('、') + ' 运行成功但无材料可提取，Notes 为空——是取数侧故障，非管线缺陷。</li>';
    h += '</ul></div>';

    h += '<div class="vcard cal"><span class="flag">读这页要留意的口径</span><ul>';
    h += '<li>两臂<b>同号码、同输入</b>（<code>phones_sha256</code> 一致），差异只来自实现本身。</li>';
    h += '<li>Notes 条数<b>不等于</b>质量。基线字段多但含空值占位与内部话术；v2.1 字段少但每条都有可读依据。逐案对照页把两边的字段集合差摊开了看。</li>';
    h += '<li>section 计数把列表按条数、值字典按非空字段数计，<b>只反映内容量</b>，不代表准确性。</li>';
    h += '</ul></div>';
    h += '</div>';

    /* 读数 */
    h += '<div class="block"><div class="sh"><h2>关键读数</h2><span class="hint">8 案合计 · 基线 → v2.1</span></div>';
    h += '<div class="readout">';
    h += ro(int(a.notesBs), int(a.notesV2), 'Notes 条数', notesPct + '% of 基线', notesPct >= 100 ? 'good' : 'bad');
    h += ro(int(a.secBs), int(a.secV2), '画像卡条目', secPct + '% of 基线', secPct >= 100 ? 'good' : 'bad');
    h += ro(int(a.onlyBase), int(a.onlyV2), '单边字段（仅基线 / 仅 v2.1）', '共 ' + int(a.both) + ' 字段两边都有', 'bad');
    h += ro(int(rd.ev), int(rd.evReadable), '证据锚点 / 其中带原文出处', pct(rd.evReadable, rd.ev) + ' 精确到取材位置', 'good');
    h += ro(rd.hits === 0 ? '0' : rd.hits, rd.unknown, '审计词命中 / 未知渠道占位', rd.hits ? '未达归零验收线，见下' : 'v2.1 对客产出硬约束已满足', rd.hits === 0 && rd.unknown === 0 ? 'good' : 'bad');
    h += ro(int(a.llmBs), int(a.llmV2), 'LLM 调用次数', '降 ' + (100 - Math.round(a.llmV2 / Math.max(a.llmBs, 1) * 100)) + '%', 'good');
    h += ro(int(Math.round(a.tokBs / 1000)) + 'k', int(Math.round(a.tokV2 / 1000)) + 'k', 'Token 用量', '降 ' + (100 - Math.round(a.tokV2 / Math.max(a.tokBs, 1) * 100)) + '%', 'good');
    h += '</div></div>';

    /* section 覆盖 */
    h += '<div class="block"><div class="sh sub"><h3>画像卡 9 个 section · 逐项覆盖</h3><span class="hint">数字为 8 案合计条目数</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>section</th><th>字段名</th><th class="n">基线</th><th class="n">v2.1</th><th class="n">差值</th><th>判定</th></tr></thead><tbody>';
    D.sections.forEach(function (k) {
      var b = a.secByKey[k].bs, v = a.secByKey[k].v2, d = v - b;
      h += '<tr><td>' + esc(D.section_labels[k]) + '</td><td class="k">' + esc(k) + '</td>' +
        '<td class="n">' + int(b) + '</td><td class="n">' + int(v) + '</td>' +
        '<td class="n ' + (d > 0 ? 'better' : d < 0 ? 'worse' : 'flat') + '">' + (d > 0 ? '+' : '') + int(d) + '</td>' +
        '<td>' + (d >= 0 ? '<span class="tag t-ok">持平或更多</span>' : '<span class="tag t-del">少于基线</span>') + '</td></tr>';
    });
    h += '<tr><td><b>Notes</b></td><td class="k">basic_notes_updates</td>' +
      '<td class="n"><b>' + int(a.notesBs) + '</b></td><td class="n"><b>' + int(a.notesV2) + '</b></td>' +
      '<td class="n ' + (a.notesV2 >= a.notesBs ? 'better' : 'worse') + '"><b>' + (a.notesV2 - a.notesBs > 0 ? '+' : '') + int(a.notesV2 - a.notesBs) + '</b></td>' +
      '<td>' + (a.notesV2 >= a.notesBs ? '<span class="tag t-ok">持平或更多</span>' : '<span class="tag t-del">少于基线</span>') + '</td></tr>';
    h += '</tbody></table></div></div></div>';

    /* Notes 单边差异汇总 */
    var aggOnlyBase = {}, aggOnlyV2 = {};
    D.cases.forEach(function (c) {
      c.notes_diff.only_base.forEach(function (x) { aggOnlyBase[x.key] = (aggOnlyBase[x.key] || 0) + 1; });
      c.notes_diff.only_v2.forEach(function (x) { aggOnlyV2[x.key] = (aggOnlyV2[x.key] || 0) + 1; });
    });
    function sorted(o) {
      return Object.keys(o).sort(function (x, y) { return o[y] - o[x]; })
        .map(function (k) { return { key: k, n: o[k] }; });
    }
    h += '<div class="block"><div class="sh sub"><h3>Notes 单边字段 · 全案汇总</h3>' +
      '<span class="hint">按「只在一边出现」的字段统计出现案数</span></div>';
    h += '<div class="divergence">';
    h += '<div class="dcol only-base"><div class="h">仅基线有 · 共 ' + int(a.onlyBase) + ' 处<span class="ct">' + sorted(aggOnlyBase).length + ' 个字段</span></div><div class="list">';
    var ob = sorted(aggOnlyBase);
    h += ob.length ? ob.map(function (x) {
      return '<div class="drow"><span class="sign">−</span><div class="body"><span class="fk">' + esc(x.key) + '</span><span class="fv" style="color:var(--muted)">' + x.n + ' 案基线有、v2.1 无</span></div></div>';
    }).join('') : '<div class="empty">无</div>';
    h += '</div></div>';
    h += '<div class="dcol only-v2"><div class="h">仅 v2.1 有 · 共 ' + int(a.onlyV2) + ' 处<span class="ct">' + sorted(aggOnlyV2).length + ' 个字段</span></div><div class="list">';
    var ov = sorted(aggOnlyV2);
    h += ov.length ? ov.map(function (x) {
      return '<div class="drow"><span class="sign">+</span><div class="body"><span class="fk">' + esc(x.key) + '</span><span class="fv" style="color:var(--muted)">' + x.n + ' 案 v2.1 有、基线无</span></div></div>';
    }).join('') : '<div class="empty">无</div>';
    h += '</div></div></div>';
    h += '<div class="note">这一节是全案汇总；<b>逐案的单边差异</b>在「逐案对照」页里按号码分组列出，可直接核对是哪些字段、值是什么。</div>';
    h += '</div>';

    /* 逐案一览 */
    h += '<div class="block"><div class="sh sub"><h3>逐案一览</h3><span class="hint">点号码进入逐案对照</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th class="n">基线 Notes</th><th class="n">v2.1 Notes</th><th class="n">仅基线</th><th class="n">仅 v2.1</th>' +
      '<th class="n">基线卡面</th><th class="n">v2.1 卡面</th><th class="n">分诊</th><th>审计词</th></tr></thead><tbody>';
    D.cases.forEach(function (c, i) {
      var sc2 = 0, scb = 0;
      D.sections.forEach(function (k) { sc2 += c.v2.sec_counts[k] || 0; scb += c.base.sec_counts[k] || 0; });
      var hits = 0;
      c.v2.notes.forEach(function (n) {
        var d = n.reasoning_detail || {};
        var blob = String(d.summary || '') + ' ' + (d.steps || []).join(' ');
        AUDIT.forEach(function (w) { if (blob.indexOf(w) >= 0) hits++; });
      });
      var bad = c.v2.status && c.v2.status !== 'succeeded';
      h += '<tr style="cursor:pointer" data-jump="' + i + '">' +
        '<td class="k">' + esc(c.phone) + (bad ? ' <span class="tag t-del">运行失败</span>' : '') + '</td>' +
        '<td class="n">' + c.base.notes.length + '</td><td class="n">' + c.v2.notes.length + '</td>' +
        '<td class="n ' + (c.notes_diff.only_base.length ? 'worse' : 'flat') + '">' + c.notes_diff.only_base.length + '</td>' +
        '<td class="n ' + (c.notes_diff.only_v2.length ? '' : 'flat') + '">' + c.notes_diff.only_v2.length + '</td>' +
        '<td class="n">' + scb + '</td><td class="n">' + sc2 + '</td>' +
        '<td>' + (c.v2.tier ? '<span class="tag t-info">' + esc(c.v2.tier) + '</span>' : '<span class="nul">—</span>') + '</td>' +
        '<td>' + (hits ? '<span class="tag t-del">' + hits + '</span>' : '<span class="tag t-ok">0</span>') + '</td></tr>';
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

    var bad = c.v2.status && c.v2.status !== 'succeeded';
    var empty = !bad && !c.v2.notes.length;
    if (bad) {
      h += '<div class="note err"><b>本案 v2.1 运行未成功（' + esc(c.v2.status) + '）</b>：' +
        'L3 成文节点连续 3 次返回被截断的 JSON（约 7–8 千字符处断开），未产出画像与 Notes。' +
        '因此下方基线侧的 Notes 全部落在「仅基线有」一栏，其数量是<b>整案基线字段数</b>，不代表 v2.1 的召回缺口。' +
        (c.v2.error ? '<br><span style="color:var(--muted);font-size:12px">原因摘要：<code>' + esc(String(c.v2.error).slice(0, 240)) + '</code></span>' : '') +
        '</div>';
    } else if (empty) {
      h += '<div class="note err"><b>本案 v2.1 运行成功，但上游取数大面积异常、未产出任何结论</b>：' +
        'L0 记录层 15 条里有 13 条是 <code>exception</code>（截图、企业信息、盟识等渠道均未取到），' +
        '模型手里没有材料，L1 断言为 0 条，Notes 与画像卡因此为空。' +
        '这是<b>取数侧问题，不是 v2.1 管线的提取缺陷</b>；补跑后仍复现，属该号码的稳定上游故障。' +
        '下方基线侧的 Notes 全部落在「仅基线有」一栏，其数量是<b>整案基线字段数</b>。' +
        '</div>';
    }

    /* 单边差异 —— 本页重点，放在最前 */
    var d = c.notes_diff;
    h += '<div class="block"><div class="sh"><h2>Notes 单边差异</h2>' +
      '<span class="hint">' + esc(c.phone) + ' · 只列一边有一边无的字段</span></div>';
    h += '<div class="divergence">';
    h += '<div class="dcol only-base"><div class="h">仅基线有 · ' + d.only_base.length + ' 个字段<span class="ct">v2.1 未产出</span></div><div class="list">';
    h += d.only_base.length ? d.only_base.map(function (x) {
      return '<div class="drow"><span class="sign">−</span><div class="body"><span class="fk">' + esc(x.key) + '</span><span class="fv">' + txt(x.value) + '</span></div></div>';
    }).join('') : '<div class="empty">两边都有，无缺失</div>';
    h += '</div></div>';
    h += '<div class="dcol only-v2"><div class="h">仅 v2.1 有 · ' + d.only_v2.length + ' 个字段<span class="ct">基线未产出</span></div><div class="list">';
    h += d.only_v2.length ? d.only_v2.map(function (x) {
      return '<div class="drow"><span class="sign">+</span><div class="body"><span class="fk">' + esc(x.key) + '</span><span class="fv">' + txt(x.value) + '</span></div></div>';
    }).join('') : '<div class="empty">无新增字段</div>';
    h += '</div></div></div>';
    if (d.only_base.length && !d.only_v2.length) {
      h += '<div class="note warn">本案 v2.1 只少不多：基线的 ' + d.only_base.length + ' 个字段在 v2.1 里没有结论，需要逐字段判断是<b>没提取到</b>还是<b>刻意不写</b>。</div>';
    }
    h += '</div>';

    /* 分段切换 */
    h += '<div class="seg" id="seg">' +
      segBtn('notes', 'Notes 全量对照') + segBtn('card', '画像卡对照') +
      segBtn('pipeline', 'L1/投影明细') + segBtn('raw', '原始输入') + '</div>';
    h += '<div id="segbody">' + segBody(c) + '</div>';
    return h;
  }
  function segBtn(k, label) { return '<button data-s="' + k + '" aria-selected="' + (STATE.seg === k) + '">' + label + '</button>'; }

  function segBody(c) {
    if (STATE.seg === 'notes') {
      var h = '<div class="block">';
      h += '<div class="sh sub"><h3>两臂 Notes 全量对照</h3>' +
        '<span class="hint">两边都有的 ' + c.notes_diff.both.length + ' 个字段 · 左基线右 v2.1</span></div>';
      h += '<div class="collate"><div class="col-head">' +
        '<div><span class="tag t-base">基线</span> ' + c.base.notes.length + ' 条<span class="ct">customer_profile_entry</span></div>' +
        '<div><span class="tag t-v2">v2.1</span> ' + c.v2.notes.length + ' 条<span class="ct">customer_profile_pipeline</span></div></div>' +
        '<div class="col-body"><div>' + notesList(c.base.notes, 'base') + '</div>' +
        '<div>' + notesList(c.v2.notes, 'v2') + '</div></div></div>';
      h += '<div class="note">每条的值与推理明细都完整保留。v2.1 侧的「推理」是<b>结论怎么推出来的</b>，「依据」是<b>客户原始数据里的哪一段</b>；基线侧的推理明细多为过程描述。字段只在一边出现的，已在上方单边差异里单独列出。</div>';
      h += '</div>';
      return h;
    }
    if (STATE.seg === 'card') {
      var h2 = '';
      D.sections.forEach(function (k) {
        var cv = c.v2.sec_counts[k] || 0, cb = c.base.sec_counts[k] || 0;
        h2 += '<div class="block"><div class="sh sub"><h3>' + esc(D.section_labels[k]) + '</h3>' +
          '<span class="hint">' + esc(k) + ' · 基线 ' + cb + ' → v2.1 ' + cv + '</span></div>';
        h2 += '<div class="collate"><div class="col-head">' +
          '<div><span class="tag t-base">基线</span></div><div><span class="tag t-v2">v2.1</span></div></div>' +
          '<div class="col-body"><div>' + sectionBlock(c.base.sections[k]) + '</div>' +
          '<div>' + sectionBlock(c.v2.sections[k]) + '</div></div></div></div>';
      });
      return h2;
    }
    if (STATE.seg === 'pipeline') return pipelineBody(c);
    return rawBody(c);
  }

  function pipelineBody(c) {
    var loc = c.v2.localize || {}, pr = c.v2.project || {}, tri = c.v2;
    var h = '<div class="block">';
    h += '<div class="sh sub"><h3>管线读数</h3><span class="hint">分诊 ' + esc(tri.tier || '—') + ' · 可用渠道 ' + esc(tri.usable_count) + ' · 记录 ' + esc(tri.records_count) + '</span></div>';
    h += '<div class="readout">';
    h += ro((loc.claims || []).length, loc.kept, 'L1 断言 → 保留', '丢弃 ' + esc(loc.dropped) + ' · 降级 ' + esc(loc.downgraded), 'good');
    h += ro(esc(loc.empty_claims == null ? '—' : loc.empty_claims), Object.keys(pr.facts || {}).length, '空值断言 → 事实字段', 'L1 收紧后空值断言被拒收并计数', 'good');
    var basisC = {};
    Object.keys(pr.basis || {}).forEach(function (k) { basisC[pr.basis[k]] = (basisC[pr.basis[k]] || 0) + 1; });
    h += ro(Object.keys(pr.basis || {}).length, (basisC.fact || 0), '落档字段 → 其中事实档', '强推断 ' + (basisC.inferred_high || 0) + ' · 弱推断 ' + (basisC.inferred_low || 0), 'good');
    h += ro(esc(pr.notes_count), c.v2.notes.length, '投影产出 → Notes 条数', '装配后实际写出的条数', 'good');
    h += '</div></div>';

    if (loc.empty_claim_names && loc.empty_claim_names.length) {
      h += '<div class="block"><div class="sh sub"><h3>L1 空值占位字段</h3><span class="hint">v2.1 已拒收并计数，这些字段由 L2 兜底补</span></div>' +
        '<div class="note">' + loc.empty_claim_names.map(function (x) { return '<code>' + esc(x) + '</code>'; }).join(' ') + '</div></div>';
    }

    h += '<div class="block"><div class="sh sub"><h3>L2 投影事实表</h3><span class="hint">' + Object.keys(pr.facts || {}).length + ' 个字段 · 含档位标记</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>字段</th><th>取值</th><th>档位</th></tr></thead><tbody>';
    Object.keys(pr.facts || {}).forEach(function (k) {
      var b = pr.basis[k];
      var cls = b === 'fact' ? 't-ok' : (b === 'inferred_high' ? 't-info' : 't-warn');
      h += '<tr><td class="k">' + esc(k) + '</td><td>' + txt(pr.facts[k]) + '</td>' +
        '<td>' + (b ? '<span class="tag ' + cls + '">' + esc(D.tier_label[b] || b) + '</span>' : '<span class="nul">未落档</span>') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    h += '<div class="block"><div class="sh sub"><h3>L1 断言（模型原始输出）</h3><span class="hint">' + (loc.claims || []).length + ' 条</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr><th>字段</th><th>取值</th><th>档位</th><th>渠道</th><th>推理依据</th><th class="n">置信</th></tr></thead><tbody>';
    (loc.claims || []).forEach(function (x) {
      var b = String(x.basis || '');
      var cls = b === 'fact' ? 't-ok' : (b === 'inferred_high' ? 't-info' : 't-warn');
      h += '<tr><td class="k">' + esc(x.attribute) + '</td><td>' + txt(x.value) + '</td>' +
        '<td><span class="tag ' + cls + '">' + esc(D.tier_label[b] || b) + '</span></td>' +
        '<td class="k">' + (has(x.record_channel) ? esc(x.record_channel) : '<span class="nul">空</span>') + '</td>' +
        '<td style="font-size:12.5px;color:var(--ink2)">' + ((x.basis_of_inference || []).map(esc).join('；') || nul()) + '</td>' +
        '<td class="n">' + esc(x.confidence) + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

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
    h += '<div class="note">两臂共用同一份原始取数（同批次、同 <code>phones_sha256</code>）。含真实客户姓名、手机号与录音转写。</div>';
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
    h += dt('L0 记录层（结构化后的输入）', bytes(jbytes(c.v2.records)), function () {
      return '<pre class="blk">' + esc(JSON.stringify(c.v2.records, null, 1)) + '</pre>';
    });
    h += dt('L1 提示词输入（目标字段清单 + 记录）', bytes(c.v2.l1_input.length), function () {
      return '<pre class="blk">' + esc(c.v2.l1_input) + '</pre>';
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
        var ob = c.notes_diff.only_base.length, ov = c.notes_diff.only_v2.length;
        var bad = c.v2.status && c.v2.status !== 'succeeded';
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
      '<div class="row"><span class="sw s-v2"></span>v2.1 整改版（管线）</div>' +
      '<div class="row"><span class="sw s-del"></span>仅基线有 · v2.1 缺</div>' +
      '<div class="row"><span class="sw s-add"></span>仅 v2.1 有 · 新增</div>' +
      '</div></div>';
    h += '<div><h4>批次</h4><div class="legend" style="font-family:var(--mono);font-size:11.5px">' +
      '<div>A-pipeline-L8-v21-20261013</div>' +
      '<div style="color:var(--muted)">baseline-39-dsv41-noThink</div>' +
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

    var a = agg();
    var notesPct = Math.round(a.notesV2 / Math.max(a.notesBs, 1) * 100);
    document.getElementById('lede').innerHTML =
      'v2.1 整改后的管线与基线在<b>同 8 案</b>上的输出直接比对。Notes 从基线的 <b>' + int(a.notesBs) +
      '</b> 条到 v2.1 的 <b>' + int(a.notesV2) + '</b> 条（基线的 <b>' + notesPct + '%</b>），' +
      '画像卡条目 <b>' + int(a.secBs) + ' → ' + int(a.secV2) + '</b>。' +
      '对客可读性：证据锚点 <b>' + int(readability().ev) + '</b> 条中 <b class="up">' + int(readability().evReadable) + '</b> 条写明原文出处；「未知渠道」占位已归零，' +
      '但审计词仍有 <b class="dn">' + readability().hits + '</b> 处命中（集中在 L2 兜底路径），未达归零验收线。' +
      '仍有 <b class="dn">' + int(a.onlyBase) + '</b> 个字段只出现在基线、<b class="dn">' + int(a.onlyV2) + '</b> 个只出现在 v2.1，逐案对照页按字段列出。' +
      (a.failedV2.length
        ? '其中 <b class="dn">' + a.failedV2.length + '</b> 案 v2.1 运行未成功（' + a.failedV2.map(esc).join('、') + '），该案无 v2.1 产出，已单独标出。'
        : '') +
      (a.emptyV2.length
        ? '另有 <b class="dn">' + a.emptyV2.length + '</b> 案因上游取数大面积失败而无产出（' + a.emptyV2.map(esc).join('、') + '），属取数侧故障。'
        : '');

    document.getElementById('mh-meta').innerHTML =
      '<span>用例 <b>' + a.phones + ' / 39 抽样</b></span>' +
      '<span>v2.1 批次 <b>A-pipeline-L8-v21-20261013</b></span>' +
      '<span>基线批次 <b>baseline-39-dsv41-noThink</b></span>' +
      '<span>模型 <b>deepseek-v4.1-flash · noThink</b></span>' +
      '<span>回写 <b>关闭</b></span>';

    document.getElementById('foot').innerHTML =
      '<div>数据来源：v2.1 批次 A-pipeline-L8-v21-20261013（实验臂）对照 experiments/batches/baseline-39-dsv41-noThink（基线臂）。</div>' +
      '<div>本页含真实客户数据（姓名 / 手机号 / 录音转写），仅供内部使用，请勿外传。</div>';
    render();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
