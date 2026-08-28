"""绩效/分析报告对外接口。

报告由 L6 运维层 Reporter 落盘到项目根目录 ``reports/``：
- ``daily_YYYYMMDD.md``    每日复盘日报
- ``weekly_起_止.md``       周报
- ``stage_起_止.md``        阶段绩效报告（evolve 周日产出）

- ``GET /report/list``：扫描目录返回报告清单（按文件名倒序，最新在前）。
- ``GET /report/content``：读取单份报告 Markdown 原文（防路径穿越）。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/report", tags=["report"])

REPORT_DIR = Path(__file__).resolve().parent.parent.parent / "reports"

_KIND_PAT = {
    "daily": re.compile(r"^daily_(\d{8})\.md$"),
    "weekly": re.compile(r"^weekly_(\d{8})_(\d{8})\.md$"),
    "stage": re.compile(r"^stage_(\d{8})_(\d{8})\.md$"),
    "reflection": re.compile(r"^reflection_(\d{8})\.md$"),
}

_KIND_LABEL = {"daily": "日报", "weekly": "周报", "stage": "阶段报告",
               "reflection": "自我复盘"}


def _meta(p: Path) -> dict | None:
    for kind, pat in _KIND_PAT.items():
        m = pat.match(p.name)
        if not m:
            continue
        groups = m.groups()
        return {
            "name": p.name,
            "kind": kind,
            "kind_label": _KIND_LABEL[kind],
            "date": groups[0],
            "date_end": groups[1] if len(groups) > 1 else None,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
        }
    return None


@router.get("/list")
def list_reports(kind: str | None = Query(None)):
    """报告清单。kind 可选 daily/weekly/stage 过滤。"""
    if not REPORT_DIR.is_dir():
        return {"reports": [], "note": "reports 目录尚不存在，运行复盘/evolve 任务后自动生成。"}
    out = []
    for p in REPORT_DIR.glob("*.md"):
        meta = _meta(p)
        if meta and (not kind or meta["kind"] == kind):
            out.append(meta)
    out.sort(key=lambda x: x["name"], reverse=True)
    return {"reports": out, "note": None if out else "暂无报告，运行复盘/evolve 任务后自动生成。"}


@router.get("/content")
def report_content(name: str = Query(...)):
    """读取单份报告 Markdown 原文。仅允许 reports 目录内的已知命名文件。"""
    matched = any(pat.match(name) for pat in _KIND_PAT.values())
    if not matched or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="非法报告名")
    p = REPORT_DIR / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="报告不存在")
    return {"name": name, "content": p.read_text(encoding="utf-8")}
