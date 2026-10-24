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
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths & .env
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/

load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH = os.getenv("TRIDENT_DB_PATH") or os.path.join(BASE_DIR, "trident_event_bus.db")
PROJECT_DIR = os.path.dirname(BASE_DIR)
TZ_SHANGHAI = timezone(timedelta(hours=8))

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
    """
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    # epoch 秒(10位)/毫秒(13位)纯数字：多数海外/程序化接口用 UTC 纪元
    if s.isdigit() and len(s) in (10, 13):
        try:
            secs = float(s) / 1000.0 if len(s) == 13 else float(s)
            return datetime.fromtimestamp(secs, tz=TZ_SHANGHAI)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # 常见无时区格式（空格/T 分隔、斜杠分隔）——按北京时间解读
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s[:19], fmt)
                return dt.replace(tzinfo=TZ_SHANGHAI)
            except ValueError:
                continue
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)  # 无时区 → 按北京时间
    return dt.astimezone(TZ_SHANGHAI)


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
    "BINANCE_NEWS_API_URL", "http://127.0.0.1:8000/api/trading/latest"
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
# 异常归因 — attribute_5m_abnormal / attribute_market_drivers
MINIMAX_ANOMALY_KEY  = _pick_key("MINIMAX_ANOMALY_KEY") or MINIMAX_API_KEY
MINIMAX_ANOMALY_MODEL = os.getenv("MINIMAX_ANOMALY_MODEL", "MiniMax-M3").strip()
# 反思池（盲区反思 / 顾问 / Text-to-SQL / 参数优化器共享 1 个 key）
MINIMAX_REFLECT_KEY    = _pick_key("MINIMAX_REFLECT_KEY") or MINIMAX_API_KEY
MINIMAX_REFLECT_MODEL  = os.getenv("MINIMAX_REFLECT_MODEL", "MiniMax-M2.7").strip()
MINIMAX_ADVISOR_MODEL  = os.getenv("MINIMAX_ADVISOR_MODEL", "MiniMax-M2.7-highspeed").strip()
MINIMAX_SQL_MODEL      = os.getenv("MINIMAX_SQL_MODEL", "MiniMax-M2.7-highspeed").strip()
MINIMAX_OPTIMIZER_MODEL = os.getenv("MINIMAX_OPTIMIZER_MODEL", "MiniMax-M2.7-highspeed").strip()

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

# 1) MiniMax 主通道（国内直连，实测稳定 1-5s）——主力
# 2026-09-22 调整（v176.6）：Text-01 实测频繁 HTTP 429 配额耗尽（昨日 2539 次 rate_limit，占全部
# 失败 61%），且是主池 + fallback 首选，导致主分析连环失败、六段落入"服务端自动补全"占位。
# 账户1 主通道改为与账户2 一致的 MiniMax-M3（两个账户并列跑 M3，配额分散、429 骤降），彻底
# 剔除 Text-01 进主池。M3 一律禁用 thinking 块（防其把 token 全耗在推理、不输出 JSON）。
if MINIMAX_API_KEY and MINIMAX_MAIN_WEIGHT > 0:
    _m1_extra: Dict[str, Any] = {}
    if (MINIMAX_MODEL or "").upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        _m1_extra["thinking"] = {"type": "disabled"}  # 禁用思考块，强制直接输出 JSON
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
AI_MODEL_ROSTER: tuple = tuple(_AI_MODEL_ROSTER_LIST)


# ══════════════════════════════════════════════════════════════════
# v187：本地 Ollama 兜底（任何 MiniMax 通道 402/网络失败时用）
# 默认 Ollama 端点 + qwen2.5-coder:7b（适合 JSON/SQL）；qwen3:latest 用于反思。
# ──────────────────────────────────────────────────────────────────
if LOCAL_LLM_ENABLED and LOCAL_LLM_BASE_URL:
    # 默认版（通用：主分析/异常归因/顾问/SQL/优化器）
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"local/{LOCAL_LLM_MODEL}",
        "request_model": LOCAL_LLM_MODEL,
        "label": f"{LOCAL_LLM_MODEL} (local-ollama)",
        "api_base": LOCAL_LLM_BASE_URL,
        "api_key": "local-no-auth",
        "use_proxy": False, "weight": 1,
        "extra_body": {"max_tokens": 1500},   # Ollama 不接受 thinking 字段
        "purpose": ["main", "anomaly", "advisor", "sql", "optimizer"],
        "key_alias": "local", "is_local": True,
        "timeout": LOCAL_LLM_TIMEOUT,
    })
    # 高速版（小模型，门卫/顾问/优化器）
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"local/{LOCAL_LLM_FAST_MODEL}",
        "request_model": LOCAL_LLM_FAST_MODEL,
        "label": f"{LOCAL_LLM_FAST_MODEL} (local-fast)",
        "api_base": LOCAL_LLM_BASE_URL,
        "api_key": "local-no-auth",
        "use_proxy": False, "weight": 1,
        "extra_body": {"max_tokens": 200},
        "purpose": ["gate", "advisor", "sql", "optimizer"],
        "key_alias": "local", "is_local": True,
        "timeout": LOCAL_LLM_TIMEOUT,
    })
    # 反思版（强思考，盲区反思）
    _AI_MODEL_ROSTER_LIST.append({
        "id": f"local/{LOCAL_LLM_REFLECT_MODEL}",
        "request_model": LOCAL_LLM_REFLECT_MODEL,
        "label": f"{LOCAL_LLM_REFLECT_MODEL} (local-reflect)",
        "api_base": LOCAL_LLM_BASE_URL,
        "api_key": "local-no-auth",
        "use_proxy": False, "weight": 1,
        "extra_body": {"max_tokens": 2000},
        "purpose": ["reflection"],
        "key_alias": "local", "is_local": True,
        "timeout": LOCAL_LLM_TIMEOUT,
    })

# 重新打包（上面动态 append 了新条目）
AI_MODEL_ROSTER: tuple = tuple(_AI_MODEL_ROSTER_LIST)


# ══════════════════════════════════════════════════════════════════
# v187：每通道独立 MiniMax 专用通道（独立 key + 独立模型）
# 每个通道独立 key,独立模型名;余额不足时本地 Ollama 自动兜底(见下)。
# ──────────────────────────────────────────────────────────────────
# 7) 门卫筛选专用通道（独立 key,每条新闻 verdict=可交易/只能看/排除）
if MINIMAX_GATE_KEY and MINIMAX_GATE_KEY != MINIMAX_API_KEY:
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
        "purpose": ["gate"], "key_alias": "minimax-gate",
    })
# 8) 异常归因专用通道
if MINIMAX_ANOMALY_KEY and MINIMAX_ANOMALY_KEY != MINIMAX_API_KEY:
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
if MINIMAX_REFLECT_KEY and MINIMAX_REFLECT_KEY != MINIMAX_API_KEY:
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

# 快速模拟评测：所有 BUY/SELL 信号按真实行情建仓，短周期结算，用于盈利率评测。
# 与正式模拟盘（多证据闸门 + 策略闸门）互不干扰，闸门结论只作为分组标记保存。
# 注意：持仓时长在建仓时写入每笔记录（quick_sim_trades.horizon_minutes），
#       改这里只影响之后新开的仓；已建仓的仍按各自记录里的时长结算。
QUICK_SIM_ENABLED = os.getenv("QUICK_SIM_ENABLED", "1").strip() not in ("0", "false", "False")
QUICK_SIM_HORIZON_MINUTES = max(1, int(os.getenv("QUICK_SIM_HORIZON_MINUTES", "20")))
QUICK_SIM_NOTIONAL_USDT = max(1.0, float(os.getenv("QUICK_SIM_NOTIONAL_USDT", "100")))


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
_sell_blocked_raw = os.getenv("SELL_GATE_BLOCKED_ASSETS", "BTC,WTI").strip()
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
NEWS_MAX_AGE_HOURS = max(1, int(os.getenv("NEWS_MAX_AGE_HOURS", "12")))
NEWS_MAX_AGE_FALLBACK_HOURS = max(1, int(os.getenv("NEWS_MAX_AGE_FALLBACK_HOURS", "2")))
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

# 新闻入库新鲜度闸（小时）：平台发布时间距今超过该时长 → 判定为"过时新闻"直接拒收，
# 不入库、不进实时信号台、不占 AI 配额（防平台补发历史旧闻/feed 缓存滞后）。
# 仅对"带平台发布时间"的条目生效；实时推送类（WS/webhook，无平台时间字段）
# 按"到达即发布"处理不受此闸影响。0/缺省 = 3 小时（快讯类平台正常分钟级，足够宽）。
NEWS_INGEST_MAX_AGE_HOURS = max(1, int(os.getenv("NEWS_INGEST_MAX_AGE_HOURS", "12")))

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
EQUITY_TICKERS = tuple(t.strip().upper() for t in os.getenv(
    "EQUITY_TICKERS", "NVDA,MSTR,COIN,MARA,RIOT"
).split(",") if t.strip())
# 美股基本面缓存时长（秒）：免费额度紧，默认 6 小时一次网络拉取，命中缓存直接复用。
EQUITY_CACHE_TTL = max(300, int(os.getenv("EQUITY_CACHE_TTL", "21600")))

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
_VALID_PURPOSES = ("main", "gate", "anomaly", "reflection", "advisor", "sql", "optimizer")


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

ADAPTIVE_INSIGHT_MIN_SAMPLE_SPARSE = max(1, int(os.getenv("ADAPTIVE_INSIGHT_MIN_SAMPLE_SPARSE", "2")))
ADAPTIVE_INSIGHT_MIN_SAMPLE_DENSE = max(2, int(os.getenv("ADAPTIVE_INSIGHT_MIN_SAMPLE_DENSE", "3")))
ADAPTIVE_INSIGHT_TOTAL_SPARSE_THRESHOLD = max(5, int(os.getenv("ADAPTIVE_INSIGHT_TOTAL_SPARSE_THRESHOLD", "60")))


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
