"""分片覆盖准入核对接口：管理分片覆盖准入汇总与分片覆盖清单。

- ``GET  /api/coverage``                 分片覆盖准入汇总（批次）
- ``GET  /api/coverage/entries``         分片覆盖清单（明细，可逐片复核）
- ``GET  /api/coverage/snapshot``        聚合等其它入口读取的同一份覆盖结论
- ``POST /api/coverage``                 准入一个分片范围版本
- ``GET  /api/coverage/export``          导出覆盖清单
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.coverage import CoverageService

router = APIRouter(prefix="/api/coverage", tags=["分片覆盖准入核对"])

service = CoverageService()

ENTRY_FIELDS = [
    "覆盖编号",
    "采集计划",
    "范围版本",
    "容量版本",
    "覆盖区间",
    "覆盖目标",
    "分配节点",
    "占用容量",
    "节点容量上限",
    "来源",
    "来源版本",
    "条目类型",
    "条目状态",
]
BATCH_FIELDS = [
    "覆盖编号",
    "采集计划",
    "范围版本",
    "容量版本",
    "冲突策略",
    "处理结论",
    "覆盖区间",
    "新增条目数",
    "补洞条目数",
    "未覆盖区间",
    "占用容量",
    "来源版本",
    "条目状态",
]


@router.get("", response_model=PageResult[dict])
def list_batches(
    plan: str | None = Query(default=None, description="按采集计划过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分片覆盖准入汇总：每个批次一行，能复核新增、补洞与未覆盖的判定结果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.batches(plan=plan, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/entries", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按覆盖编号或采集计划检索"),
    status: str | None = Query(default=None, description="已确认、未覆盖、已替换"),
    plan: str | None = Query(default=None, description="按采集计划过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分片覆盖清单：每条明细都能核对覆盖区间、来源版本、节点与容量上限。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, plan=plan, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/snapshot", response_model=dict)
def snapshot() -> dict[str, Any]:
    """覆盖结论快照：其它业务入口同步取用同一份汇总与清单。"""
    return service.coverage_snapshot()


@router.post("", response_model=ActionResult)
def admit(payload: EntryPayload) -> ActionResult:
    """准入一个分片范围版本；范围、策略与容量不合法时给出可读原因。"""
    batch, message = service.admit(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分片覆盖清单：批次汇总与明细一起返回，方便整批复核。"""
    entries, total = service.list_entries(page=1, size=10000)
    batches, batch_total = service.batches(page=1, size=10000)
    return {
        "module": "coverage",
        "total": total,
        "items": [row for row in entries if row.get("条目类型") != "批次"],
        "batches": batches,
        "batch_total": batch_total,
        "fields": {"entries": ENTRY_FIELDS, "batches": BATCH_FIELDS},
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条覆盖清单明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None or entry.get("条目类型") == "批次":
        raise HTTPException(status_code=404, detail=f"覆盖清单条目 {entry_id} 不存在或不属于明细")
    return entry
