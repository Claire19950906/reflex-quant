"""Provider 实现：null / file / http / sqlite，统一返回 base.py 里的四组数据。"""

from .base import VERSION, Bundle, DriversData, HistoryData, KlineData, RulesData
from .file import FileBundle
from .http import HttpBundle
from .null import NullBundle
from .snapshot import build as build_snapshot
from .sqlite import SqliteBundle

__all__ = [
    "VERSION",
    "Bundle",
    "RulesData",
    "DriversData",
    "HistoryData",
    "KlineData",
    "NullBundle",
    "FileBundle",
    "HttpBundle",
    "SqliteBundle",
    "build_snapshot",
]
