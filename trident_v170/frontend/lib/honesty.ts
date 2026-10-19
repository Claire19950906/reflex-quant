/** 项目诚实宪法 — 前端边界条款 (Frontend Honesty Contract)
 *
 * v282.0+ 启用。与 backend/src_python/_honesty.py 配对的 TS 版真理源。
 *
 * ============================================================================
 * 第一条款 (Clause 1): 数据真实性
 * ============================================================================
 *  - 任何 unknown / missing / fetch-failed 必须用 null,绝不用 0 / "" / "—"(语义上 OK)
 *    假装有数让前端误展示为真实。
 *  - 24h 涨跌 / 价格变化 / 涨跌幅 / 收益率 在源是 null 时必须显示 "—"(灰字 + title),
 *    严禁显示 "+0.00%" / "0%" 这种假数据。
 *
 * ============================================================================
 * 第二条款 (Clause 2): 结论保守
 * ============================================================================
 *  - 信号方向、置信度、胜率必须基于真实样本,不接受 LLM 失败时 fallback 到假结论。
 *
 * ============================================================================
 * 如何引用
 * ============================================================================
 *  在任何「绝不能捏造」的边界处:
 *
 *      import { NEVER_FABRICATE_INVARIANT } from "@/lib/honesty";
 *      // 该模块绝不允许把 null 折叠成 0.00% / 假价格
 *
 * ============================================================================
 * 如何防止回归
 * ============================================================================
 *  tests/test_no_fabrication.py 会 grep 所有 .ts / .tsx,发现禁用模式 → test fail。
 */

/** 单一不变量 — import 此常量即承认这是不可违反的红线。 */
export const NEVER_FABRICATE_INVARIANT: string =
  "v282+: 绝不允许捏造数据/结论。失败必须返回 null,严禁用 0/'—'(语义OK)/placeholder 假装有数。详见 _honesty.py 第一/二/三条款。";

/** 前端显示「数据不可用」的占位符(语义上是诚实的,不是假数据)。 */
export const UNAVAILABLE_PLACEHOLDER: string = "—";

/** 用于 tooltip 提示用户为什么这里是 "—"。 */
export function unavailableTooltip(reason: string): string {
  return `数据不可用 — ${reason}`;
}

/** 类型守卫: 判断数值是否「真有数」(非 null 且非 NaN)。 */
export function isRealNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** 把可能为 null 的数值格式化为字符串;诚实显示 "—"。 */
export function formatPctOrDash(value: number | null | undefined, digits = 2): string {
  if (!isRealNumber(value)) return UNAVAILABLE_PLACEHOLDER;
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

/** 把可能为 null 的数值格式化为价格;诚实显示 "—"。 */
export function formatPriceOrDash(value: number | null | undefined): string {
  if (!isRealNumber(value)) return UNAVAILABLE_PLACEHOLDER;
  return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}