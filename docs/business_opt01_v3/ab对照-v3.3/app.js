/* 画像管线 AB 对照 v3.3 — 静态渲染器
 * 数据：payload.json（39 案两臂的 analysis_result 十段落 + Notes 并集 + trace 读数）
 * 只读一次，按需构建 DOM。两个视图：总览 / 逐案对照。
 *
 * 呈现口径与 ab对照-v3.2 一致，两处按 v3.3 的实际情况调整：
 * 1. 段落清单是 analysis_result 的**十个**键（含 basic_notes_updates），不是九个——
 *    v3.2 把 Notes 单列，这里仍单列，因此画像卡只列前九个；
 * 2. 去掉了 v3.2 页面上「第三方画像标签准入 / 品牌归属路由 / 9 节完整性」三块——那些是
 *    v3.2 的验收读数，本批要看的对象是**两条工作流**（管线臂 vs 基线臂）在同输入下的输出，
 *    以及 v3.3 可靠性改进（重试 / 异常留痕）留下的机器痕迹。
 */
(function () {
  'use strict';

  var STATE = { view: 'overview', kase: 0, seg: 'card' };
  var D = null;
  /* 画像卡九节（Notes 单列，故从十段落里剔除 basic_notes_updates）。 */
  var CARD = [];

  /* ---------- 基础工具 ---------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function int(n) { return (Number(n) || 0).toLocaleString('en-US'); }
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
  function dur(ms) {
    var n = Number(ms) || 0;
    if (n < 1000) return n + ' ms';
    return (n / 1000).toFixed(1) + ' s';
  }
  function ktok(n) {
    var v = Number(n) || 0;
    if (v < 1000) return String(v);
    return (v / 1000).toFixed(1) + 'k';
  }

  /* ---------- 聚合 ---------- */
  function agg() {
    var c = D.cases, o = {
      phones: c.length, okNew: 0, okBase: 0,
      notesNew: 0, notesBase: 0, secNew: 0, secBase: 0,
      llmNew: 0, llmBase: 0, tokNew: 0, tokBase: 0,
      durNew: 0, durBase: 0,
      onlyBase: 0, onlyNew: 0, both: 0,
      retried: [], anomaly: 0, failedNew: [], failedBase: [], sparseNew: [],
      secByKey: {}, durNewList: [], durBaseList: []
    };
    D.sections.forEach(function (k) { o.secByKey[k] = { base: 0, 'new': 0 }; });
    c.forEach(function (x) {
      var b = x.base, n = x['new'];
      if (n) {
        if (n.status === 'succeeded') o.okNew++; else o.failedNew.push(x.phone);
      }
      if (b) {
        if (b.status === 'succeeded') o.okBase++; else o.failedBase.push(x.phone);
      }
      if (n) {
        o.notesNew += n.notes.length;
        o.llmNew += n.llm || 0; o.tokNew += n.tok || 0; o.durNew += n.duration_ms || 0;
        o.durNewList.push(n.duration_ms || 0);
        if ((n.max_attempt || 1) > 1) o.retried.push(x.phone);
        o.anomaly += Number(n.anomaly || 0);
        var blank = 0;
        CARD.forEach(function (k) { if (!(n.sec_counts[k] > 0)) blank++; });
        if (blank) o.sparseNew.push({ phone: x.phone, blank: blank });
      }
      if (b) {
        o.notesBase += b.notes.length;
        o.llmBase += b.llm || 0; o.tokBase += b.tok || 0; o.durBase += b.duration_ms || 0;
        o.durBaseList.push(b.duration_ms || 0);
      }
      var diff = notesDiff(x);
      o.onlyBase += diff.only_base.length;
      o.onlyNew += diff.only_new.length;
      o.both += diff.both.length;
      D.sections.forEach(function (k) {
        o.secByKey[k]['base'] += (b && b.sec_counts[k]) || 0;
        o.secByKey[k]['new'] += (n && n.sec_counts[k]) || 0;
        if (k !== 'basic_notes_updates') {
          o.secBase += (b && b.sec_counts[k]) || 0;
          o.secNew += (n && n.sec_counts[k]) || 0;
        }
      });
    });
    o.durNewList.sort(function (a, b) { return a - b; });
    o.durBaseList.sort(function (a, b) { return a - b; });
    o.medNew = o.durNewList[Math.floor(o.durNewList.length / 2)] || 0;
    o.medBase = o.durBaseList[Math.floor(o.durBaseList.length / 2)] || 0;
    return o;
  }

  function byKey(notes) {
    var m = {};
    (notes || []).forEach(function (n) {
      if (n && n.attribute_key) m[String(n.attribute_key)] = n;
    });
    return m;
  }

  /* 两臂 Notes 按 attribute_key 对齐，一侧缺该字段即计入 only_*。 */
  function notesDiff(c) {
    var bm = byKey(c.base && c.base.notes), nm = byKey(c['new'] && c['new'].notes);
    var onlyBase = [], onlyNew = [], both = [];
    Object.keys(bm).forEach(function (k) {
      if (nm[k]) both.push(k); else onlyBase.push(k);
    });
    Object.keys(nm).forEach(function (k) { if (!bm[k]) onlyNew.push(k); });
    return { only_base: onlyBase, only_new: onlyNew, both: both, bm: bm, nm: nm };
  }

  /* ---------- 推理明细 ---------- */
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
    if (!n) return '<div class="nu-c miss"><div class="miss-in">⊘ 本臂无此字段</div></div>';
    var rd = n.reasoning_detail || {};
    var conf = rd.confidence != null ? '<span class="conf">置信 ' + esc(rd.confidence) + '</span>' : '';
    return '<div class="nu-c"><div class="nc-v">' + txt(n.attribute_value) + conf + '</div>' + reasoning(rd, false) + '</div>';
  }

  /* 两臂 Notes 并集：按 attribute_key 对齐，缺的一侧留空并高亮。 */
  function notesUnion(c) {
    var d = notesDiff(c);
    var keys = Object.keys(d.bm);
    Object.keys(d.nm).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    keys.sort();
    var baseMissing = 0, newMissing = 0;
    keys.forEach(function (k) { if (!d.bm[k]) baseMissing++; if (!d.nm[k]) newMissing++; });

    var h = '<div class="nu-legend">' +
      '<span class="k"><span class="sw2" style="background:var(--delbg);border-color:var(--delline)"></span>该侧无此字段（留空高亮）</span>' +
      '<span class="k">共 ' + int(keys.length) + ' 个字段 · 基线缺 ' + int(baseMissing) + ' 个 · v3.3 缺 ' + int(newMissing) + ' 个</span>' +
      '</div>';
    h += '<div class="nunion"><div class="nu-head">' +
      '<div class="nu-k">字段</div>' +
      '<div class="nu-c base">基线<span class="ct">' + int((c.base && c.base.notes.length) || 0) + ' 条</span></div>' +
      '<div class="nu-c v3">v3.3<span class="ct">' + int((c['new'] && c['new'].notes.length) || 0) + ' 条</span></div></div>';
    keys.forEach(function (k) {
      var bn = d.bm[k], nn = d.nm[k];
      var cls = 'nu-row' + (!bn ? ' miss-left' : '') + (!nn ? ' miss-right' : '');
      h += '<div class="' + cls + '">' +
        '<div class="nu-k">' + esc(k) + '</div>' +
        noteCell(bn) + noteCell(nn) + '</div>';
    });
    h += '</div>';
    return h;
  }

  /* ---------- 段落渲染 ---------- */
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
    if (!has(val)) return '<div class="note">本臂该段为空。</div>';
    if (Array.isArray(val)) {
      if (!val.length) return '<div class="note">本臂该段为空数组。</div>';
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
        return Object.keys(val).map(function (k) {
          var v = val[k] || {};
          return '<div class="item"><div class="t">' + esc(k) + ' <span style="font-weight:400;color:var(--ink2)">' + txt(v.value) + '</span></div>' +
            reasoning(v.reasoning_detail, false) + '</div>';
        }).join('');
      }
      return '<dl class="kv">' + Object.keys(val).map(function (k) {
        return '<dt>' + esc(k) + '</dt><dd>' + txt(val[k]) + '</dd>';
      }).join('') + '</dl>';
    }
    return '<div>' + txt(val) + '</div>';
  }

  /* ---------- 视图：总览 ---------- */
  function viewOverview() {
    var a = agg();
    var h = '';

    var notesPct = Math.round(a.notesNew / Math.max(a.notesBase, 1) * 100);
    var secPct = Math.round(a.secNew / Math.max(a.secBase, 1) * 100);

    h += '<div class="verdicts">';
    h += '<div class="vcard up"><span class="flag">v3.3 达标的项</span><ul>';
    h += '<li><b>' + int(a.okNew) + '/' + int(a.phones) + ' 案全部跑通</b>：管线臂无一失败。</li>';
    h += '<li><b>异常留痕列已生效</b>：本批 <code>anomaly</code> 命中 <b>' + int(a.anomaly) + '</b> 条；<code>finish_reason</code> 已按次记录。</li>';
    h += '<li><b>自动重试生效 ' + int(a.retried.length) + ' 案</b>：首次尝试失败后由重试层救回，未升级为整案失败。</li>';
    h += '<li><b>画像卡十段齐全</b>：每一案都含 <code>analysis_result</code> 的全部十段，无结构性缺失。</li>';
    h += '<li><b>输出量下限闸门已接线</b>：日志节点可按 <code>min_completion_tokens</code> 声明输出下限（缺省不启用）。</li>';
    h += '</ul></div>';

    h += '<div class="vcard dn"><span class="flag">需关注</span><ul>';
    var gap = a.notesBase - a.notesNew;
    h += '<li><b>Notes 条数 ' + int(a.notesNew) + ' vs 基线 ' + int(a.notesBase) + '</b>（差 ' + (gap >= 0 ? '−' : '+') + int(Math.abs(gap)) + '）：条数不等于质量，逐案看并集大表。</li>';
    h += '<li><b>两侧字段集合不同</b>：仅基线有 ' + int(a.onlyBase) + ' 个、仅 v3.3 有 ' + int(a.onlyNew) + ' 个，两边都有 ' + int(a.both) + ' 个。</li>';
    if (a.sparseNew.length) {
      h += '<li><b>' + a.sparseNew.length + ' 案有段落留空</b>：' + a.sparseNew.map(function (x) { return '<span class="mono">' + esc(x.phone) + '</span>（' + x.blank + ' 段）'; }).join('、') + '。留空**不视为缺陷**——事实簿信息稀薄时，空段是正确产出；逐案页会标出是哪个案。</li>';
    } else {
      h += '<li><b>无段落留空</b>：39 案十段均有内容。</li>';
    }
    if (a.failedBase.length) h += '<li><b>' + a.failedBase.length + ' 案基线臂未成功</b>：' + a.failedBase.map(esc).join('、') + '。</li>';
    h += '</ul></div>';

    h += '<div class="vcard cal"><span class="flag">读这页要留意的口径</span><ul>';
    h += '<li>两臂<b>同号码、不同工作流</b>：基线是 <code>' + esc(D.base_workflow) + '</code>（现行主入口），v3.3 是 <code>' + esc(D.new_workflow) + '</code>（事实账本管线）。差异来自实现本身。</li>';
    h += '<li>Notes 并集大表把<b>两臂字段对齐</b>：某字段只在一侧有时，另一侧留空并高亮，一眼可辨。</li>';
    h += '<li>段落计数把列表按条数、值字典按非空字段数计，<b>只反映内容量</b>，不代表准确性。</li>';
    h += '</ul></div>';
    h += '</div>';

    /* 关键读数 */
    h += '<div class="block"><div class="sh"><h2>关键读数</h2><span class="hint">' + int(a.phones) + ' 案合计 · 基线 → v3.3</span></div>';
    h += '<div class="readout">';
    h += ro(int(a.notesBase), int(a.notesNew), 'Notes 条数', notesPct + '% of 基线', notesPct >= 100 ? 'good' : 'bad');
    h += ro(int(a.secBase), int(a.secNew), '画像卡条目（九段）', secPct + '% of 基线', secPct >= 100 ? 'good' : 'bad');
    h += ro(int(a.onlyBase), int(a.onlyNew), '单边字段（仅基线 / 仅 v3.3）', '共 ' + int(a.both) + ' 字段两边都有', 'bad');
    h += ro(int(a.llmBase), int(a.llmNew), 'LLM 调用次数', 'v3.3 = ' + int(a.llmNew) + ' 次', 'flat');
    h += ro(ktok(a.tokBase), ktok(a.tokNew), 'Token 用量', 'v3.3 = ' + ktok(a.tokNew), 'flat');
    h += ro(int(a.okBase) + '/' + int(a.phones), int(a.okNew) + '/' + int(a.phones), '跑通案数', '成功 / 总案数', a.okNew === a.phones ? 'good' : 'bad');
    h += ro(dur(a.medBase), dur(a.medNew), '单案耗时（中位）', '全过程 wall time', 'flat');
    h += ro(int(a.retried.length), int(a.anomaly), '重试案数 / 异常条数', 'v3.3 可靠性留痕', a.anomaly === 0 ? 'good' : 'bad');
    h += '</div></div>';

    /* 十段覆盖 */
    h += '<div class="block"><div class="sh sub"><h3>analysis_result 十段 · 逐项覆盖</h3><span class="hint">数字为 ' + int(a.phones) + ' 案合计条目数</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>段落</th><th>字段名</th><th class="n">基线</th><th class="n">v3.3</th><th class="n">差值</th><th>判定</th></tr></thead><tbody>';
    D.sections.forEach(function (k) {
      var b = a.secByKey[k]['base'], v = a.secByKey[k]['new'], d = v - b;
      h += '<tr><td>' + esc(D.section_labels[k]) + '</td><td class="k">' + esc(k) + '</td>' +
        '<td class="n">' + int(b) + '</td><td class="n">' + int(v) + '</td>' +
        '<td class="n ' + (d > 0 ? 'better' : d < 0 ? 'worse' : 'flat') + '">' + (d > 0 ? '+' : '') + int(d) + '</td>' +
        '<td>' + (d >= 0 ? '<span class="tag t-ok">持平或更多</span>' : '<span class="tag t-del">少于基线</span>') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    /* 逐案一览 */
    h += '<div class="block"><div class="sh sub"><h3>逐案一览</h3><span class="hint">点号码进入逐案对照</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th class="n">基线 Notes</th><th class="n">v3.3 Notes</th><th class="n">仅基线</th><th class="n">仅 v3.3</th>' +
      '<th class="n">基线卡面</th><th class="n">v3.3 卡面</th><th class="n">v3.3 LLM</th><th class="n">v3.3 重试</th><th>状态</th></tr></thead><tbody>';
    D.cases.forEach(function (c, i) {
      var scn = 0, scb = 0, d = notesDiff(c);
      D.sections.forEach(function (k) {
        if (k === 'basic_notes_updates') return;
        scn += (c['new'] && c['new'].sec_counts[k]) || 0;
        scb += (c.base && c.base.sec_counts[k]) || 0;
      });
      var bad = c['new'] && c['new'].status !== 'succeeded';
      var retry = c['new'] && (c['new'].max_attempt || 1) > 1;
      h += '<tr style="cursor:pointer" data-jump="' + i + '">' +
        '<td class="k">' + esc(c.phone) + '</td>' +
        '<td class="n">' + int((c.base && c.base.notes.length) || 0) + '</td>' +
        '<td class="n">' + int((c['new'] && c['new'].notes.length) || 0) + '</td>' +
        '<td class="n ' + (d.only_base.length ? 'worse' : 'flat') + '">' + int(d.only_base.length) + '</td>' +
        '<td class="n ' + (d.only_new.length ? '' : 'flat') + '">' + int(d.only_new.length) + '</td>' +
        '<td class="n">' + int(scb) + '</td><td class="n">' + int(scn) + '</td>' +
        '<td class="n">' + int((c['new'] && c['new'].llm) || 0) + '</td>' +
        '<td class="n">' + (retry ? '<span class="tag t-info">' + esc(c['new'].max_attempt) + '</span>' : '<span class="nul">1</span>') + '</td>' +
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
    var n = c['new'];
    var h = '';

    if (n && n.status !== 'succeeded') {
      h += '<div class="note err"><b>本案 v3.3 运行未成功（' + esc(n.status) + '）</b>：未产出画像与 Notes。' +
        (n.error ? '<br><span style="color:var(--muted);font-size:12px">原因摘要：<code>' + esc(String(n.error).slice(0, 240)) + '</code></span>' : '') +
        '</div>';
    } else {
      var blanks = CARD.filter(function (k) { return !(n && n.sec_counts[k] > 0); });
      if (blanks.length) {
        h += '<div class="note warn"><b>本案有 ' + blanks.length + ' 段留空</b>：' +
          blanks.map(function (k) { return esc(D.section_labels[k]); }).join('、') +
          '。留空按原样呈现，<b>不补空、不视为缺陷</b>——若事实簿确无对应材料，空段是正确产出。</div>';
      }
    }

    h += '<div class="seg" id="seg">' +
      segBtn('card', '画像卡逐段对照') + segBtn('notes', 'Notes 并集对照') + segBtn('machine', '机器读数') + '</div>';
    h += '<div id="segbody">' + segBody(c) + '</div>';
    return h;
  }
  function segBtn(k, label) { return '<button data-s="' + k + '" aria-selected="' + (STATE.seg === k) + '">' + label + '</button>'; }

  function segBody(c) {
    if (STATE.seg === 'card') {
      var h2 = '';
      D.sections.forEach(function (k) {
        if (k === 'basic_notes_updates') return;
        var cn = (c['new'] && c['new'].sec_counts[k]) || 0, cb = (c.base && c.base.sec_counts[k]) || 0;
        h2 += '<div class="block"><div class="sh sub"><h3>' + esc(D.section_labels[k]) + '</h3>' +
          '<span class="hint">' + esc(k) + ' · 基线 ' + cb + ' → v3.3 ' + cn + '</span></div>';
        h2 += '<div class="collate"><div class="col-head">' +
          '<div><span class="tag t-base">基线</span></div><div><span class="tag t-v3">v3.3</span></div></div>' +
          '<div class="col-body"><div>' + sectionBlock(c.base && c.base.sections[k]) + '</div>' +
          '<div>' + sectionBlock(c['new'] && c['new'].sections[k]) + '</div></div></div></div>';
      });
      return h2;
    }
    if (STATE.seg === 'notes') {
      var d = notesDiff(c);
      var h = '<div class="block">';
      h += '<div class="sh sub"><h3>两臂 Notes · 并集对照</h3>' +
        '<span class="hint">按字段对齐，共 ' + int(d.both.length + d.only_base.length + d.only_new.length) + ' 个字段 · 左基线右 v3.3</span></div>';
      h += notesUnion(c);
      h += '<div class="note">某一字段只在一侧有结论时，另一侧<b>留空并高亮</b>（<span style="color:var(--del);font-weight:700">红色 ⊘</span>）。' +
        '每条的值与推理明细都完整保留：「推理」是<b>结论怎么推出来的</b>，「依据」是<b>客户原始数据里的哪一段</b>。</div>';
      h += '</div>';
      return h;
    }
    return machineBody(c);
  }

  function machineBody(c) {
    var b = c.base, n = c['new'];
    var h = '<div class="block">';
    h += '<div class="sh sub"><h3>机器读数对照</h3><span class="hint">取自两侧 trace.db · 零 token</span></div>';
    h += '<div class="tblwrap"><div class="scroll"><table><thead><tr>' +
      '<th>读数</th><th class="n">基线</th><th class="n">v3.3</th><th>说明</th></tr></thead><tbody>';
    h += mRow('工作流', (b && b.workflow_id) || '—', (n && n.workflow_id) || '—', '两臂走的是不同工作流');
    h += mRow('状态', b ? b.status : '—', n ? n.status : '—', 'succeeded 即成功');
    h += mRow('耗时', b ? dur(b.duration_ms) : '—', n ? dur(n.duration_ms) : '—', '含子工作流的全过程');
    h += mRow('LLM 调用次数', b ? int(b.llm) : '—', n ? int(n.llm) : '—', '含每次重试');
    h += mRow('Token 用量', b ? ktok(b.tok) : '—', n ? ktok(n.tok) : '—', 'usage.total_tokens 求和');
    h += mRow('最大尝试次数', b ? int(b.max_attempt) : '—', n ? int(n.max_attempt) : '—', '>1 表示发生过重试');
    h += '<tr><td class="k">异常条数</td>' +
      '<td class="n"><span class="nul">未记录</span></td>' +
      '<td class="n">' + (n && n.anomaly != null ? int(n.anomaly) : '<span class="nul">—</span>') + '</td>' +
      '<td>' + ((n && n.anomaly != null) ? '<span class="tag t-ok">v3.3 新增留痕</span>' : '<span class="nul">—</span>') +
      ' 基线库无 <code>anomaly</code> 列，故不列数字——「列不存在」不等于「无异常」</td></tr>';
    h += '</tbody></table></div></div>';
    h += '</div>';
    return h;
  }

  function mRow(label, b, n, note) {
    return '<tr><td class="k">' + esc(label) + '</td><td class="n">' + esc(b) + '</td><td class="n">' + esc(n) + '</td>' +
      '<td style="font-size:12.5px;color:var(--ink2)">' + esc(note) + '</td></tr>';
  }

  /* ---------- 左栏 ---------- */
  function rail() {
    var h = '';
    if (STATE.view === 'cases') {
      h += '<div><h4>用例</h4><div class="rail-list">';
      D.cases.forEach(function (c, i) {
        var d = notesDiff(c);
        var bad = c['new'] && c['new'].status !== 'succeeded';
        var col = bad ? 'var(--del)' : (d.only_base.length > d.only_new.length ? 'var(--del)' : (d.only_base.length < d.only_new.length ? 'var(--add)' : 'var(--muted)'));
        h += '<button data-k="' + i + '" aria-pressed="' + (i === STATE.kase) + '">' +
          '<span class="dot" style="background:' + col + '"></span>' +
          '<span class="ph">' + esc(c.phone) + '</span>' +
          '<span class="n">−' + int(d.only_base.length) + ' +' + int(d.only_new.length) + '</span></button>';
      });
      h += '</div></div>';
    }
    h += '<div><h4>图例</h4><div class="legend">' +
      '<div class="row"><span class="sw s-base"></span>基线（现行主入口）</div>' +
      '<div class="row"><span class="sw s-v3"></span>v3.3（事实账本管线）</div>' +
      '<div class="row"><span class="sw s-del"></span>某侧缺该字段 · 留空高亮</div>' +
      '<div class="row"><span class="sw s-add"></span>较基线新增字段</div>' +
      '</div></div>';
    h += '<div><h4>批次</h4><div class="legend" style="font-family:var(--mono);font-size:11.5px">' +
      '<div>' + esc(D.batch_new || '') + '</div>' +
      '<div style="color:var(--muted)">' + esc(D.batch_base || '') + '</div>' +
      '</div></div>';
    return h;
  }

  /* ---------- 渲染调度 ---------- */
  function render() {
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
      b.onclick = function () { STATE.kase = Number(b.getAttribute('data-k')); render(); try { window.scrollTo(0, 0); } catch (e) {} };
    });
    document.querySelectorAll('tr[data-jump]').forEach(function (tr) {
      tr.onclick = function () {
        STATE.kase = Number(tr.getAttribute('data-jump')); STATE.view = 'cases'; STATE.seg = 'card';
        document.querySelectorAll('#views button').forEach(function (x) { x.setAttribute('aria-selected', x.getAttribute('data-v') === 'cases'); });
        render(); try { window.scrollTo(0, 0); } catch (e) {}
      };
    });
    document.querySelectorAll('#seg button').forEach(function (b) {
      b.onclick = function () { STATE.seg = b.getAttribute('data-s'); render(); };
    });
  }

  /* ---------- 启动 ---------- */
  function boot() {
    var el = document.getElementById('d-payload');
    if (!el) { document.getElementById('shell').innerHTML = '<div class="err-box">页面内未找到内联数据块（d-payload）。</div>'; return; }
    try { D = JSON.parse(el.textContent); }
    catch (e) { document.getElementById('shell').innerHTML = '<div class="err-box">数据解析失败：' + esc(e.message) + '</div>'; return; }

    CARD = D.sections.filter(function (k) { return k !== 'basic_notes_updates'; });
    var a = agg();
    var notesPct = Math.round(a.notesNew / Math.max(a.notesBase, 1) * 100);

    document.getElementById('lede').innerHTML =
      'v3.3 管线与基线在<b>同 ' + int(a.phones) + ' 案</b>上的输出直接比对。' +
      'Notes 从基线的 <b>' + int(a.notesBase) + '</b> 条到 v3.3 的 <b>' + int(a.notesNew) + '</b> 条（基线的 <b>' + notesPct + '%</b>），' +
      '画像卡条目 <b>' + int(a.secBase) + ' → ' + int(a.secNew) + '</b>。' +
      'v3.3 可靠性读数：跑通 <b class="up">' + int(a.okNew) + '/' + int(a.phones) + '</b> 案、' +
      '重试救回 <b>' + int(a.retried.length) + '</b> 案、异常留痕 <b class="' + (a.anomaly === 0 ? 'up' : 'dn') + '">' + int(a.anomaly) + '</b> 条。';

    document.getElementById('mh-meta').innerHTML =
      '<span>用例 <b>' + int(a.phones) + ' / 39</b></span>' +
      '<span>v3.3 批次 <b>' + esc(D.batch_new || '') + '</b></span>' +
      '<span>基线批次 <b>' + esc(D.batch_base || '') + '</b></span>' +
      '<span>LLM 次数 <b>' + int(a.llmBase) + ' → ' + int(a.llmNew) + '</b></span>';

    document.getElementById('foot').innerHTML =
      '<div>数据来源：v3.3 批次 ' + esc(D.batch_new || '') + '（实验臂 ' + esc(D.new_workflow || '') + '）对照 ' + esc(D.batch_base || '') + '（基线臂 ' + esc(D.base_workflow || '') + '）。</div>' +
      '<div>本页含真实客户数据（姓名 / 手机号 / 录音转写），仅供内部使用，请勿外传。</div>';
    render();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
