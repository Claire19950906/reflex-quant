# -*- coding: utf-8 -*-
"""备份文件路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条端点：/api/backups 列表、/{filename} 下载、/open 打开目录。
BACKUP_DIR 与 _backup_download_path 留在 api_server（重置备份的创建函数也在用），
router 用 _api() 回引。下载路径做了路径穿越防护（basename 必须等于原名）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

import license_auth
from fastapi.responses import FileResponse

# v453 安全：这些路由全是「写」操作（重置/归档/备份/策略/凭证），
# full 模式默认要求 API Key；仅回环地址在 TRIDENT_LOCAL_TRUST=1 时免鉴权。
router = APIRouter(dependencies=[Depends(license_auth.require_api_key)])


def _api():
    """延迟回引 api_server（BACKUP_DIR / _backup_download_path 仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/backups/{filename}")
async def download_backup(filename: str):
    """下载 backend/backups/ 下的重置备份文件（JSON / Excel），供复盘页备份链接使用。"""
    path = _api()._backup_download_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail="backup not found")
    media_type = (
        "application/json" if path.endswith(".json")
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return FileResponse(path, media_type=media_type, filename=os.path.basename(path))


@router.get("/api/backups")
async def list_backups():
    """重置备份目录信息：绝对路径 + 最近备份文件列表（供前端显示与打开文件夹）。"""
    os.makedirs(_api().BACKUP_DIR, exist_ok=True)
    files: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(_api().BACKUP_DIR), reverse=True)
        for name in names:
            full = os.path.join(_api().BACKUP_DIR, name)
            if os.path.isfile(full):
                files.append({
                    "name": name,
                    "size": os.path.getsize(full),
                    "modified": os.path.getmtime(full),
                })
    except OSError:
        pass
    return {"backup_dir": _api().BACKUP_DIR, "files": files[:30]}


@router.post("/api/backups/open")
async def open_backup_dir():
    """在服务器本机打开备份文件夹（资源管理器）。仅当 API 与桌面同机时有效。"""
    os.makedirs(_api().BACKUP_DIR, exist_ok=True)
    try:
        os.startfile(_api().BACKUP_DIR)  # Windows 资源管理器
        return {"ok": True, "backup_dir": _api().BACKUP_DIR}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "backup_dir": _api().BACKUP_DIR}
