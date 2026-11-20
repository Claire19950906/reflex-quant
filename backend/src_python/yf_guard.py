#!/usr/bin/env python3
"""雅虎快速熔断：让仍在调用 yfinance 的模块立刻返回空，而不是等 6 次重试。

背景：
  Yahoo v8 接口对我们已经是长期 429 / 无数据。已经停掉了两条写入路径，
  但 api_server、market_snapshot、es_feed、dollar_index_feed 等模块里
    仍然写死了 `import yfinance as yf` —— 实测 api.err.log 累计
  **80,977 行** "possibly delisted; no price data found"，每行背后都是一次
  完整的 DNS + TLS + 重试（单次可达 20 秒以上）。

  这些模块本来就有"拿不到就跳过/用别的源"的逻辑，问题只在于**等得太久**。
  这里做一个进程级熔断：调用方不用改代码，yf.download / yf.Ticker 立即返回空，
  让它们自己的兜底分支马上接管。

  原则：不是造假数据 —— 返回的是**空结果**，调用方必须自己判断"没有"。
  绝不给任何默认值或昨收价。
"""
from typing import Any, List

_ENABLED = False
_PATCHED = False


class _EmptyFrame:
    """最小可用的空 DataFrame 替身：支持 .empty / len() / 列访问 / 迭代。"""

    empty = True

    def __len__(self) -> int:
        return 0

    def __bool__(self) -> bool:
        return False

    def __getitem__(self, key: Any) -> "_EmptyFrame":
        return self

    def __iter__(self) -> Any:
        return iter(())

    def iterrows(self) -> Any:
        return iter(())

    def dropna(self, *a: Any, **kw: Any) -> "_EmptyFrame":
        return self

    def reset_index(self, *a: Any, **kw: Any) -> "_EmptyFrame":
        return self

    def to_dict(self, *a: Any, **kw: Any) -> dict:
        return {}

    def __repr__(self) -> str:
        return "EmptyFrame(yahoo disabled)"


class _EmptyTicker:
    """yf.Ticker 的替身：任何属性访问都返回空 DataFrame / 空 dict。"""

    def __init__(self, symbol: str = "", *a: Any, **kw: Any) -> None:
        self.symbol = symbol

    def history(self, *a: Any, **kw: Any) -> _EmptyFrame:
        return _EmptyFrame()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _EmptyFrame()


def disable(verbose: bool = False) -> bool:
    """把已导入的 yfinance 换成空实现；未安装则什么也不做。返回是否生效。"""
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import yfinance as yf
    except Exception:
        return False

    def _download(*a: Any, **kw: Any) -> _EmptyFrame:
        return _EmptyFrame()

    yf.download = _download
    yf.Ticker = _EmptyTicker
    _PATCHED = True
    if verbose:
        print("[YF-GUARD] yfinance 已熔断：下载/行情一律返回空，由调用方兜底", flush=True)
    return True


if __name__ == "__main__":
    print("已熔断" if disable(verbose=True) else "未安装 yfinance")
