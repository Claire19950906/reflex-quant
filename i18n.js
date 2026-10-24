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

      /* === PRICING PAGE === */
      'pricing.meta.title':       'Reflex Quant — Commercial License',
      'pricing.header.title':     'Reflex Quant — Commercial License',
      'pricing.header.sub':       'Pricing & terms for proprietary use · Last updated 2026-10-06',
      'pricing.lang.toggle.aria': 'Switch language to Chinese',

      'pricing.section.why':      'Why a commercial license?',
      'pricing.section.why.p1':   'Reflex Quant is dual-licensed. The core code is AGPL-3.0, which means anyone who forks and modifies it must release their changes under the same license. This protects the project from being quietly absorbed into closed-source products.',
      'pricing.section.why.p2':   'If you want to use Reflex Quant in a proprietary product, run it as a service without releasing your modifications, or embed it in a larger closed-source system — you need a commercial license from us.',

      'pricing.section.tiers':    'Pricing tiers',
      'pricing.section.calc':     'Pricing calculator',
      'pricing.section.included': "What's included in every tier",
      'pricing.section.terms':    'License terms (summary)',
      'pricing.section.terms.intro': 'The full commercial license agreement is a 12-page legal document. The key terms, in plain English:',
      'pricing.section.faq':      'FAQ',

      'pricing.tier.indie.name':  'Indie / Research',
      'pricing.tier.team.name':   'Team / Desk',
      'pricing.tier.firm.name':   'Firm / Institutional',
      'pricing.tier.custom.name': 'Custom / OEM',
      'pricing.tier.custom.price':"Let's talk",

      'pricing.tier.indie.f1':    'Up to 2 quants / researchers',
      'pricing.tier.indie.f2':    'Internal use only',
      'pricing.tier.indie.f3':    'No redistribution',
      'pricing.tier.indie.f4':    'No SaaS / managed offering',
      'pricing.tier.indie.f5':    'Email support',

      'pricing.tier.team.f1':     'Up to 10 quants',
      'pricing.tier.team.f2':     'Internal use + client reports',
      'pricing.tier.team.f3':     'No SaaS',
      'pricing.tier.team.f4':     'Custom checker development (4 / yr)',
      'pricing.tier.team.f5':     'Priority support',

      'pricing.tier.firm.f1':     'Unlimited quants at one site',
      'pricing.tier.firm.f2':     'Internal use + redistribution to clients',
      'pricing.tier.firm.f3':     'SaaS allowed for clients only',
      'pricing.tier.firm.f4':     'Unlimited custom checker dev',
      'pricing.tier.firm.f5':     'Dedicated support engineer',
      'pricing.tier.firm.f6':     'Source code escrow available',

      'pricing.tier.custom.f1':   'OEM / embedded use',
      'pricing.tier.custom.f2':   'Multi-site / global',
      'pricing.tier.custom.f3':   'White-label',
      'pricing.tier.custom.f4':   'On-prem / air-gapped',
      'pricing.tier.custom.f5':   'Custom SLAs',
      'pricing.tier.custom.f6':   'Volume discounts',

      'pricing.calc.label.tier':     'Tier',
      'pricing.calc.label.seats':    'Number of seats',
      'pricing.calc.label.term':     'Contract term',
      'pricing.calc.label.support':  'Support tier',

      'pricing.calc.tier.indie':  'Indie / Research ($5k/yr)',
      'pricing.calc.tier.team':   'Team / Desk ($25k/yr)',
      'pricing.calc.tier.firm':   'Firm / Institutional ($100k/yr)',
      'pricing.calc.tier.custom': 'Custom / OEM (contact us)',

      'pricing.calc.term.1': '1 year (standard)',
      'pricing.calc.term.2': '2 years (-10%)',
      'pricing.calc.term.3': '3 years (-20%)',

      'pricing.calc.support.email':     'Email (included)',
      'pricing.calc.support.priority':  'Priority (+15%)',
      'pricing.calc.support.dedicated': 'Dedicated engineer (+40%)',

      'pricing.estimate.label':       'Estimated annual cost',
      'pricing.estimate.cta':         'Contact sales',
      'pricing.estimate.custom':      'Custom',
      'pricing.estimate.custom.note': 'OEM / multi-site / white-label — direct conversation required',
      'pricing.calc.note':            'All prices in USD. Volume discounts available above 50 seats (firm tier). Custom / OEM tier requires direct conversation — no online estimate.',

      'pricing.table.feature': 'Feature',
      'pricing.table.col.indie':  'Indie',
      'pricing.table.col.team':   'Team',
      'pricing.table.col.firm':   'Firm',
      'pricing.table.col.custom': 'Custom',

      'pricing.table.row.core':      'Core 7-layer reflection engine',
      'pricing.table.row.meta':      'Meta-loop (system self-reflection)',
      'pricing.table.row.kg':        'Knowledge graph persistence',
      'pricing.table.row.source':    'Source code access',
      'pricing.table.row.mod':       'Proprietary modifications allowed',
      'pricing.table.row.internal':  'Internal use',
      'pricing.table.row.redist':    'Redistribution to clients',
      'pricing.table.row.saas':      'SaaS offering',
      'pricing.table.row.whitelabel':'White-label / OEM',
      'pricing.table.row.custom':    'Custom checker development',
      'pricing.table.row.escrow':    'Source code escrow',
      'pricing.table.row.reports_only': 'Reports only',
      'pricing.table.row.clients_only': 'Clients only',
      'pricing.table.row.optional':     'Optional',
      'pricing.table.row.four_per_year':'4 / year',
      'pricing.table.row.unlimited':    'Unlimited',

      'pricing.terms.term':       'Term',
      'pricing.terms.usecase':    'Use cases',
      'pricing.terms.usecase.body':'covered uses are listed in your tier. Anything not listed is not licensed.',
      'pricing.terms.mod':        'Modifications',
      'pricing.terms.mod.body':   "all proprietary modifications remain your IP. We don't ask for assignment.",
      'pricing.terms.conf':       'Confidentiality',
      'pricing.terms.conf.body':  'we keep your use confidential unless you opt in to a public reference.',
      'pricing.terms.support':    'Support',
      'pricing.terms.support.body':'response SLAs vary by support tier. Dedicated engineer tier includes a quarterly on-site (or video) review.',
      'pricing.terms.term2':      'Termination',
      'pricing.terms.term2.body': "either party can terminate at the end of term. No refunds on early termination (you keep what's already licensed for the term you paid for).",

      'pricing.terms.term1.body': '1, 2, or 3 years. Auto-renews unless cancelled 30 days before expiry.',

      'pricing.faq.q1': 'Can I evaluate before buying?',
      'pricing.faq.a1': "Yes — the Reflex Quant showcase is a live, clickable demo with synthetic data. No account needed, runs offline in your browser.",
      'pricing.faq.q2': "What's the difference between AGPL-3.0 and the commercial license?",
      'pricing.faq.a2': 'AGPL-3.0: free, but you must release your modifications under AGPL-3.0 if you deploy as a network service. Commercial: paid, no release requirement.',
      'pricing.faq.q3': 'Do you offer academic pricing?',
      'pricing.faq.a3': 'Yes — for accredited universities and not-for-profit research, the Indie tier is waived. Email us with your institution.',
      'pricing.faq.q4': 'Can I switch tiers mid-contract?',
      'pricing.faq.a4': 'Upgrades yes (prorated), downgrades at term renewal only.',
      'pricing.faq.q5': 'What about consulting / custom integration?',
      'pricing.faq.a5': 'Custom / OEM tier only. Starts at $50k for a 6-week engagement.',

      'pricing.contact.text':    'Questions?',
      'pricing.contact.email':   'z2132743607@163.com',
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

      /* === PRICING PAGE (zh) === */
      'pricing.meta.title':       'Reflex Quant — 商业授权',
      'pricing.header.title':     'Reflex Quant — 商业授权',
      'pricing.header.sub':       '专有用途的定价与条款 · 最后更新 2026-10-06',
      'pricing.lang.toggle.aria': '切换语言为英文',

      'pricing.section.why':      '为什么需要商业授权？',
      'pricing.section.why.p1':   'Reflex Quant 采用双重授权。核心代码为 AGPL-3.0，即任何 fork 并修改者必须在同一协议下发布其修改。这保护项目不被悄悄并入闭源产品。',
      'pricing.section.why.p2':   '若你需要在专有产品中使用 Reflex Quant、或在不发版的情况下作为服务运行、或嵌入更大的闭源系统 — 则需要我们的商业授权。',

      'pricing.section.tiers':    '价格档位',
      'pricing.section.calc':     '价格计算器',
      'pricing.section.included': '每档包含内容',
      'pricing.section.terms':    '授权条款（摘要）',
      'pricing.section.terms.intro': '完整商业授权协议为 12 页法律文件。以下为通俗版本的关键条款：',
      'pricing.section.faq':      '常见问题',

      'pricing.tier.indie.name':  '个人 / 研究',
      'pricing.tier.team.name':   '团队 / 交易台',
      'pricing.tier.firm.name':   '公司 / 机构',
      'pricing.tier.custom.name': '定制 / OEM',
      'pricing.tier.custom.price':'面议',

      'pricing.tier.indie.f1':    '最多 2 名量化 / 研究员',
      'pricing.tier.indie.f2':    '仅供内部使用',
      'pricing.tier.indie.f3':    '不可再分发',
      'pricing.tier.indie.f4':    '不可作为 SaaS / 托管服务',
      'pricing.tier.indie.f5':    '邮件支持',

      'pricing.tier.team.f1':     '最多 10 名量化',
      'pricing.tier.team.f2':     '内部使用 + 客户报告',
      'pricing.tier.team.f3':     '不可作为 SaaS',
      'pricing.tier.team.f4':     '定制检查器开发 (4 / 年)',
      'pricing.tier.team.f5':     '优先支持',

      'pricing.tier.firm.f1':     '单站点不限量化人数',
      'pricing.tier.firm.f2':     '内部使用 + 向客户再分发',
      'pricing.tier.firm.f3':     '仅可向客户提供 SaaS',
      'pricing.tier.firm.f4':     '不限定制检查器开发',
      'pricing.tier.firm.f5':     '专属支持工程师',
      'pricing.tier.firm.f6':     '可提供源代码托管',

      'pricing.tier.custom.f1':   'OEM / 嵌入式使用',
      'pricing.tier.custom.f2':   '多站点 / 全球',
      'pricing.tier.custom.f3':   '白标',
      'pricing.tier.custom.f4':   '本地部署 / 物理隔离',
      'pricing.tier.custom.f5':   '定制 SLA',
      'pricing.tier.custom.f6':   '批量折扣',

      'pricing.calc.label.tier':     '档位',
      'pricing.calc.label.seats':    '席位数量',
      'pricing.calc.label.term':     '合同期限',
      'pricing.calc.label.support':  '支持级别',

      'pricing.calc.tier.indie':  '个人 / 研究 ($5k/年)',
      'pricing.calc.tier.team':   '团队 / 交易台 ($25k/年)',
      'pricing.calc.tier.firm':   '公司 / 机构 ($100k/年)',
      'pricing.calc.tier.custom': '定制 / OEM（联系我们）',

      'pricing.calc.term.1': '1 年（标准）',
      'pricing.calc.term.2': '2 年 (-10%)',
      'pricing.calc.term.3': '3 年 (-20%)',

      'pricing.calc.support.email':     '邮件（已含）',
      'pricing.calc.support.priority':  '优先 (+15%)',
      'pricing.calc.support.dedicated': '专属工程师 (+40%)',

      'pricing.estimate.label':       '预估年度费用',
      'pricing.estimate.cta':         '联系销售',
      'pricing.estimate.custom':      '定制',
      'pricing.estimate.custom.note': 'OEM / 多站点 / 白标 — 需直接沟通',
      'pricing.calc.note':            '所有价格以美元计。50 席位以上（公司档）可享批量折扣。定制 / OEM 档需直接沟通，不提供在线报价。',

      'pricing.table.feature': '功能',
      'pricing.table.col.indie':  '个人',
      'pricing.table.col.team':   '团队',
      'pricing.table.col.firm':   '公司',
      'pricing.table.col.custom': '定制',

      'pricing.table.row.core':      '核心 7 层反思引擎',
      'pricing.table.row.meta':      '元迭代（系统自反思）',
      'pricing.table.row.kg':        '知识图谱持久化',
      'pricing.table.row.source':    '源代码访问',
      'pricing.table.row.mod':       '允许专有修改',
      'pricing.table.row.internal':  '内部使用',
      'pricing.table.row.redist':    '向客户再分发',
      'pricing.table.row.saas':      'SaaS 服务',
      'pricing.table.row.whitelabel':'白标 / OEM',
      'pricing.table.row.custom':    '定制检查器开发',
      'pricing.table.row.escrow':    '源代码托管',
      'pricing.table.row.reports_only': '仅限报告',
      'pricing.table.row.clients_only': '仅限客户',
      'pricing.table.row.optional':     '可选',
      'pricing.table.row.four_per_year':'4 / 年',
      'pricing.table.row.unlimited':    '不限',

      'pricing.terms.term':       '期限',
      'pricing.terms.usecase':    '使用场景',
      'pricing.terms.usecase.body':'已覆盖场景见你所选档位，未列出的场景不在授权范围内。',
      'pricing.terms.mod':        '修改',
      'pricing.terms.mod.body':   '所有专有修改仍归你所有。我们不要求转让。',
      'pricing.terms.conf':       '保密',
      'pricing.terms.conf.body':  '未经你同意，我们对你的使用情况保密，不主动公开。',
      'pricing.terms.support':    '支持',
      'pricing.terms.support.body':'响应 SLA 因支持级别而异。专属工程师级别包含每季度一次现场（或视频）评审。',
      'pricing.terms.term2':      '终止',
      'pricing.terms.term2.body': '任一方可在期限届满时终止。提前终止不退款（已付期限内的授权继续有效）。',

      'pricing.terms.term1.body': '1、2 或 3 年。除非在到期前 30 天取消，否则自动续约。',

      'pricing.faq.q1': '购买前可以试用吗？',
      'pricing.faq.a1': '可以 — Reflex Quant showcase 是一个可点击的实时演示，使用合成数据。无需账号，浏览器内可离线运行。',
      'pricing.faq.q2': 'AGPL-3.0 与商业授权有什么区别？',
      'pricing.faq.a2': 'AGPL-3.0：免费，但若以网络服务方式部署则必须以 AGPL-3.0 发布你的修改。商业授权：付费，无发布要求。',
      'pricing.faq.q3': '提供学术优惠吗？',
      'pricing.faq.a3': '是的 — 对于经认证的大学和非营利研究机构，个人档免费。请发邮件并附上机构信息。',
      'pricing.faq.q4': '可以在合同期内切换档位吗？',
      'pricing.faq.a4': '升级可以（按比例计费），降级仅在续约时允许。',
      'pricing.faq.q5': '咨询 / 定制集成呢？',
      'pricing.faq.a5': '仅限定制 / OEM 档。起步价 $50k，6 周合作。',

      'pricing.contact.text':    '有问题？',
      'pricing.contact.email':   'z2132743607@163.com',
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