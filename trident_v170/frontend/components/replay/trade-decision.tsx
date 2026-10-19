'use client'

// 交易决策说明：每条信号展示"下单 / 不下单"的原因链
// 规则：AI 结果为主——观望(HOLD)绝不下单；BUY/SELL 需证据闸门 + 策略积分都满足才建仓。
// 多品种：event-bus 按品种逐行渲染时传 assetLabel/tag（如 "XAU" / "副品种"），
// 在卡片内显示品种头；单品种场景不传，渲染与旧版完全一致（零回归）。

type Props = {
  action?: string | null
  score?: number | null
  entryPrice?: number | null
  noTradeReason?: string | null
  gateReason?: string | null
  // 品种头（可选）：多品种总览按品种分行时用
  assetLabel?: string | null
  tag?: string | null
  // 品种附加说明（可选）：副品种传 AI 摘要（reason），不传时零回归
  note?: string | null
}

export function TradeDecisionNote({ action, score, entryPrice, noTradeReason, gateReason, assetLabel, tag, note }: Props) {
  const a = (action || '').toUpperCase()
  if (!a || !['BUY', 'SELL', 'HOLD', 'FAILED'].includes(a)) return null

  // 品种头（可选）：显示在卡片内首行，主/副品种分行时一眼可辨
  const header =
    assetLabel || tag ? (
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        {assetLabel && (
          <span className="font-mono text-[10px] font-bold tracking-wide text-foreground/90">
            {assetLabel.toUpperCase()}
          </span>
        )}
        {tag && (
          <span className="rounded bg-secondary px-1.5 py-px font-mono text-[9px] font-bold uppercase tracking-wider text-muted-foreground">
            {tag}
          </span>
        )}
      </div>
    ) : null

  // ① AI 观望
  if (a === 'HOLD') {
    return (
      <div className="rounded border border-hold/40 bg-hold/10 px-2.5 py-1.5">
        {header}
        <div className="font-mono text-[10px] uppercase tracking-wider text-hold">⚪ 交易决策：AI 观望，不下单</div>
        <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
          系统规则：AI 结果为主——模型判断"观望"就不交易（分数/积分再强也不转方向）。
          该信号只作分析记录，不会建仓。
        </p>
        {note && <p className="mt-1 border-t border-border/20 pt-1 text-[10px] leading-relaxed text-muted-foreground/80">{note}</p>}
      </div>
    )
  }

  // ①-b 分析失败：模型未输出有效八段分析 → 不交易，只作「分析失败」记录
  if (a === 'FAILED') {
    return (
      <div className="rounded border border-short/40 bg-short/10 px-2.5 py-1.5">
        {header}
        <div className="font-mono text-[10px] uppercase tracking-wider text-short">⚠️ 交易决策：分析失败，不下单</div>
        <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
          系统规则：模型未输出有效八段分析，本条不做 AI 方向判断、也不建仓，仅记录为「分析失败」。
        </p>
        {note && <p className="mt-1 border-t border-border/20 pt-1 text-[10px] leading-relaxed text-muted-foreground/80">{note}</p>}
      </div>
    )
  }

  const traded = !!entryPrice && Number(entryPrice) > 0

  // ② 已建仓
  if (traded) {
    // 区分两种建仓路径（此前一律写"闸门通过"，而宽松模式下闸门实际未过，
    // 界面上会出现"闸门通过（证据互相矛盾…仅输出观望结论）"自相矛盾的文案）：
    //   · 闸门通过 → 严格模式正常建仓
    //   · 闸门未过 → 宽松模式（gate_enabled=0）跳过闸门建仓，闸门结论仅作标记
    const gateBlocked = !!gateReason && /闸门|低于|观望结论|矛盾/.test(gateReason)
    return (
      <div className="rounded border border-long/40 bg-long/10 px-2.5 py-1.5">
        {header}
        <div className="font-mono text-[10px] uppercase tracking-wider text-long">
          ✅ 交易决策：已建仓 {a}（分数 {score != null ? (score > 0 ? '+' : '') + Number(score).toFixed(2) : '—'}）
        </div>
        <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
          {gateBlocked ? (
            <>
              AI 方向明确（{a}）→ <b className="text-hold">宽松模式跳过闸门</b> → 真实行情可取 → 自动建仓
              <br />
              <span className="text-muted-foreground/80">闸门原结论（仅作标记，未拦截）：{gateReason}</span>
            </>
          ) : (
            <>AI 方向明确（{a}）→ 证据闸门通过（{gateReason || '证据充分'}）→ 策略积分匹配 → 真实行情可取 → 自动建仓</>
          )}
        </p>
        {note && <p className="mt-1 border-t border-border/20 pt-1 text-[10px] leading-relaxed text-muted-foreground/80">{note}</p>}
      </div>
    )
  }

  // ③ 未建仓（BUY/SELL 但被拦下）
  const reason = noTradeReason || gateReason || '未触发建仓条件'
  return (
    <div className="rounded border border-short/40 bg-short/10 px-2.5 py-1.5">
      {header}
      <div className="font-mono text-[10px] uppercase tracking-wider text-short">
        ⛔ 交易决策：未建仓（AI 方向 {a}，分数 {score != null ? (score > 0 ? '+' : '') + Number(score).toFixed(2) : '—'}）
      </div>
      <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">
        未建仓原因：{reason}
      </p>
      {note && <p className="mt-1 border-t border-border/20 pt-1 text-[10px] leading-relaxed text-muted-foreground/80">{note}</p>}
    </div>
  )
}
