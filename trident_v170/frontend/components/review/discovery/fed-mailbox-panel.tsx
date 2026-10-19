'use client'

import { useMemo } from 'react'
import { Mail } from 'lucide-react'
import { useFedWatch } from '@/lib/contexts/fed-watch-context'
import { cn } from '@/lib/utils'

/**
 * v277 · 📧 美联储来信
 *
 * 把"美联储路径预期 + 异动 + 临近事件"自动翻译成 3 条人话邮件,
 * 每条邮件含: 主题(人话) + 正文(对市场影响) + 时间戳 + 鹰鸽标签。
 *
 * 设计目标:
 *   - 普通用户打开就能读懂"市场认为 Fed 要怎样 + 股票/债/汇/黄金/数字币会怎么动"
 *   - 不依赖任何技术指标,纯白话 + emoji
 *
 * 数据源:/api/discovery/fed-watch → current.expectation + adaptive_threshold + next_fomc + events_7d
 * 60s 轮询。
 */

type FedWatchResponse = {
  ok: boolean
  ts: number
  current: {
    expectation?: {
      direction?: 'hawkish' | 'dovish' | 'neutral'
      spread_bp?: number
      expected_24m_change_bp?: number
      narrative?: string
    }
    anomaly?: {
      is_anomaly: boolean
      severity?: 'low' | 'med' | 'high'
      message?: string
    }
  }
  adaptive_threshold?: {
    regime?: 'low_vol' | 'normal' | 'high_vol' | 'panic' | 'unknown'
    vix?: number | null
    note?: string
  }
  next_fomc?: {
    title: string
    event_time: number
    minutes_to: number
    star?: number
    forecast?: string
    previous?: string
  } | null
  events_7d?: Array<{
    title: string
    event_time: number
    minutes_to: number
    star: number
    impact: string
    forecast?: string
    previous?: string
    actual?: string
  }>
  explain?: string
  last_push?: { last_push_ts?: number | null; severity?: string }
}

type MailItem = {
  id: string
  tag: string
  from: string
  subject: string
  body: string
  directionWord: string
  urgent: boolean
  toneClass: string
  icon: string
  ts: number
}

const DIR_LABEL = {
  hawkish: { word: '市场看涨利率(预期会加息)', color: 'text-rose-700 dark:text-rose-300', tone: 'border-rose-500/50 bg-rose-500/5', icon: '🔴' },
  dovish: { word: '市场看跌利率(预期会降息)', color: 'text-emerald-700 dark:text-emerald-300', tone: 'border-emerald-500/50 bg-emerald-500/5', icon: '🟢' },
  neutral: { word: '市场预期利率不变', color: 'text-slate-700 dark:text-slate-300', tone: 'border-slate-500/50 bg-slate-500/5', icon: '⚖️' },
} as const

/**
 * 把专业事件标题 → 普通中文人话版
 * 设计原则:
 *   - 数字 + 普通人能脑补的类比,不堆砌 jargon
 *   - "强劲 → 加息" 这种二选一,直接说"如果数据比预期好,市场会更倾向于加息"
 */
function humanizeEventTitle(raw: string): string {
  if (/FOMC\s*Meeting\s*Minutes|美联储议息纪要|议息会议纪要/i.test(raw)) {
    return '美联储公布上次议息会议纪要'
  }
  if (/FOMC\s*Press\s*Conf|FOMC\s*新闻发布会|美联储新闻发布会|鲍威尔.*讲话|鲍尔.*讲话|鲍威尔记者会/i.test(raw)) {
    return '美联储主席鲍威尔召开记者会'
  }
  if (/Powell/i.test(raw)) {
    return '美联储主席鲍威尔公开讲话'
  }
  if (/FOMC\s*Statement|FOMC\s*利率决议|美联储利率决议|议息决议/i.test(raw)) {
    return '美联储公布最新利率决议'
  }
  if (/FOMC\s*Member|FOMC\s*委员|美联储委员/i.test(raw)) {
    return '美联储某位委员公开讲话'
  }
  if (/CPI/i.test(raw)) {
    return '美国 CPI 通胀数据公布(老百姓日常物价的涨跌)'
  }
  if (/PCE/i.test(raw)) {
    return '美国 PCE 通胀数据公布(美联储最看重的物价指标)'
  }
  if (/NFP|非农|Employment Situation/i.test(raw)) {
    return '美国非农就业数据公布(新增多少个就业岗位)'
  }
  if (/GDP/i.test(raw)) {
    return '美国 GDP 经济增长数据公布'
  }
  if (/PMI/i.test(raw)) {
    return '美国 PMI 制造业/服务业景气数据公布(高于 50 算扩张,低于 50 算萎缩)'
  }
  if (/PPI/i.test(raw)) {
    return '美国 PPI 工厂出厂价格数据公布(常领先 CPI)'
  }
  if (/失业率|Unemployment/i.test(raw)) {
    return '美国失业率数据公布'
  }
  if (/Retails? Sales|零售销售/i.test(raw)) {
    return '美国零售销售数据公布(老百姓买买买的力度)'
  }
  if (/ISM/i.test(raw)) {
    return '美国 ISM 制造业/服务业景气数据公布'
  }
  if (/JOLTS|职位空缺/i.test(raw)) {
    return '美国职位空缺数据公布(企业招工需求)'
  }
  if (/Consumer Confidence|消费者信心|密歇根/i.test(raw)) {
    return '美国消费者信心指数公布(老百姓对未来经济乐观不乐观)'
  }
  // 兜底:未知事件
  return raw
}

/**
 * 影响速查 — 把"鹰=加息 鸽=降息"翻译成一句人话:
 * "这次数据出来后,市场是会更担心加息还是更期待降息?"
 */
function impactForTitle(title: string): string {
  if (/FOMC\s*Meeting\s*Minutes|议息会议纪要/i.test(title)) {
    return '这份纪要记录了上次美联储官员讨论了什么 — 重点看他们对加息/降息的态度是更硬了还是更软了。如果出现"还会耐心观察"这种偏软的措辞,市场就开心;出现"准备进一步收紧"这种偏硬的措辞,市场就要跌。'
  }
  if (/FOMC\s*Statement|利率决议/i.test(title)) {
    return '这是美联储最正式的政策表态 — 直接告诉你利率是加还是不加。会后 30 分钟还有鲍威尔的记者会,记者会上的回答往往比决议本身更能撬动市场。'
  }
  if (/FOMC\s*Press\s*Conf|新闻发布会|鲍威尔|鲍尔/i.test(title)) {
    return '美联储主席鲍威尔亲口回答记者提问 — 这是整个金融市场波动最大的单一事件,经常几分钟内让股票/债券/黄金/汇率大起大落。听不懂英文也没关系,关注他的语气:如果显得"对降息不着急",市场就会跌;如果显得"对经济有点担心",市场就会涨。'
  }
  if (/Powell/i.test(title)) {
    return '美联储主席鲍威尔公开讲话 — 他说的每个词都会被放大解读。'
  }
  if (/FOMC\s*Member|美联储委员/i.test(title)) {
    return '美联储某位有投票权的委员发表个人看法 — 影响相对小,但能透露内部风向。'
  }
  if (/CPI/i.test(title)) {
    return '美国 CPI(老百姓日常物价)公布 — 这是判断"会不会加息"最关键的数据:实际数字比预期高 = 加息预期升温 = 股票/债/黄金跌;实际数字比预期低 = 降息预期升温 = 股票/债/黄金涨。'
  }
  if (/PCE/i.test(title)) {
    return '美国 PCE 物价数据公布 — 这是美联储自己最看重的通胀指标,反应比 CPI 更"剔除了油价等噪音"。逻辑和 CPI 一样:高 = 鹰(加息),低 = 鸽(降息)。'
  }
  if (/NFP|非农|Employment Situation/i.test(title)) {
    return '美国非农就业数据公布 — 新增多少个就业岗位。逻辑反直觉:数据太好=经济过热=加息预期升温=股票跌;数据太差=经济要凉=降息预期升温=股票涨。所以"差"有时候反而是"好消息"。'
  }
  if (/GDP/i.test(title)) {
    return '美国 GDP 经济增长数据公布 — 经济增长太快可能引发加息;增长太慢可能引发降息。一般温和增长(2-3%)市场最舒服。'
  }
  if (/PMI/i.test(title)) {
    return '美国 PMI 景气数据公布 — 高于 50 算经济在扩张,低于 50 算在萎缩。数字越高市场越担心加息,数字越低市场越期待降息。'
  }
  if (/PPI/i.test(title)) {
    return '美国 PPI 工厂出厂价格数据公布 — 这是上游原材料的涨价,经常领先 CPI 几个月。'
  }
  if (/失业率|Unemployment/i.test(title)) {
    return '美国失业率公布 — 失业率走高 = 经济不行 = 降息预期升温 = 股票短期可能涨(因为担心降息);但持续走高就是真衰退,反而要跌。'
  }
  if (/Retails? Sales|零售销售/i.test(title)) {
    return '美国零售销售数据公布 — 老百姓买买买花了多少钱。数据强 = 消费旺 = 加息预期;数据弱 = 消费冷 = 降息预期。'
  }
  if (/ISM/i.test(title)) {
    return '美国 ISM 景气数据公布 — 制造业 ISM 高于 50 = 在扩张,低于 50 = 在萎缩。逻辑和 PMI 一样。'
  }
  if (/JOLTS|职位空缺/i.test(title)) {
    return '美国职位空缺数据公布 — 企业还在拼命招人 = 经济热 = 加息预期;招不动了 = 经济冷 = 降息预期。'
  }
  if (/Consumer Confidence|消费者信心|密歇根/i.test(title)) {
    return '美国消费者信心指数公布 — 老百姓对未来经济乐观不乐观。太乐观可能反而担心加息;太悲观可能反而期待降息。'
  }
  return '美联储相关的经济数据 — 实际数字比预期高,市场就担心加息(资产跌);实际数字比预期低,市场就期待降息(资产涨)。'
}

function hoursFromMin(min: number): string {
  if (min < 0) return '已发生'
  if (min < 60) return `${min}m 后`
  const h = min / 60
  if (h < 48) return `${h.toFixed(1)}h 后`
  return `${(h/24).toFixed(1)}d 后`
}

function buildMailbox(data: FedWatchResponse): MailItem[] {
  const exp = data.current.expectation || {}
  const anomaly = data.current.anomaly || { is_anomaly: false }
  const direction = (exp.direction || 'neutral') as 'hawkish' | 'dovish' | 'neutral'
  const meta = DIR_LABEL[direction]
  const change24m = exp.expected_24m_change_bp ?? 0
  const steps = Math.abs(change24m / 25)
  const vix = data.adaptive_threshold?.vix
  const regimeWord = {
    low_vol: '市场低波动',
    normal: '市场正常波动',
    high_vol: '市场高波动',
    panic: '市场恐慌',
    unknown: '市场平静',
  }[data.adaptive_threshold?.regime || 'unknown']
  const severityWord = { low: '轻微', med: '中等', high: '严重' }[anomaly.severity || 'low'] || '中等'

  const mails: MailItem[] = []

  // 邮件 1: 当前路径状态
  mails.push({
    id: 'mail-1',
    tag: '当前利率预期',
    from: '美联储利率监测 <no-reply@fed.gov>',
    subject:
      direction === 'hawkish'
        ? `📈 市场预期美联储要加息 ${steps.toFixed(0)} 次,总共上调约 ${(change24m / 100).toFixed(2)}%`
        : direction === 'dovish'
          ? `📉 市场预期美联储要降息 ${steps.toFixed(0)} 次,总共下调约 ${(Math.abs(change24m) / 100).toFixed(2)}%`
          : '⚖️ 市场预期美联储利率保持不变',
    body:
      direction === 'hawkish'
        ? `一句话:市场觉得经济太热了,美联储还得踩刹车。\n\n对你的影响:\n  • 美元:更值钱(外国钱会回流美国)\n  • 银行存款理财:利息变高(好消息)\n  • 房贷/信用贷:还款压力变大(坏消息)\n  • 股票:整体承压,尤其科技股/小盘股\n  • 黄金/比特币:承压(因为钱跑去银行赚利息了)\n  • 债券基金:会跌一阵(新发的利息比老的高,老的不值钱)`
        : direction === 'dovish'
          ? `一句话:市场觉得经济要凉了,美联储要开始放水救市。\n\n对你的影响:\n  • 美元:走弱(外国钱没那么急着来美国)\n  • 银行存款理财:利息变低(坏消息)\n  • 房贷/信用贷:还款压力变小(好消息)\n  • 股票:整体利好,尤其科技股/小盘股涨得最猛\n  • 黄金/比特币:利好(钱没地方去就会去这些)\n  • 债券基金:涨(老债券变得更值钱)`
          : `一句话:美联储暂时按兵不动,既不加也不降。\n\n对你的影响:\n  • 宏观层面:大方向没有,看个股和行业自身逻辑\n  • 该看什么:公司财报、地缘政治、油价、美元指数等微观事件\n  • 适合什么策略:中长期定投或选股,不折腾短线`,
    directionWord: meta.word,
    urgent: direction === 'hawkish' || direction === 'dovish',
    toneClass: meta.tone,
    icon: meta.icon,
    ts: data.ts,
  })

  // 邮件 2: 异动 / VIX 联动预警
  if (anomaly.is_anomaly) {
    mails.push({
      id: 'mail-2',
      tag: '异常波动',
      from: '美联储监测 <alert@fed.gov>',
      subject: `🚨 警报:美国短期国债出现异常波动(${severityWord}级别)`,
      body: `发生了什么:美国 2 年期国债(短期借贷利率的风向标)24 小时内波动异常,说明市场正在重新评估美联储下一步动作。\n\n${anomaly.message ? `可能原因:${anomaly.message}\n\n` : ''}${vix != null ? `当前市场恐慌指数(VIX):${vix.toFixed(1)} ${vix > 30 ? '(偏高,大家都紧张)' : vix > 20 ? '(中等)' : '(平静)'}\n\n` : ''}建议:接下来几小时密切留意新闻,可能有大行情。`,
      directionWord: meta.word,
      urgent: true,
      toneClass: 'border-rose-500/70 bg-rose-500/10',
      icon: '🚨',
      ts: data.ts - 1,
    })
  } else if (data.adaptive_threshold?.regime === 'panic') {
    mails.push({
      id: 'mail-2',
      tag: '恐慌预警',
      from: '美联储监测 <alert@fed.gov>',
      subject: `😱 当前市场处于恐慌状态,美联储利率预期会被放大`,
      body: `当前恐慌指数(VIX)=${vix?.toFixed(1)} —— 意味着大家都非常紧张,股市避险情绪强烈。\n\n这会怎样:平时不起眼的小波动,现在会被市场放大解读,可能引发连锁反应。\n\n${data.adaptive_threshold.note ? `原因:${data.adaptive_threshold.note}\n\n` : ''}建议:这个时候最容易亏钱也最容易赚大钱,新手建议离场观望。`,
      directionWord: meta.word,
      urgent: true,
      toneClass: 'border-amber-500/70 bg-amber-500/10',
      icon: '😱',
      ts: data.ts - 1,
    })
  } else {
    mails.push({
      id: 'mail-2',
      tag: '平稳',
      from: '美联储监测 <alert@fed.gov>',
      subject: '😌 美联储利率预期平稳,没有什么大变化',
      body: `${regimeWord} —— 没有什么需要特别关注的事情。\n\n${data.adaptive_threshold?.note ? `${data.adaptive_threshold.note}\n\n` : ''}${vix != null ? `当前市场恐慌指数(VIX):${vix.toFixed(1)} ${vix > 30 ? '(偏高)' : vix > 20 ? '(中等)' : '(平静)'}\n\n` : ''}建议:该干嘛干嘛,不用一直盯盘。`,
      directionWord: meta.word,
      urgent: false,
      toneClass: 'border-slate-500/30 bg-slate-500/5',
      icon: '😌',
      ts: data.ts - 1,
    })
  }

  // 邮件 3: 临近事件
  const next = data.next_fomc
  const nextEvent = next || (data.events_7d && data.events_7d[0])
  if (nextEvent) {
    const isFive = (nextEvent.star ?? 0) >= 5
    const starText = isFive ? '5 星·特别重要' : `${nextEvent.star ?? 0} 星`
    const humanTitle = humanizeEventTitle(nextEvent.title)
    mails.push({
      id: 'mail-3',
      tag: '日程提醒',
      from: '美联储日程 <calendar@fed.gov>',
      subject: `🗓 ${hoursFromMin(nextEvent.minutes_to)}后:${humanTitle}(${starText})`,
      body: `${impactForTitle(nextEvent.title)}\n\n${
        nextEvent.forecast || nextEvent.previous
          ? `数据预期:市场预测是 "${nextEvent.forecast || '—'}",上次实际是 "${nextEvent.previous || '—'}"。\n实际数字比预期高 = 加息预期升温 = 资产可能跌;比预期低 = 降息预期升温 = 资产可能涨。\n\n`
          : ''
      }对普通人的建议:${isFive ? '这种重大事件前后 30 分钟最好别操作,行情太刺激容易做错方向。' : '关注一下就好,不用太紧张。'}`,
      directionWord: meta.word,
      urgent: isFive,
      toneClass: isFive ? 'border-rose-500/70 bg-rose-500/10' : 'border-sky-500/40 bg-sky-500/5',
      icon: isFive ? '🌟' : '🗓',
      ts: nextEvent.event_time,
    })
  } else {
    mails.push({
      id: 'mail-3',
      tag: '日程提醒',
      from: '美联储日程 <calendar@fed.gov>',
      subject: '🗓 未来一周没有重要美联储事件',
      body: '本周美联储"放假"—— 没有重要数据或官员讲话。\n\n这段时间市场通常比较平淡,关注:\n  • 个股公司财报\n  • 其他国家央行(欧央行/日本央行)\n  • 油价/地缘政治\n  • 行业自身利基事件',
      directionWord: meta.word,
      urgent: false,
      toneClass: 'border-slate-500/30 bg-slate-500/5',
      icon: '🗓',
      ts: data.ts + 86400,
    })
  }

  return mails
}

export function FedMailboxPanel() {
  // v283+ · 改用共享 FedWatchContext,与 fed-watch-panel 共用同一份 60s 轮询。
  // 解决 #2「双组件同接口重复请求」。本组件不再自己拉,只读 Provider 的 state。
  const fedWatch = useFedWatch<FedWatchResponse>()
  const data = fedWatch.data
  const loading = fedWatch.loading
  const err = fedWatch.error

  const mails = useMemo(() => (data ? buildMailbox(data) : []), [data])
  const direction = (data?.current.expectation?.direction ?? 'neutral') as 'hawkish' | 'dovish' | 'neutral'
  const dirMeta = DIR_LABEL[direction]

  if (!data) {
    return (
      <section
        id="disc-fedmailbox"
        aria-label="美联储来信"
        className="scroll-mt-3 surface-card border-slate-500/30 bg-slate-500/5 p-3"
      >
        <header className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-muted-foreground" aria-hidden />
          <h3 className="font-mono text-sm font-bold">📧 美联储来信</h3>
        </header>
        <div className="mt-2 text-[11px] text-muted-foreground">
          {loading ? '加载中…' : err ? `⚠️ ${err}` : '等待数据…'}
        </div>
      </section>
    )
  }

  return (
    <section
      id="disc-fedmailbox"
      aria-label="美联储来信"
      className={cn('scroll-mt-3 surface-card border-2 p-3', dirMeta.tone)}
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-muted-foreground" aria-hidden />
          <h3 className="font-mono text-sm font-bold">📧 美联储来信</h3>
          <span className={cn('rounded border px-1.5 py-0.5 font-mono text-[10px]', dirMeta.color, 'border-current/30')}>
            {dirMeta.icon} {dirMeta.word}
          </span>
        </div>
        <span className="font-mono text-[9px] text-muted-foreground/70">60s 自动刷新</span>
      </header>

      <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
        {data.explain || '把美联储的预期和动态,自动翻译成 3 封小白也能看懂的"信":① 现在市场觉得美联储要加息还是降息?② 最近有没有异常波动?③ 接下来会发生什么重要的事?每封信都会告诉你对你的钱袋子有什么影响。'}
      </p>

      <ul className="mt-2 space-y-1.5">
        {mails.map((m) => (
          <li
            key={m.id}
            className={cn(
              'rounded border bg-background/70 p-2',
              m.urgent ? 'border-rose-500/60 ring-1 ring-rose-500/30' : 'border-border'
            )}
          >
            <div className="flex items-start gap-2">
              <div className={cn(
                'flex h-8 w-8 shrink-0 items-center justify-center rounded border text-base',
                m.urgent ? 'border-rose-500/50 bg-rose-500/10' : 'border-border bg-secondary/40'
              )} aria-hidden>
                {m.icon}
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5 font-mono text-[9px] text-muted-foreground/80">
                  <span>{m.from}</span>
                  <span className={cn(
                    'rounded border px-1',
                    m.urgent
                      ? 'border-rose-500/40 bg-rose-500/15 text-rose-700 dark:text-rose-300'
                      : 'border-border bg-secondary/30 text-muted-foreground'
                  )}>
                    {m.tag}
                  </span>
                  <span className="ml-auto font-mono text-[9px] text-muted-foreground/60">
                    {hoursFromMin(Math.round((m.ts - data.ts) / 60))}
                  </span>
                </div>

                <div className={cn('mt-0.5 text-[12px] font-bold', m.urgent ? 'text-rose-700 dark:text-rose-300' : 'text-foreground')}>
                  {m.subject}
                </div>

                <div className="mt-1 whitespace-pre-wrap text-[11px] leading-5 text-foreground/85">
                  {m.body}
                </div>
              </div>
            </div>
          </li>
        ))}
      </ul>

      <footer className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[9px] text-muted-foreground/70">
        <span>📌 邮件生成规则在 buildMailbox() · 数据源 /api/discovery/fed-watch</span>
        {data.last_push?.last_push_ts && (
          <span>
            上次推送:{new Date(data.last_push.last_push_ts * 1000).toLocaleString('zh-CN')}
            {data.last_push.severity && (
              <span className="ml-1 text-rose-500">({data.last_push.severity})</span>
            )}
          </span>
        )}
      </footer>
    </section>
  )
}