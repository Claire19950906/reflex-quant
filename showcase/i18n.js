/* Reflex Quant — i18n shim
 * Default: English. Toggle in top-right of header.
 * Persists choice in localStorage['rq-lang']. Detects browser language on first visit.
 *
 * Usage:
 *   <script src="i18n.js"></script>
 *   <h1 data-i18n="brand.tagline"></h1>
 *   <button data-i18n="lang.toggle" data-i18n-aria="lang.toggle.aria"></button>
 *   JS:  i18n.t('brand.tagline')  ->  translated string
 *        i18n.toggle()            ->  switch EN <-> ZH
 */
(function() {
  'use strict';

  var STRINGS = {
    en: {
      'meta.title':         'Reflex Quant 🪞 — Live Reflection Showcase',

      'brand.tagline':      'Self-aware AI for quant trading',
      'header.status':      'System operational',
      'lang.toggle':        '中',
      'lang.toggle.aria':   'Switch language to Chinese',

      'stat.signals.label': 'Signals Today',
      'stat.signals.delta': 'live feed',
      'stat.checks.label':  'Reflection Checks',
      'stat.checks.delta':  'across 7 layers',
      'stat.errors.label':  'Errors Caught',
      'stat.errors.delta':  'E001 – E027',
      'stat.meta.label':    'Meta-Loop Cycles',
      'stat.meta.delta':    'self-reflection runs',

      'section.signals.title':  'Live Trading Signals',
      'section.signals.refresh':'refreshes every 5s',
      'section.cases.title':    '27-Layer Reflection — Case Studies',
      'section.cases.hint':     'click to deep-dive',
      'section.meta.title':     'Meta-Loop Self-Reflection',
      'section.meta.hint':      'when 7 checkers all miss...',
      'section.kg.title':       'Why this matters — Knowledge Graph Growth',

      'step':                  'Step',
      'meta.step1.label':      'Trade Closed',
      'meta.step1.detail':     '7 reasoning-logic checkers all PASSED',
      'meta.step2.label':      'Outcome = LOSE',
      'meta.step2.detail':     '-3.0% on WTI long',
      'meta.step3.label':      'Meta-Loop Trigger',
      'meta.step3.detail':     '"But all 7 passed?"',
      'meta.step4.label':      'Pattern Detected',
      'meta.step4.detail':     'cross-temporal drift missed',
      'meta.step5.label':      'Proposal',
      'meta.step5.detail':     'add E028 checker',

      'meta.proposal.title':   '📝 META_REFLECTION.md entry (excerpt)',
      'meta.proposal.text':    'On 2026-09-28, 7 reasoning-logic checkers passed a WTI long @ 77.20 that lost -3.0% intraday. Root cause: news from Tuesday was no longer relevant by Thursday (cross-temporal drift). None of the 7 checkers tested for time-windowed context validity. Proposed E028: cross_temporal_consistency — verify news age against decision horizon. Added to v226 roadmap.',

      'case.badge.error':      'Errors Caught',
      'case.badge.correct':    'Correctly NOT Flagged',
      'case.badge.meta':       'Meta-Loop Triggered',
      'case.outcome':          'Outcome',
      'case.sources':          'Sources',
      'case.checks':           'Checks',
      'case.severity':         'Severity',
      'case.reflection':       'Reflection Output',
      'case.correct.note':     '✓ Inverse-pair awareness correct — no false positive',
      'case.meta.note':        '⚠ All 7 reasoning-logic checkers passed — but trade lost',

      'modal.title':           'Case Detail',
      'modal.outcome':         'Outcome',
      'modal.decision':        'Decision',
      'modal.reflection':      'Reflection Output',
      'modal.system_learning': 'System Learning',
      'modal.no_errors':       'No errors caught by 27-layer reflection.',

      'footer.text':           'Reflex Quant v225 — Showcase only. All data shown is illustrative. Live system access requires NDA + design partner agreement. Contact: ',

      /* knowledge graph layer descriptions */
      'kg.event_logic.desc':   'event extraction patterns',
      'kg.causal.desc':        'cause-effect chains confirmed',
      'kg.reasoning_logic.desc':'sub-layers: assumption, counterfactual, calibration',
      'kg.sample_integrity.desc':'multi-source validation rules',
      'kg.cross_asset.desc':   'inverse-pair, correlation clusters',
      'kg.meta_patterns.desc': 'blindspots from meta-loop runs',
    },

    zh: {
      /* === SECTION 1: header + lang === */
      'meta.title':         'Reflex Quant 🪞 — 实时反思展示',
      'brand.tagline':      '自感知量化交易 AI',
      'header.status':      '系统运行中',
      'lang.toggle':        'EN',
      'lang.toggle.aria':   '切换语言为英文',

      /* === SECTION 2: stats bar === */
      'stat.signals.label': '今日信号',
      'stat.signals.delta': '实时推送',
      'stat.checks.label':  '反思检查',
      'stat.checks.delta':  '覆盖 7 层',
      'stat.errors.label':  '捕获错误',
      'stat.errors.delta':  'E001 – E027',
      'stat.meta.label':    '元迭代循环',
      'stat.meta.delta':    '自反思运行',

      /* === SECTION 3: section titles === */
      'section.signals.title':  '实时交易信号',
      'section.signals.refresh':'每 5 秒刷新',
      'section.cases.title':    '27 层反思 — 案例分析',
      'section.cases.hint':     '点击查看详情',
      'section.meta.title':     '元迭代自反思',
      'section.meta.hint':      '当 7 个检查器都错过时',
      'section.kg.title':       '为什么这很重要 — 知识图谱增长',

      /* === SECTION 4: meta-flow steps === */
      'step':                  '步骤',
      'meta.step1.label':      '交易已平仓',
      'meta.step1.detail':     '7 个推理逻辑检查器均通过',
      'meta.step2.label':      '结果 = 亏损',
      'meta.step2.detail':     'WTI 多头 -3.0%',
      'meta.step3.label':      '触发元迭代',
      'meta.step3.detail':     '“但 7 个检查都说可以?”',
      'meta.step4.label':      '检出模式',
      'meta.step4.detail':     '跨时间漂移被遗漏',
      'meta.step5.label':      '提出建议',
      'meta.step5.detail':     '新增 E028 检查器',

      /* === SECTION 5: meta proposal box === */
      'meta.proposal.title':   '📝 META_REFLECTION.md 记录（节选）',
      'meta.proposal.text':    '2026-09-28，7 个推理逻辑检查器都通过了一笔 WTI 多头以 77.20 平仓单，但付上亏损 3.0%。根本原因：服务于周二的新闻到周四时已不再有效（跨时间漂移）。7 个检查器都未考虑时间窗口上下文有效性。提出 E028：cross_temporal_consistency（跨时间一致性）—— 校验新闻年龄与决策时间的匹配度。已加入 v226 路线图。',

      /* === SECTION 6: case cards === */
      'case.badge.error':      '捕获错误',
      'case.badge.correct':    '未误报',
      'case.badge.meta':       '触发元迭代',
      'case.outcome':          '结果',
      'case.sources':          '信息源',
      'case.checks':           '检查',
      'case.severity':         '严重级',
      'case.reflection':       '反思输出',
      'case.correct.note':     '✓ 反向交易对识别正确 — 无误报',
      'case.meta.note':        '⚠ 7 个检查器都通过 — 但交易亏损',

      /* === SECTION 7: modal === */
      'modal.title':           '案例详情',
      'modal.outcome':         '结果',
      'modal.decision':        '决策',
      'modal.reflection':      '反思输出',
      'modal.system_learning': '系统学习',
      'modal.no_errors':       '27 层反思未捕获任何错误。',

      /* === SECTION 8: footer === */
      'footer.text':           'Reflex Quant v225 — 仅供展示。所有数据均为示例。生产环境访问需 NDA 与合作协议。联系：',

      /* knowledge graph layer descriptions */
      'kg.event_logic.desc':   '事件提取模式',
      'kg.causal.desc':        '因果链已确认',
      'kg.reasoning_logic.desc':'子层：假设、反事实、校准',
      'kg.sample_integrity.desc':'多源验证规则',
      'kg.cross_asset.desc':   '反向对、相关性聚类',
      'kg.meta_patterns.desc': '元迭代运行发现的盲点',
    },
  };

  /* === RUNTIME === */
  function detectInitialLang() {
    var stored;
    try { stored = localStorage.getItem('rq-lang'); } catch(e) { stored = null; }
    if (stored && STRINGS[stored]) return stored;
    var nav = (navigator.language || navigator.userLanguage || 'en').toLowerCase();
    if (nav.indexOf('zh') === 0) return 'zh';
    return 'en';
  }

  var state = { lang: detectInitialLang(), listeners: [] };

  function t(key) {
    var dict = STRINGS[state.lang] || STRINGS.en;
    return (key in dict) ? dict[key] : (STRINGS.en[key] || key);
  }

  function apply() {
    var dict = STRINGS[state.lang] || STRINGS.en;
    var nodes = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) {
      var k = nodes[i].getAttribute('data-i18n');
      if (k in dict) nodes[i].textContent = dict[k];
    }
    var ariaNodes = document.querySelectorAll('[data-i18n-aria]');
    for (var j = 0; j < ariaNodes.length; j++) {
      var ka = ariaNodes[j].getAttribute('data-i18n-aria');
      if (ka in dict) ariaNodes[j].setAttribute('aria-label', dict[ka]);
    }
    var titleEl = document.querySelector('[data-i18n-attr="title:meta.title"]');
    if (titleEl && 'meta.title' in dict) titleEl.textContent = dict['meta.title'];
    document.documentElement.setAttribute('lang', state.lang);
    for (var n = 0; n < state.listeners.length; n++) {
      try { state.listeners[n](state.lang); } catch(e) {}
    }
  }

  function setLang(lang) {
    if (!STRINGS[lang]) return;
    state.lang = lang;
    try { localStorage.setItem('rq-lang', lang); } catch(e) {}
    apply();
  }

  function toggle() { setLang(state.lang === 'en' ? 'zh' : 'en'); }

  function onChange(fn) { state.listeners.push(fn); }

  window.i18n = {
    t: t, setLang: setLang, toggle: toggle, onChange: onChange, apply: apply,
    get lang() { return state.lang; }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
})();