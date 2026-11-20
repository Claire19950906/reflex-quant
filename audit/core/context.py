"""CheckContext —— 审计层的唯一外部依赖入口。

两种构造方式：

    CheckContext.from_sqlite(path)     直连库（最快，内部比对用）
    CheckContext.from_bundle(bundle)   任意 provider → 内存快照（对外交付用）

空实现不是"静默跳过"：调用方必须看 `ctx.requires_satisfied(layer)`，
不满足的层在报告里写 not_applicable + 原因。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..providers import Bundle, FileBundle, HttpBundle, NullBundle, SqliteBundle, build_snapshot

# 每层依赖哪类 provider：None = 纯函数层，任何环境都能跑
# 依赖按"真实读取的表"细分，不再用笼统的 history：
#   causal / event_logic 读 asset_driver_nodes → drivers
#   historical_counterfactual 读 signal_performance → samples
#   sample_integrity 读 quick_sim_trades → trades
# 这样没有成交样本的客户，只有这一个子检查器跳过，不会整层作废。
LAYER_REQUIRES: Dict[str, Optional[str]] = {
    "data": None,
    "logic": None,
    "rules": "rules",
    "causal": "drivers",
    "market": None,
    "event_causality": None,   # 有子检查器，依赖在层内部逐个判定
    "reasoning": None,
}

_PROBE_TABLES = {
    "rules": "review_insights",
    "drivers": "asset_driver_nodes",
    "samples": "signal_performance",
    "trades": "quick_sim_trades",
    "decisions": "ai_decisions",
}

_REQUIRE_LABEL = {
    "rules": "铁律",
    "drivers": "驱动因子节点",
    "samples": "历史样本",
    "trades": "模拟成交样本",
    "decisions": "决策历史",
}

# 判断"具备"的最小行数（0 行等价于没有 provider）
_MIN_ROWS = {"rules": 1, "drivers": 1, "samples": 1, "trades": 1, "decisions": 1}


@dataclass
class CheckContext:
    """审计上下文。默认构造 = 纯离线模式（无任何 provider）。"""

    conn: Optional[sqlite3.Connection] = None
    available: Dict[str, bool] = field(default_factory=lambda: {"rules": False, "history": False, "kline": False})
    label: str = "null"
    read_only: bool = True
    notes: List[str] = field(default_factory=list)
    owned: bool = True  # False = 借用调用方的连接，close() 不关它

    # ── 构造器 ──────────────────────────────────────────────
    @classmethod
    def null(cls) -> "CheckContext":
        return cls()

    @classmethod
    def from_sqlite(cls, db_path: str | Path, *, read_only: bool = True) -> "CheckContext":
        path = str(db_path)
        if read_only:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15.0, check_same_thread=False)
        else:
            conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        available = {k: cls._probe(conn, t) for k, t in _PROBE_TABLES.items()}
        available["history"] = bool(available.get("samples") or available.get("trades"))
        available["kline"] = False
        return cls(conn=conn, available=available, label=f"sqlite:{path}", read_only=read_only)

    @classmethod
    def from_bundle(cls, bundle: Bundle) -> "CheckContext":
        """任意 provider → 内存快照。

        行数为 0 的 provider 视为"不具备"，对应层报 not_applicable，
        并在 notes 里保留 provider 自己的加载错误。
        """
        rules = bundle.rules()
        drivers = bundle.drivers()
        history = bundle.history()
        kline = bundle.kline()

        available = {
            "rules": len(rules.rules) >= _MIN_ROWS["rules"],
            "drivers": len(drivers.nodes) >= _MIN_ROWS["drivers"],
            "samples": len(history.samples) >= _MIN_ROWS["samples"],
            "trades": len(history.trades) >= _MIN_ROWS["trades"],
            "decisions": len(history.decisions) >= _MIN_ROWS["decisions"],
            "history": (len(history.samples) + len(history.trades)) >= 1,
            "kline": len(kline.bars) > 0,
        }
        conn = build_snapshot(rules, drivers, history, kline)
        notes = list(getattr(bundle, "errors", []) or [])
        return cls(conn=conn, available=available, label=bundle.label, notes=notes)

    @classmethod
    def from_file(cls, root: str | Path) -> "CheckContext":
        return cls.from_bundle(FileBundle(root))

    @classmethod
    def from_http(cls, base_url: str, *, token: str | None = None, timeout: float = 10.0) -> "CheckContext":
        return cls.from_bundle(HttpBundle(base_url, token=token, timeout=timeout))

    @classmethod
    def export_from_sqlite(cls, db_path: str | Path) -> "CheckContext":
        """从现有库导出数据再走快照（验证 provider 契约是否完整）。"""
        bundle = SqliteBundle(db_path)
        try:
            return cls.from_bundle(bundle)
        finally:
            bundle.close()

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection, *, label: str = "conn") -> "CheckContext":
        """借用调用方已有的连接（不新建、不关闭）。

        门禁场景专用：决策刚 INSERT、尚未 commit，只有同一连接能看到这行；
        同时避免在写事务里再开一个 sqlite 连接（WAL 下会互相等锁）。
        """
        available = {k: cls._probe(conn, t) for k, t in _PROBE_TABLES.items()}
        available["history"] = bool(available.get("samples") or available.get("trades"))
        available["kline"] = False
        return cls(conn=conn, available=available, label=label, read_only=False, owned=False)

    # ── 查询 ────────────────────────────────────────────────
    @staticmethod
    def _probe(conn: sqlite3.Connection, table: str) -> bool:
        """表存在且非空才算"具备"。空表 = 没数据，不能当作"审过了没问题"。"""
        try:
            return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return False

    def requires_satisfied(self, layer: str) -> bool:
        need = LAYER_REQUIRES.get(layer)
        return need is None or bool(self.available.get(need, False))

    def why_not(self, layer: str) -> str:
        need = LAYER_REQUIRES.get(layer)
        if need is None or self.requires_satisfied(layer):
            return ""
        return self.unavailable_note(need)

    def unavailable_note(self, key: str) -> str:
        extra = f"；provider 告警：{' / '.join(self.notes[:2])}" if self.notes else ""
        label = _REQUIRE_LABEL.get(key, key)
        return f"缺少 {key} provider 数据（{label}，{self.label}）{extra}"

    def close(self) -> None:
        if not self.owned:
            self.conn = None
            return
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    def __enter__(self) -> "CheckContext":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
