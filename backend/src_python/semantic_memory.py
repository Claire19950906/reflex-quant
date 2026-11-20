# -*- coding: utf-8 -*-
"""v379 · 语义记忆检索层（semantic memory retrieval）

为什么需要
----------
此前 prompt 里的「历史经验」是按 **asset / event_type 精确匹配 + 时间倒序** 取的，
本质是关键词过滤：同一个机理、不同品种、不同措辞的历史教训永远召不回来。
结果是 prompt 越来越长（全量经验块），而真正相关的教训经常缺席。

本模块把库里已有的反思文本（trade_reflections_v2 / decision_reflection /
structured_reflections / experience_entries）建成 **字符 n-gram TF-IDF 向量**，
按「品种 × 事件类型 × 当前新闻文本」做余弦相似度召回 top-k。相对基线的三点差别：

1. **按相似度取，不按时间取** —— 只给 5 条，且每条截断到 ~110 字，省 token；
2. **同品种 / 同事件类型加权** —— 召回里优先给能直接用上的；
3. **零新表、零写入、零网络** —— 只读现有表，索引在内存里按 TTL 重建，
   不碰生产 DB 文件（符合 AGENTS.md 铁律 3）。

为什么不用 embedding 模型
--------------------------
本机没有常驻 embedding 服务（Ollama 已停），引入远程 embedding 会在每次分析前
多一跳网络 + 多一次额度消耗。字符 n-gram 向量对中文金融文本（大量四字术语、
品种代号、错误码）已经足够区分；接口留了 `EMBED_PROVIDER` 口子，后续要换真
embedding 只需替换 `embed_text()`，上层调用方式不变。

对外接口
--------
- `search(query, asset=None, event_type=None, k=5)` → list[dict]
- `load_semantic_block(asset, event_type, query)` → str（供 ai_worker 注入 prompt）
- `build_index(conn)` → 索引对象（测试用）
- CLI: `python semantic_memory.py --query "非农超预期" --asset XAU --k 5`
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# 分词：中文走字符 bigram（金融术语多为 2-4 字，bigram 已能切开），
#       英文/数字走小写词 + 数字串。不引入 jieba（离线、零依赖）。
# ──────────────────────────────────────────────────────────────────────────────
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9._\-]*")

# 高频无信息词：只去掉最钝的一层，不做停用词大全（金融文本里"市场/预期"有信息量）
_STOP_BIGRAMS = {
    "的了", "是的", "这一", "一个", "我们", "可以", "没有", "就是", "不是",
    "因为", "所以", "但是", "如果", "已经", "以及", "还有", "这个", "那个",
}


def _tokenize(text: str) -> List[str]:
    """把一段文本切成检索 token。"""
    if not text:
        return []
    low = str(text).lower()
    out: List[str] = []
    for m in _CJK_RE.finditer(low):
        seg = m.group(0)
        if len(seg) == 1:
            out.append(seg)
            continue
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in _STOP_BIGRAMS:
                out.append(bg)
    for m in _LATIN_RE.finditer(low):
        tok = m.group(0)
        if len(tok) >= 2:
            out.append(tok)
    return out


def _snippet(text: str, limit: int = 110) -> str:
    """压缩成一行可读摘要。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    # 去掉反思文本里的 hash 标记与内部路径（v345 起前端已剥，这里也剥一层）
    s = re.sub(r"\[hash:[0-9a-fA-F]+\]", "", s)
    s = re.sub(r"http://127\.0\.0\.1:\d+[^\s]*", "", s)
    s = s.strip(" |")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


# ──────────────────────────────────────────────────────────────────────────────
# 语料装载（只读）
# ──────────────────────────────────────────────────────────────────────────────
def _safe_rows(conn, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
    try:
        cur = conn.execute(sql, params)
        keys = [d[0] for d in (cur.description or [])]
        return [dict(zip(keys, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _load_corpus(conn, days: int = 400) -> List[Dict[str, Any]]:
    """从四张表拼出统一的检索语料。任何一张表缺字段都只是少一类来源，不抛错。"""
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")
    docs: List[Dict[str, Any]] = []

    # ① 单笔交易反思（最具体：有结果、有根因、有教训）
    for r in _safe_rows(conn, """
        SELECT id, asset, dimension, subcategory, reflection_text, root_cause, lesson,
               deep_lesson, verification, counterfactual, pnl_pct, hit, created_at
          FROM trade_reflections_v2
         WHERE created_at >= ?
         ORDER BY created_at DESC LIMIT 2000
    """, (since,)):
        body = " | ".join(str(x) for x in (
            r.get("reflection_text"), r.get("root_cause"), r.get("deep_lesson") or r.get("lesson"),
            r.get("counterfactual"), r.get("verification"),
        ) if x)
        docs.append({
            "doc_id": f"trade_reflection:{r.get('id')}",
            "source": "单笔反思",
            "asset": str(r.get("asset") or "").upper(),
            "event_type": str(r.get("subcategory") or r.get("dimension") or ""),
            "ts": str(r.get("created_at") or ""),
            "text": body,
            "outcome": r.get("pnl_pct"),
            "hit": r.get("hit"),
        })

    # ② 维度级复盘（窗口统计：一句话偏差结论 + 可执行建议）
    for r in _safe_rows(conn, """
        SELECT id, asset, window, bias_text, error_root_cause, actionable_suggestion,
               avg_pnl_pct, ts
          FROM decision_reflection
         WHERE ts >= ?
         ORDER BY ts DESC LIMIT 800
    """, (since,)):
        body = " | ".join(str(x) for x in (
            r.get("bias_text"), r.get("error_root_cause"), r.get("actionable_suggestion"),
        ) if x)
        docs.append({
            "doc_id": f"decision_reflection:{r.get('id')}",
            "source": "维度复盘",
            "asset": str(r.get("asset") or "").upper(),
            "event_type": "",
            "ts": str(r.get("ts") or ""),
            "text": body,
            "outcome": r.get("avg_pnl_pct"),
            "hit": None,
        })

    # ③ 结构化反思（事件类型 × 判定，短但类型标签最准）
    for r in _safe_rows(conn, """
        SELECT id, asset, event_type_code, verdict, score_coverage, score_fabrication,
               created_at
          FROM structured_reflections
         WHERE created_at >= ?
         ORDER BY created_at DESC LIMIT 800
    """, (since,)):
        body = (f"事件类型={r.get('event_type_code') or 'NA'}; 判定={r.get('verdict') or 'NA'}; "
                f"证据覆盖={r.get('score_coverage')}; 编造度={r.get('score_fabrication')}")
        docs.append({
            "doc_id": f"structured_reflection:{r.get('id')}",
            "source": "结构化反思",
            "asset": str(r.get("asset") or "").upper(),
            "event_type": str(r.get("event_type_code") or ""),
            "ts": str(r.get("created_at") or ""),
            "text": body,
            "outcome": None,
            "hit": None,
        })

    # ④ 经验条目（人工/复盘沉淀的规则，命中过就有样本数）
    for r in _safe_rows(conn, """
        SELECT id, kind, title, content, applicable_assets, applicable_kinds,
               weight, apply_count, hit_count, created_at
          FROM experience_entries
         WHERE COALESCE(status, 'active') != 'disabled'
         ORDER BY COALESCE(weight, 0) DESC LIMIT 300
    """, ()):
        assets = str(r.get("applicable_assets") or "")
        docs.append({
            "doc_id": f"experience:{r.get('id')}",
            "source": "经验条目",
            "asset": assets.split(",")[0].strip().upper() if assets and assets not in ("*", "NONE") else "",
            "event_type": str(r.get("applicable_kinds") or r.get("kind") or ""),
            "ts": str(r.get("created_at") or ""),
            "text": f"{r.get('title') or ''} | {r.get('content') or ''}",
            "outcome": None,
            "hit": r.get("hit_count"),
        })

    return [d for d in docs if _tokenize(d["text"])]


# ──────────────────────────────────────────────────────────────────────────────
# 向量索引（TF-IDF + 余弦）
# ──────────────────────────────────────────────────────────────────────────────
class MemoryIndex:
    """内存中的 TF-IDF 索引。docs 数量级 1e3，构建 <100ms，无需持久化。"""

    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = docs
        self.n_docs = len(docs)
        self.tfs: List[Dict[str, int]] = []
        self.norms: List[float] = []
        df: Dict[str, int] = {}
        for d in docs:
            tf: Dict[str, int] = {}
            for tok in _tokenize(d["text"]):
                tf[tok] = tf.get(tok, 0) + 1
            self.tfs.append(tf)
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1
        self.idf = {tok: math.log((1 + self.n_docs) / (1 + n)) + 1.0 for tok, n in df.items()}

    # —— 单文档向量（稀疏 dict，已做 L2 归一化，余弦=点积）——
    def _vec(self, tf: Dict[str, int]) -> Dict[str, float]:
        v: Dict[str, float] = {}
        for tok, n in tf.items():
            idf = self.idf.get(tok)
            if idf is None:
                continue
            v[tok] = (1.0 + math.log(n)) * idf
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {k: x / norm for k, x in v.items()}

    def search(self, query: str, asset: str = "", event_type: str = "",
               k: int = 5, max_age_days: int = 400) -> List[Dict[str, Any]]:
        qtokens = _tokenize(query)
        if not qtokens:
            return []
        qtf: Dict[str, int] = {}
        for tok in qtokens:
            qtf[tok] = qtf.get(tok, 0) + 1
        qvec = self._vec(qtf)
        asset_u = (asset or "").upper().strip()
        if not qvec:
            # 查询词全不在词表（全新术语）→ 先试子串命中，再退化为同品种最近教训
            return self._fallback_substring(query, asset, event_type, k) or self._fallback_asset(asset_u, k)

        et_low = (event_type or "").lower().strip()
        now = datetime.now(timezone.utc)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for i, d in enumerate(self.docs):
            vec = self._vec(self.tfs[i])
            cos = sum(w * vec.get(tok, 0.0) for tok, w in qvec.items())
            if cos <= 0:
                continue
            # 上下文加权：同品种最有用，同事件类型次之；都不匹配时仍可凭语义入选
            boost = 0.0
            if asset_u and d["asset"] == asset_u:
                boost += 0.22
            if et_low and et_low in d["event_type"].lower():
                boost += 0.12
            age_days = 999.0
            try:
                ts = datetime.strptime(d["ts"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
                if age_days > max_age_days:
                    continue
            except Exception:
                pass
            boost += 0.05 * math.exp(-age_days / 90.0)
            scored.append((cos + boost, {**d, "similarity": round(cos, 3)}))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            # 查询词全是新术语（语料里没有）→ 退化为「同品种最近教训」，
            # 比返回空更可用：至少给出该品种近期反复踩的坑，并标注 similarity=0。
            return self._fallback_asset(asset_u, k)
        return self._dedupe([d for _, d in scored], k)

    def _fallback_asset(self, asset: str, k: int) -> List[Dict[str, Any]]:
        pool = [d for d in self.docs if asset and d["asset"] == asset]
        pool.sort(key=lambda d: str(d.get("ts") or ""), reverse=True)
        return self._dedupe([{**d, "similarity": 0.0} for d in pool], k)

    def _fallback_substring(self, query: str, asset: str, event_type: str, k: int) -> List[Dict[str, Any]]:
        q = query.strip()
        hits = [d for d in self.docs if q and q in d["text"]]
        if asset:
            hits.sort(key=lambda d: 0 if d["asset"] == asset.upper() else 1)
        return [{**d, "similarity": 0.0} for d in hits[:k]]

    @staticmethod
    def _dedupe(items: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
        """同一句话在库里常有几十个重复行（行级放大），按前 40 字指纹去重。"""
        out: List[Dict[str, Any]] = []
        seen = set()
        for d in items:
            fp = _snippet(d["text"], 40)
            if fp in seen:
                continue
            seen.add(fp)
            out.append(d)
            if len(out) >= k:
                break
        return out


def build_index(conn=None, days: int = 400) -> MemoryIndex:
    if conn is None:
        import db as _db
        conn = _db.get_connection()
        try:
            return MemoryIndex(_load_corpus(conn, days=days))
        finally:
            conn.close()
    return MemoryIndex(_load_corpus(conn, days=days))


# ──────────────────────────────────────────────────────────────────────────────
# 进程内缓存：索引重建按「语料版本」失效（60s 内复用，避免每次分析重建）
# ──────────────────────────────────────────────────────────────────────────────
_INDEX_CACHE: Dict[str, Any] = {"ts": 0.0, "idx": None, "version": None}
_INDEX_TTL_S = 60.0


def _corpus_version(conn) -> Tuple[int, str]:
    """轻量版本号：四表行数 + 最新一行的时间。任一新反思落库 → 自动失效。"""
    parts: List[str] = []
    total = 0
    for table, ts_col in (
        ("trade_reflections_v2", "created_at"),
        ("decision_reflection", "ts"),
        ("structured_reflections", "created_at"),
        ("experience_entries", "created_at"),
    ):
        try:
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(MAX({ts_col}), '') FROM {table}"
            ).fetchone()
            total += int(row[0] or 0)
            parts.append(str(row[1] or ""))
        except Exception:
            parts.append("")
    return total, "|".join(parts)


def get_index(conn=None, force: bool = False) -> MemoryIndex:
    now = time.monotonic()
    cached = _INDEX_CACHE
    if not force and cached.get("idx") is not None and (now - float(cached.get("ts") or 0)) < _INDEX_TTL_S:
        return cached["idx"]

    own_conn = False
    if conn is None:
        import db as _db
        conn = _db.get_connection()
        own_conn = True
    try:
        version = _corpus_version(conn)
        if not force and cached.get("idx") is not None and cached.get("version") == version:
            _INDEX_CACHE["ts"] = now
            return cached["idx"]
        idx = MemoryIndex(_load_corpus(conn, days=int(os.getenv("SEMANTIC_MEMORY_DAYS", "400"))))
        _INDEX_CACHE.update({"ts": now, "idx": idx, "version": version})
        return idx
    finally:
        if own_conn:
            conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# 对外：检索 + prompt 分区
# ──────────────────────────────────────────────────────────────────────────────
def search(query: str, asset: str = "", event_type: str = "", k: int = 5) -> List[Dict[str, Any]]:
    try:
        idx = get_index()
        return idx.search(query or "", asset=asset or "", event_type=event_type or "", k=max(1, int(k)))
    except Exception as exc:  # 零注入原则：检索挂了也不能杀死分析链路
        print(f"[SEMANTIC] 检索失败: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
        return []


def format_block(items: List[Dict[str, Any]], asset: str = "", event_type: str = "",
                 total_docs: Optional[int] = None) -> str:
    if not items:
        return ""
    head = ("# 语义记忆检索（历史同类教训 · 只作校准依据，不可替代本次证据）\n"
            f"  → asset={asset or 'ANY'} event={event_type or 'ANY'} · "
            f"召回 {len(items)} 条" + (f"/{total_docs} 条记忆" if total_docs else ""))
    lines = [head, ""]
    for i, d in enumerate(items, 1):
        outcome = ""
        try:
            if d.get("outcome") is not None:
                outcome = f" · 结果{float(d['outcome']):+.2f}%"
        except Exception:
            outcome = ""
        lines.append(
            f"[M{i}] 相似度{d.get('similarity', 0):.2f} · {d.get('asset') or 'ANY'} · "
            f"{d.get('source')} · {str(d.get('ts') or '')[:10]}{outcome}"
        )
        lines.append(f"  {_snippet(d.get('text') or '', 110)}")
    lines.append("")
    lines.append("用法：仅当这些教训与本次证据链一致时才可引用；冲突时以本次盘面/新闻证据为准。")
    return "\n".join(lines)


_SEM_BLOCK_CACHE: Dict[str, Any] = {"ts": 0.0, "key": None, "text": "", "ids": []}


def load_semantic_block(asset: str = "", event_type: str = "", query: str = "",
                        k: int = 5, max_chars: int = 1500, ttl: float = 30.0) -> str:
    """供 ai_worker 注入：30s TTL、按 (asset,event,query 前缀) 缓存。"""
    if os.getenv("SEMANTIC_MEMORY_INJECT", "1").lower() in ("0", "false", "off", "no"):
        return ""
    key = ((asset or "").upper(), (event_type or "").lower(), (query or "")[:120])
    now = time.monotonic()
    cached = _SEM_BLOCK_CACHE
    if cached.get("key") == key and (now - float(cached.get("ts") or 0)) < ttl:
        return cached.get("text") or ""
    text = ""
    ids: List[str] = []
    try:
        idx = get_index()
        items = idx.search(query or "", asset=asset or "", event_type=event_type or "", k=k)
        text = format_block(items, asset=asset, event_type=event_type, total_docs=idx.n_docs)
        ids = [d.get("doc_id", "") for d in items]
        if len(text) > max_chars:
            text = text[: max_chars - 40] + "\n…(语义记忆块已截断)"
    except Exception as exc:
        print(f"[SEMANTIC] 构建失败: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
        text = ""
    _SEM_BLOCK_CACHE.update({"ts": now, "key": key, "text": text, "ids": ids})
    return text


def last_injected_ids() -> List[str]:
    return list(_SEM_BLOCK_CACHE.get("ids") or [])


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="语义记忆检索（只读）")
    ap.add_argument("--query", default="", help="查询文本（通常传当前新闻标题/正文）")
    ap.add_argument("--asset", default="", help="品种，如 XAU/BTC")
    ap.add_argument("--event", default="", help="事件类型/子类，如 non_farm")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 prompt 文本")
    ap.add_argument("--days", type=int, default=400, help="语料时间窗（天）")
    ap.add_argument("--rebuild", action="store_true", help="忽略缓存重建索引")
    args = ap.parse_args(argv)

    import db as _db
    conn = _db.get_connection()
    try:
        idx = get_index(conn, force=args.rebuild)
        items = idx.search(args.query, asset=args.asset, event_type=args.event, k=args.k)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"docs": idx.n_docs, "items": items}, ensure_ascii=False, indent=2))
    else:
        block = format_block(items, asset=args.asset, event_type=args.event, total_docs=idx.n_docs)
        print(block or f"(无召回：docs={idx.n_docs})")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
