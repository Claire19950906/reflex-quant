#!/usr/bin/env python3
"""
Trident Agent MVP — Central Configuration
==========================================

Single entry point for environment loading, API credentials, DB path and
tuning thresholds. Both engine.py and api_server.py import this module
(plain `import config` — backend/src_python is on sys.path at runtime).

.env is loaded exactly once, here, from backend/.env (parent of src_python/).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & .env
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/


def _under_test() -> bool:
    """是否跑在 pytest 里（测试进程不该被生产 .env 带着漂）。"""
    if os.getenv("TRIDENT_TEST_MODE"):
        return True
    try:
        import sys as _sys
        return "pytest" in _sys.modules or bool(os.getenv("PYTEST_CURRENT_TEST"))
    except Exception:
        return False


# v476：测试进程不读生产 .env。
# 事故：backend/.env 是把真实账户口径调上去的那份（MAX_GROSS_LEVERAGE_PCT=200、
# MAX_ASSET_WEIGHT_PCT=60 等），而 config import 时 load_dotenv 会把它灌进
# os.environ → 所有用例都在"生产参数"下跑。口径类用例（组合闸 / 结算 / 风控）
# 本来按代码默认值 50% / 100% 写的断言，于是整片整片地算不出"撞上限"。
# 测试进程一律走代码默认值，需要特定参数的用例自己 monkeypatch.setenv。
if not _under_test():
    load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH = os.getenv("TRIDENT_DB_PATH") or os.path.join(BASE_DIR, "trident_event_bus.db")
PROJECT_DIR = os.path.dirname(BASE_DIR)

# ── 内部服务地址（P2-5：本地回环端口集中到这里，env 可覆盖）──────────────────
# 这些都是本机回环服务；散落硬编码会导致改端口要满仓搜替换。统一从这里取。
LOCAL_HOST = os.getenv("TRIDENT_LOCAL_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("TRIDENT_BRIDGE_PORT", "8100"))
ENGINE_WEBHOOK_PORT = int(os.getenv("TRIDENT_ENGINE_WEBHOOK_PORT", "9000"))
MASTER_PORT = int(os.getenv("TRIDENT_MASTER_PORT", "9001"))
FRONTEND_PORT = int(os.getenv("TRIDENT_FRONTEND_PORT", "3030"))
BRIDGE_BASE_URL = os.getenv("TRIDENT_BRIDGE", f"http://{LOCAL_HOST}:{BRIDGE_PORT}")
ENGINE_WEBHOOK_URL = os.getenv("TRIDENT_ENGINE_WEBHOOK_URL",
                               f"http://{LOCAL_HOST}:{ENGINE_WEBHOOK_PORT}")
MASTER_BASE_URL = os.getenv("TRIDENT_MASTER_URL",
                            f"http://{LOCAL_HOST}:{MASTER_PORT}")

# ── v452：版本号的单一真值源 ─────────────────────────────────────────────
# 事故：CHANGELOG 已写到 v451，而 `/api/health` 写死 `v398`、8100 bridge 写死
# `v308-p1` —— 探针看到的"版本"和实际代码差 50 多版，出问题时没人能靠它对账。
# 现在两个 server 都读这里；发版时只改这一行 + CHANGELOG 首行，
# `tests/test_v452_version_single_source.py` 会比对两者，漂了就红。
# 现在两个 server 都读这里；发版时只改 CHANGELOG 首行，
# `tests/test_v452_version_single_source.py` 会比对两者，漂了就红。
def _read_changelog_version() -> str:
    """从 CHANGELOG 首行取 `vNNN`，作为版本号的唯一真值源。

    v452 二次：原来这里是手写常量，并发开发时每天要追着 CHANGELOG 改
    （v451 → v460 → v461 已连漂两次）。改成直接读首行，发版只写 CHANGELOG 一处。
    """
    try:
        path = os.path.join(PROJECT_DIR, "CHANGELOG.md")
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                m = re.search(r"(v\d+(?:\.\d+)?)", line)
                if m:
                    return m.group(1)
                break
    except Exception:
        pass
    return "unknown"


CODE_VERSION = _read_changelog_version()
TZ_SHANGHAI = timezone(timedelta(hours=8))
# v309 P1: time_utils.BJ 是全项目唯一时区口径常量, 与上面等价。
# 这里保持 TZ_SHANGHAI 的名字/行为不变 (大量代码在用), 但显式说明二者同源,
# 避免以后有人只改一处导致 8 小时偏差。
# (time_utils 刻意不 import config —— config 会 load_dotenv, 依赖太重;
#  反过来 config 局部 import time_utils, 避免循环依赖。)

# ── v230+ : 跨平台路径 env 化 ─────────────────────────────────────────────
# 设计: 默认走 macOS 老路径 (向后兼容), Windows / Docker 部署时通过 env 覆盖。
#   TRIDENT_HOME   — 所有 ~/.trident/* 子目录的根 (logs, config, exports)
#   TRIDENT_LOG_DIR — 仅覆盖日志目录 (log / cron.log / *.csv backup)
#   TRIDENT_DB_PATH — 仅覆盖主 DB (见 DB_PATH 上方, 2024 已支持)
#
# 例:
#   Windows:   set TRIDENT_HOME=C:\trident-data
#   Docker:    TRIDENT_HOME=/var/lib/trident
#   测试隔离:  TRIDENT_LOG_DIR=/tmp/trident-test/log
TRIDENT_HOME = os.getenv("TRIDENT_HOME") or os.path.join(os.path.expanduser("~"), ".trident")
LOG_DIR = os.getenv("TRIDENT_LOG_DIR") or os.path.join(TRIDENT_HOME, "log")


# ── v200+: 模拟盘默认开启 ─────────────────────────────────────────────────
# 启动时（api_server lifespan / engine 入口 / init_db）会自动检查 paper_trading_settings，
# 若 is_running=0 且从未主动停过（started_at 为空），自动翻 1 并补建 RUNNING 行。
# 这样反思 / 经验 / 日报三大链路默认就有数据，无需手动开。
# 排查 / 演示 / 回测场景可设 PAPER_TRADING_AUTO_START=0 关闭此自动行为。
PAPER_TRADING_AUTO_START = os.getenv("PAPER_TRADING_AUTO_START", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
# 默认开启的赛道（全 4 类，crypto/gold/oil/stocks）
PAPER_TRADING_DEFAULT_TRACKS = ("crypto", "gold", "oil", "stocks")


# ── 统一时间解析（全项目唯一入口：一律换算/解读为北京时间）────────────────
# 平台返回的发布时间格式/时区五花八门，任何"时间差/新鲜度/年龄"计算都必须
# 先过这里，避免把 UTC 无时区串当北京时间而差 8 小时。
# 约定：带时区（+08:00 / Z / ±HH:MM）→ 换算到北京；不带时区 → 视为已是北京时间。

def parse_time_to_bj(value: Any) -> Optional[datetime]:
    """解析平台时间串 → 北京时间 aware datetime；无法解析返回 None。

    支持：
      - ISO 带时区（+08:00 / Z / ±HH:MM）→ 换算到北京
      - 无时区 ISO / 'YYYY-MM-DD HH:MM:SS' → 视为已是北京时间
      - epoch 秒 / 毫秒（10 或 13 位纯数字）→ UTC 纪元 → 换算到北京

    v309 P1: 实现已下沉到 time_utils.parse_ts_to_bj()（全项目时间口径唯一入口）,
    本函数保留为薄封装, 避免项目里存在**两套**时间解析逻辑导致口径漂移。
    """
    from common import time_utils  # 局部导入: 避免 config ←→ time_utils 循环依赖
    return time_utils.parse_ts_to_bj(value)


def fmt_bj(value: Any) -> str:
    """平台时间串 → 统一北京时间字符串 'YYYY-MM-DD HH:MM:SS'（无时区=北京语义）；解析失败返回 ''。"""
    dt = parse_time_to_bj(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else ""

# ── File upload ────────────────────────────────────────────────
UPLOAD_DIR = os.path.abspath(os.getenv(
    "UPLOAD_DIR", os.path.join(PROJECT_DIR, "backend", "uploads")
))
MAX_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
# 收紧：移除 text/html / text/csv（防 Stored XSS + CSV formula injection）
# 仅允许图片/PDF/纯文本，CSV 用户改用上传后前端解析或数据导入端点
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf",
    "text/plain",
}


# ── Per-IP rate limit (P2 安全加固) ─────────────────────────────────────
# 滑动窗口 IP 限流；in-memory 单进程；多进程 / 多实例需升级到 Redis。
# 设 0 = 关闭。
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
RATE_LIMIT_AGENT_CHAT_PER_MIN = int(os.getenv("RATE_LIMIT_AGENT_CHAT_PER_MIN", "60"))
RATE_LIMIT_UPLOAD_PER_MIN = int(os.getenv("RATE_LIMIT_UPLOAD_PER_MIN", "10"))
# 鉴权用户 (带 X-Trident-API-Key) 的额外倍数；用于 admin / CI 不被误伤
RATE_LIMIT_AUTH_MULTIPLIER = float(os.getenv("RATE_LIMIT_AUTH_MULTIPLIER", "10.0"))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Binance Futures trading (USE_TESTNET defaults to False: real trading)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_USE_TESTNET = _env_bool("BINANCE_USE_TESTNET", False)
BINANCE_NOTIONAL_USDT = float(os.getenv("BINANCE_NOTIONAL_USDT", "5000"))
BINANCE_LEVERAGE = int(os.getenv("BINANCE_LEVERAGE", "5"))
BINANCE_SIGNAL_THRESHOLD = float(os.getenv("BINANCE_SIGNAL_THRESHOLD", "0.0"))
BINANCE_TRAILING_CALLBACK_RATE = float(os.getenv("BINANCE_TRAILING_CALLBACK_RATE", "0.5"))
BINANCE_NEWS_API_URL = os.getenv(
    "BINANCE_NEWS_API_URL",
    f"http://{LOCAL_HOST}:{BRIDGE_PORT}/api/trading/latest"
).strip()
# 黄金 ETF 持仓 CSV：默认 SPDR 官网（部分网络环境需在 .env 覆盖为镜像/代理地址）
GOLD_ETF_CSV_URL = os.getenv(
    "GOLD_ETF_CSV_URL",
    "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv",
).strip()
BINANCE_POLL_INTERVAL = float(os.getenv("BINANCE_POLL_INTERVAL", "2"))
BINANCE_REQUEST_TIMEOUT = float(os.getenv("BINANCE_REQUEST_TIMEOUT", "5"))
BINANCE_STATE_PATH = os.path.abspath(os.getenv(
    "BINANCE_STATE_PATH", os.path.join(PROJECT_DIR, "latest_news.json")
))
BINANCE_LOG_PATH = os.path.abspath(os.getenv(
    "BINANCE_LOG_PATH", os.path.join(PROJECT_DIR, "news_trading.log")
))
TRADING_MANAGER_TOKEN = os.getenv("TRADING_MANAGER_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# LLM API credentials
# ---------------------------------------------------------------------------

# DeepSeek API (OpenAI-compatible endpoint)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Aiping API — primary news analysis and Data Copilot endpoint
# 平台已切换到 api.nassaapi.xyz（OpenAI 兼容）；AIPING_* 命名保留兼容。
AIPING_API_KEY = os.getenv("AIPING_API_KEY", "").strip()
AIPING_BASE_URL = os.getenv(
    "AIPING_BASE_URL", "https://api.nassaapi.xyz/v1"
).strip().rstrip("/")
AIPING_MODEL = os.getenv("AIPING_MODEL", "gpt-5.4-mini").strip()
# nassaapi 不需要 aiping 的专属字段；保留空 dict 避免 payload 带未知字段
AIPING_EXTRA_BODY: Dict[str, Any] = {}

# OpenRouter remains available for webhook translation and realtime_filter only.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter 分析通道模型（独立配额，走代理 22307）
OPENROUTER_ANALYSIS_MODEL = os.getenv("OPENROUTER_ANALYSIS_MODEL", "deepseek/deepseek-chat").strip()

# MiniMax 备用模型 — OpenRouter 余额不足(402)/不可用时自动切换（权重不变，余额恢复自动切回）
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "").strip()
# 国内版 api.minimax.chat / 国际版 api.minimaxi.com
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1").strip().rstrip("/")
# v186 默认值从 Text-01 改为 MiniMax-M3：
#   Text-01 实测频繁 HTTP 429 配额耗尽（占全部失败 61%），是历史兜底通道；
#   M3 是 MiniMax 当前主力模型（2026-09-22 v176.6 已全量切到 M3，并禁用 thinking 块
#   强制直接输出 JSON）。新部署/换机器默认值直接到 M3，避免回退到坏通道。
# 如需回退到 Text-01 设 MINIMAX_MODEL=MiniMax-Text-01 显式覆盖。
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3").strip()
# MiniMax 走不走代理（国内版直连更快；国际版/受限网络设 true 走 HTTP_PROXY）
MINIMAX_USE_PROXY = os.getenv("MINIMAX_USE_PROXY", "true").strip().lower() not in ("0", "false", "False")

# 第二个 MiniMax key（可选）：OpenRouter 失效兜底时与第一个 key 并行（第一个优先）。
# 复用 MINIMAX_BASE_URL / MINIMAX_USE_PROXY（同一服务的第二账户），但模型名独立配置。
# 默认沿用 MINIMAX_MODEL；想用新一代模型（如 MiniMax-M3）可单独指定而不影响主池。
MINIMAX2_API_KEY = os.getenv("MINIMAX2_API_KEY", "").strip()
MINIMAX2_MODEL = os.getenv("MINIMAX2_MODEL", "").strip() or MINIMAX_MODEL

# ══════════════════════════════════════════════════════════════════
# v187 多通道独立 MiniMax Key（每通道独立账单追踪）
# 用 MINIMAX_MAIN_KEY 没配时退化到旧 MINIMAX_API_KEY，保证向后兼容。
# 余额不足(402)→ 自动降级到 LOCAL_LLM（见下）。
# ──────────────────────────────────────────────────────────────────
def _pick_key(*names: str) -> str:
    """多个候选 key 中按顺序取第一个非空的；兼容 v186 前的旧 env 命名。"""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""

# 主分析通道 — 每条新闻 AI 决策
MINIMAX_MAIN_KEY     = _pick_key("MINIMAX_MAIN_KEY") or MINIMAX_API_KEY
MINIMAX_MAIN_MODEL   = os.getenv("MINIMAX_MAIN_MODEL", "MiniMax-M3").strip()
# 门卫筛选 — verdict 64 tokens,高频
MINIMAX_GATE_KEY     = _pick_key("MINIMAX_GATE_KEY") or MINIMAX_API_KEY
MINIMAX_GATE_MODEL   = os.getenv("MINIMAX_GATE_MODEL", "MiniMax-M2.7-highspeed").strip()
# 门卫是高频短 verdict（max_tokens=200），不该吃主分析 60s 默认超时；
# 实测 Flash 通道慢请求 12~24s 才超时，长时间占并发。独立超时默认 25s，env 可调。
MINIMAX_GATE_TIMEOUT = float(os.getenv("MINIMAX_GATE_TIMEOUT_S", "25"))
# v342：门卫的"真·独立配额"预留位。
# 事故背景：MINIMAX_GATE_KEY / MINIMAX_MAIN_KEY / MINIMAX_API_KEY 实测是同一个 key
# → config 里写的"gate 专用 + gate 备用"两条通道只建成 1 条，和主分析抢同一份配额；
# 配额一满 gate 全池 429 → 新闻冻结等回放（实测断流数小时）。
# 这里给一个**任意 OpenAI 兼容端点**的口子：只要在 .env 里填
#   GATE_ALT_BASE_URL / GATE_ALT_API_KEY / GATE_ALT_MODEL
# 就会自动生成一条 purpose='gate' 的独立通道（独立 key_alias → 独立熔断器），
# 不需要改任何代码。三个量缺一个就不建，保证不会造出"空壳通道"。
# 主分析的同类口子：模型"只思考不回答"时，把主分析切到另一个端点即可，同样不用改代码。
MAIN_ALT_BASE_URL = os.getenv("MAIN_ALT_BASE_URL", "").strip()
MAIN_ALT_API_KEY  = os.getenv("MAIN_ALT_API_KEY", "").strip()
MAIN_ALT_MODEL    = os.getenv("MAIN_ALT_MODEL", "").strip()

GATE_ALT_BASE_URL = os.getenv("GATE_ALT_BASE_URL", "").strip()
GATE_ALT_API_KEY  = os.getenv("GATE_ALT_API_KEY", "").strip()
GATE_ALT_MODEL    = os.getenv("GATE_ALT_MODEL", "").strip()
# 异常归因 — attribute_5m_abnormal / attribute_market_drivers
MINIMAX_ANOMALY_KEY  = _pick_key("MINIMAX_ANOMALY_KEY") or MINIMAX_API_KEY
MINIMAX_ANOMALY_MODEL = os.getenv("MINIMAX_ANOMALY_MODEL", "MiniMax-M3").strip()
# 反思池（盲区反思 / 顾问 / Text-to-SQL / 参数优化器共享 1 个 key）
MINIMAX_REFLECT_KEY    = _pick_key("MINIMAX_REFLECT_KEY") or MINIMAX_API_KEY
MINIMAX_REFLECT_MODEL  = os.getenv("MINIMAX_REFLECT_MODEL", "MiniMax-M2.7").strip()
MINIMAX_ADVISOR_MODEL  = os.getenv("MINIMAX_ADVISOR_MODEL", "MiniMax-M2.7-highspeed").strip()
MINIMAX_SQL_MODEL      = os.getenv("MINIMAX_SQL_MODEL", "MiniMax-M2.7-highspeed").strip()
MINIMAX_OPTIMIZER_MODEL = os.getenv("MINIMAX_OPTIMIZER_MODEL", "MiniMax-M2.7-highspeed").strip()
# ──────────────────────────────────────────────────────────────────
# v314 P0 同行评审专用通道
# 验收 #4 要求"多模型一致性"真的落库 (extra_models_consensus 有内容) 且
# consensus_mult 有 ≥2 个取值。之前 consensus_mult **恒为 1.0**, 根因不是
# 代码没写, 而是**模型池里只有一个族**: 主池只剩 minimax2/MiniMax-M3
# (MINIMAX_MAIN_WEIGHT=0), 门卫/反思通道因 key 相同被跳过 → 评审模型和
# 主模型都是 MiniMax-M3 → signal_weight_evaluator._model_family 判为同族 →
# 一票被折叠 → 共识未定义 → fail-closed 回 1.0。
# 这里给评审单独开一个**不同族**的通道 (MiniMax-M2.7, 独立账户/独立配额),
# 主分析通道完全不动 (仍全是 M3), 只让"评审"这一路多出一个独立模型。
# ──────────────────────────────────────────────────────────────────
MINIMAX_PEER_KEY    = _pick_key("MINIMAX_PEER_KEY") or MINIMAX_REFLECT_KEY
MINIMAX_PEER_MODEL  = os.getenv("MINIMAX_PEER_MODEL", "MiniMax-M2.7").strip()
MINIMAX_PEER_WEIGHT = int(os.getenv("MINIMAX_PEER_WEIGHT", "1").strip() or "1")
# 第二个评审通道（与 M2.7 不同族）：signal_weight_evaluator._consensus_mult
# 要求**至少两票**（同族折叠后仍要 ≥2 个族）共识才成立，否则 fail-closed 回 1.0。
# 目前可达的模型只有 MiniMax-M3 与 MiniMax-M2.7 两族 → 评审组合 = M2.7（跨族）
# + M3（与主模型同族的独立复核）。**诚实边界**：这两票里有一票与主模型同族，
# 真正独立的外部模型族依然只有 MiniMax 一家（OpenRouter 401 / Xinshu 429 /
# AIPING SSL 故障 / 其它 key 未配），这是当前**能力上限**，不是实现缺陷。
# 2026-11-02：默认回落从 MINIMAX2_API_KEY（账户2）改为 MINIMAX_API_KEY（账户1）。
# 原因：账户2 Token Plan 配额耗尽（HTTP 429 实测）→ 第二票永远拿不到 →
# _consensus_mult 只剩一票 → fail-closed 回 1.0 → consensus_mult 恒为 1.0
# （capability_acceptance 验收 #4「consensus_mult 有 ≥2 个取值」一直不达标）。
#
# ── 评审调用超时 ────────────────────────────────────────────────────────
# 2026-11-02：verify 调用的超时原来硬编码 20s，但**完整八段式分析单次调用
# 实测普遍 22-49s**（ai_call_stats 近 6h：M3 12-30s / M2.7-highspeed 32-49s
# / M2.7 22-29s，prompt ~15k token）。20s 超时**必然**把评审票全丢掉 →
# peer_reviews 只有空/一票 → _consensus_mult fail-closed 回 1.0 →
# consensus_mult 恒为 1.0（这是"恒 1.0"的第二个根因，与模型族折叠独立）。
# 75s > 主模型 65s：评审再慢也不会比主路径先超时而被丢弃。
# 2026-11-02 追加：实测评审的 HTTP 部分就有 17-50s，加上 prompt 构建（曾因
# 金十 MCP 7 个 token 全配额耗尽而白等）总耗时可能超过 75s → 票又被丢掉。
# 现在金十 MCP 配额耗尽会短路（jin10_mcp._all_tokens_exhausted），prompt
# 构建不再白等；这里仍留 90s 余量，保证评审票不再被超时吃掉。
#
# 2026-11-03：上面说的"短路"其实**没有真正生效**——金十 MCP 的 token 轮换
# 是**跨线程共享全局 _token_idx**（主分析/评审/watcher/api_server 并发调用），
# 于是：① 各线程互相推进下标，把"已耗尽"标记记到**别的 token** 上 →
#   _all_tokens_exhausted 永远 False → 每次调用仍要白等 7 次 HTTP；
#   ② 轮换时 _reset_session() 把**别的线程正在用的会话**关掉。
#   实测日志里的"配额耗尽 → 切换到第 2/7 → 3/7 → 4/7 → 2/7"循环就是它。
# 已修（jin10_mcp.py）：每 token 独立会话 + 每 token 一把初始化锁 +
# call_tool 用**局部变量**推进 token（不写回共享下标）→ 短路真正生效。
# 效果：引擎日志 JIN10 配额轮换记录归零，评审调用 max 延迟 114s → 60s。
#
# 2026-11-03（验收 #2 权重校准，勿再"优化"）：ρ(Spearman 权重 vs |实际涨跌|)
# 实测 +0.1992，阈值 0.2，**只差 0.0008**。根因不是公式 bug，而是
# **组内没有可用信号**：
#   · 影响维 (signal_impact) 是**品种级**的量，在"资产×持有期"组内被
#     硬截断成常数（15 个组里 10 个组 impact_mult 只有 1 个取值）→
#     组内零区分度；
#   · LLM 自评维度 94% 是常数（sentiment=0 / impact_score=5 / heat_score=5）
#     → base_mult 90% 相同。
# 实测组内 ρ 平均 **-0.085**（n≥30 的 11 个组），整体 ρ≈+0.20 **几乎全部
# 来自跨品种波动率差异**（XAU 1.33 vs BTC/WTI 1.5）。
# **踩过的坑（别再试）**：把 impact 的硬截断 [0.3,1.5] 换成软压缩/放开上限
# 实测**降低** ρ（当前 clamp +0.2215 → 软压缩上限1.5 +0.1511 / 原始 ratio
# +0.2063 / clamp[0.3,5.0] +0.2058）——去截断恢复的是品种 σ 的日间噪声，
# 不是"这条新闻能推动多少价格"的信号。所以保持 clamp 不动。
# 要真正过 #2，需要**组内有区分度的新信号**（LLM 打分方差，或按品种近期
# 实际反应/成交量构造的组内影响先验），不是再动乘子公式。
#
# 2026-11-02 23:3x 再调：实测评审调用延迟 p50=35s / p75=45s / p90=56s /
# max=186s（ai_call_stats 近 3h, n=32），而 HTTP 层单次尝试 timeout=60s
# 且网络异常会重试 3 次（3+6+12s 退避）→ 单次调用最坏 261s。90s 仍会
# 吃掉 p90 以上的慢票（实测近 3h 有 3 次 >90s）。评审已改为与主分析
# **并行**跑（总耗时 = max(主, 评审) 而非相加），把超时提到 120s 不会
# 拖慢整体节奏，但能把慢票救回来 → 评审票数从"1 票"回到"2 票" →
# consensus_mult 不再恒 1.0。
PEER_REVIEW_TIMEOUT_SEC = float(os.getenv("PEER_REVIEW_TIMEOUT_SEC", "120") or 120)
# ── 主分析超时 ──────────────────────────────────────────────────────────
# 2026-11-02 23:3x：主分析超时原来硬编码 65s，比 HTTP 层单次尝试(60s)
# + 一次重试退避(3s) 还短 → **一次网络抖动就把整条新闻判失败**。
# 实测近 3h 主路径失败 47 次（TimeoutError 29 + RuntimeError 18），
# 而主调用延迟 p50=25s / p75=44s / p90=56s —— 也就是说绝大多数失败
# 不是模型慢，而是"65s 窗口内没跑完"。更糟的是：主路径失败 → 走
# failover → peer_reviews 直接置 None → **这条新闻的交叉验证票全丢**
# （评审填充率只有 3.6%，而抽样率是 20%）。
# 现在：主分析超时独立可配（默认 120s，可覆盖 2 次 HTTP 尝试），
# 且评审与主分析并行、failover 路径同样保留评审票。
LLM_MAIN_TIMEOUT_SEC = float(os.getenv("TRIDENT_LLM_MAIN_TIMEOUT_SEC", "120") or 120)
# 账户1 配额充足，模型仍是 MiniMax-M3（与评审通道 M2.7 不同族，不折叠）→ 两票可成立。
MINIMAX_PEER2_KEY    = _pick_key("MINIMAX_PEER2_KEY") or MINIMAX_API_KEY
MINIMAX_PEER2_MODEL  = os.getenv("MINIMAX_PEER2_MODEL", "MiniMax-M3").strip()
MINIMAX_PEER2_WEIGHT = int(os.getenv("MINIMAX_PEER2_WEIGHT", "1").strip() or "1")


# ══════════════════════════════════════════════════════════════════
# v187 本地模型兜底（Ollama OpenAI 兼容端点，免费、零余额可用）
# 任何 MiniMax 通道 402/网络错误 → 自动降级到此。
# ──────────────────────────────────────────────────────────────────
LOCAL_LLM_ENABLED    = os.getenv("LOCAL_LLM_ENABLED", "1").strip().lower() not in ("0", "false")
LOCAL_LLM_BASE_URL   = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1").strip().rstrip("/")
LOCAL_LLM_MODEL      = os.getenv("LOCAL_LLM_MODEL", "qwen2.5-coder:7b").strip()
LOCAL_LLM_FAST_MODEL = os.getenv("LOCAL_LLM_FAST_MODEL", "qwen2.5:3b").strip()
LOCAL_LLM_REFLECT_MODEL = os.getenv("LOCAL_LLM_REFLECT_MODEL", "qwen3:latest").strip()
LOCAL_LLM_MAX_CONCURRENT = int(os.getenv("LOCAL_LLM_MAX_CONCURRENT", "2"))
LOCAL_LLM_TIMEOUT    = float(os.getenv("LOCAL_LLM_TIMEOUT", "90"))

# 火山方舟（Volcengine Ark）DeepSeek v4 分析通道 — 国内直连（保留原模型地址，新增此通道）
# ARK_API_KEY / ARK_MODEL 一律从环境变量读取, 不要把值写进代码或 .env.example:
#   1) .env 是本机私有文件, 提交到仓库会泄露凭据
#   2) 即便是模型 endpoint id, 也会被 GitHub Push Protection 误判为 API Key
#      而直接拦截整个 push (历史中的字面量同样会被扫描, 需一并清理)
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").strip().rstrip("/")
ARK_MODEL = os.getenv("ARK_MODEL", "").strip()
# 默认开启；没配 ARK_API_KEY / ARK_MODEL 时 build 阶段会自动跳过（不影响其它模型）
ARK_ENABLED = os.getenv("ARK_ENABLED", "1").strip().lower() not in ("0", "false", "False")

# 被墙行情/新闻源（OKX www.okx.com、BlockBeats api.theblockbeats.news）是否走 env 代理
# （HTTP_PROXY/HTTPS_PROXY）。默认 False=直连（无代理环境现状）。
# 配好本地代理(如 Clash 7890 / OpenRouter 22307)后，在 .env 设 MARKET_USE_PROXY=true
# 并启用 HTTP_PROXY/HTTPS_PROXY，即可让 OKX/BlockBeats 经代理绕过 SNI 阻断。
MARKET_USE_PROXY = os.getenv("MARKET_USE_PROXY", "false").strip().lower() in ("1", "true", "yes", "on")

# Xinshu AI 中转（gpt.xinshu.ai）— 测试通道（同 gpt-5.5 模型经独立中转，对比速度/稳定性）
# 实测大量 TimeoutError（失败 → MiniMax 慢兜底，单条拖到 100s+），默认关闭不进主池；
# 需要对比测试时设 XINSHU_ENABLED=1 再开。
XINSHU_ENABLED = os.getenv("XINSHU_ENABLED", "0").strip().lower() not in ("0", "false", "False")
XINSHU_API_KEY = os.getenv("XINSHU_API_KEY", "").strip()
XINSHU_BASE_URL = os.getenv("XINSHU_BASE_URL", "https://gpt.xinshu.ai/v1").strip().rstrip("/")
XINSHU_MODEL = os.getenv("XINSHU_MODEL", "gpt-5.5").strip()
# 国内中转直连（不走代理，延迟更低）
XINSHU_USE_PROXY = False

def _model_label(model_id: str) -> str:
    """按模型 ID 自动生成显示名：Aiping 的 DeepSeek / nassaapi 中转 / OpenRouter。"""
    if model_id.startswith("DeepSeek"):
        return f"{model_id} (Aiping)"
    if model_id.startswith("deepseek/") or "/" in model_id:
        return f"{model_id} (OpenRouter)"
    return f"{model_id} (nassaapi)"


# 主分析模型池（多通道多模型并行分片，速度优先）：
# 2026-09-17 重构：OpenRouter key 失效（HTTP 401 "User not found"）、nassaapi 模型
# 下线（HTTP 503 model_not_found）→ 这两个坏通道默认停用（env 开关可恢复），
# 把实测可用的 MiniMax/MiniMax2/Xinshu（国内直连，1-5s）提为主池主力。
# 权重：快通道拿大头；同模型多中转：id 用唯一内部标识，request_model 放实际模型名。
OPENROUTER_ENABLED = os.getenv("OPENROUTER_ENABLED", "0").strip().lower() not in ("0", "false", "False")
AIPING_ENABLED = os.getenv("AIPING_ENABLED", "0").strip().lower() not in ("0", "false", "False")

_AI_MODEL_ROSTER_LIST: List[Dict[str, Any]] = []

# 通道权重可由 .env 覆盖（默认主 2 / #2 4）。设 0 = 临时从主池摘除该通道，
# 适合「主 key 失效但不想动 .env / 不想重启」的运维场景；下次 _build_model_pool()
# 重建（每批自动）即生效。符号型 fix：v302+ 加的紧急旁路。
MINIMAX_MAIN_WEIGHT = int(os.getenv("MINIMAX_MAIN_WEIGHT", "2").strip() or "2")
MINIMAX2_WEIGHT    = int(os.getenv("MINIMAX2_WEIGHT",    "4").strip() or "4")

# v320: MiniMax 族级紧急旁路。
# 主通道(#main / #2)权重都为 0 时，**整族** minimax 通道都要摘除 —— 否则
# gate/anomaly/reflect/advisor/sql/optimizer 这些专用通道会残留，
# "紧急摘除 MiniMax" 的运维开关就名不副实（v302 注释里称"符号型 fix"）。
# 这也是 tests/test_minimax_weight_override.py::test_both_zero_yields_empty_minimax_pool
# 的断言语义。
MINIMAX_FAMILY_DISABLED = (MINIMAX_MAIN_WEIGHT <= 0 and MINIMAX2_WEIGHT <= 0)

# 1) MiniMax 主通道（国内直连，实测稳定 1-5s）——主力
# 2026-09-22 调整（v176.6）：Text-01 实测频繁 HTTP 429 配额耗尽（昨日 2539 次 rate_limit，占全部
# 失败 61%），且是主池 + fallback 首选，导致主分析连环失败、六段落入"服务端自动补全"占位。
# 账户1 主通道改为与账户2 一致的 MiniMax-M3（两个账户并列跑 M3，配额分散、429 骤降），彻底
# 剔除 Text-01 进主池。M3 一律禁用 thinking 块（防其把 token 全耗在推理、不输出 JSON）。
if MINIMAX_API_KEY and MINIMAX_MAIN_WEIGHT > 0:
    _m1_extra: Dict[str, Any] = {}
    if (MINIMAX_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _m1_extra["thinking"] = {"type": "disabled"}  # 禁用思考块，强制直接输出 JSON
    # 注：这里的 max_tokens 在正式分析路径上会被 ai_worker 的
    # MINIMAX_MAX_TOKENS（默认 4000）覆盖，只对直连调用生效；别指望改这里能治短输出。
    _m1_extra["max_tokens"] = 2500  # 兜底（防 thinking 模式挤出 JSON）
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax/{MINIMAX_MODEL}",
        "request_model": MINIMAX_MODEL,        # 实际请求模型名（id 带通道前缀保持唯一）
        "label": f"{MINIMAX_MODEL} (MiniMax)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX_API_KEY,
        "use_proxy": False,                    # 国内直连（走代理绕路且不稳）
        "weight": MINIMAX_MAIN_WEIGHT,
        "extra_body": _m1_extra,
        "purpose": ["main"],                   # v187：用途标签
        "key_alias": "minimax1",               # v187：API key 别名（账单追踪）
    })
# 2) MiniMax#2（第二账户，并行补充）
if MINIMAX2_API_KEY and MINIMAX2_WEIGHT > 0:
    # M3 / 新一代模型需更大 token 预算（实测：长 prompt（~8K chars）下 M3 的 thinking tokens
    # 可达 2500+，把 JSON 全部挤出 token 预算）。给到 8000 让 thinking 跑完、完整输出 JSON。
    # 关键：实测 M3 在长 prompt 下会把全部 8000 token 耗在 thinking 而不输出 JSON
    # （finish_reason=length, reasoning_tokens=7999, cleaned_len=0）。
    # 解决：禁用 thinking，让 M3 直接输出 JSON → 130 tokens 就够 → token 预算留给
    # 八段 reasoning_path + 20 字段 JSON。
    _minimax2_extra: Dict[str, Any] = {}
    if (MINIMAX2_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _minimax2_extra["thinking"] = {"type": "disabled"}  # 禁用思考块，强制直接输出 JSON
        # max_tokens 留空 → ai_worker 写 6000(覆盖此默认值)
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax2/{MINIMAX2_MODEL}",
        "request_model": MINIMAX2_MODEL,
        "label": f"{MINIMAX2_MODEL} (MiniMax#2)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX2_API_KEY,
        "use_proxy": False,
        "extra_body": _minimax2_extra,
        "weight": MINIMAX2_WEIGHT,
        "purpose": ["main"],
        "key_alias": "minimax2",
    })
# 3) Xinshu AI 中转（gpt.xinshu.ai）— 直连 2-3s，实测可用
if XINSHU_ENABLED and XINSHU_API_KEY:
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"xinshu/{XINSHU_MODEL}",
        "request_model": XINSHU_MODEL,
        "label": f"{XINSHU_MODEL} (xinshu)",
        "api_base": XINSHU_BASE_URL,
        "api_key": XINSHU_API_KEY,
        "use_proxy": False,
        "weight": 4,
        "purpose": ["main"],
        "key_alias": "xinshu",
    })
# 4) OpenRouter（默认停用：key 失效 401；换有效 key 后设 OPENROUTER_ENABLED=1 恢复）
if OPENROUTER_ENABLED and OPENROUTER_API_KEY:
    _AI_MODEL_ROSTER_LIST.append({
        "id": "openai/gpt-4o-mini",
        "label": "gpt-4o-mini (OpenRouter)",
        "api_base": OPENROUTER_BASE_URL,
        "api_key": OPENROUTER_API_KEY,
        "use_proxy": True,                     # OpenRouter 走代理（海外）
        "weight": 5,
        "purpose": ["main"],
        "key_alias": "openrouter",
    })
    _AI_MODEL_ROSTER_LIST.append({
        "id": OPENROUTER_ANALYSIS_MODEL,
        "label": _model_label(OPENROUTER_ANALYSIS_MODEL),
        "api_base": OPENROUTER_BASE_URL,
        "api_key": OPENROUTER_API_KEY,
        "use_proxy": True,
        "weight": 4,
        "purpose": ["main"],
        "key_alias": "openrouter",
    })
# 5) nassaapi（默认停用：模型下线 503；恢复后设 AIPING_ENABLED=1）
if AIPING_ENABLED and AIPING_API_KEY:
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"nassaapi/{AIPING_MODEL}",
        "request_model": AIPING_MODEL,
        "label": f"{AIPING_MODEL} (nassaapi)",
        "api_base": AIPING_BASE_URL,
        "api_key": AIPING_API_KEY,
        "use_proxy": False,
        "weight": 3,
        "purpose": ["main"],
        "key_alias": "aiping",
    })
# 6) 火山方舟 DeepSeek v4（国内直连）— 没配 ARK_API_KEY / ARK_MODEL 时跳过
if ARK_ENABLED and ARK_API_KEY and ARK_MODEL:
    _AI_MODEL_ROSTER_LIST.append({
        "id": "ark/deepseekv4",
        "request_model": ARK_MODEL,
        "label": "deepseekv4 (ark)",
        "api_base": ARK_BASE_URL,
        "api_key": ARK_API_KEY,
        "use_proxy": False,
        "weight": 4,
        "purpose": ["main"],
        "key_alias": "ark",
    })
# 7) DeepSeek 官方（国内直连）— v324 新增
#
# v430（2026-11-17）：**从 if ARK_ENABLED 里移出来**。
# 这条注册原先缩进在 `if ARK_ENABLED and ARK_API_KEY and ARK_MODEL:` 内部，
# 于是没配火山方舟时 DeepSeek 根本不会进池 —— .env 里填了 DEEPSEEK_API_KEY 也白填。
# 实测后果：2026-11-17 13 点 MiniMax 两个账户配额同时打满，引擎日志
# `通道体检 ready=False reason=全部通道熔断: minimax1[quota], minimax2[quota]`，
# 池里没有第三条通道可切，整批分析 429 全失败、当天零新信号。
if DEEPSEEK_API_KEY:
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"deepseek/{DEEPSEEK_MODEL}",
        "request_model": DEEPSEEK_MODEL,
        "label": f"{DEEPSEEK_MODEL} (deepseek)",
        "api_base": DEEPSEEK_BASE_URL,
        "api_key": DEEPSEEK_API_KEY,
        "use_proxy": False,          # 国内直连, 不吃代理
        "weight": 4,
        "purpose": ["main"],
        "key_alias": "deepseek",
    })
AI_MODEL_ROSTER: tuple = tuple(_AI_MODEL_ROSTER_LIST)


# ══════════════════════════════════════════════════════════════════
# v187：本地 Ollama 兜底（任何 MiniMax 通道 402/网络失败时用）
# 默认 Ollama 端点 + qwen2.5-coder:7b（适合 JSON/SQL）；qwen3:latest 用于反思。
# ──────────────────────────────────────────────────────────────────
# v322 (2026-11-04)：本地默认版**不再进 main 池**（默认），理由见下。
#
# 为什么把 "main" 从本地默认版的 purpose 里去掉：
#   主分析的 prompt 是「八段推理链 + 20 字段 JSON + 十几次上下文注入」，
#   实测 prompt 长度 10k+ tokens。qwen2.5:3b 在 Mac CPU 上生成 4000 tokens
#   要 5-10 分钟 → 必然 ReadTimeout（本地通道 timeout=180s，实测 3 次重试全超时）。
#   就算侥幸跑通，3B 模型的八段式 JSON 质量远达不到 M3 的水平 —— 写进
#   ai_decisions 会**污染 7730 条历史高质量信号**，后患远大于收益。
#
#   本地模型真正的价值在**门卫**（L2 二分类 / L3 四步标签）：
#   prompt 短、输出小、判"噪音/可交易"不需要长推理 —— 实测 qwen2.5:3b
#   跑门卫 L3 成功（147-175s）。门卫能用 = 新闻不被冻结 = 队列不卡死
#   = MiniMax 一恢复就立刻能出信号。这才是"兜底"该做的事。
#
#   想让本地也跑主分析：装一个够大的模型（qwen2.5-coder:7b 及以上），
#   然后设 LOCAL_LLM_MAIN_ENABLED=1。
LOCAL_LLM_MAIN_ENABLED = os.getenv(
    "LOCAL_LLM_MAIN_ENABLED", "0").strip().lower() in ("1", "true", "yes")

if LOCAL_LLM_ENABLED and LOCAL_LLM_BASE_URL:
    # v322: 三个用途可能配了**同一个**模型名（如都填 qwen2.5:3b），
    # 此时 f"local/{MODEL}" 三个 id 会完全相同 → 池里出现重复条目、
    # 熔断器 key_alias 也撞在一起。这里按用途给 id 加区分后缀，
    # 保证 id 唯一（模型名本身不变，请求的还是同一个模型）。
    def _local_id(model_name: str, suffix: str) -> str:
        # v324 P0 修复 —— **v322 的判据选错了, 去重实际没生效**:
        #   v322 用「模型名」反推用途 (model_name == LOCAL_LLM_FAST_MODEL → -fast)。
        #   但 .env 里 LOCAL_LLM_MODEL / LOCAL_LLM_FAST_MODEL / LOCAL_LLM_REFLECT_MODEL
        #   **三个都是 qwen2.5:3b** (实测 2026-11-10) → 第一个 if 永远命中 →
        #   三个 id 全都变成 "local/qwen2.5:3b-fast", 依然完全相同。
        #   后果不是"多一条重复条目"这么轻: 熔断器名按 key_alias 拼
        #   (realtime_filter._llm_breaker → llm_net:{alias} / llm_auth:{alias}),
        #   三个通道共用一个 alias ⇒ **一个通道超时, 三个一起被拉黑**;
        #   实测 llm_net:local 与 llm_auth:local 同时 open, 面板显示本地全挂,
        #   而 ollama 实测 HTTP 200 正常 (qwen2.5:3b, 7.7~25s)。
        #   修法: id **一律**按用途加后缀, 与模型名无关 —— 模型名相同或不同都唯一。
        return f"local/{model_name}-{suffix}"

    # 默认版（通用：异常归因/顾问/SQL/优化器；主分析需显式开启）
    _AI_MODEL_ROSTER_LIST.append({
        "id": _local_id(LOCAL_LLM_MODEL, "std"),
        "request_model": LOCAL_LLM_MODEL,
        "label": f"{LOCAL_LLM_MODEL} (local-ollama)",
        "api_base": LOCAL_LLM_BASE_URL,
        "api_key": "local-no-auth",
        "use_proxy": False, "weight": 1,
        "extra_body": {"max_tokens": 1500},   # Ollama 不接受 thinking 字段
        "purpose": (
            ["main", "anomaly", "advisor", "sql", "optimizer"]
            if LOCAL_LLM_MAIN_ENABLED
            else ["anomaly", "advisor", "sql", "optimizer"]
        ),
        # v324: key_alias 原本三个本地通道都是 "local" —— 熔断器按 key_alias 命名
        # (realtime_filter._llm_breaker → llm_net:{alias} / llm_auth:{alias}),
        # 于是三个本地通道**共用同一组熔断器**: 任一通道超时/被判 auth 失败,
        # 另外两个健康的通道一起被拉黑。实测 2026-11-10: llm_net:local 与
        # llm_auth:local 同时处于 open, 面板显示 3 个本地通道全挂, 而实测
        # ollama /v1/chat/completions 正常返回 HTTP 200 (qwen2.5:3b, 7.7~25s)。
        # 修法: 与 v322 的 id 去重同思路 —— 每个本地通道一个独立 alias,
        # 熔断只影响真正出问题的那个通道。
        "key_alias": _local_id(LOCAL_LLM_MODEL, "std"), "is_local": True,
        "timeout": LOCAL_LLM_TIMEOUT,
    })
    # 高速版（小模型，门卫/顾问/优化器）
    _AI_MODEL_ROSTER_LIST.append({
        "id": _local_id(LOCAL_LLM_FAST_MODEL, "fast"),
        "request_model": LOCAL_LLM_FAST_MODEL,
        "label": f"{LOCAL_LLM_FAST_MODEL} (local-fast)",
        "api_base": LOCAL_LLM_BASE_URL,
        "api_key": "local-no-auth",
        "use_proxy": False, "weight": 1,
        "extra_body": {"max_tokens": 200},
        "purpose": ["gate", "advisor", "sql", "optimizer"],
        "key_alias": _local_id(LOCAL_LLM_FAST_MODEL, "fast"), "is_local": True,
        "timeout": LOCAL_LLM_TIMEOUT,
    })
    # 反思版（强思考，盲区反思）
    _AI_MODEL_ROSTER_LIST.append({
        "id": _local_id(LOCAL_LLM_REFLECT_MODEL, "reflect"),
        "request_model": LOCAL_LLM_REFLECT_MODEL,
        "label": f"{LOCAL_LLM_REFLECT_MODEL} (local-reflect)",
        "api_base": LOCAL_LLM_BASE_URL,
        "api_key": "local-no-auth",
        "use_proxy": False, "weight": 1,
        "extra_body": {"max_tokens": 2000},
        "purpose": ["reflection"],
        "key_alias": _local_id(LOCAL_LLM_REFLECT_MODEL, "reflect"), "is_local": True,
        "timeout": LOCAL_LLM_TIMEOUT,
    })

# 重新打包（上面动态 append 了新条目）
AI_MODEL_ROSTER: tuple = tuple(_AI_MODEL_ROSTER_LIST)


# ══════════════════════════════════════════════════════════════════
# v187：每通道独立 MiniMax 专用通道（独立 key + 独立模型）
# 每个通道独立 key,独立模型名;余额不足时本地 Ollama 自动兜底(见下)。
# ──────────────────────────────────────────────────────────────────
# 7) 门卫筛选专用通道（独立 key,每条新闻 verdict=可交易/只能看/排除）
# v320 修正：原守卫是 `MINIMAX_GATE_KEY != MINIMAX_API_KEY` —— 当专用 key 与主
# key 相同（.env 里 gate/anomaly/reflect 全都复用主 key）时，这条通道**根本不会
# 被创建**，于是 purpose='gate' 池为空 → get_pool_for_purpose 回退到主池 →
# gate 只剩主池那一个通道。主池通道一旦配额耗尽（实测 MiniMax 429 全池挂 3h），
# 门卫就彻底没有备用通道可用。
# v314 给同行评审通道（10)）已经明确否定过这套守卫（"同 key 不同模型同样是两个族"），
# 这里统一改为：只要 key 非空就建通道；key_alias 保持独立，账单分渠道统计，
# 反而更利于定位"哪个通道在烧额度"。
if MINIMAX_GATE_KEY and not MINIMAX_FAMILY_DISABLED:
    _gate_extra = {"max_tokens": 200}
    if (MINIMAX_GATE_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _gate_extra["thinking"] = {"type": "disabled"}
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax-gate/{MINIMAX_GATE_MODEL}",
        "request_model": MINIMAX_GATE_MODEL,
        "label": f"{MINIMAX_GATE_MODEL} (gate专用)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX_GATE_KEY,
        "use_proxy": False, "weight": 3, "extra_body": _gate_extra,
        "timeout": MINIMAX_GATE_TIMEOUT,
        "purpose": ["gate"], "key_alias": "minimax-gate",
    })
    # v321: 门卫备用通道 —— 消除"门卫与主分析共用一个 key"的单点故障。
    # 事故复盘（2026-11-03）：.env 里 MINIMAX_GATE_KEY 与 MINIMAX_MAIN_KEY 都是账户1，
    # 而 purpose='gate' 池里**只有**这一条通道（LOCAL_LLM_ENABLED=0 时本地兜底也不存在），
    # 于是账户1 配额一打满 → 门卫 100% 不可用 → 每条新新闻都落成 gate_unavailable=1
    # → ai_worker 按 v320 规则排除它们 → 队列整体卡死。
    # 更隐蔽的是：门卫 key 与主分析 key 相同，两者抢同一个 Token Plan 配额 ——
    # 主分析是高频大 prompt 的热路径，配额被它吃光后，门卫连一次都发不出去。
    #
    # v321 修正（第二次）：备用通道的 key 选错了。
    #   上一版写的是 MINIMAX2_API_KEY，但 .env 里 MINIMAX_GATE_KEY 已经改成账户2，
    #   而 MINIMAX2_API_KEY **也是**账户2 → 两条门卫通道同 key、同配额，
    #   账户2 一挂两条一起挂 → "冗余"是假的（假冗余比没冗余更危险：
    #   面板显示两条通道，实际共用一份配额，会误判"还有备用"）。
    #   旧守卫 `MINIMAX2_API_KEY != MINIMAX_GATE_KEY` 本意正是拦掉这种假冗余，
    #   只不过它把整个备用通道一起挡掉了（等于回到单通道）。
    #
    # 正确方案（跨账户冗余）：
    #   minimax-gate  (主) → MINIMAX_GATE_KEY   = 账户2（门卫专用配额）
    #   minimax-gate2 (备) → MINIMAX_MAIN_KEY   = 账户1（主分析那条）
    # 于是：账户1 被主分析打满 → 门卫走账户2 照常工作 ✅
    #       账户2 被打满       → 门卫 fallback 账户1 ✅
    #       两个都打满         → 才真的无解（这时 LOCAL_LLM_ENABLED=0 是另一个隐患）
    # key_alias 独立 → 熔断器独立 → 一个账户挂掉不连带熔断另一个。
    _gate2_key = (MINIMAX_MAIN_KEY or MINIMAX_API_KEY or "").strip()
    if _gate2_key and _gate2_key != MINIMAX_GATE_KEY:
        _gate2_extra: Dict[str, Any] = {"max_tokens": 200}
        if (MINIMAX_GATE_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
            _gate2_extra["thinking"] = {"type": "disabled"}
        _AI_MODEL_ROSTER_LIST.append({
            "id": f"minimax-gate2/{MINIMAX_GATE_MODEL}",
            "request_model": MINIMAX_GATE_MODEL,
            "label": f"{MINIMAX_GATE_MODEL} (gate备用)",
            "api_base": MINIMAX_BASE_URL,
            "api_key": _gate2_key,
            "use_proxy": False, "weight": 2, "extra_body": _gate2_extra,
            "timeout": MINIMAX_GATE_TIMEOUT,
            "purpose": ["gate"], "key_alias": "minimax-gate2",
        })
# 7.4) v342：主分析独立端点通道（权重高于原 main，等效"换供应商"）
if MAIN_ALT_API_KEY and MAIN_ALT_BASE_URL and MAIN_ALT_MODEL:
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"main-alt/{MAIN_ALT_MODEL}",
        "request_model": MAIN_ALT_MODEL,
        "label": f"{MAIN_ALT_MODEL} (主分析独立端点)",
        "api_base": MAIN_ALT_BASE_URL,
        "api_key": MAIN_ALT_API_KEY,
        "use_proxy": False, "weight": 6, "extra_body": {"max_tokens": 4000},
        "purpose": ["main"], "key_alias": "main-alt",
        "timeout": float(os.getenv("MAIN_ALT_TIMEOUT_S", "65")),
    })

# 7.5) v342：门卫独立端点通道（任意 OpenAI 兼容：DeepSeek / 另一账户 MiniMax / 自建）
if GATE_ALT_API_KEY and GATE_ALT_BASE_URL and GATE_ALT_MODEL:
    _ga_extra: Dict[str, Any] = {"max_tokens": 400}
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"gate-alt/{GATE_ALT_MODEL}",
        "request_model": GATE_ALT_MODEL,
        "label": f"{GATE_ALT_MODEL} (gate独立端点)",
        "api_base": GATE_ALT_BASE_URL,
        "api_key": GATE_ALT_API_KEY,
        "use_proxy": False, "weight": 4, "extra_body": _ga_extra,
        "purpose": ["gate"], "key_alias": "gate-alt",
        "timeout": float(os.getenv("GATE_ALT_TIMEOUT_S", "25")),
    })

# 8) 异常归因专用通道
# 2026-11-16: 该通道实测 24h 318 次调用 / 318 次全失败（429 限流 299 + 529 过载 19），
# 配额一直被烧且零产出 —— 提供 MINIMAX_ANOMALY_DISABLED=1 开关直接不建通道，
# 让 attribute_5m_abnormal 走原有 fallback（不再往限流里撞）。判定口径不变。
if MINIMAX_ANOMALY_KEY and not MINIMAX_FAMILY_DISABLED and os.getenv("MINIMAX_ANOMALY_DISABLED", "0") != "1":
    _anomaly_extra = {"max_tokens": 600}
    if (MINIMAX_ANOMALY_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _anomaly_extra["thinking"] = {"type": "disabled"}
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax-anomaly/{MINIMAX_ANOMALY_MODEL}",
        "request_model": MINIMAX_ANOMALY_MODEL,
        "label": f"{MINIMAX_ANOMALY_MODEL} (anomaly专用)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX_ANOMALY_KEY,
        "use_proxy": False, "weight": 2, "extra_body": _anomaly_extra,
        "purpose": ["anomaly"], "key_alias": "minimax-anomaly",
    })
# 9) 反思/顾问/SQL/优化器专用通道（共享 1 个 key，但模型独立选）
# v320: 同 7)/8) —— 去掉 `!= MINIMAX_API_KEY` 守卫，key 非空即建通道。
if MINIMAX_REFLECT_KEY and not MINIMAX_FAMILY_DISABLED:
    # 9a) 反思（盲区反思，慢思考）
    _reflect_extra = {"max_tokens": 1500}
    if (MINIMAX_REFLECT_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _reflect_extra["thinking"] = {"type": "disabled"}
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax-reflect/{MINIMAX_REFLECT_MODEL}",
        "request_model": MINIMAX_REFLECT_MODEL,
        "label": f"{MINIMAX_REFLECT_MODEL} (reflect专用)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX_REFLECT_KEY,
        "use_proxy": False, "weight": 2, "extra_body": _reflect_extra,
        "purpose": ["reflection"], "key_alias": "minimax-reflect",
    })
    # 9b-d) 顾问/SQL/优化器（高速版）
    for _purpose, _model, _label_suffix in (
        ("advisor", MINIMAX_ADVISOR_MODEL, "advisor"),
        ("sql", MINIMAX_SQL_MODEL, "sql"),
        ("optimizer", MINIMAX_OPTIMIZER_MODEL, "optimizer"),
    ):
        _extra = {"max_tokens": 800 if _purpose != "sql" else 1000}
        if (_model or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
            _extra["thinking"] = {"type": "disabled"}
        _AI_MODEL_ROSTER_LIST.append({
            "id": f"minimax-{_label_suffix}/{_model}",
            "request_model": _model,
            "label": f"{_model} ({_label_suffix}专用)",
            "api_base": MINIMAX_BASE_URL,
            "api_key": MINIMAX_REFLECT_KEY,
            "use_proxy": False, "weight": 2, "extra_body": _extra,
            "purpose": [_purpose], "key_alias": "minimax-reflect",
        })

# 10) 同行评审专用通道（v314 P0）—— purpose=verify，不进主分析池。
# 只给"交叉验证/同行评审"这一路用，保证评审模型与主模型**不同族**。
# 注意：不复用门卫那套 `key != MINIMAX_API_KEY` 守卫 —— 评审通道与主通道
# 用不同 key 是常态（主分析已切到 minimax2 账户），同 key 不同模型同样是两个族。
if MINIMAX_PEER_KEY and MINIMAX_PEER_WEIGHT > 0 and not MINIMAX_FAMILY_DISABLED:
    _peer_extra = {"max_tokens": 2500}
    if (MINIMAX_PEER_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _peer_extra["thinking"] = {"type": "disabled"}
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax-peer/{MINIMAX_PEER_MODEL}",
        "request_model": MINIMAX_PEER_MODEL,
        "label": f"{MINIMAX_PEER_MODEL} (peer评审)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX_PEER_KEY,
        "use_proxy": False, "weight": MINIMAX_PEER_WEIGHT, "extra_body": _peer_extra,
        "purpose": ["verify"], "key_alias": "minimax-peer",
    })
# 10b) 第二个评审通道（与 10) 不同族）：_consensus_mult 要求 ≥2 票才成立
if MINIMAX_PEER2_KEY and MINIMAX_PEER2_WEIGHT > 0 and not MINIMAX_FAMILY_DISABLED:
    _peer2_extra = {"max_tokens": 2500}
    if (MINIMAX_PEER2_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _peer2_extra["thinking"] = {"type": "disabled"}
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"minimax-peer2/{MINIMAX_PEER2_MODEL}",
        "request_model": MINIMAX_PEER2_MODEL,
        "label": f"{MINIMAX_PEER2_MODEL} (peer复核)",
        "api_base": MINIMAX_BASE_URL,
        "api_key": MINIMAX_PEER2_KEY,
        "use_proxy": False, "weight": MINIMAX_PEER2_WEIGHT, "extra_body": _peer2_extra,
        "purpose": ["verify"], "key_alias": "minimax-peer2",
    })

# v187：重新打包（上面动态 append 了专用通道 + 本地兜底）
AI_MODEL_ROSTER: tuple = tuple(_AI_MODEL_ROSTER_LIST)
# 默认模型动态取主池第一个通道（OpenRouter 停用时不再指向失效的 openai/gpt-4o-mini）
DEFAULT_AI_MODEL_ID = _AI_MODEL_ROSTER_LIST[0]["id"] if _AI_MODEL_ROSTER_LIST else "openai/gpt-4o-mini"
AI_MODEL_STATE_PATH = os.path.abspath(os.path.join(BASE_DIR, "runtime", "ai_model.json"))


def get_selected_ai_model_id() -> str:
    try:
        with open(AI_MODEL_STATE_PATH, "r", encoding="utf-8") as stream:
            model_id = json.load(stream).get("model_id")
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return DEFAULT_AI_MODEL_ID
    valid_ids = {model["id"] for model in AI_MODEL_ROSTER}
    return model_id if model_id in valid_ids else DEFAULT_AI_MODEL_ID


def write_selected_ai_model_id(model_id: str) -> str:
    valid_ids = {model["id"] for model in AI_MODEL_ROSTER}
    if model_id not in valid_ids:
        raise ValueError("unsupported AI model")

    state_dir = os.path.dirname(AI_MODEL_STATE_PATH)
    os.makedirs(state_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="ai_model_", suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"model_id": model_id}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, AI_MODEL_STATE_PATH)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise
    return model_id

# xAI (Grok) API — OpenAI-compatible direct endpoint
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"

# Doubao (火山引擎) API — OpenAI-compatible endpoint
# NOTE: DOUBAO_MODEL must be an Endpoint ID (ep-xxxxx), NOT a model name string.
# Create an Inference Endpoint at https://console.volces.com/ark before setting this.
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_BASE_URL = os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "")

# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------

TECHFLOW_NEWS_URL = os.getenv(
    "TECHFLOW_NEWS_URL",
    "https://www.techflowpost.com/api/client/newsflashes?page=1&page_size=50",
).strip()
EASTMONEY_NEWS_URL = os.getenv(
    "EASTMONEY_NEWS_URL",
    "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=345&order=1&needInteractData=0&page_index=1&page_size=50",
).strip()
# 律动 BlockBeats 快讯：2026 年起开放 API 已迁移到 api-pro 并需 api-key 鉴权。
# 旧端点 api.theblockbeats.news/v1/open-api/open-flash 已废弃（始终返回空 data）。
# .env 配 BLOCKBEATS_API_KEY 后启用；未配 key 时该采集源标 unavailable（其余源兜底）。
BLOCKBEATS_NEWS_URL = os.getenv(
    "BLOCKBEATS_NEWS_URL",
    "https://api-pro.theblockbeats.info/v1/newsflash?page=1&size=50&lang=cn",
).strip()
BLOCKBEATS_API_KEY = os.getenv("BLOCKBEATS_API_KEY", "").strip()
NEWS_SOURCE_POLL_SECONDS = max(5, int(os.getenv("NEWS_SOURCE_POLL_SECONDS", "10")))
NEWS_SOURCE_PAGES = max(1, int(os.getenv("NEWS_SOURCE_PAGES", "6")))
EVENTS_LIST_MAX = max(200, int(os.getenv("EVENTS_LIST_MAX", "10000")))
PAPER_INITIAL_EQUITY_USDT = max(0.0, float(os.getenv("PAPER_INITIAL_EQUITY_USDT", "10000")))

# ── 多证据闸门：通过统一置信度的下限（0-100），低于则动作降级为 HOLD ──
# 默认 10（v202+ 是 40 → 20 → 10）—— 2026-10-08 weekly_reflection shadow 投影：
#   闸门 40 → 0% 通过（system 没在交易）
#   闸门 20 → 2/169 通过, PnL +0.2%, 胜率 50%（真实数据支持的最优解）
#   闸门 10 → 改 10 是为了让"统一置信度 14.02 低于闸门 20.0，仅输出观望结论"这类
#     实际有方向性但被卡住的样本(比如 14.02)能被评估为可操作,而不是全部回退观望。
#     需要复跑 weekly_reflection 投影验证新阈值不会把噪声信号放进来。
# 保留 P2 #10 prompt 改 (5x boost) 代码但不被调用——5x 投影 PnL -6.1% 反亏。
# 调高（更保守）= 50/55；调低（更激进）= 5（不建议低于 5，噪声信号会污染 alpha）。
# 改完需重启 backend 生效（evidence.py 在 import 时读一次）。
EVIDENCE_CONFIDENCE_GATE = max(0.0, min(100.0, float(os.getenv("EVIDENCE_CONFIDENCE_GATE", "10"))))

# v231+ 单一来源 (2026-10-10): 反思层/quick_sim/audit 共用的"gate 通过"语义
# 任何 ai_decisions.trade_gate_reason 等于此字符串 → 视为 R1 gate 通过
# 改动警告: 这是 reflection / quick_sim / audit 三处的硬契约, 改了立即影响
# 所有 is_valid_for_alpha 计算; 改了必须同步改:
#   - quick_sim.py:_PASSED_REASON  (别删除本地别名, 改用 config.TRADE_GATE_PASSED_REASON)
#   - sample_quality_filter.py:pre_trade_check / _check_one
#   - audit_trades (间接, 通过 ai_decisions 反查)
# 默认值与 v202+ evidence gate 通过字符串保持一致
TRADE_GATE_PASSED_REASON = os.getenv("TRADE_GATE_PASSED_REASON", "证据充分，允许输出方向性结论")

# v231+ (C3): 模拟盘允许建仓的资产白名单 (R4)
# env: 逗号分隔, e.g. "XAU,WTI" (默认); 改了需重启 backend
# SQL 端 quick_sim.pending_signals() 动态生成 IN (?, ?, ...) 占位符
# Python 端 sample_quality_filter.pre_trade_check() 用 tuple 比较
SIM_ALLOWED_ASSETS = tuple(
    a.strip().upper() for a in os.getenv("SIM_ALLOWED_ASSETS", "XAU,WTI").split(",")
    if a.strip()
)

# v396 P2 — 报价健康闸门（R10）
# 背景：WTI 报价 88.985 冻结 22.5 小时（7,160 tick 同价）期间系统照常建仓，
# 产出的交易在"不存在的行情"上，入场价、止损距离、盈亏全部不可信。
# 默认开启（fail-closed）：报价冻结/过期 → 该笔信号不建仓，写入 quality_rejections。
QUOTE_HEALTH_GATE = os.getenv("QUOTE_HEALTH_GATE", "on").strip().lower() not in ("off", "0", "false")
# 冻结判定的时间窗口与最少 tick 数（窗口内 ≥N 笔且只有 1 个价 → 冻）
QUOTE_FROZEN_WINDOW_MINUTES = max(5, int(os.getenv("QUOTE_FROZEN_WINDOW_MINUTES", "60")))
QUOTE_FROZEN_MIN_TICKS = max(3, int(os.getenv("QUOTE_FROZEN_MIN_TICKS", "20")))
# 报价最大年龄：超过视为过期（分钟）
QUOTE_MAX_AGE_MINUTES = max(1, int(os.getenv("QUOTE_MAX_AGE_MINUTES", "15")))


# 快速模拟评测：所有 BUY/SELL 信号按真实行情建仓，短周期结算，用于盈利率评测。
# 与正式模拟盘（多证据闸门 + 策略闸门）互不干扰，闸门结论只作为分组标记保存。
# 注意：持仓时长在建仓时写入每笔记录（quick_sim_trades.horizon_minutes），
#       改这里只影响之后新开的仓；已建仓的仍按各自记录里的时长结算。
QUICK_SIM_ENABLED = os.getenv("QUICK_SIM_ENABLED", "1").strip() not in ("0", "false", "False")
QUICK_SIM_HORIZON_MINUTES = max(1, int(os.getenv("QUICK_SIM_HORIZON_MINUTES", "20")))


# ── v453 持有期分档 + 成本覆盖闸 ─────────────────────────────────────────
# 实测（2026-11-18，16 笔成交全部 20 分钟）：平均最大有利位移 0.183%，
# 平均往返成本 0.182% —— 位移刚够付手续费，结构性负期望（净 -0.33%/笔）。
# 所以：① 强事件/早期事件给更长窗口；② 窗口内的中位位移盖不住成本就不开仓。
#
# 分档表达式 "high:60,medium:40,low:20"，env 可改成任意 "强度:分钟" 列表。
def _parse_horizon_tiers(raw: str) -> dict:
    out = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        try:
            out[k.strip().lower()] = max(1, int(float(v.strip())))
        except (TypeError, ValueError):
            continue
    return out


QUICK_SIM_HORIZON_TIERS = _parse_horizon_tiers(
    os.getenv("QUICK_SIM_HORIZON_TIERS", "high:60,medium:40,low:20")
)
QUICK_SIM_HORIZON_EARLY_MULT = float(os.getenv("QUICK_SIM_HORIZON_EARLY_MULT", "2.0"))
QUICK_SIM_HORIZON_LATE_MULT = float(os.getenv("QUICK_SIM_HORIZON_LATE_MULT", "0.5"))
QUICK_SIM_HORIZON_MIN_BOUND = max(1, int(os.getenv("QUICK_SIM_HORIZON_MIN_BOUND", "20")))
QUICK_SIM_HORIZON_MAX_BOUND = max(1, int(os.getenv("QUICK_SIM_HORIZON_MAX_BOUND", "240")))

# 成本覆盖闸：同资产同持有期的中位 MFE 必须 ≥ 倍数 × 往返成本（bps）。
# 样本不足 → 放行并标 edge_unverified（不猜、不拦）。
QUICK_SIM_COST_EDGE_ENFORCE = os.getenv(
    "QUICK_SIM_COST_EDGE_ENFORCE", "1").strip().lower() not in ("0", "false", "no")
QUICK_SIM_COST_EDGE_MULT = float(os.getenv("QUICK_SIM_COST_EDGE_MULT", "1.5"))
QUICK_SIM_COST_EDGE_LOOKBACK_DAYS = max(1, int(os.getenv("QUICK_SIM_COST_EDGE_LOOKBACK_DAYS", "90")))
QUICK_SIM_COST_EDGE_MIN_SAMPLES = max(1, int(os.getenv("QUICK_SIM_COST_EDGE_MIN_SAMPLES", "6")))
QUICK_SIM_NOTIONAL_USDT = max(1.0, float(os.getenv("QUICK_SIM_NOTIONAL_USDT", "100")))
# v314 P0: 结算宽限期(分钟) —— 仓位到期后超过这个窗口仍未结算, 说明结算链停摆。
#   · 宽限期内: 到期价取不到时允许用现价兜底 (此时 now ≈ due_ts, 误差可接受)
#   · 超出宽限期: 仍按**到期价**结算 (这才是正确的 horizon 收益); 若连到期价
#     都取不到 → 熔断废弃 (verdict=ABANDONED, 不进统计, 不回填 ai_decisions)
# 实测事故: 结算链被 TypeError 掐断 8.85 天, 20 分钟的仓位拖成 12742.9 分钟,
#           收益被放大到 ±50% 量级 → 界面均值 +117.71%。
QUICK_SIM_SETTLE_GRACE_MINUTES = max(1, int(os.getenv("QUICK_SIM_SETTLE_GRACE_MINUTES", "60")))


# ── v230+ 模拟盘安全护栏 (P0.3 日亏熔断 + P0.4 事件窗口) ──────────────────
# 设计：默认开启，可通过 env 关闭；详细语义见 backend/src_python/safety_guards.py
DAILY_LOSS_GUARD_ENABLED = os.getenv("DAILY_LOSS_GUARD_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
EVENT_WINDOW_GUARD_ENABLED = os.getenv("EVENT_WINDOW_GUARD_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
# 当日允许最大亏损 (USDT)；None / 0 → 用 QUICK_SIM_NOTIONAL_USDT
DAILY_LOSS_LIMIT_USDT = float(os.getenv("DAILY_LOSS_LIMIT_USDT", "0") or 0.0) or None
# 事件窗口大小 (秒)，单条事件可用 window_seconds 字段覆盖
EVENT_WINDOW_SECONDS = max(30, int(os.getenv("EVENT_WINDOW_SECONDS", "300")))
# 高影响事件 JSON 文件路径 (例: docs/macro_events.json)，缺失/解析失败 → 静默放行
MACRO_EVENTS_FILE = os.getenv("MACRO_EVENTS_FILE", "").strip() or None
QUICK_SIM_POLL_SECONDS = max(5, int(os.getenv("QUICK_SIM_POLL_SECONDS", "15")))

# ── v230+ P0.5 MAE 早砍（软止损）────────────────────────────────────────────
# 经验值：SELL WIN 最大 MAE=0.251%，SELL LOSS 平均 MAE=0.247%（最大 0.93%）。
# 阈值 0.3% 把 SELL LOSS 砍掉 3/12 且 0 误杀 WIN；BUY 侧也能捞回 1 笔失控。
# 设为 0 / None 或 EARLY_LOSS_CUT_ENABLED=0 → 关闭。
EARLY_LOSS_CUT_ENABLED = os.getenv("EARLY_LOSS_CUT_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
_early_cut_raw = os.getenv("EARLY_LOSS_CUT_PCT", "0.3").strip()
EARLY_LOSS_CUT_PCT: Optional[float] = float(_early_cut_raw) if _early_cut_raw else None
if EARLY_LOSS_CUT_PCT is not None and EARLY_LOSS_CUT_PCT <= 0:
    EARLY_LOSS_CUT_PCT = None

# ── v230+ P0.5b SELL 信号门槛（建仓前拒签）──────────────────────────────
# 历史 23 笔 SELL 复盘：
#   - high event_strength SELL  : n=3  WR 33.3%（反向指标）
#   - BTC SELL                  : n=6  WR 33.3%
#   - WTI SELL                  : n=8  WR 37.5%
# 组合 no_high+no_btc+no_wti：n=8 / WR 62.5% / avg +0.007%（唯一正期望子集）
# 设 SELL_GATE_ENABLED=0 关闭；改 SELL_GATE_BLOCKED_ASSETS 调整拒签资产清单。
SELL_GATE_ENABLED = os.getenv("SELL_GATE_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
SELL_GATE_BLOCK_HIGH_STRENGTH = (
    os.getenv("SELL_GATE_BLOCK_HIGH_STRENGTH", "1").strip().lower()
    in ("1", "true", "yes", "on")
)
# v335: 默认值从 "BTC,WTI" 改成 "BTC"，与 .env 的实际配置和已写进注释的结论对齐。
#   原来默认值带 WTI，而 WTI 又在 SIM_ALLOWED_ASSETS 白名单里 → 一旦 .env 没读到，
#   WTI 立刻变回"只能做多"，SELL_GATE 拒签重新变成自伤。默认值不该埋这种雷。
_sell_blocked_raw = os.getenv("SELL_GATE_BLOCKED_ASSETS", "BTC").strip()
SELL_GATE_BLOCKED_ASSETS = frozenset(
    a.strip().upper() for a in _sell_blocked_raw.split(",") if a.strip()
)

# ── v230+ P0.2: TP/SL 强制结算 ───────────────────────────────────────
# paper_trading_settings.tp_enabled 是每运行独立开关；这里是全局 kill-switch。
# 设为 0 强制跳过 check_tp_sl()，便于紧急回滚或离线分析。回测/A/B 可临时关。
TP_SL_ENABLED = os.getenv("TP_SL_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
# TP1/TP2/SL 默认倍数与配比（每个运行可在 settings 页覆写）。
#   tp1_mult=0.6 → 止盈 1 笔 0.6% / tp2_mult=1.0 → 止盈 2 笔 1.0% /
#   tp1_ratio=0.5 → TP1 平 50% / tp2_ratio=0.3 → TP2 平 30% /
#   sl_mult=0.5 → 止损 0.5%。盈亏比 0.6% : 0.5% ≈ 1.2:1（保守），可通过
#   env TP_SL_DEFAULT_TP1_MULT 等覆盖（用于回测不同参数组合）。
_TP_SL_DEFAULT_TP1_MULT_RAW = os.getenv("TP_SL_DEFAULT_TP1_MULT", "0.6").strip()
_TP_SL_DEFAULT_TP2_MULT_RAW = os.getenv("TP_SL_DEFAULT_TP2_MULT", "1.0").strip()
_TP_SL_DEFAULT_SL_MULT_RAW = os.getenv("TP_SL_DEFAULT_SL_MULT", "0.5").strip()
TP_SL_DEFAULT_TP1_MULT = float(_TP_SL_DEFAULT_TP1_MULT_RAW) if _TP_SL_DEFAULT_TP1_MULT_RAW else 0.6
TP_SL_DEFAULT_TP2_MULT = float(_TP_SL_DEFAULT_TP2_MULT_RAW) if _TP_SL_DEFAULT_TP2_MULT_RAW else 1.0
TP_SL_DEFAULT_SL_MULT = float(_TP_SL_DEFAULT_SL_MULT_RAW) if _TP_SL_DEFAULT_SL_MULT_RAW else 0.5
_TP_SL_DEFAULT_TP1_RATIO_RAW = os.getenv("TP_SL_DEFAULT_TP1_RATIO", "0.5").strip()
_TP_SL_DEFAULT_TP2_RATIO_RAW = os.getenv("TP_SL_DEFAULT_TP2_RATIO", "0.3").strip()
TP_SL_DEFAULT_TP1_RATIO = float(_TP_SL_DEFAULT_TP1_RATIO_RAW) if _TP_SL_DEFAULT_TP1_RATIO_RAW else 0.5
TP_SL_DEFAULT_TP2_RATIO = float(_TP_SL_DEFAULT_TP2_RATIO_RAW) if _TP_SL_DEFAULT_TP2_RATIO_RAW else 0.3
# v230+ P0.2 part B: breakeven-move。TP1 命中后把 SL 上移到 entry（±offset）。
# 关闭后 TP1 只锁盈利、不动 SL（即恢复 P0.2 默认行为）。
# offset 默认 0.0：完全保本；正数（如 0.0001）= 把 SL 推到 entry+0.01%，锁住微薄保证利润。
TP_SL_BREAKEVEN_ENABLED = os.getenv("TP_SL_BREAKEVEN_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
_TP_SL_BREAKEVEN_OFFSET_PCT_RAW = os.getenv("TP_SL_BREAKEVEN_OFFSET_PCT", "0.0").strip()
TP_SL_BREAKEVEN_OFFSET_PCT = float(_TP_SL_BREAKEVEN_OFFSET_PCT_RAW) if _TP_SL_BREAKEVEN_OFFSET_PCT_RAW else 0.0

# ── AI 复盘经验回灌（review_insights → prompt）─────────────────────────
# 启用的经验会以独立区块（═══ 复盘经验 ═══）注入每次 AI 分析的 prompt。
# 条数由你在「复盘页」逐条启用/停用控制 —— 停用的不注入，所以默认不限条数：
# 启用多少就注入多少（你点启用 = 它进 prompt，不点就不进）。
# REVIEW_INSIGHTS_LIMIT: 上限兜底，0 = 不限制。经验多了想收敛时改成 20/30 之类。
#   体积参考：实测 102 条约 2279 字（≈1500 token，占 Prompt Force 的 29%）；
#   20 条约 614 字。条数越多 prompt 越长，会挤压八段推演空间并增加耗时。
# v170 token 优化：默认从 0 改为 3（top 3 经验已覆盖 80% 高频模式，剩 20% 让模型自由判断）；
#   实测 3 条约 90 字（≈60 tokens），可放心开启。如需放开，按 .env 改回 0 即可。
# REVIEW_INSIGHTS_MIN_SAMPLES: 最少样本数门槛，样本太少的经验不可靠，默认 5。
REVIEW_INSIGHTS_LIMIT = max(0, int(os.getenv("REVIEW_INSIGHTS_LIMIT", "3")))
REVIEW_INSIGHTS_MIN_SAMPLES = max(1, int(os.getenv("REVIEW_INSIGHTS_MIN_SAMPLES", "5")))

# v453: 经验注入排序开关。1 = 按"证据强度"排序并做同参数键冲突消解（verified 优先，
#   同一参数键只注入一条，避免"提高门槛/降低门槛"两条同时进 prompt）；
#   0 = 退回旧行为（纯 source_samples 排序）。改动只影响 prompt 装配，不动任何交易参数。
REVIEW_INSIGHTS_RANK = 1 if os.getenv("REVIEW_INSIGHTS_RANK", "1") == "1" else 0

# v191 #1: weekly_insight_apply 把 review_insights 推到 strategy_param_adjustments 的门槛。
# 默认 5：跟 v188 REVIEW_INSIGHTS_MIN_SAMPLES=5 一致；v191 加了模糊关键词映射 (sizing.position_weight
# 等)，当前 27 个 kind=1 insight 中 3-4 个能直接产出 INSERT；门槛过高会"等不到数据"。
# 如要更严，可设 env WEEKLY_INSIGHT_MIN_SAMPLES=10。
WEEKLY_INSIGHT_MIN_SAMPLES = max(1, int(os.getenv("WEEKLY_INSIGHT_MIN_SAMPLES", "5")))

# ── v231+ meta_reflection_loop 阈值（offline 可校准）──────────────────────
# meta_reflection_loop.py 的 7 个 magic number 集中在这里。
# 改这里会改 runtime 行为；可用 reflection_calibration.py 离线扫 grid 重选值。
# 默认值与 v170 内联常量一致；如需重新校准，跑：
#   python reflection_calibration.py --n-bootstrap 100
META_WR_DELTA = float(os.getenv("META_WR_DELTA", "0.15"))
# 0.15 → 视为 refined/invalidated 的 WR 变化幅度
META_CONFIRM_BAND = float(os.getenv("META_CONFIRM_BAND", "0.05"))
# 0.05 → WR 稳定带宽（< band 视为中性）
META_MIN_SAMPLES = max(1, int(os.getenv("META_MIN_SAMPLES", "5")))
# 5 → 低于此样本数跳过 meta-loop
META_LUCK_THRESHOLD = float(os.getenv("META_LUCK_THRESHOLD", "0.30"))
# 0.30 → luck 视为运气主导的门槛

# ── v231+ meta-loop 自适应深度（信息增益停止规则）────────────────────
# 旧逻辑写死 max_level=2：当样本规模 / 信号粒度变后，要么过深要么过浅。
# 新规则：当下一层 finding_kind 与上一层 < eps 比例变化 + 当前层熵 < max_entropy
#         才继续；否则停在当前层。
# meta_loop_stopping.py 独立模块可单测；本处只暴露配置。
META_MAX_LEVEL = max(1, int(os.getenv("META_MAX_LEVEL", "4")))
# 4 → 硬上限（v170 默认 2；新规则下建议 3-4）
META_STOPPING_EPS = float(os.getenv("META_STOPPING_EPS", "0.05"))
# 0.05 → 下一层至少 5% 改判率才算"有新信息"
META_MAX_ENTROPY = float(os.getenv("META_MAX_ENTROPY", "0.85"))
# 0.85 → 改判率达标但 kind 分布太散（震荡）也停

# ── v240+ meta-loop 贝叶斯升级（reflection_bayes.py）────────────────────
# 把"delta=0.15 即 invalidated"的硬阈值改成 Beta-Binomial 后验比较：
#   - META_USE_BAYES=1 → 用 P(curr<base) > META_BAYES_PROB_THRESHOLD 判定
#   - 任一侧样本 < META_BAYES_MIN_SAMPLES → 退化为原 delta 逻辑
#   - 关闭 (META_USE_BAYES=0) → 100% 保留 v170/v230 行为，向后兼容
META_USE_BAYES = os.getenv("META_USE_BAYES", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
META_BAYES_PROB_THRESHOLD = float(os.getenv("META_BAYES_PROB_THRESHOLD", "0.90"))
# 0.90 → P(curr<base) > 0.90 才算 invalidated（双侧对应 ~5% 显著性）
META_BAYES_MIN_SAMPLES = max(1, int(os.getenv("META_BAYES_MIN_SAMPLES", "5")))
# 5 → 任一侧 < 5 样本则用 delta fallback（避免 Beta 后验过度乐观）
META_BAYES_MC_SAMPLES = max(100, int(os.getenv("META_BAYES_MC_SAMPLES", "10000")))
# 10000 → Monte Carlo 样本数；测试可降至 500 以加速

# ── v240+ BOCPD 漂移检测（changepoint.py）──────────────────────────────
# 用 BOCPD（Adams & MacKay 2007）检测每个 signal 的 trade outcome 流是否出现 regime shift.
# 把 fresh_run_prob 写入 evidence._changepoint_prob, dashboard 可据此决策.
# 默认关闭（opt-in），开启后会注入额外 Bayesian 推理开销（~10ms / signal）.
META_BOCPD_ENABLED = os.getenv("META_BOCPD_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
META_BOCPD_HAZARD_LAMBDA = max(2, int(os.getenv("META_BOCPD_HAZARD_LAMBDA", "50")))
# 50 → 期望 regime 持续 ~50 个 trade；典型量化信号 ~1-2 周
META_BOCPD_THRESHOLD = float(os.getenv("META_BOCPD_THRESHOLD", "0.50"))
# 0.50 → fresh_run_prob > 0.5 视为 regime shift detected
META_BOCPD_FRESH_RUN_K = max(1, int(os.getenv("META_BOCPD_FRESH_RUN_K", "5")))
# 5 → 检测窗口（sum P(r in [0, 5])）
META_BOCPD_MAX_RUN = max(10, int(os.getenv("META_BOCPD_MAX_RUN", "200")))
# 200 → max_run_length cap, 平衡内存 vs 检测灵敏度

# ── v240+ LLM Verifier Tournament（reflection_tournament.py）───────────
# 用 k=3 并发 verifier 投票替代单次 LLM 调用, 减少单点偏差 + 偶发 hallucination.
# 默认关闭（opt-in），开启后 LLM 调用次数 x3 → 注意 API 成本.
# 关闭时走 v200+ 单次 _llm_call 路径，100% 向后兼容.
TOURNAMENT_ENABLED = os.getenv("TOURNAMENT_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
TOURNAMENT_QUORUM = max(1, int(os.getenv("TOURNAMENT_QUORUM", "2")))
# 2 → 至少 2/3 verifier 同意才 confirm；如需更严设 3
TOURNAMENT_TIMEOUT = max(2, int(os.getenv("TOURNAMENT_TIMEOUT", "60")))
# 60 → 单次 tournament wall-clock 上限（秒）
TOURNAMENT_PARALLEL = os.getenv("TOURNAMENT_PARALLEL", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
# 1 → 默认并发；设 0 用于调试（serial 跑）

# v240+ Tournament Circuit Breaker (tournament_metrics.py)
# 滑动窗口内 "全 verifier 失败" 率 ≥ 阈值 → OPEN, 短路回单次 LLM, 冷却 N 次后重试
# 防 LLM 网关抖动 / 配额耗尽时, tournament 持续 3x 调用拖慢整个反射
TOURNAMENT_CB_THRESHOLD = float(os.getenv("TOURNAMENT_CB_THRESHOLD", "0.7"))
# 0.7 → 70% 全失败率触发 open
TOURNAMENT_CB_WINDOW = int(os.getenv("TOURNAMENT_CB_WINDOW", "10"))
# 10 → 窗口大小 = 最近 10 次 tournament 调用
TOURNAMENT_CB_COOLDOWN = int(os.getenv("TOURNAMENT_CB_COOLDOWN", "20"))
# 20 → open 后冷却 20 次反射再 half-open

# ── v231+ causal_graph_guard 阈值 ──────────────────────────────────
# causal_graph.py 的自动更新（如 update_cross_asset_weights）默认无回滚。
# causal_graph_guard.py 提供 snapshot → diff → 回滚保护；这里暴露阈值。
# 改大阈值 = 容忍更多漂移；改小阈值 = 更保守（更易回滚）。
CAUSAL_MAX_DIFF_FRAC = float(os.getenv("CAUSAL_MAX_DIFF_FRAC", "0.20"))
# 0.20 → 行列漂移 > 20% 触发回滚
CAUSAL_HARD_LIMIT = float(os.getenv("CAUSAL_HARD_LIMIT", "0.50"))
# 0.50 → 任意单格 > 50% 变化触发回滚（防一格毒害全表）

# ── v231+ jury / 三轨裁决阈值 ─────────────────────────────────────
# jury.py 的 MIN_CONFIDENCE / MIN_QUORUM 提到 config，便于调参不重启。
JURY_MIN_CONFIDENCE = float(os.getenv("JURY_MIN_CONFIDENCE", "0.20"))
# 0.20 → 任何 track 投出的 confidence 低于此值会被过滤（Bayes 静态 tilter 阈值）
JURY_MIN_QUORUM = int(os.getenv("JURY_MIN_QUORUM", "2"))
# 2 → 至少 2 个 track 投票才计 verdict，否则落 "no_quorum"

# ── v231+ watermark transparency ──────────────────────────────────
# reflection_cycles 新增 source_kind 列，区分 live / demo / stress_test / replay。
# 默认 'live'；demo_mode.IS_DEMO=True 时自动升级为 'demo'。要强制覆盖可显式传 source_ctx。
REFLECTION_SOURCE_KIND_DEFAULT = os.getenv("REFLECTION_SOURCE_KIND_DEFAULT", "live")

# ── 经验的自动启停评估（按命中归因，替代旧的"全局胜率"判定）──────────────
# 旧逻辑：拿"该经验创建之后的全部交易胜率"判断单条经验好坏 —— 归因不成立，
#   且所有经验共享同一判定窗口 → 一荣俱荣一损俱损（实测曾一次性停用全部 110 条），
#   而且只遍历 active=1 的经验 → 停用后永不复查（单向不可恢复）。
# 新逻辑：用 ai_decisions.insight_hit_ids（建仓时记录的"本条命中了哪些经验"）
#   逐条统计"命中该经验的交易"胜率：
#     · 样本 ≥ INSIGHT_MIN_SAMPLE 且 命中胜率 < INSIGHT_DEACTIVATE_WINRATE → 停用
#     · 已停用经验若命中胜率回升 ≥ INSIGHT_REACTIVATE_WINRATE → 自动恢复启用
#   （双向可恢复：好经验错杀了能回来，坏经验不会永久占位）
INSIGHT_MIN_SAMPLE = max(3, int(os.getenv("INSIGHT_MIN_SAMPLE", "10")))
INSIGHT_DEACTIVATE_WINRATE = float(os.getenv("INSIGHT_DEACTIVATE_WINRATE", "0.35"))
INSIGHT_REACTIVATE_WINRATE = float(os.getenv("INSIGHT_REACTIVATE_WINRATE", "0.55"))

# ── "影响大小"判据（用户要求：影响小的就不下单，但要写清楚怎么判断）──
# 两步走：先用【可实证】的判据拦（传导链），再校准打分刻度、够样本后再决定阈值。
#
# ① 传导链闸门（已启用，有实证支撑）
#    实测 348 条已结算方向单：有传导链 -0.033%/胜率48%；无传导链 -0.178%/胜率38%。
#    说不出"事件 → 该品种价格"的传导路径 = 只是字面提到 = 影响不明 → 不给方向单。
#    置 0 可关闭该闸门（仅用于排查对比，不建议长期关闭）。
MECHANISM_GATE_ENABLED = bool(int(os.getenv("MECHANISM_GATE_ENABLED", "1")))

# ② asset_impact 阈值（默认 0 = 不拦，只标记）
#    为什么不直接当闸门：实测该字段校准很弱 —— 模型基本只给 5 和 7（5 占 41%），
#    "<4（间接/背景）"全库仅 6 条；分档胜率 43%/48%/56% 区分度不足。
#    所以先只标记（写进 ai_decisions.low_impact）并统计，等刻度校准 + 样本够了再调高。
#    语义档位（提示词里的定义）：0-3 间接/背景；4-6 间接传导；7-8 直接催化；9-10 决定性。
#    建议校准后取值 7（直接催化及以上）。
MIN_ASSET_IMPACT_FOR_TRADE = max(0, int(os.getenv("MIN_ASSET_IMPACT_FOR_TRADE", "0")))

# v181: L3 门卫 - 新闻时效上限（小时）
# 超过此时长的新闻直接排除：12 小时是一般事件被市场消化完毕的窗口。
# 缺失 published_at 时降级使用入库 ts + 更严的 2 小时窗（采集时间不可信 → 收紧）。
# v432（2026-11-17）：12h → 48h、fallback 2h → 12h。
# 这是 **bridge 侧 L0 时效闸** 的阈值，和 api_server 入库闸用的
# NEWS_INGEST_MAX_AGE_HOURS 是两道**独立**的闸 —— 只改后者时这里仍是 12h，
# 于是"改完还是被拒"。实测日志累计 `[FILTER] L0 STALE` 1039 次，
# 典型样本「深市光通信龙头密集回应」age=37.45h threshold=12h 被丢掉。
# 防重复本来靠 content_fingerprint 去重，时间截断只该防灌库，不该当主力筛子。
# fallback 2h 太严：没有 published_at 的新闻（多数 RSS/转发源）刚入库就被判过期。
NEWS_MAX_AGE_HOURS = max(1, int(os.getenv("NEWS_MAX_AGE_HOURS", "48")))
NEWS_MAX_AGE_FALLBACK_HOURS = max(1, int(os.getenv("NEWS_MAX_AGE_FALLBACK_HOURS", "12")))
# v181: 影响力打分最低阈值（1-100 制）。< 此值的 LLM 自评一律排除。
NEWS_GATE_IMPACT_MIN = max(0, min(100, int(os.getenv("NEWS_GATE_IMPACT_MIN", "20"))))

# 飞书告警机器人 webhook — 为空时 engine 只打印一次警告并跳过发送
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")

# Slack Incoming Webhook — 在 Slack App → Incoming Webhooks 创建后填入 URL
# 为空时自动跳过发送（notification_channels.register_known_channels 检测）
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# Discord Channel Webhook — 在频道设置 → Integrations → Webhooks 创建后填入
# 为空时自动跳过发送
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ── 邮件告警（SMTP）— 为空时跳过发送 ──
# 例：QQ 邮箱 smtp.qq.com:465 (SSL)；SMTP_PASS 用「授权码」而非登录密码。
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()
SMTP_TO = os.getenv("SMTP_TO", "").strip()

# ── 微信告警（Server酱 / PushPlus）— 留空为跳过 ──
# Server酱：https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "").strip()
# PushPlus：http://www.pushplus.plus/send/{PUSHPLUS_TOKEN}  (token 前缀与 Server酱不同，二选一)
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()

# ── 飞书消息接收（金十推送 → 飞书群 → 自建应用长连接读群）──
# 需要：open.feishu.cn 创建企业自建应用 → 开启机器人 → 订阅 im.message.receive_v1
#       → 使用长连接接收事件 → 应用机器人进金十所在群（内部群）。
# 依赖：pip install lark-oapi
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
FEISHU_LISTENER_ENABLED = os.getenv("FEISHU_LISTENER_ENABLED", "0").strip() not in ("0", "false", "False")
# 限定只收金十群消息：填群 chat_id（如 oc_xxxx）；留空 = 收全部群消息再按关键词过滤
FEISHU_JIN10_CHAT_ID = os.getenv("FEISHU_JIN10_CHAT_ID", "").strip()

# ── 数据源健康告警（v187 新增）──
# 复用 FEISHU_WEBHOOK_URL 推送。无 webhook 时自动跳过。
SOURCE_ALERT_ENABLED = os.getenv("SOURCE_ALERT_ENABLED", "1").strip() not in ("0", "false", "False")
SOURCE_ALERT_THRESHOLD = max(1, int(os.getenv("SOURCE_ALERT_THRESHOLD", "3")))        # 连续失败 N 次触发
SOURCE_ALERT_INTERVAL = max(30, int(os.getenv("SOURCE_ALERT_INTERVAL", "120")))       # 轮询间隔(秒,下限30)
SOURCE_ALERT_SILENT_MIN = max(1, int(os.getenv("SOURCE_ALERT_SILENT_MIN", "30")))     # 同源静默期(分钟)
SOURCE_ALERT_HEARTBEAT_MIN = max(0, int(os.getenv("SOURCE_ALERT_HEARTBEAT_MIN", "60")))  # 心跳摘要间隔(分钟,0=关闭)
# "心脏"通道白名单：这些源 1 次失败就告警(不走 N 次阈值)。逗号分隔。
# 默认保留核心 3 个(api_server/engine_webhook/trident_event_bus_db),可通过 env 加。
SOURCE_ALERT_HEARTBEAT_SOURCES = frozenset(
    s.strip() for s in os.getenv(
        "SOURCE_ALERT_HEARTBEAT_SOURCES",
        "api_server,engine_webhook,trident_event_bus_db",
    ).split(",") if s.strip()
)

# ── 宏观日历提前 + 即时通知（v202 新增）──
# 复用 SMTP/Server酱/PushPlus 三通道（见上方 + engine.alerts.send_important_news），
# 不新加 SMTP / 公众号 API。每 5 分钟扫一次金十财经日历：
#   star ≥ MACRO_NOTIFY_MIN_STAR 的事件，提前 MACRO_NOTIFY_LEAD_MINUTES 推一次，
#   事件当刻再推一次。UNIQUE (event_id, window) 幂等防重。
MACRO_NOTIFY_ENABLED = os.getenv("MACRO_NOTIFY_ENABLED", "1").strip() not in ("0", "false", "False")
# v226: 默认从 3 星抬到 4 星 — 用户反馈 4 星以下不通知（噪音太多）
MACRO_NOTIFY_MIN_STAR = max(1, min(5, int(os.getenv("MACRO_NOTIFY_MIN_STAR", "4"))))
MACRO_NOTIFY_LEAD_MINUTES = max(1, int(os.getenv("MACRO_NOTIFY_LEAD_MINUTES", "120")))  # 提前 2h
MACRO_NOTIFY_POLL_SECONDS = max(30, int(os.getenv("MACRO_NOTIFY_POLL_SECONDS", "300")))  # 5min
# 即时窗口容差(分钟)：事件当刻 ±MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN 都算"当刻"
MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN = max(1, int(os.getenv("MACRO_NOTIFY_AT_TIME_TOLERANCE_MIN", "5")))

# 金十数据 MCP — 快讯/行情/财经日历（Bearer token 鉴权，SSE 流式 JSON-RPC）
# 免费 token 申请：https://mcp.jin10.com/app/ ；买会员后同端点、token 权限升级。
# 支持多 token 轮换：逗号分隔（如 sk-a,sk-b,sk-c），某个 token 配额用完自动切下一个。
JIN10_MCP_TOKEN = os.getenv("JIN10_MCP_TOKEN", "").strip()
JIN10_MCP_TOKENS: list[str] = [
    t.strip() for t in JIN10_MCP_TOKEN.split(",") if t.strip()
]
JIN10_MCP_URL = os.getenv("JIN10_MCP_URL", "https://mcp.jin10.com/mcp").strip().rstrip("/")
# 快讯采集轮询间隔（秒）：金十有独立 watcher，专属节奏；免费 token 限流时调大
JIN10_MCP_POLL_SECONDS = max(5, int(os.getenv("JIN10_MCP_POLL_SECONDS", "10")))
# v170+ 数据源加固：市场情绪（Fear&Greed / Truth Social Trump 推文）轮询间隔
MARKET_SENTIMENT_POLL_SECONDS = max(60, int(os.getenv("MARKET_SENTIMENT_POLL_SECONDS", "3600")))
# ── Fed 利率预期 + 异动通知（v226 新增）──
# 用 first principles 从 2Y UST yield + Fed funds rate 推导未来 24 月累计升降息预期
# 异动阈值（bp）：2Y yield 24h 移动 > 该值触发推送
FED_RATE_NOTIFY_ENABLED = os.getenv("FED_RATE_NOTIFY_ENABLED", "1").strip() not in ("0", "false", "False")
FED_RATE_NOTIFY_POLL_SECONDS = max(60, int(os.getenv("FED_RATE_NOTIFY_POLL_SECONDS", "300")))  # 5min
FED_RATE_YIELD_MOVE_BP = max(3, int(os.getenv("FED_RATE_YIELD_MOVE_BP", "10")))  # 2Y yield 24h 移动
FED_RATE_EXPECT_MOVE_BP = max(5, int(os.getenv("FED_RATE_EXPECT_MOVE_BP", "15")))  # 推导预期 24h 移动
# v276: Fed 事件事前通知 daemon(30min/24h 窗口提醒)
FED_PRE_NOTIFIER_ENABLED = os.getenv("FED_PRE_NOTIFIER_ENABLED", "1").strip() not in ("0", "false", "False")
FED_ADAPTIVE_THRESHOLD_ENABLED = os.getenv("FED_ADAPTIVE_THRESHOLD_ENABLED", "1").strip() not in ("0", "false", "False")
FED_PRE_INTERVAL_S = max(60, int(os.getenv("FED_PRE_INTERVAL_S", "300")))  # 5min
# v276: 每日发现汇总 cron(07:00 推送)
DISCOVERY_DIGEST_ENABLED = os.getenv("DISCOVERY_DIGEST_ENABLED", "1").strip() not in ("0", "false", "False")
# ── 跨资产异常同步检测（v226 新增）──
# ≥ 4 个不同行业同向同幅度 + 形态相似时推送系统级异动
CROSS_ASSET_ANOMALY_ENABLED = os.getenv("CROSS_ASSET_ANOMALY_ENABLED", "1").strip() not in ("0", "false", "False")
CROSS_ASSET_ANOMALY_POLL_SECONDS = max(60, int(os.getenv("CROSS_ASSET_ANOMALY_POLL_SECONDS", "600")))  # 10min
CROSS_ASSET_ANOMALY_MIN_CLASSES = max(2, int(os.getenv("CROSS_ASSET_ANOMALY_MIN_CLASSES", "4")))  # 行业数阈值
CROSS_ASSET_ANOMALY_MIN_PCT = float(os.getenv("CROSS_ASSET_ANOMALY_MIN_PCT", "0.5"))  # 同向最小幅度
# 财经日历发布检测：星级门槛（1/2/3），★≥该值的条目"公布"时才生成事件进新闻流
# （默认 2：PMI/零售/GDP 及以上；调 3 只留 CPI/非农/利率等最高影响；调 1 全量小数据）
JIN10_CALENDAR_MIN_STAR = max(1, min(3, int(os.getenv("JIN10_CALENDAR_MIN_STAR", "2"))))
# 日历发布检测轮询间隔（秒）：独立 watcher；公布检测延迟 ≤ 此值，调小更实时但更费配额
JIN10_CALENDAR_POLL_SECONDS = max(10, int(os.getenv("JIN10_CALENDAR_POLL_SECONDS", "30")))

# 新闻写库到 SSE 的内部增量检测周期（毫秒）。第三方上游延迟不受此项控制。
NEWS_WATCH_INTERVAL_MS = max(20, min(1000, int(os.getenv("NEWS_WATCH_INTERVAL_MS", "50"))))

# CORS 白名单 — 逗号分隔，默认本地 Next.js 开发端口
_cors_raw = os.getenv("CORS_ALLOW_ORIGINS", "")
CORS_ALLOW_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()] or [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:3002",
    "http://127.0.0.1:3002",
    "http://localhost:3030",
    "http://127.0.0.1:3030",
]

# ---------------------------------------------------------------------------
# Tuning thresholds (数值与 engine.py 原定义一致，勿改)
# ---------------------------------------------------------------------------

# VIP KOL monitoring
VIP_KOLS: Dict[str, str] = {
    "Trump":   "[VIP:TRUMP]", "特朗普": "[VIP:TRUMP]",
    "Musk":    "[VIP:MUSK]",  "马斯克": "[VIP:MUSK]", "Elon": "[VIP:MUSK]",
    "Powell":  "[VIP:FED]",   "鲍威尔": "[VIP:FED]", "FOMC": "[VIP:FED]", "美联储": "[VIP:FED]",
    "Vance":   "[VIP:OTHER]", "万斯": "[VIP:OTHER]", "Bessent": "[VIP:OTHER]",
}
VIP_SCORE_BOOST = 1.25

# Active-trade aggregation
_AGG_WINDOW_HOURS = 1          # how long a parent stays "active" (tight window to avoid over-clustering)
_AGG_MIN_SCORE    = 0.25       # only aggregate signals with |score| >= this

# AI worker batch size（每批新闻数；越大消化积压越快，但请求更密集可能触发上游限流）
# 积压消化后可回调到 10：env AI_BATCH_SIZE 可随时覆盖（改完重启引擎生效）
BATCH_SIZE = max(1, int(os.getenv("AI_BATCH_SIZE", "16")))

# 美股新闻（Yahoo 个股新闻，source=yahoo_stock）轮询周期（秒）
# yfinance 限频，最小 300s；默认 12 分钟一轮，覆盖 13 只精选美股的新新闻。
STOCK_NEWS_POLL_SECONDS = max(300, int(os.getenv("STOCK_NEWS_POLL_SECONDS", "720")))
STOCK_NEWS_ENABLED = os.getenv("STOCK_NEWS_ENABLED", "1").strip() not in ("0", "false", "False")
# Finnhub 免费 key（finnhub.io 注册，配了才走 Finnhub 美股新闻；否则 Yahoo 兜底）
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()

# IG Markets —— 客户情绪指数（散户多空比，XAU/WTI/BTC）。交易商需付费开户，但 REST API 免费可读。
# 认证需三步：请求头带 X-IG-API-KEY + Version:2，POST /gateway/deal/session(identifier+password)
# → 返回 CST + X-SECURITY-TOKEN，之后带这两个头调用受保护端点（如 /gateway/deal/sentiment/{epic}）。
# 仅配 IG_API_KEY 无法登录；IG_IDENTIFIER/IG_PASSWORD 必填（demo 环境用 demo-api.ig.com）。
# 留空则 ig 层标 unavailable，不影响其它功能。
IG_API_KEY = os.getenv("IG_API_KEY", "").strip()
IG_IDENTIFIER = os.getenv("IG_IDENTIFIER", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
IG_ENV = os.getenv("IG_ENV", "demo").strip().lower()  # demo / live
IG_BASE_URL = os.getenv("IG_BASE_URL", "").strip()   # 留空按 IG_ENV 自动选 demo/live

# Reuters 路透社（source=reuters，经 Google News RSS site:reuters.com 聚合）：
# ⚠️ 已停用（默认 0）：Google News 收录路透滞后 ~2h，对交易信号时效无价值，
# 且与金十（秒级转发路透一手）重复占 AI 配额。路透时效由金十承担。
# 如需重新启用：REUTERS_NEWS_ENABLED=1 且网络可达 Google。
REUTERS_NEWS_POLL_SECONDS = max(20, int(os.getenv("REUTERS_NEWS_POLL_SECONDS", "60")))
REUTERS_NEWS_ENABLED = os.getenv("REUTERS_NEWS_ENABLED", "0").strip() not in ("0", "false", "False")

# 多源 RSS 新闻（Yahoo Finance 官方 RSS + Google News 聚合 FXStreet / MarketWatch）：
# source 分别 = yahoo_finance / fxstreet / marketwatch。英文标题 → 翻译 → 入库 → AI 分析。
# RSS 为分钟级更新，聚合源（Google News）另有 ~2h 收录滞后，故轮询无需高频（默认 10 分钟）。
RSS_NEWS_POLL_SECONDS = max(300, int(os.getenv("RSS_NEWS_POLL_SECONDS", "600")))
RSS_NEWS_ENABLED = os.getenv("RSS_NEWS_ENABLED", "1").strip() not in ("0", "false", "False")

# 晴红·标卡（QingHong）第三方聚合资讯 API —— 试用评估源。
# 用于评估第三方资讯接入的质量/时效；key 为当日试用（23:59 到期自动停服），
# 本地也在 QINGHONG_EXPIRES_AT 到点自动停止采集，避免对已过期 key 发无效请求。
# 未配 QINGHONG_API_KEY 时本源静默退出（不影响其它 16 个源）。
QINGHONG_API_KEY = os.getenv("QINGHONG_API_KEY", "").strip()
QINGHONG_NEWS_BASE = os.getenv("QINGHONG_NEWS_BASE", "http://124.223.180.129:3220").strip()
QINGHONG_NEWS_ENABLED = os.getenv("QINGHONG_NEWS_ENABLED", "1").strip() not in ("0", "false", "False")
# 采集的子源 id（逗号分隔）。均选财经快讯类，与套利品种（贵金属/原油/宏观）最相关：
#   cls-telegraph 财联社电报 / wallstreetcn-quick 华尔街见闻快讯 / fastbull-express 汇通财经快讯
QINGHONG_SOURCES = [
    s.strip()
    for s in os.getenv(
        "QINGHONG_SOURCES", "cls-telegraph,wallstreetcn-quick,fastbull-express",
    ).split(",")
    if s.strip()
]
# 到期自动关闭时刻（上海时区）：超过即停止采集并在信息源面板标 offline
QINGHONG_EXPIRES_AT = os.getenv("QINGHONG_EXPIRES_AT", "2026-09-16 23:59:59").strip()
QINGHONG_POLL_SECONDS = max(20, int(os.getenv("QINGHONG_POLL_SECONDS", "45")))

# 金十快讯新鲜度闸：条目发布时间超过该小时数（默认 24h）→ 视为旧闻不收入库
# （防金十 MCP 公测/免费层返回历史缓存条目；正常快讯都在分钟-小时级）
JIN10_FLASH_MAX_AGE_HOURS = max(1, int(os.getenv("JIN10_FLASH_MAX_AGE_HOURS", "24")))

# ── 新闻数据整理（按平台分文档导出，供去各平台对比时效）──
NEWS_EXPORT_DIR = os.path.abspath(os.getenv(
    "NEWS_EXPORT_DIR", os.path.join(BASE_DIR, "news_exports")
))
# 自动整理间隔（分钟）：到点后若有新新闻则生成一批按平台分文件的文档
NEWS_EXPORT_INTERVAL_MINUTES = max(5, int(os.getenv("NEWS_EXPORT_INTERVAL_MINUTES", "30")))
# 单批回溯窗口（小时）：每批导出最近 N 小时的新闻（对比时效用近几小时即可）
NEWS_EXPORT_WINDOW_HOURS = max(1, int(os.getenv("NEWS_EXPORT_WINDOW_HOURS", "24")))
# 保留最近 N 批（超出自动清理最老的批次，防目录膨胀）
NEWS_EXPORT_KEEP_BATCHES = max(3, int(os.getenv("NEWS_EXPORT_KEEP_BATCHES", "30")))

# 新闻驱动交易时效门槛（分钟）：新闻发布超过该时长，BUY/SELL 信号不再建仓
# （避免用过时新闻追单；仅影响交易，不影响新闻分析与统计入库）
NEWS_TRADE_MAX_AGE_MINUTES = max(5, int(os.getenv("NEWS_TRADE_MAX_AGE_MINUTES", "45")))

# 门卫未判定（gate_unavailable=2）行的归档门槛（分钟）。
# 这类行是门卫故障/兜底放行，主分析认领窗口默认 30min（TRIDENT_DEGRADED_CLAIM_MAX_AGE_MIN），
# 过了 NEWS_TRADE_MAX_AGE_MINUTES 建仓窗口就已不可交易。原默认 2h，列表会挂大量陈旧待分析；
# 现贴近建仓窗口留 5min 冗余归档（仍留在库里可回放，filter_reason=gate_down）。
# 0 = 关闭该段归档（退回旧行为，交由常规 6h 档处理）。
NEWS_EXPIRE_GATE_DOWN_MIN = max(0, int(os.getenv("NEWS_EXPIRE_GATE_DOWN_MIN", "50")))

# 新闻入库新鲜度闸（小时）：平台发布时间距今超过该时长 → 判定为"过时新闻"直接拒收，
# 不入库、不进实时信号台、不占 AI 配额（防平台补发历史旧闻/feed 缓存滞后）。
# 仅对"带平台发布时间"的条目生效；实时推送类（WS/webhook，无平台时间字段）
# 按"到达即发布"处理不受此闸影响。0/缺省 = 3 小时（快讯类平台正常分钟级，足够宽）。
# v424：旧闻拒收窗口 12h → 48h。
# 事故（2026-11-17）：finnhub 等美股源夜间发布的新闻，北京时间早上已是 12–18h 前，
# 全部被"至今 > 12h"当旧闻丢弃（日志累计拒收 268497 次，最近一轮 3 条全拒、入库 0）。
# 防重复本来就靠 content_fingerprint/[hash:marker] 指纹去重，时间截断是多余的一道，
# 且会把跨时区/隔夜新闻成批切掉。留 48h 作为"防灌库"宽松闸，重复交给指纹。
NEWS_INGEST_MAX_AGE_HOURS = max(1, int(os.getenv("NEWS_INGEST_MAX_AGE_HOURS", "48")))

# v322: **认领时效门槛**（小时）—— 主分析只处理"还可能交易"的新闻。
# 事故（2026-11-05 实测）：队列积压 3700+ 条，认领 SQL 是 `ORDER BY id ASC`
# （最老优先）→ 引擎一条条分析**14 天前**的老新闻（news=11982 已 21315 分钟），
# 而 NEWS_TRADE_MAX_AGE_MINUTES=45 的建仓闸又把这些信号全部丢弃
# → 「仍入库统计不交易」—— **额度照烧，信号一条也用不上**，
# 而且队列永远追不上（每天新新闻进来，老新闻排队）。
#
# 现在：超过该时长的新闻**不再送去分析**，直接标 FILTERED（陈旧，不值得花额度）。
# 0 = 不限制（保留旧行为）。默认 6 小时 —— 快讯类新闻的有效交易窗口。
NEWS_ANALYZE_MAX_AGE_HOURS = max(0, int(os.getenv("NEWS_ANALYZE_MAX_AGE_HOURS", "6")))

# v444（2026-11-18，用户定策）：**通道恢复后不补历史新闻**。
# 原话："以后所有的 LLM 通道恢复之后，都不去做历史的新闻……它只分析现在进来的，
#   包括现在到以后进来的新闻，他不用分析以前的新闻。否则这样如果堆积的多的话，
#   我这个打不住的。"
#
# 背景（2026-11-18 实测）：MiniMax 额度打满 → 门卫 5 小时发不出判定 → 积压 216 条
# PENDING；通道一恢复，认领会把这些已经错失交易窗口的旧闻一条条送去主分析，
# 额度全烧在历史上，新新闻反而排不上（额度是唯一的稀缺资源）。
#
# 实现：`news_cutoff.py` 维护一个持久化 epoch 水位 floor_ts。
#   · 主分析连续 NEWS_RECOVERY_GAP_MIN 分钟没有一次成功调用 → 断档；
#   · 恢复的那一刻把水位推进到"现在"，此前的新闻一律不分析，直接归档留痕
#     （filter_reason='pre_recovery_skip'，原始行仍在库里可查）；
#   · 认领 / 门卫回放 / 超龄归档 / 面板计数 共用一个口径。
# 0 = 关闭该策略（重放历史做回归时才这么设）。
NEWS_SKIP_BACKLOG_ON_RECOVERY = max(0, int(os.getenv("NEWS_SKIP_BACKLOG_ON_RECOVERY", "1")))
# 断档判定阈值（分钟）：主分析多久没有一次成功调用就算"断过"。0 = 关闭判定。
# 15 分钟的依据：正常状态下 purpose='main' 的成功调用中位间隔 **0.5 分钟**，
# 实测一个分析批次 6 条要 8–21 分钟，取 15 分钟既能容忍慢批次、又能第一时间
# 认出真正的停摆（2026-11-18 那次断了 5.3 小时）。
NEWS_RECOVERY_GAP_MIN = max(0, int(os.getenv("NEWS_RECOVERY_GAP_MIN", "15")))
# v445（2026-11-18）：队列容量兜底。待分析条数 > CAP 时，水位推进到"只保留最新
# KEEP 条"，老的一次性归档（同一套 0 额度清理路径，不花 LLM）。CAP=0 关闭。
# 依据：不改大小时实测到达 200 条/小时、分析 24 条/小时，队列只涨不降。
NEWS_BACKLOG_CAP = max(0, int(os.getenv("NEWS_BACKLOG_CAP", "50")))
NEWS_BACKLOG_KEEP = max(1, int(os.getenv("NEWS_BACKLOG_KEEP", "20")))

# v446（2026-11-18）：入库前零成本筛子 —— 重复 / 统计·分析类（非新闻）。
# 实测入库 16,432 条/30 分钟，其中同一条快讯被多源转发 + 榜单/复盘类占绝大多数。
# 去重窗口 6 小时：同文重复超过 6 小时才当新事件。
NEWS_PREFILTER_ENABLED = max(0, int(os.getenv("NEWS_PREFILTER_ENABLED", "1")))
NEWS_DEDUP_ON_INGEST = max(0, int(os.getenv("NEWS_DEDUP_ON_INGEST", "1")))
NEWS_DEDUP_WINDOW_H = max(0.0, float(os.getenv("NEWS_DEDUP_WINDOW_H", "6") or 0))
NEWS_NONNEWS_FILTER = max(0, int(os.getenv("NEWS_NONNEWS_FILTER", "1")))

# 内置 Hermes 多 agent 写作驱动：分席简报 + 已结算技能回灌（不改主 prompt 语义）
HERMES_AGENT_ENABLED = os.getenv("HERMES_AGENT_ENABLED", "1").strip() not in ("0", "false", "False")
HERMES_SKILL_MIN_SAMPLE = max(1, int(os.getenv("HERMES_SKILL_MIN_SAMPLE", "8")))

# 新闻之外的盘面/舆情/加息预期/资金流（失败标 unavailable，不造数）
MACRO_CONTEXT_ENABLED = os.getenv("MACRO_CONTEXT_ENABLED", "1").strip() not in ("0", "false", "False")
# 历史 K 线知识库（XAU/BTC/ETH/WTI 的 1d+4h 位置/趋势），反共识对照；0 关闭
KLINE_KB_ENABLED = os.getenv("KLINE_KB_ENABLED", "1").strip() not in ("0", "false", "False")
SOSOVALUE_ETF_URL = os.getenv("SOSOVALUE_ETF_URL", "").strip()

# FRED (St. Louis Fed) — 官方宏观指标（CPI/非农/利率/M2），无新闻快讯。
# 留空则 fred 层标 unavailable、不生成发布事件；免费申请: fred.stlouisfed.org
FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
FRED_BASE_URL = os.getenv("FRED_BASE_URL", "https://api.stlouisfed.org").strip().rstrip("/")
# 发布事件影响分级：high=仅 CPI/非农/利率/美债等即时影响行情的指标生成事件（默认）；
# all=所有指标（含 M2 等滞后月度数据）都生成事件，可能刷屏。
FRED_EVENT_IMPACT = os.getenv("FRED_EVENT_IMPACT", "high").strip().lower()
# FRED 检测轮询间隔（秒）：数据发布检测专用，独立于普通新闻源轮询。
# FRED 免费 Key 限速 120 次/分钟；合并拉取约 9 个 series/轮。
# FRED 数据更新滞后官方发布数小时不定（中转源），10s 级高频无意义——
# 60s 轮询 ≈ 1440 次/日（仍 ≪ 配额），检测延迟远小于 FRED 自身更新滞后。
FRED_POLL_SECONDS = max(10, int(os.getenv("FRED_POLL_SECONDS", "60")))

# EIA (U.S. Energy Information Administration) — 美国官方周度石油库存（原油/汽油）。
# 每周三发布，周度数据（有滞后）；免费申请 Key：https://www.eia.gov/opendata/register.php
# 留空则 eia 层标 unavailable、不生成趋势事件；不影响其它功能。
EIA_API_KEY = os.getenv("EIA_API_KEY", "").strip()
EIA_BASE_URL = os.getenv("EIA_BASE_URL", "https://api.eia.gov/v2").strip().rstrip("/")

# BLS (U.S. Bureau of Labor Statistics) — 美国劳工统计局官方原始数据
# （CPI/核心 CPI/非农/失业率一手发布，比 FRED 更接近发布时刻；接口同 FRED 发布检测模式）。
# 免费申请 Key：https://data.bls.gov/registrationEngine/（格式 XXXX-XXXX-XXXX-XXXX）；
# v2 免费 25 次/日/500 series → 轮询默认 3600s，防烧配额。
# 留空则 BLS 层标 unavailable、不生成事件，不影响其它功能。
BLS_API_KEY = os.getenv("BLS_API_KEY", "").strip()
BLS_BASE_URL = os.getenv("BLS_BASE_URL", "https://api.bls.gov").strip().rstrip("/")
BLS_POLL_SECONDS = max(60, int(os.getenv("BLS_POLL_SECONDS", "3600")))

# Coinglass — 加密衍生品数据（全网爆仓 / 全市场 OI / 资金费率 / 恐惧贪婪）。
# 注意：Coinglass 已升级 API V4，正确 base URL = https://open-api-v4.coinglass.com（认证头 CG-API-KEY，
#       不再使用旧的 open-api.coinglass.com）。真实元数据端点如
#       /api/futures/open-interest/aggregated-history、/api/futures/funding-rate/history、
#       /api/index/fear-greed-history。
# 关键限制：免费 tier 对所有数据端点（含 supported-coins）返回 {"code":"401","msg":"Upgrade plan"}，
#       即「不付费订阅拿不到任何数据」。coinglass 层会探测并优雅降级为 unavailable，不阻塞、不造数。
# 留空则 coinglass 层标 unavailable，不影响其他功能。
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()
COINGLASS_BASE_URL = os.getenv("COINGLASS_BASE_URL", "https://open-api-v4.coinglass.com").strip().rstrip("/")

# FMP / Polygon / Alpha Vantage — 美股基本面/行情（免费 key，务必低频 + TTL 缓存防烧配额）
# 免费额度：FMP 250 次/日、Polygon 5 次/分、Alpha Vantage 仅 25 次/日（AV 只作兜底）。
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
FMP_BASE_URL = os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com").strip().rstrip("/")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
# 美股基本面层关注标的（加密高相关；"慢变量"按 TTL 缓存）。逗号分隔，可自行扩充。
# v406: 基线名单从 5 只扩到"真正会被决策到的美股"。旧名单漏了 AMZN/META/AAPL 这些
# 在同一周里被 ai_decisions 判过的票，结果是「决策有它、基本面没有它」。名单只是**基线**，
# 运行期还会并上 ai_decisions 近 30 天出现过的美股代码（见 fundamental_factor.universe()）。
EQUITY_TICKERS = tuple(t.strip().upper() for t in os.getenv(
    "EQUITY_TICKERS",
    "NVDA,MSTR,COIN,MARA,RIOT,AMZN,META,AAPL,TSLA,MSFT,GOOGL,PLTR,SMCI,MU,HUT,CLSK",
).split(",") if t.strip())
# 美股基本面缓存时长（秒）：免费额度紧，默认 6 小时一次网络拉取，命中缓存直接复用。
EQUITY_CACHE_TTL = max(300, int(os.getenv("EQUITY_CACHE_TTL", "21600")))

# v406: 「多久算旧」。三表是季报 —— 一周内重抓一遍纯属浪费配额；只有财报日附近才需要重抓。
EQUITY_STALE_HOURS = max(1, int(os.getenv("EQUITY_STALE_HOURS", "168")))
# 每轮最多刷新几只。v406 从 6 降到 3：25 次/日的免费档装不下 16 只 × (1 快照 + 4 三表)，
# 硬抢的结果是快照层被饿死 → eq.pe 分位永远起不来（实测新增 11 只 eq.pe 一条都没有）。
# 现在把额度按「快照要密、三表是季报」切：三表 3 只/轮（12 次/日），余下 13 次给快照层。
# 16 只基线靠 TTL 轮转，5~6 天一轮；已有 9 只 7 天内不会被重抓，实际只在补缺口。
EQUITY_MAX_PER_RUN = max(1, int(os.getenv("EQUITY_MAX_PER_RUN", "3")))
# AV 日配额（次）：免费档 25。超了不报 429，而是 200 + Note 文本，必须自己记账。
AV_DAILY_BUDGET = max(0, int(os.getenv("AV_DAILY_BUDGET", "25")))
# v406: 配额是「美股快照层」和「财报三表 cron」共用的，必须硬切，不能谁先跑谁全吃。
# 12 = 3 只 × (三表 3 次 + 一致预期 1 次)。快照层只能花剩下的 13 次。
# 为什么偏向快照层：估值分位要靠快照累积 ~60 个样本才有意义，缺一轮下轮能补；
# 三表是季报，90 天才变一次，慢几天不丢东西（但缺整季就没法算同比）。
AV_SNAPSHOT_RESERVE = max(0, int(os.getenv("AV_SNAPSHOT_RESERVE", "12")))

# v406: A股/港股基本面（东方财富 datacenter-web）。A股 6 位代码、港股 5 位代码可解析；
# CN_STOCK/HK_STOCK 这类占位标签是"没解析出具体代码"，不是"不适用"，语义必须分开。
CN_FUNDAMENTALS_ENABLED = os.getenv("CN_FUNDAMENTALS_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
CN_FUNDAMENTALS_TTL = max(600, int(os.getenv("CN_FUNDAMENTALS_TTL", "86400")))
CN_PE_MIN_SAMPLES = max(20, int(os.getenv("CN_PE_MIN_SAMPLES", "60")))

# v406-fix: 财报日更的"自带调度"。
# 背景：原本用 launchd(plist) 每天 05:10 触发，但 macOS TCC 不给后台进程
# ~/Desktop 访问权 → 实测 kickstart 退出码 126 + "Operation not permitted"，
# 装了但永远跑不起来。改由常驻引擎（api_server）自己按日触发：
# 引擎进程本身有目录权限，且 24 小时在跑；launchd 保留为备份，靠同一个
# 状态文件互斥，谁先跑谁写，后到者当天直接跳过。
EQUITY_DAILY_AUTO_ENABLED = os.getenv(
    "EQUITY_DAILY_AUTO_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
EQUITY_DAILY_HOUR = max(0, min(23, int(os.getenv("EQUITY_DAILY_HOUR", "5"))))
EQUITY_DAILY_MINUTE = max(0, min(59, int(os.getenv("EQUITY_DAILY_MINUTE", "10"))))
EQUITY_DAILY_CHECK_S = max(60, int(os.getenv("EQUITY_DAILY_CHECK_S", "600")))

# AI 交易复盘：每日自动复盘的小时（本地时间，0=午夜后第一次检查执行）
REVIEW_HOUR = max(0, min(23, int(os.getenv("REVIEW_HOUR", "0"))))

# "今日信号"新鲜度窗口（小时）：只把最近 N 小时内产生的 BUY/SELL 算作实时信号。
# 早场 LLM 健康时生成的信号，过了这个窗口就视作"陈旧"，不入主统计数字。
# 目的：LLM/数据源全挂时，"今日信号"卡显示真实状态（=0），不靠早上留下的 stale 单撑场。
# 默认 4 小时 ≈ 一个交易时段（亚盘 / 欧盘 / 美盘 任选其一）；
# 设 0 = 关掉 fresh 过滤，行为退化到原始全天计数。
FRESH_SIGNAL_HOURS = max(0, int(os.getenv("FRESH_SIGNAL_HOURS", "4")))

# Asset-specific impact thresholds for forward-tracker verdict ruling
IMPACT_THRESHOLD = {"BTC": 2.0, "ETH": 2.0, "SOL": 2.0,
                    "XAU": 1.0, "GOLD": 1.0,
                    "WTI": 1.5}


# ══════════════════════════════════════════════════════════════════
# v187：按 purpose 路由模型池（每通道独立选模型 + 独立 key）
# 调用方传 purpose="main"/"gate"/"anomaly"/"reflection"/"advisor"/"sql"/"optimizer"
# 系统自动选该池里 weight 最高的条目（实际可加重试/故障转移）。
# ──────────────────────────────────────────────────────────────────
_VALID_PURPOSES = ("main", "gate", "anomaly", "reflection", "advisor", "sql", "optimizer",
                   "verify")


def get_pool_for_purpose(purpose: str) -> List[Dict[str, Any]]:
    """返回 purpose 对应的模型池（已按 weight 降序；空池则返回主池作 fallback）。

    每条 entry 含字段: id, request_model, label, api_base, api_key, weight,
    extra_body, purpose(list), key_alias(str), is_local(bool), timeout(float)。
    """
    pool = [m for m in AI_MODEL_ROSTER if purpose in (m.get("purpose") or [])]
    if not pool:
        # 兜底：返回主池（向后兼容 v186 前的代码）
        pool = [m for m in AI_MODEL_ROSTER if "main" in (m.get("purpose") or [])]
    return sorted(pool, key=lambda m: -m.get("weight", 1))


def pick_model_for_purpose(purpose: str, *, default_id: Optional[str] = None) -> Dict[str, Any]:
    """从 purpose 池里选一个 model dict（默认取 weight 最高的）。

    default_id: 显式指定 model.id 时优先用,否则取池中最高 weight。
    返回的 dict 已经过 build_model_config 风格的处理（id/label/api_base/api_key/extra_body）。
    """
    pool = get_pool_for_purpose(purpose)
    if not pool:
        raise RuntimeError(f"No AI model available for purpose={purpose}")
    target: Optional[Dict[str, Any]] = None
    if default_id:
        target = next((m for m in pool if m.get("id") == default_id), None)
    if target is None:
        target = pool[0]  # 池已按 weight 降序
    # 复制并标准化字段（与 build_model_config 输出对齐）
    api_base = str(target.get("api_base") or AIPING_BASE_URL)
    return {
        "id": target["id"],
        "request_model": str(target.get("request_model") or target["id"]),
        "label": target.get("label", target["id"]),
        "api_base": api_base,
        "api_key": str(target.get("api_key") or AIPING_API_KEY),
        "extra_body": target.get("extra_body", {}),
        "use_proxy": bool(target.get("use_proxy", False)),
        "json_mode": "aiping.cn" in api_base,
        # v187 扩展字段
        "purpose": target.get("purpose", []),
        "key_alias": target.get("key_alias", "unknown"),
        "is_local": bool(target.get("is_local", False)),
        "timeout": float(target.get("timeout", 60.0)),
    }


def key_alias_for_purpose(purpose: str) -> str:
    """purpose → 账单追踪用 key 别名（minimax-gate / minimax-reflect / local / ...）。"""
    pool = get_pool_for_purpose(purpose)
    if pool:
        return pool[0].get("key_alias", "unknown")
    return "unknown"


# ══════════════════════════════════════════════════════════════════
# v189-7：Coinglass + LSR 中转 API（行情衍生品多空比数据）
# 服务: https://vip2.coinglass.site  (手册: Coinglass_LSR_API_用户使用手册_v4)
# 认证: 请求头 X-API-Key (手册要求不放 URL); Key 通常以 cg_ 开头.
# 配额: coinglass standard 10/60s + lsr standard 10/60s (两套独立窗口).
# 实测 (2026-09-25 20:29 北京时间):
#   - account: plan=standard 双 standard, remaining_days=1 (明天过期)
#   - lsr/signal/BTC?mode=trader: ratio=4.17 (+316%), delta_2m/30m/4h 同向+0.07/0.12/0.74
#   - lsr/signal/BTC?mode=whale:  返回完整 whale_ratio 字段
#   - futures/coins-markets: code="0", BTC 完整 OI/funding/volume 数据
#   - WebSocket enabled=false (试用套餐不包含, 不尝试)
# ──────────────────────────────────────────────────────────────────
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()
COINGLASS_BASE_URL = os.getenv("COINGLASS_BASE_URL", "https://vip2.coinglass.site").strip().rstrip("/")
COINGLASS_ENABLED = _env_bool("COINGLASS_ENABLED", False)
COINGLASS_LSR_POLL_SECONDS = max(5, int(os.getenv("COINGLASS_LSR_POLL_SECONDS", "60")))
COINGLASS_SYMBOLS = tuple(
    s.strip().upper() for s in os.getenv("COINGLASS_SYMBOLS", "BTC,ETH,SOL").split(",") if s.strip()
)
# key 过期日（仅用于启动时打印警告，不强制停服；过期时 API 自动返 401）
COINGLASS_EXPIRES_AT = os.getenv("COINGLASS_EXPIRES_AT", "").strip()
# remaining ≤ 此值时 watcher 加倍 sleep, 避免打满配额 (实测 standard 10/60s)
COINGLASS_RATE_WARN_REMAINING = max(1, int(os.getenv("COINGLASS_RATE_WARN_REMAINING", "3")))


def coinglass_key_masked() -> str:
    """返回脱敏后的 key 用于日志 (前 8 位 + ...), key 为空时返回 '<not_set>'."""
    if not COINGLASS_API_KEY:
        return "<not_set>"
    return COINGLASS_API_KEY[:8] + "***"


# ════════════════════════════════════════════════════════════════════════════
# v200+ Q5 自适应遗忘三件套 — 衰减/保护期/门槛都跟样本量挂钩
# ════════════════════════════════════════════════════════════════════════════
#
# 设计动机: 数据稀疏时 (早期 cold start) 应该"宽容", 让早期经验/知识节点有
#   成长空间; 数据充足时 (样本 > 30) 才"严格", 加速衰减/拉高门槛/缩短保护期.
# 这样避免冷启动时被一两条样本"误杀"所有知识/经验, 同时又不让数据多了之后
#   衰减变慢 → 失忆风险.
#
# 三件套互相独立, 但都遵循同一个核心公式:
#   样本稀疏 → 宽容 (衰减慢, 保护期长, 门槛低)
#   样本充足 → 严格 (衰减快, 保护期短, 门槛高)
#
# 由 os.getenv 可被 .env 覆盖; 默认值是 v200+ 实战调优值, 不必改.
# ════════════════════════════════════════════════════════════════════════════

ADAPTIVE_PROTECT_VERSION_MIN = max(1, int(os.getenv("ADAPTIVE_PROTECT_VERSION_MIN", "1")))
ADAPTIVE_PROTECT_VERSION_MAX = max(3, int(os.getenv("ADAPTIVE_PROTECT_VERSION_MAX", "11")))
ADAPTIVE_PROTECT_VERSION_SPARSE_THRESHOLD = max(1, int(os.getenv("ADAPTIVE_PROTECT_VERSION_SPARSE_THRESHOLD", "10")))

ADAPTIVE_DECAY_CORE_SPARSE = float(os.getenv("ADAPTIVE_DECAY_CORE_SPARSE", "0.9"))
ADAPTIVE_DECAY_CORE_DENSE = float(os.getenv("ADAPTIVE_DECAY_CORE_DENSE", "0.7"))
ADAPTIVE_DECAY_COND_SPARSE = float(os.getenv("ADAPTIVE_DECAY_COND_SPARSE", "0.95"))
ADAPTIVE_DECAY_COND_DENSE = float(os.getenv("ADAPTIVE_DECAY_COND_DENSE", "0.85"))

# v200 自适应经验门槛。默认值必须与 adaptive_min_samples() 的文档契约一致:
#   total < 30  → 门槛 5,  confidence="low"   (早期经验也能跑)
#   total >= 30 → 门槛 10, confidence="normal" (样本够了才用严格门槛)
# 旧默认 (2 / 3 / 60) 与函数自己的 docstring、验收测试
# (tests/test_v200_adaptive_forgetting.py) 都不一致: 稀疏期门槛低到 2 条经验
# 就敢生成"经验", 稠密期门槛反而只有 3 —— 越有数据越不严格, 方向反了。
ADAPTIVE_INSIGHT_MIN_SAMPLE_SPARSE = max(1, int(os.getenv("ADAPTIVE_INSIGHT_MIN_SAMPLE_SPARSE", "5")))
ADAPTIVE_INSIGHT_MIN_SAMPLE_DENSE = max(2, int(os.getenv("ADAPTIVE_INSIGHT_MIN_SAMPLE_DENSE", "10")))
ADAPTIVE_INSIGHT_TOTAL_SPARSE_THRESHOLD = max(5, int(os.getenv("ADAPTIVE_INSIGHT_TOTAL_SPARSE_THRESHOLD", "30")))


def adaptive_decay_factor(sample_count: int) -> tuple:
    """自适应衰减倍数 — 返回 (core_decay, condition_decay).

    数据稀疏 (sample_count < 10): 用更宽容的衰减
      core * 0.9 / condition * 0.95 (旧值 0.7 / 0.85)
    数据充足 (sample_count >= 10): 用严格的衰减
      core * 0.7 / condition * 0.85 (旧值)

    examples:
        >>> adaptive_decay_factor(0)
        (0.9, 0.95)
        >>> adaptive_decay_factor(20)
        (0.7, 0.85)
    """
    try:
        n = int(sample_count or 0)
    except (TypeError, ValueError):
        n = 0
    if n < ADAPTIVE_PROTECT_VERSION_SPARSE_THRESHOLD:
        return (ADAPTIVE_DECAY_CORE_SPARSE, ADAPTIVE_DECAY_COND_SPARSE)
    return (ADAPTIVE_DECAY_CORE_DENSE, ADAPTIVE_DECAY_COND_DENSE)


def adaptive_protect_version(sample_count: int) -> int:
    """自适应保护期 — 数据越多保护期越短.

    样本 < 10 → 起步 3 (防冷启动误杀);
    样本 >= 10 → max(MIN, min(sample_n // 2, MAX)).

    examples:
        >>> adaptive_protect_version(0)
        3
        >>> adaptive_protect_version(20)
        10
        >>> adaptive_protect_version(50)
        11
    """
    try:
        n = int(sample_count or 0)
    except (TypeError, ValueError):
        n = 0
    if n < ADAPTIVE_PROTECT_VERSION_SPARSE_THRESHOLD:
        return ADAPTIVE_PROTECT_VERSION_MIN + 2
    cap = ADAPTIVE_PROTECT_VERSION_MAX
    return max(ADAPTIVE_PROTECT_VERSION_MIN, min(n // 2, cap))


def adaptive_min_samples(total_review_samples: int) -> tuple:
    """自适应经验门槛 — 返回 (min_samples, confidence_flag).

    数据稀疏 (total < 30): 降低门槛让早期经验也能跑, 标 confidence="low".
    数据充足 (total >= 30): 严格门槛 10, confidence="normal".

    examples:
        >>> adaptive_min_samples(10)
        (5, 'low')
        >>> adaptive_min_samples(50)
        (10, 'normal')
    """
    try:
        n = int(total_review_samples or 0)
    except (TypeError, ValueError):
        n = 0
    if n < ADAPTIVE_INSIGHT_TOTAL_SPARSE_THRESHOLD:
        return (ADAPTIVE_INSIGHT_MIN_SAMPLE_SPARSE, "low")
    return (ADAPTIVE_INSIGHT_MIN_SAMPLE_DENSE, "normal")


# ──────────────────────────── v316 Crowd Pulse ────────────────────────────
# 二级市场人群画像 / 权威度 / 月榜。
# 全部数据来自真实抓取（crowd_sources.py），每条原始条目带 url 可回溯；
# 采集失败时如实记入 crowd_source_runs.errors_json，绝不用占位数据填充。

# 总开关（0/false/no/off 关闭）
CROWD_PULSE_ENABLED = os.getenv("CROWD_PULSE_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")

# 参与采集的源（逗号分隔）。可用值见 crowd_sources.SOURCE_FETCHERS：
#   baidu_hot / baidu_finance / ths_hot / coingecko / gnews / gnews_crypto
# gnews_crypto = Google News 英文加密/商品语料池（v317）。
#   单独成一个源的原因：该语料池覆盖的资产（BTC/ETH/SOL/XAU/XAG/WTI）
#   在本系统有分钟级行情，能做「注意力是否先于价格移动」的验证；
#   中文财经语料池覆盖的 A 股没有分钟级行情，验不了。
CROWD_PULSE_SOURCES = tuple(
    s.strip() for s in os.getenv(
        "CROWD_PULSE_SOURCES",
        "baidu_hot,baidu_finance,ths_hot,coingecko,gnews,gnews_crypto").split(",")
    if s.strip()
)

# 采集间隔（分钟）
CROWD_PULSE_INTERVAL_MINUTES = max(
    5, int(os.getenv("CROWD_PULSE_INTERVAL_MINUTES", "60")))

# Google News RSS 的检索式（决定"财经媒体在讨论什么"）
CROWD_PULSE_GNEWS_QUERY = os.getenv(
    "CROWD_PULSE_GNEWS_QUERY", "股市 OR 美股 OR A股")

# ──────────────────── v317 注意力领先（attention lead）验证 ────────────────────
# 检验命题：跨平台注意力（多个独立平台同时提到同一资产）是否**先于**有意义的
# 价格移动出现。全部参数可调，且每个阈值都会写进 explain 文本，可审计。

# 注意力事件的最少独立平台数（1=任意提及；>=2 才算跨平台共识）
CROWD_LEAD_MIN_PLATFORMS = max(1, int(os.getenv("CROWD_LEAD_MIN_PLATFORMS", "1")))

# 判定"有意义的价格移动"的幅度阈值（百分比，如 0.35 表示 0.35%）
CROWD_LEAD_MOVE_PCT = max(0.01, float(os.getenv("CROWD_LEAD_MOVE_PCT", "0.35")))

# 价格移动的观察窗口（分钟）
CROWD_LEAD_HORIZON_MINUTES = max(
    5, int(os.getenv("CROWD_LEAD_HORIZON_MINUTES", "180")))

# 回看窗口（小时）—— 从这个窗口内找注意力事件
CROWD_LEAD_LOOKBACK_HOURS = max(
    1, int(os.getenv("CROWD_LEAD_LOOKBACK_HOURS", "72")))

# 价格数据的时间框（kline_bars.timeframe）
CROWD_LEAD_TIMEFRAME = os.getenv("CROWD_LEAD_TIMEFRAME", "1m")

# 榜单返回条数上限
CROWD_PULSE_TOP_N = max(1, int(os.getenv("CROWD_PULSE_TOP_N", "50")))

# ── v322: LLM 额度保险丝（Budget Fuse）────────────────────────────────────
# 事故背景（2026-11-04）：4 个 MiniMax 通道全挂时主循环仍在疯狂重试，
# 单条新闻最多 30+ 次 HTTP 请求 → 账户余额被打穿（用户反馈"流量被干没了"）。
# 熔断器只防"通道全挂"，防不住"通道半死不活、持续小口烧额度"。
# 所以加三道**与通道健康度无关**的硬闸门 —— 看今天花了多少钱，超了就停。
#
#   LLM_DAILY_TOKEN_BUDGET    每日 token 上限（0 = 不限）
#   LLM_DAILY_REQUEST_BUDGET  每日请求数上限（0 = 不限）← 最兜底的一道
#   LLM_DAILY_NEWS_BUDGET     每日信号条数上限（0 = 不限）
#
# 默认值按"一天正常跑几百条新闻"估算：主分析单条约 12k token（10k 输入 + 4k 输出）。
# 想彻底止血就把三个值都调小；想放开就调大或设 0。
LLM_DAILY_TOKEN_BUDGET = max(0.0, float(os.getenv("LLM_DAILY_TOKEN_BUDGET", "0")))
LLM_DAILY_REQUEST_BUDGET = max(0.0, float(os.getenv("LLM_DAILY_REQUEST_BUDGET", "0")))
LLM_DAILY_NEWS_BUDGET = max(0.0, float(os.getenv("LLM_DAILY_NEWS_BUDGET", "0")))

# ── v322: LLM 速率闸门（Rate Fuse）──────────────────────────────────────────
# 总额度闸门管「今天一共花多少」，管不住「这一秒同时打出去多少」。
# 事故形态（用户描述的「突然抽风」）：
#   通道恢复 → 熔断冷却结束 → 所有排队请求同时醒来 → 几十个并发打向同一个 key
#   → 立刻 429 → 再次熔断 → 循环。总用量其实不多，但服务表现为「时好时坏」。
#
# LLM_MAX_RPM = 最近 60 秒内允许的最大 LLM 请求数（0 = 不限制）。
#   · 跨进程统一计数（engine / api / review_agent / judge_worker 四个进程
#     各打各的，不统一会把并发放大 4 倍）
#   · 滑动窗口，不是固定整分钟窗口（避免整点对齐的突发）
#   · 在**真正发 HTTP 请求前**扣名额，不是认领时扣（认领时扣会白扣）
#
# 取值建议：当前实际用量约 8 次/分钟，设 120 有 15 倍余量 ——
# 既完全不影响正常使用，又能挡住「一瞬间并发几十个」的打法。
LLM_MAX_RPM = max(0.0, float(os.getenv("LLM_MAX_RPM", "0")))

# 跨进程熔断共享：=1 时熔断状态落盘，一个进程发现通道坏了其它进程一起冷却。
# 熔断状态是进程内的 → 多进程各自重试 → 同一份额度被放大 N 倍消耗。
LLM_BREAKER_SHARED = os.getenv(
    "LLM_BREAKER_SHARED", "1").strip().lower() in ("1", "true", "yes")

# 全局急停开关：=1 时**任何** LLM 调用都不发出去（认领前就停，零请求零额度）。
# 用于"怀疑在烧钱"时一键止血：改 .env → 重启引擎 → 完全停止。
LLM_GLOBAL_KILL_SWITCH = os.getenv(
    "LLM_GLOBAL_KILL_SWITCH", "0").strip().lower() in ("1", "true", "yes")

# 门卫本地优先：=1 时先只打**本地**模型（免费），本地答得上来就完全不发远程请求；
# 本地失败/超时才回退远程通道。
#
# ⚠️ 默认 0（关闭）—— 2026-11-04 实测结论：
#   本机只有 8GB 物理内存，且被 GUI 应用（豆包/VSCode/Chrome）占满，
#   load average 长期 700+、空闲内存仅 12MB → Ollama 加载 1.9GB 权重后
#   被 swap 到磁盘，实测 chat/completions **90 秒不返回**（/api/tags 却秒回，
#   说明服务活着、纯推理被内存拖死）。
#   开着只会让每条新闻白等超时再回退远程 —— 既不省钱又拖慢 ingest。
#   内存扩容到 16GB+ 且空闲内存 >3GB 后，可以改回 1 启用。
GATE_LOCAL_FIRST = os.getenv(
    "GATE_LOCAL_FIRST", "0").strip().lower() in ("1", "true", "yes")

# 门卫走本地通道时的等待上限（秒）。超过就放弃本地、直接回退远程，
# 避免本地模型太慢把 ingest 拖住。
GATE_LOCAL_TIMEOUT_S = max(1.0, float(os.getenv("GATE_LOCAL_TIMEOUT_S", "8")))

# ── v322: 门卫远程通道「阶梯降级」（省请求数的真正抓手）──────────────────
# 实测（2026-11-04）：门卫今天 4397 次调用 / 606 条入库新闻 = **每条 7.26 次**，
# 而主分析只用了 607 次。根因：`_call_pool_sync` 把一条新闻**并行发给池里
# 全部远程通道**，但只用「第一个返回成功」的结果（无投票逻辑）——
# 于是每条新闻的请求数 = 通道数 × (L2 + L3) ≈ 7，其中 6 次是纯浪费，
# 还顺带把 284 次 429 错误和 88% 的请求配额全烧在门卫上。
#
# 阶梯降级：按 weight 顺序**逐级**尝试 —— 第 1 级只打主通道，成功即返回；
# 第 1 级全失败才进第 2 级（其余通道并行兜底）。
# 效果：主通道正常时每条新闻只花 1 次请求（省 ~86%），延迟不变
#       （原本也是"任一成功即返回"）；主通道坏了自动降级，行为不退化。
# 置 0 可一键回退到旧的「全池并行」行为。
GATE_REMOTE_STAIR_STEP = os.getenv(
    "GATE_REMOTE_STAIR_STEP", "1").strip().lower() in ("1", "true", "yes")

# ── v322: 门卫结果缓存（省钱的真正抓手）──────────────────────────────────
# 实测（2026-11-04）：门卫 4278 次调用 / 3.68M token，占全天 40%，
# 而主分析只产出 244 条信号 —— 每条信号搭了 ~17 次门卫调用。
# 门卫是 temperature=0 的确定性判断（prompt 只含标题+摘要）→ 同输入同输出，
# 缓存不改变判定结果，却能省掉重复新闻 / 多源转载 / gate-replay 回放的重复付费。
#
# 内存约束：本机只有 8GB RAM，所以上限刻意保守（500 条 / 6 小时）。
GATE_CACHE_ENABLED = os.getenv(
    "GATE_CACHE_ENABLED", "1").strip().lower() in ("1", "true", "yes")
GATE_CACHE_MAX_ENTRIES = max(0, int(os.getenv("GATE_CACHE_MAX_ENTRIES", "500")))
GATE_CACHE_TTL_S = max(0.0, float(os.getenv("GATE_CACHE_TTL_S", "21600")))

# 额度账本（DB）读不到时是否按"已超预算"拦截（fail-closed）。
# 默认 1 —— 宁可停下也不烧钱。设 0 可恢复"账本读不到就放行"的宽松行为。
LLM_BUDGET_FAIL_CLOSED = os.getenv(
    "LLM_BUDGET_FAIL_CLOSED", "1").strip().lower() in ("1", "true", "yes")

# 单条主分析允许的最大输出 token（八段式 JSON，4000 足够且是上界保险丝）
LLM_MAIN_MAX_OUTPUT_TOKENS = max(500, int(os.getenv("LLM_MAIN_MAX_OUTPUT_TOKENS", "4000")))

# 单条新闻允许的最大总估算 token（含输入）。超了直接放弃这条，
# 防止"一条超长新闻吃掉全天预算"。
LLM_MAX_TOKENS_PER_NEWS = max(1000, int(os.getenv("LLM_MAX_TOKENS_PER_NEWS", "30000")))

# 单次 HTTP 超时（秒）
CROWD_PULSE_HTTP_TIMEOUT = max(3, int(os.getenv("CROWD_PULSE_HTTP_TIMEOUT", "15")))

# ── v321.3 P1: 出网代理 (需求 #6 挂单量/资金流 / #14 外部源) ─────────────────
# 实测: 本机 VPN 是 **IPv6-only TUN** (utun0/1/2 只有 IPv6 地址, IPv4 默认路由
# 仍走 192.168.3.1) → 对 Binance / Yahoo / FRED 这类**需要 IPv4** 的源无效。
# 而「代理模式」的客户端会开一个**本地 HTTP/SOCKS 端口**, 需要显式告诉 Python
# 走它 —— 关键是 **launchd 拉起的进程不继承 shell 的环境变量**, 所以只在终端里
# export 没用, 必须落到 .env + launchd plist。
#
# 用法 (填 .env, 重启 api/bridge/engine 即生效):
#     HTTP_PROXY=http://127.0.0.1:7890
#     HTTPS_PROXY=http://127.0.0.1:7890
#     ALL_PROXY=socks5://127.0.0.1:7890    # SOCKS 需 requests[socks] 才有意义
#
# 填了之后立刻能通的外网源 (实测): Binance / Yahoo Finance / FRED / Coinglass
# 付费接口 —— 也就是归因页的**挂单量 + 资金费率**能直接拿到真数据。
HTTP_PROXY = (os.getenv("HTTP_PROXY") or "").strip()
HTTPS_PROXY = (os.getenv("HTTPS_PROXY") or "").strip()
ALL_PROXY = (os.getenv("ALL_PROXY") or "").strip()
PROXY_ENABLED = bool(HTTPS_PROXY or HTTP_PROXY or ALL_PROXY)


def apply_proxy_env() -> bool:
    """把代理写进 os.environ —— 让 requests / urllib 走代理。

    返回是否应用成功 (没配代理时返回 False, 不报错)。

    为什么必须显式写 os.environ:
      · urllib.request 默认读环境变量 (getproxies()), 但**进程启动后**再改不一定生效;
      · requests 只在 **Session 创建时**读一次代理 —— 所以必须尽早 (进程启动时) 设置。
    """
    if not PROXY_ENABLED:
        return False
    if HTTP_PROXY:
        os.environ.setdefault("HTTP_PROXY", HTTP_PROXY)
    if HTTPS_PROXY:
        os.environ.setdefault("HTTPS_PROXY", HTTPS_PROXY)
    if ALL_PROXY:
        os.environ.setdefault("ALL_PROXY", ALL_PROXY)
    try:
        import urllib.request
        urllib.request.install_opener(urllib.request.build_opener())
    except Exception:
        pass
    print(f"[PROXY] 已启用出网代理: {HTTPS_PROXY or HTTP_PROXY or ALL_PROXY}", flush=True)
    return True
