/* 画像管线 AB 对照 — 静态渲染器
 * 数据：payload.json（8 案完整输入+全部输出字段）/ findings.json（v2 内容质量排查）
 *       / old_problems.json（基线 vs v2 十类老问题复检）
 * 全部按需渲染：原始数据最大单案 636KB，只在选中该案时才构建 DOM。
 */
(function () {
  'use strict';

  var STATE = { view: 'overview', kase: 0, sub: 'raw' };
  var D = { payload: null, findings: null, problems: null };
  var OPTIONAL_KEYS = ['image_url', 'icon', 'id', 'rank_order']; // 渲染时跳过的占位字段
  var PRED_WORDS = ['可能', '推测', '预测', '表明', '估计', '大概率', '或许', '应该', '似乎'];

  /* ---------- 工具 ---------- */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function bytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(2) + ' MB';
  }
  function int(n) { return (Number(n) || 0).toLocaleString('en-US'); }
  function pct(a, b) { return b ? Math.round(a / b * 1000) / 10 + '%' : '—'; }
  function has(v) {
    if (v == null) return false;
    if (Array.isArray(v)) return v.length > 0;
    if (typeof v === 'object') return Object.keys(v).length > 0;
    if (typeof v === 'string') return v.trim() !== '';
    return true;
  }
  function isNul(v) { return !has(v); }
  function nul() { return '<span class="nul">— 空 —</span>'; }
  function txt(v) {
    if (isNul(v)) return nul();
    if (typeof v === 'object') return '<code>' + esc(JSON.stringify(v, null, 0)) + '</code>';
    return esc(v);
  }
  function jstr(v) { return JSON.stringify(v, null, 2); }
  function jbytes(v) { try { return new Blob([JSON.stringify(v)]).size; } catch (e) { return JSON.stringify(v).length; } }

  /* ---------- 9 个 section 的通用渲染 ---------- */
  function reasoning(rd, depth) {
    if (!has(rd)) return '';
    var parts = [];
    if (has(rd.summary)) parts.push('<div><span class="lb">推理</span><div>' + esc(rd.summary) + '</div></div>');
    if (has(rd.steps)) {
      parts.push('<div><span class="lb">步骤</span><ol>' + rd.steps.map(function (s) {
        return '<li>' + esc(s) + '</li>';
      }).join('') + '</ol></div>');
    }
    if (has(rd.evidence)) {
      parts.push('<div><span class="lb">证据锚点</span><div class="ev">' + rd.evidence.map(function (s) {
        return '<span>' + esc(s) + '</span>';
      }).join('') + '</div></div>');
    }
    if (rd.confidence != null) parts.push('<div><span class="lb">置信度</span> <span class="conf">' + esc(rd.confidence) + '</span></div>');
    if (!parts.length) return '';
    var open = depth ? '' : ' open';
    return '<details class="rz"' + open + '><summary>推理明细</summary><div class="rz-bd">' + parts.join('') + '</div></details>';
  }

  function itemBlock(title, desc, extra, rd, conf) {
    var h = '<div class="item">';
    if (has(title) || conf != null) {
      h += '<div class="t"><span>' + esc(title || '（未命名）') + '</span>';
      if (extra) h += extra;
      if (conf != null) h += '<span class="conf">置信度 ' + esc(conf) + '</span>';
      h += '</div>';
    }
    if (has(desc)) h += '<div class="d">' + esc(desc) + '</div>';
    h += reasoning(rd, 1);
    return h + '</div>';
  }

  function isValueShape(obj) {
    // customer_overview / emotion_state: {k: {value, reasoning_detail}}
    var ks = Object.keys(obj);
    if (!ks.length) return false;
    var hit = 0;
    ks.forEach(function (k) {
      var v = obj[k];
      if (v && typeof v === 'object' && !Array.isArray(v) && ('value' in v || 'reasoning_detail' in v)) hit++;
    });
    return hit > ks.length / 2;
  }

  function renderSection(key, val) {
    if (isNul(val)) return '<div class="note">该 section 在本臂为空（装配层以空形态占位，未编造内容）。</div>';

    if (Array.isArray(val)) {
      if (!val.length) return '<div class="note">该 section 在本臂为空数组。</div>';
      return val.map(function (it) {
        if (typeof it !== 'object' || it === null) return itemBlock('', String(it), '', null, null);
        var title = it.title || it.strategy_name || it.motivation_name || it.preference_name || it.resistance_name || it.id || '';
        var desc = it.desc || it.description || '';
        var extra = '';
        if (has(it.business_scenario)) extra += '<span class="tag">' + esc(it.business_scenario) + '</span>';
        if (has(it.severity)) extra += '<span class="badge b-warn">严重度 ' + esc(it.severity) + '</span>';
        if (it.rank_order != null && it.rank_order !== '') extra += '<span class="tag">#' + esc(it.rank_order) + '</span>';
        return itemBlock(title, desc, extra, it.reasoning_detail, it.confidence);
      }).join('');
    }

    if (typeof val === 'object') {
      // {value, reasoning_detail} 形状
      if (isValueShape(val)) {
        return '<div class="item">' + Object.keys(val).map(function (k) {
          var v = val[k] || {};
          return '<div style="padding:9px 0;border-bottom:1px solid var(--line-2)">' +
            '<div class="t" style="font-weight:600;font-size:13.5px">' + esc(k) +
            ' <span style="font-weight:400;color:var(--fg-2)">' + txt(v.value) + '</span></div>' +
            reasoning(v.reasoning_detail, 1) + '</div>';
        }).join('') + '</div>';
      }
      // 纯字符串字段
      var rows = Object.keys(val).map(function (k) {
        return '<dt>' + esc(k) + '</dt><dd>' + txt(val[k]) + '</dd>';
      }).join('');
      return '<dl class="kv">' + rows + '</dl>';
    }
    return '<div>' + txt(val) + '</div>';
  }

  /* ---------- Notes 表 ---------- */
  function notesTable(notes) {
    if (!has(notes)) return '<div class="note">本臂 Notes 为空。</div>';
    var rows = notes.map(function (n) {
      var rd = n.reasoning_detail || {};
      var ev = has(rd.evidence) ? rd.evidence : [];
      var evKind = ev.map(function (s) {
        var t = String(s);
        if (t.indexOf('未知渠道') >= 0) return '<span class="badge b-err">' + esc(t) + '</span>';
        return '<span class="badge b-mut">' + esc(t) + '</span>';
      }).join(' ');
      var steps = has(rd.steps) ? '<ol class="mono" style="margin:4px 0 0;padding-left:16px;font-size:12px;color:var(--fg-2)">' +
        rd.steps.map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ol>' : '';
      return '<tr>' +
        '<td class="mono" style="white-space:nowrap">' + esc(n.attribute_key || '') + '</td>' +
        '<td><strong>' + txt(n.attribute_value) + '</strong></td>' +
        '<td style="min-width:190px">' + (has(rd.summary) ? '<div>' + esc(rd.summary) + '</div>' : '') + steps + '</td>' +
        '<td style="min-width:150px">' + (evKind || nul()) + '</td>' +
        '<td class="mono">' + (rd.confidence != null ? esc(rd.confidence) : '—') + '</td>' +
        '</tr>';
    }).join('');
    return '<div class="scroll"><table><thead><tr>' +
      '<th>字段</th><th>取值</th><th>推理摘要 / 步骤</th><th>证据锚点</th><th>置信度</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  /* ---------- 老问题复检：聚合 ---------- */
  function agg(rows) {
    var out = { base: {}, v2: {} };
    function sum(arm, key, mode) {
      return rows.reduce(function (n, r) {
        var v = (r[arm] || {})[key];
        if (mode === 'bool') return n + (v ? 1 : 0);
        if (Array.isArray(v)) return n + v.length;
        if (typeof v === 'boolean') return n + (v ? 1 : 0);
        if (v == null) return n;
        return n + 1;
      }, 0);
    }
    ['enum_labels', 'synonym_split', 'unknown_keys', 'purchase', 'level_conflict',
      'age_format', 'evidence_self_ref', 'dirty'].forEach(function (k) {
        out.base[k] = sum('base', k); out.v2[k] = sum('v2', k);
      });
    ['occupation_third_party'].forEach(function (k) {
      out.base[k] = sum('base', k, 'bool'); out.v2[k] = sum('v2', k, 'bool');
    });
    out.base.desync = [0, 0]; out.v2.desync = [0, 0];
    rows.forEach(function (r) {
      out.base.desync[0] += r.base.desync[0]; out.base.desync[1] += r.base.desync[1];
      out.v2.desync[0] += r.v2.desync[0]; out.v2.desync[1] += r.v2.desync[1];
    });
    return out;
  }

  function statBadge(a, b, goodDirection) {
    // goodDirection: 'up' 表示数值上升是好事
    if (a === b) return '<span class="badge b-mut">持平</span>';
    var better = goodDirection === 'up' ? b > a : b < a;
    return better ? '<span class="badge b-ok">改善</span>' : '<span class="badge b-err">退化</span>';
  }

  /* ---------- 视图：总览 ---------- */
  function viewOverview() {
    var cases = D.payload.cases, rows = D.problems, a = agg(rows), F = D.findings;
    var v2llm = 0, v2tok = 0, bllm = 0, btok = 0;
    cases.forEach(function (c) {
      v2llm += c.v2.metrics.llm_calls; v2tok += c.v2.metrics.tokens;
      bllm += c.base.metrics.llm_calls; btok += c.base.metrics.tokens;
    });
    var errN = F.filter(function (f) { return f.severity === 'error'; }).length;
    var warnN = F.filter(function (f) { return f.severity === 'warn'; }).length;

    var h = '';
    h += '<div class="kpi">';
    h += kpi('LLM 调用', int(bllm) + ' → ' + int(v2llm), '降 ' + (100 - Math.round(v2llm / bllm * 100)) + '% | 逐案 ' + Math.round(bllm / 8) + ' → ' + Math.round(v2llm / 8));
    h += kpi('Token 用量', int(Math.round(btok / 1000)) + 'k → ' + int(Math.round(v2tok / 1000)) + 'k', '降 ' + (100 - Math.round(v2tok / btok * 100)) + '% | 逐案 ' + int(Math.round(btok / 8)) + ' → ' + int(Math.round(v2tok / 8)));
    h += kpi('证据锚点引用自产画像', int(a.base.evidence_self_ref) + ' → ' + int(a.v2.evidence_self_ref), 'v2 归零');
    h += kpi('模板外 key', int(a.base.unknown_keys) + ' → ' + int(a.v2.unknown_keys), 'v2 归零');
    h += kpi('v2 内容排查', errN + ' 错 / ' + warnN + ' 警', '共 ' + F.length + ' 条');
    h += kpi('用例数', '39 抽 8', '20% 随机抽样');
    h += '</div>';

    h += '<div class="sec-h">结论</div>';
    h += '<div class="verdict">';
    h += '<div class="vc up"><h4>确实修好了</h4><ul>' +
      '<li><strong>证据锚点不再引用自产画像</strong>：基线 8 案共 ' + a.base.evidence_self_ref + ' 处引用「客户画像显示…」「第三方画像…」等中间产物作为证据；v2 为 0 处。</li>' +
      '<li><strong>模板外 key 归零</strong>：基线出现 ' + a.base.unknown_keys + ' 个不在契约模板里的字段名，v2 全部落到契约内。</li>' +
      '<li><strong>职业不再只靠第三方预测</strong>：基线 2 例的职业唯一来源是极光预测，v2 为 0 例。</li>' +
      '<li><strong>丢弃漏斗关闭</strong>：v2 的 L1→投影阶段 <code>dropped</code> 全案为 0，基线存在的「断言被静默丢弃」路径已消除。</li>' +
      '</ul></div>';
    h += '<div class="vc dn"><h4>新出现 / 仍然存在</h4><ul>' +
      '<li><strong>渠道标注整体塌陷（最严重）</strong>：v2 全案 ' + errN + ' 级问题里，绝大多数是证据锚点退化成「社交自呈现（未知渠道）」——值来自哪个渠道无法追溯，审计链断掉。</li>' +
      '<li><strong>三分类中间档塌成最低档</strong>：7/8 案的 <code>inferred_high</code> 与 <code>fact</code> 在字段级全部降级为 <code>inferred_low</code>，Notes 置信度只剩 0.5 / 0.7 两个值。</li>' +
      '<li><strong>age 跨节写法不一致</strong>：v2 由基线的 1 例涨到 8 例，卡片写「40-50岁」而 Notes 写「40 ~ 50 岁」，全案如此。</li>' +
      '<li><strong>summary 与 steps 脱节</strong>：v2 12/152 个推理块，中文 summary 与 steps 字面零重叠（基线 0/247）。</li>' +
      '<li><strong>section 空缺变多</strong>：v2 在用車场景、抗性/顾虑等 section 上的空缺明显多于基线。</li>' +
      '<li><strong>微信号等于手机号</strong>：两臂均 8 例全中，未改善。</li>' +
      '</ul></div>';
    h += '<div class="vc fl"><h4>口径提醒</h4><ul>' +
      '<li>「证据锚点引用自产画像」基线 ' + a.base.evidence_self_ref + ' 处中，多数实为「第三方画像/极光预测」，属<em>引用外部预测</em>而非引用自产画像；两类我都计入，页内可下钻查看原始锚点。</li>' +
      '<li>「枚举用中文标签」两臂数字都被本版检查器放大：取值是 <code>{"key":...}</code> 字典型时会被误判为非法串。</li>' +
      '<li>⑦ age 是<strong>格式</strong>不一致，不是值不一样（号码段相同）。</li>' +
      '<li>⑨ 的具体成因见「口径与边界」。</li>' +
      '</ul></div>';
    h += '</div>';

    h += '<div class="sec-h">十类老问题 · 基线 8 案 vs v2 8 案</div>';
    var CATS = [
      ['①枚举用了中文标签/非契约值', 'enum_labels', 'down', '取值非契约枚举成员的处数'],
      ['②同义 key 分裂（组数）', 'synonym_split', 'down', '同一概念被写成多个字段名'],
      ['③模板外 key', 'unknown_keys', 'down', '不在契约模板里的字段'],
      ['④购买类型与旧车矛盾', 'purchase', 'down', 'purchase_type / 旧车 / 置换 三处不自洽'],
      ['⑤职业仅靠第三方预测', 'occupation_third_party', 'down', '职业唯一来源是极光预测'],
      ['⑥等级字段打架', 'level_conflict', 'down', 'deal_level ≠ business_opp_level'],
      ['⑦age 跨节格式不一致', 'age_format', 'down', '卡片与 Notes 写法不同'],
      ['⑧证据锚点引用画像/人设', 'evidence_self_ref', 'down', '锚点指向中间产物而非原始渠道'],
      ['⑩姓名/微信号脏值', 'dirty', 'down', '姓名碎片 / 微信号等于手机号']
    ];
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>类别</th><th class="n">基线</th><th class="n">v2</th><th>判定</th><th>含义</th></tr></thead><tbody>';
    CATS.forEach(function (c) {
      var b = a.base[c[1]], v = a.v2[c[1]];
      h += '<tr><td>' + esc(c[0]) + '</td><td class="n">' + int(b) + '</td><td class="n">' + int(v) + '</td>' +
        '<td>' + statBadge(b, v, c[2]) + '</td><td style="color:var(--muted);font-size:12.5px">' + esc(c[3]) + '</td></tr>';
    });
    var db = a.base.desync, dv = a.v2.desync;
    h += '<tr><td>⑨summary 与 steps 脱节</td><td class="n">' + db[0] + '/' + db[1] + '</td><td class="n">' + dv[0] + '/' + dv[1] + '</td>' +
      '<td>' + (dv[0] === 0 ? '<span class="badge b-mut">持平</span>' : '<span class="badge b-err">退化</span>') + '</td>' +
      '<td style="color:var(--muted);font-size:12.5px">推理块中 summary 与 steps 字面零重叠</td></tr>';
    h += '</tbody></table></div></div></div>';
    h += '<div class="note warn">⑦ 与 ⑨ 的判定需要结合成因看：⑦ 是格式而非值；⑨ 的 v2 steps 改为字段名+取值形式后与中文 summary 天然无字面重叠，其中相当一部分是<strong>口径变化</strong>而非内容脱节。详见「口径与边界」。</div>';

    h += '<div class="sec-h">逐案成本</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th class="n">基线 LLM</th><th class="n">v2 LLM</th><th class="n">基线 Token</th><th class="n">v2 Token</th>' +
      '<th class="n">基线 HTTP</th><th class="n">v2 HTTP</th><th>分诊</th></tr></thead><tbody>';
    cases.forEach(function (c) {
      h += '<tr><td class="mono">' + esc(c.phone) + '</td>' +
        '<td class="n">' + c.base.metrics.llm_calls + '</td><td class="n">' + c.v2.metrics.llm_calls + '</td>' +
        '<td class="n">' + int(c.base.metrics.tokens) + '</td><td class="n">' + int(c.v2.metrics.tokens) + '</td>' +
        '<td class="n">' + c.base.metrics.http_calls + '</td><td class="n">' + c.v2.metrics.http_calls + '</td>' +
        '<td>' + (c.v2.tier ? '<span class="badge b-info">' + esc(c.v2.tier) + '</span>' : '—') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';
    return h;
  }
  function kpi(label, num, delta) {
    return '<div class="u"><div class="n">' + esc(num) + '</div><div class="l">' + esc(label) + '</div>' +
      (delta ? '<div class="d" style="color:var(--muted)">' + esc(delta) + '</div>' : '') + '</div>';
  }

  /* ---------- 视图：老问题复检 ---------- */
  function viewProblems() {
    var rows = D.problems, a = agg(rows);
    var h = '';
    h += '<div class="note">判据来自 <code>docs/business_opt01/基线画像质量诊断.md</code>。两臂用同一份检查器，口径一致，可直接横向比较。</div>';

    h += '<div class="sec-h">⑨ summary 与 steps 脱节 · 逐案</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th class="n">基线 (脱节/总数)</th><th class="n">v2 (脱节/总数)</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td class="mono">' + esc(r.phone) + '</td>' +
        '<td class="n">' + r.base.desync[0] + ' / ' + r.base.desync[1] + '</td>' +
        '<td class="n">' + r.v2.desync[0] + ' / ' + r.v2.desync[1] + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    h += '<div class="sec-h">④ 购买类型与旧车矛盾 · 逐案明细</div>';
    var p4 = rows.filter(function (r) { return (r.base.purchase || []).length || (r.v2.purchase || []).length; });
    if (!p4.length) h += '<div class="note ok">8 案两臂均无此类矛盾。</div>';
    p4.forEach(function (r) {
      h += '<div class="cmp"><div class="cmp-hd"><div><span class="badge b-base">基线</span></div><div><span class="badge b-v2">v2</span></div></div>';
      h += '<div class="cmp-row"><div class="mono" style="color:var(--muted);font-size:12px">' + esc(r.phone) + '</div>' +
        '<div class="mono" style="color:var(--muted);font-size:12px">' + esc(r.phone) + '</div></div>';
      h += '<div class="cmp-row"><div>' + (r.base.purchase.length ? '<ul style="margin:0;padding-left:16px">' + r.base.purchase.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : nul()) + '</div>';
      h += '<div>' + (r.v2.purchase.length ? '<ul style="margin:0;padding-left:16px">' + r.v2.purchase.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : nul()) + '</div></div>';
      h += '</div>';
    });

    h += '<div class="sec-h">⑦ age 跨节格式 · 逐案</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th>基线</th><th>v2</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td class="mono">' + esc(r.phone) + '</td>' +
        '<td>' + (r.base.age_format ? esc(r.base.age_format) : '<span class="badge b-ok">一致</span>') + '</td>' +
        '<td>' + (r.v2.age_format ? esc(r.v2.age_format) : '<span class="badge b-ok">一致</span>') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';
    h += '<div class="note">注意：两侧的<strong>号码段完全相同</strong>，差异只在分隔符与「岁」字。这是表达层格式问题，不是取值冲突。</div>';

    h += '<div class="sec-h">① 枚举取值 · 逐条明细</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th>臂</th><th>字段</th><th>取值</th><th>说明</th></tr></thead><tbody>';
    var any = false;
    rows.forEach(function (r) {
      ['base', 'v2'].forEach(function (arm) {
        (r[arm].enum_labels || []).forEach(function (x) {
          any = true;
          var v = String(x.value);
          var dicty = /^\{/.test(v.trim());
          h += '<tr><td class="mono">' + esc(r.phone) + '</td>' +
            '<td><span class="badge ' + (arm === 'v2' ? 'b-v2' : 'b-base') + '">' + (arm === 'v2' ? 'v2' : '基线') + '</span></td>' +
            '<td class="mono">' + esc(x.field) + '</td>' +
            '<td><code style="font-size:12px">' + esc(v) + '</code></td>' +
            '<td style="font-size:12.5px">' + esc(x.why) +
            (dicty ? ' <span class="badge b-mut">疑为字典型取值，非真违规</span>' : '') +
            (v === 'unknown' ? ' <span class="badge b-info">unknown 仅 purchase_type 合法</span>' : '') +
            '</td></tr>';
        });
      });
    });
    if (!any) h += '<tr><td colspan="5">无</td></tr>';
    h += '</tbody></table></div></div></div>';

    h += '<div class="sec-h">⑧ 证据锚点引用画像/人设 · 逐案</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th class="n">基线</th><th class="n">v2</th><th>基线锚点样例</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      var sample = (r.base.evidence_self_ref || []).slice(0, 3).map(function (x) {
        return '<div style="margin-bottom:3px"><span class="tag">' + esc(x.path) + '</span><br>' + esc(x.anchor) + '</div>';
      }).join('') || nul();
      h += '<tr><td class="mono">' + esc(r.phone) + '</td>' +
        '<td class="n">' + (r.base.evidence_self_ref || []).length + '</td>' +
        '<td class="n">' + (r.v2.evidence_self_ref || []).length + '</td>' +
        '<td style="font-size:12.5px;color:var(--fg-2)">' + sample + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    h += '<div class="sec-h">⑩ 姓名/微信号脏值 · 逐案</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th>基线</th><th>v2</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td class="mono">' + esc(r.phone) + '</td>' +
        '<td>' + ((r.base.dirty || []).length ? r.base.dirty.map(esc).join('<br>') : '<span class="badge b-ok">干净</span>') + '</td>' +
        '<td>' + ((r.v2.dirty || []).length ? r.v2.dirty.map(esc).join('<br>') : '<span class="badge b-ok">干净</span>') + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';

    h += '<div class="sec-h">②③ 同义 key 分裂 / 模板外 key · 逐案</div>';
    h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
      '<th>手机号</th><th>基线 模板外 key</th><th>v2 模板外 key</th><th>基线 同义分裂</th><th>v2 同义分裂</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><td class="mono">' + esc(r.phone) + '</td>' +
        '<td class="mono" style="font-size:12px">' + ((r.base.unknown_keys || []).map(esc).join('<br>') || nul()) + '</td>' +
        '<td class="mono" style="font-size:12px">' + ((r.v2.unknown_keys || []).map(esc).join('<br>') || nul()) + '</td>' +
        '<td class="mono" style="font-size:12px">' + ((r.base.synonym_split || []).map(function (g) { return esc(g.join(' / ')); }).join('<br>') || nul()) + '</td>' +
        '<td class="mono" style="font-size:12px">' + ((r.v2.synonym_split || []).map(function (g) { return esc(g.join(' / ')); }).join('<br>') || nul()) + '</td></tr>';
    });
    h += '</tbody></table></div></div></div>';
    return h;
  }

  /* ---------- 视图：逐案对照 ---------- */
  function viewCases() {
    var cases = D.payload.cases;
    var c = cases[STATE.kase];
    var h = '';
    h += '<div class="chips">' + cases.map(function (x, i) {
      var f = D.findings.filter(function (y) { return y.phone === x.phone; });
      var e = f.filter(function (y) { return y.severity === 'error'; }).length;
      var col = e >= 2 ? 'var(--err)' : e ? 'var(--warn)' : 'var(--ok)';
      return '<button class="chip" data-k="' + i + '" aria-pressed="' + (i === STATE.kase) + '">' +
        '<span style="display:inline-block;width:6px;height:6px;border-radius:99px;background:' + col + ';margin-right:6px;vertical-align:1px"></span>' +
        esc(x.phone) + '</button>';
    }).join('') + '</div>';

    var f = D.findings.filter(function (y) { return y.phone === c.phone; });
    var err = f.filter(function (y) { return y.severity === 'error'; });
    var warn = f.filter(function (y) { return y.severity === 'warn'; });
    h += '<div class="card"><div class="hd"><h3>' + esc(c.phone) + ' · v2 内容排查</h3>' +
      '<span class="meta">' + err.length + ' 错误 · ' + warn.length + ' 警告</span></div><div class="bd">';
    if (!f.length) h += '<div class="note ok">未发现内容矛盾或表述不清。</div>';
    f.forEach(function (x) {
      var cls = x.severity === 'error' ? 'b-err' : (x.severity === 'warn' ? 'b-warn' : 'b-info');
      h += '<div class="finding"><div class="sev"><span class="badge ' + cls + '">' + esc(x.severity) + '</span></div>' +
        '<div class="fx"><div class="ft">' + esc(x.title) + '</div><div style="color:var(--fg-2)">' + esc(x.detail) + '</div></div></div>';
    });
    h += '</div></div>';

    h += '<div class="card"><div class="hd"><h3>管线中间产物</h3>' +
      '<span class="meta">tier=' + esc(c.v2.tier || '—') + ' · 可用渠道 ' + esc(c.v2.usable_count) + ' · 记录 ' + esc(c.v2.records_count) + '</span></div><div class="bd">';
    h += '<dl class="kv"><dt>分诊理由</dt><dd>' + txt(c.v2.tier_reason) + '</dd></dl>';
    h += renderPipelineTrace(c);
    h += '</div></div>';

    h += '<div class="subtabs" id="subtabs">' +
      subBtn('raw', '原始输入') + subBtn('notes', 'Notes 对照') + subBtn('card', '画像卡对照') + subBtn('pipeline', 'L1/投影明细') +
      '</div>';
    h += '<div id="subbody">' + subView(c) + '</div>';
    return h;
  }
  function subBtn(k, label) {
    return '<button data-s="' + k + '" aria-selected="' + (STATE.sub === k) + '">' + label + '</button>';
  }

  function renderPipelineTrace(c) {
    var loc = c.v2.localize || {}, pr = c.v2.project || {};
    var h = '<div class="grid2" style="margin-top:12px">';
    h += '<div><div class="sec-h">L1 取证 / 本地化</div><dl class="kv">' +
      '<dt>断言</dt><dd>' + (loc.claims || []).length + ' 条</dd>' +
      '<dt>保留</dt><dd>' + esc(loc.kept) + '</dd>' +
      '<dt>丢弃</dt><dd>' + esc(loc.dropped) + '</dd>' +
      '<dt>降级</dt><dd>' + esc(loc.downgraded) + '</dd></dl>';
    var dg = loc.downgraded_detail || [];
    var byReason = {};
    dg.forEach(function (d) { byReason[d.reason] = (byReason[d.reason] || 0) + 1; });
    h += '<div style="margin-top:9px" class="ev">' + Object.keys(byReason).map(function (k) {
      return '<span>' + esc(k) + ' × ' + byReason[k] + '</span>';
    }).join('') + '</div></div>';
    h += '<div><div class="sec-h">L2 投影</div><dl class="kv">' +
      '<dt>事实字段</dt><dd>' + Object.keys(pr.facts || {}).length + '</dd>' +
      '<dt>降级台账</dt><dd>' + Object.keys(pr.basis || {}).length + '</dd>' +
      '<dt>待确认</dt><dd>' + (pr.to_confirm || []).length + '</dd>' +
      '<dt>被拒</dt><dd>' + (pr.rejected || []).length + '</dd>' +
      '<dt>Notes 条数</dt><dd>' + esc(pr.notes_count) + '</dd></dl>';
    var bases = {};
    Object.keys(pr.basis || {}).forEach(function (k) { bases[pr.basis[k]] = (bases[pr.basis[k]] || 0) + 1; });
    h += '<div style="margin-top:9px" class="ev">' + (Object.keys(bases).map(function (k) {
      return '<span>' + esc(k) + ' × ' + bases[k] + '</span>';
    }).join('') || '<span>无降级</span>') + '</div></div>';
    h += '</div>';
    return h;
  }

  function subView(c) {
    if (STATE.sub === 'notes') {
      return '<div class="cmp"><div class="cmp-hd">' +
        '<div><span class="badge b-base">基线 Notes</span><span class="meta">' + c.base.notes.length + ' 条</span></div>' +
        '<div><span class="badge b-v2">v2 Notes</span><span class="meta">' + c.v2.notes.length + ' 条</span></div>' +
        '</div><div class="cmp-row"><div>' + notesTable(c.base.notes) + '</div><div>' + notesTable(c.v2.notes) + '</div></div></div>';
    }
    if (STATE.sub === 'card') {
      var SEC = D.payload.sections, LBL = D.payload.section_labels;
      var h = '';
      SEC.forEach(function (k) {
        h += '<div class="sec-h">' + esc(LBL[k]) + ' <span class="tag">' + esc(k) + '</span></div>';
        h += '<div class="cmp"><div class="cmp-hd">' +
          '<div><span class="badge b-base">基线</span></div><div><span class="badge b-v2">v2</span></div></div>' +
          '<div class="cmp-row"><div>' + renderSection(k, c.base.sections[k]) + '</div>' +
          '<div>' + renderSection(k, c.v2.sections[k]) + '</div></div></div>';
      });
      return h;
    }
    if (STATE.sub === 'pipeline') {
      var h = '';
      h += '<div class="sec-h">L1 断言（模型原始输出）</div>';
      var fl = (c.v2.l1_llm && c.v2.l1_llm.fields) || [];
      h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr>' +
        '<th>字段</th><th>取值</th><th>basis</th><th>record_channel</th><th>推理依据</th><th>置信</th></tr></thead><tbody>';
      fl.forEach(function (x) {
        var b = String(x.basis || '');
        var badge = b === 'fact' ? 'b-ok' : (b === 'inferred_high' ? 'b-info' : 'b-warn');
        h += '<tr><td class="mono">' + esc(x.attribute) + '</td>' +
          '<td><strong>' + txt(x.value) + '</strong></td>' +
          '<td><span class="badge ' + badge + '">' + esc(b) + '</span></td>' +
          '<td class="mono" style="font-size:12px">' + (has(x.record_channel) ? esc(x.record_channel) : '<span class="badge b-err">空</span>') + '</td>' +
          '<td style="font-size:12.5px;color:var(--fg-2)">' + ((x.basis_of_inference || []).map(esc).join('；') || nul()) + '</td>' +
          '<td class="mono">' + esc(x.confidence) + '</td></tr>';
      });
      h += '</tbody></table></div></div></div>';

      h += '<div class="sec-h">L2 投影事实表</div>';
      var facts = c.v2.project.facts || {}, basis = c.v2.project.basis || {};
      h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr><th>字段</th><th>取值</th><th>降级标记</th></tr></thead><tbody>';
      Object.keys(facts).forEach(function (k) {
        var bk = basis[k];
        h += '<tr><td class="mono">' + esc(k) + '</td><td>' + txt(facts[k]) + '</td>' +
          '<td>' + (bk ? '<span class="badge ' + (String(bk) === 'inferred_low' ? 'b-warn' : 'b-info') + '">' + esc(bk) + '</span>' : '<span class="badge b-ok">未降级</span>') + '</td></tr>';
      });
      h += '</tbody></table></div></div></div>';

      h += '<div class="sec-h">本地化流水（降级/丢弃归因）</div>';
      var dg = c.v2.localize.downgraded_detail || [];
      h += '<div class="card"><div class="bd tight"><div class="scroll"><table><thead><tr><th>字段</th><th>原因</th><th>锚点</th></tr></thead><tbody>';
      dg.forEach(function (d) {
        h += '<tr><td class="mono">' + esc(d.attribute) + '</td>' +
          '<td><span class="badge ' + (d.reason === 'unknown_channel' ? 'b-err' : 'b-warn') + '">' + esc(d.reason) + '</span></td>' +
          '<td class="mono" style="font-size:12px">' + (has(d.anchor) ? esc(d.anchor) : nul()) + '</td></tr>';
      });
      h += '</tbody></table></div></div></div>';
      return h;
    }
    return subRaw(c);
  }

  function subRaw(c) {
    var r = c.raw, h = '';
    h += '<div class="note">以下为两臂共用的同一份原始取数（同一批次、同一 <code>phones_sha256</code>）。数据来自真实客户，含姓名、手机号与录音转写。</div>';

    var transcriptBytes = jbytes(r.transcripts);
    h += dt('录音转写（原始识别结果）', bytes(transcriptBytes), function () {
      var keys = Object.keys(r.transcripts || {});
      if (!keys.length) return '<div class="note">无</div>';
      return keys.map(function (k) {
        return '<div class="sec-h" style="margin-top:10px">' + esc(k) + '</div><pre class="blk">' + esc(pretty(r.transcripts[k])) + '</pre>';
      }).join('');
    });

    h += dt('各渠道取数结果', bytes(jbytes(r.channels)), function () {
      var o = r.channels || {}, keys = Object.keys(o);
      if (!keys.length) return '<div class="note">无</div>';
      return '<dl class="kv">' + keys.map(function (k) {
        var v = o[k];
        var s = typeof v === 'string' ? v : pretty(v);
        return '<dt>' + esc(k) + '</dt><dd>' + (String(s).length > 400 ? '<details class="rz"><summary>展开（' + String(s).length + ' 字）</summary><pre class="blk">' + esc(s) + '</pre></details>' : '<div style="font-size:13px">' + esc(s) + '</div>') + '</dd>';
      }).join('') + '</dl>';
    });

    h += dt('历史数据接口（GET /history）', bytes(jbytes(r.history_body)), function () {
      return '<pre class="blk">' + esc(pretty(r.history_body)) + '</pre>';
    });

    h += dt('其他原始节点产出（v2）', bytes(jbytes(r.v2_extras)), function () {
      if (!(r.v2_extras || []).length) return '<div class="note">无</div>';
      return r.v2_extras.map(function (x) {
        return '<div class="sec-h" style="margin-top:10px">' + esc(x.label) + '</div><pre class="blk">' + esc(x.text) + '</pre>';
      }).join('');
    });

    h += dt('其他原始节点产出（基线）', bytes(jbytes(r.base_extras)), function () {
      if (!(r.base_extras || []).length) return '<div class="note">无</div>';
      return r.base_extras.map(function (x) {
        return '<div class="sec-h" style="margin-top:10px">' + esc(x.label) + '</div><pre class="blk">' + esc(x.text) + '</pre>';
      }).join('');
    });

    h += dt('L0 记录层（结构化后的输入）', bytes(jbytes(c.v2.records)), function () {
      return '<pre class="blk">' + esc(JSON.stringify(c.v2.records, null, 1)) + '</pre>';
    });

    h += dt('L1 提示词输入（目标字段清单 + 记录）', bytes(c.v2.l1_input.length), function () {
      return '<pre class="blk">' + esc(c.v2.l1_input) + '</pre>';
    });

    return h;
  }
  function dt(title, meta, bodyFn) {
    // 折叠块：展开时才调用 bodyFn 构建 DOM
    var id = 'dt' + Math.random().toString(36).slice(2, 9);
    var o = '<details class="dt" data-lazy="' + id + '"><summary><span>' + esc(title) + '</span>' +
      '<span class="dt-meta">' + esc(meta) + '</span></summary><div class="dt-bd" id="' + id + '"></div></details>';
    LAZY[id] = bodyFn;
    return o;
  }
  var LAZY = {};

  function pretty(v) {
    if (typeof v === 'string') {
      try { return JSON.stringify(JSON.parse(v), null, 1); } catch (e) { return v; }
    }
    return JSON.stringify(v, null, 1);
  }

  /* ---------- 视图：口径与边界 ---------- */
  function viewMethod() {
    var h = '';
    h += '<div class="sec-h">可比性</div>';
    h += '<div class="card"><div class="bd"><dl class="kv">' +
      '<dt>抽样</dt><dd>39 案中随机抽取 8 案（20%），两臂同案同输入</dd>' +
      '<dt>输入一致性</dt><dd><code>phones_sha256</code> 与基线批次完全一致</dd>' +
      '<dt>代码冻结</dt><dd>每个 workflow 在本批内只有一个 <code>definition_hash</code>；经 <code>normalise()</code> 后与 HEAD 逐字节一致</dd>' +
      '<dt>回写</dt><dd><code>WRITEBACK_ENABLED=false</code>，<code>update-profile</code> 调用 0 次，全部运行 <code>succeeded</code></dd>' +
      '<dt>模型</dt><dd>deepseek-v4.1-flash，noThink</dd>' +
      '</dl></div></div>';

    h += '<div class="sec-h">⑦ 与 ⑨ 的成因（避免误读）</div>';
    h += '<div class="card"><div class="bd" style="display:flex;flex-direction:column;gap:12px">';
    h += '<div class="note warn"><strong>⑦ age 跨节格式</strong><br>v2 卡片层输出「40-50岁」，Notes 层输出「40 ~ 50 岁」。两侧号码段一致，差异仅分隔符与「岁」字。属表达层未统一格式，<em>不是</em>取值冲突；但它会让下游按字符串精确匹配的消费方拿不到同一结果。</div>';
    h += '<div class="note warn"><strong>⑨ summary 与 steps 字面零重叠</strong><br>基线的 steps 是自然语言长句（如「客户手机号17119850673属于171虚拟运营商号段，常见于低资费…」），与中文 summary 天然共享大量字词；v2 的 steps 改为「字段名+取值」形式（如「purchase_type为replacement」）。当把 <code>purchase_type</code> 译成中文「购买类型」时字面重叠消失，检查器即判为脱节。因此 12/152 中<strong>有相当比例是 steps 写法变化导致的口径差</strong>，而非推理链断裂。逐案明细可在「老问题复检」下钻。</div>';
    h += '<div class="note warn"><strong>① 枚举取值被高估</strong><br>当某字段取值是 <code>{"key": "yes", "input_value": ""}</code> 这类字典型时，检查器按字符串比对会判为「非契约值」。基线 14 处、v2 11 处里都含这类误报。剔除后真正的问题集中在：v2 的 <code>unknown</code>（仅 <code>purchase_type</code> 允许该值，其余四个字段不允许）与基线的中文取值。</div>';
    h += '<div class="note warn"><strong>⑧ 锚点性质需区分</strong><br>「引用自产画像」与「引用第三方预测」我计在同一类。基线这 ' + agg(D.problems).base.evidence_self_ref + ' 处里，多数形如「第三方画像数据：预测年龄段为40-49岁」「客户画像显示其更偏实用生活主义」——前者是外部预测数据，后者才是引用系统自产画像。v2 两类均为 0。严格区分需逐条判定，本页给出逐案样例供人工复核。</div>';
    h += '</div></div>';

    h += '<div class="sec-h">已知缺陷</div>';
    h += '<div class="card"><div class="bd"><div class="note err">本次运行的 <code>manifest.code_commit</code> 记录的是 HEAD <code>bea6ea2</code>，而实际执行的是<strong>未提交的工作区</strong>（指纹 <code>d3d912bb</code>）。实质性判据（每臂单一 <code>definition_hash</code>）仍然成立，但若要作为正式 AB 结论，P1–P5 的代码需先提交。</div></div></div>';

    h += '<div class="sec-h">数据来源</div>';
    h += '<div class="card"><div class="bd"><dl class="kv">' +
      '<dt>v2 批次</dt><dd class="mono">experiments/abexperiments/batches/A-pipeline-L8-20261012</dd>' +
      '<dt>基线批次</dt><dd class="mono">experiments/batches/baseline-39-dsv41-noThink</dd>' +
      '<dt>诊断依据</dt><dd class="mono">docs/business_opt01/基线画像质量诊断.md</dd>' +
      '<dt>本页数据</dt><dd class="mono">payload.json / findings.json / old_problems.json</dd>' +
      '</dl></div></div>';
    return h;
  }

  /* ---------- 渲染调度 ---------- */
  function render() {
    var app = document.getElementById('app');
    LAZY = {};                       // 必须先清空，视图函数在构建 HTML 时向 LAZY 注册回调
    var h;
    if (STATE.view === 'overview') h = viewOverview();
    else if (STATE.view === 'problems') h = viewProblems();
    else if (STATE.view === 'cases') h = viewCases();
    else h = viewMethod();
    app.className = 'view';
    app.innerHTML = h;
    wire(app);
  }

  function wire(root) {
    document.querySelectorAll('#tabs button').forEach(function (b) {
      b.onclick = function () { STATE.view = b.getAttribute('data-v'); document.querySelectorAll('#tabs button').forEach(function (x) { x.setAttribute('aria-selected', x === b); }); render(); try { window.scrollTo(0, 0); } catch (e) {} };
    });
    root.querySelectorAll('.chip[data-k]').forEach(function (b) {
      b.onclick = function () { STATE.kase = Number(b.getAttribute('data-k')); render(); };
    });
    root.querySelectorAll('#subtabs button').forEach(function (b) {
      b.onclick = function () { STATE.sub = b.getAttribute('data-s'); render(); };
    });
    root.querySelectorAll('details[data-lazy]').forEach(function (d) {
      d.ontoggle = function () {
        if (!d.open || d.dataset.done) return;
        var box = d.querySelector('.dt-bd');
        var fn = LAZY[d.getAttribute('data-lazy')];
        if (box && fn) { box.innerHTML = fn(); d.dataset.done = '1'; }
      };
    });
  }

  function readEmbedded(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    return JSON.parse(el.textContent);
  }

  /* ---------- 启动 ---------- */
  function boot() {
    Promise.resolve([
      readEmbedded('d-payload'),
      readEmbedded('d-findings'),
      readEmbedded('d-oldprob')
    ]).then(function (res) {
      if (!res[0]) throw new Error('页面内未找到内联数据块 d-payload');
      D.payload = res[0]; D.findings = res[1]; D.problems = res[2];
      var p = D.payload;
      document.getElementById('brand-sub').textContent =
        res[2].length + ' 案 · v2 vs 基线 · ' + p.sections.length + ' 个 section · ' + p.cases.length + ' 案完整输入';
      document.getElementById('foot').innerHTML =
        '<div>批次：A-pipeline-L8-20261012（v2） vs baseline-39-dsv41-noThink（基线）</div>' +
        '<div>本页含真实客户数据（姓名 / 手机号 / 录音转写），仅供内部使用，请勿外传。</div>' +
        '<div>判据来源：docs/business_opt01/基线画像质量诊断.md</div>';
      render();
    }).catch(function (e) {
      document.getElementById('app').innerHTML = '<div class="err-box">数据载入失败：' + esc(e.message) +
        '<br><br>数据已内联在本页中；若仍失败，说明本文件在生成或传输过程中被截断。</div>';
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
