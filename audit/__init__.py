"""audit — 决策审计层（独立包，无数据库强依赖）

对外只有一个入口：

    >>> from audit import audit
    >>> report = audit(decision, ctx=None)
    >>> report["verdict"], len(report["findings"])

设计约束（见 README）：
1. 输入是 decision.v1 契约（dict），不要求结算、不要求盈亏。
2. 不 import `db`、不写库、不改任何业务表。
3. 每条 finding 必带 evidence_refs + remediation，缺一即进 incomplete 清单。
4. 无法执行的层必须显式报 not_applicable，禁止静默跳过。
"""

from __future__ import annotations

from .core.context import CheckContext
from .core.pipeline import audit, audit_many

__all__ = ["audit", "audit_many", "CheckContext", "__version__"]
__version__ = "1.0.0"
