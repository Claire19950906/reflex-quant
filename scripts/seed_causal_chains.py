# -*- coding: utf-8 -*-
"""预置因果链库种子（幂等：已存在同名+同因则不重复插入）。

种子数据单一来源 = causal_chains.DEFAULT_SEEDS（与 init_db.py 自动播种共用，
避免两份数据漂移）。本脚本供手动补种/修复用：

    cd scripts && python seed_causal_chains.py

覆盖：美联储利率 / 通胀数据 / OPEC 原油 / 地缘避险 / 加密监管 ETF /
美债收益率 / 美元指数 / 流动性宽松。触发词命中的新闻才注入 AI 分析。
"""
import os
import sys
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # scripts -> 项目根
sys.path.insert(0, os.path.join(BASE, "backend", "src_python"))

import causal_chains
import db


def main() -> int:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        db.migrate(conn)  # 确保 causal_chains 表存在
        result = causal_chains.seed_defaults(conn)
        print(f"完成：新增 {result['added']} 条，库里共 {result['total']} 条")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
