"""任务分片调度业务规则：状态流转、字段校验与筛选口径都收在这里。

新建分片任务不再直接落库，而是先经过分片覆盖准入核对：范围要能解析成
``[起点, 终点)``，同一分片重复提交只占用一份节点容量，节点分配结果会写进
覆盖清单，供聚合侧逐片核对覆盖区间与容量上限。
"""
from __future__ import annotations

from typing import Any

from app.services.coverage import CoverageService
from app.store import store

MODULE = "shard"
REQUIRED_FIELDS = ["分片编号", "采集计划", "分片范围"]
STATUS_ORDER = ["待分配", "执行中", "已完成", "执行失败"]
ACTION_RULES = {"分配节点": "待分配", "重试分片": "执行中", "标记完成": "执行失败"}
NEGATIVE_ACTIONS = []

coverage_service = CoverageService()


class ShardService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("分片编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str], str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing, ""
        # 新建即准入：分片范围、容量版本与节点分配都先过覆盖清单核对。
        batch, message = coverage_service.sync_from_shard(values)
        if batch is None:
            return None, [], message
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["分配节点"] = batch.get("分配节点") or values.get("分配节点")
        entry["覆盖编号"] = batch.get("覆盖编号")
        entry["占用容量"] = batch.get("占用容量")
        entry["节点容量上限"] = batch.get("节点容量上限")
        entry["条目状态"] = batch.get("条目状态")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = batch.get("条目状态") == "容量不足"
        rows.append(entry)
        return entry, [], ""

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分片任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于任务分片调度可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分片任务已{action}"

    def coverage_view(self) -> dict[str, Any]:
        """分片入口同步取用聚合侧同一份覆盖准入汇总与覆盖清单。"""
        snapshot = coverage_service.coverage_snapshot()
        return {
            "summary": snapshot["summary"],
            "admission": snapshot["batches"],
            "entries": snapshot["entries"],
            "capacity": snapshot["capacity"],
        }

    def plan_coverage(self, plan: str) -> dict[str, Any]:
        """逐片核对一个采集计划的覆盖区间、节点与容量上限。"""
        snapshot = coverage_service.coverage_snapshot()
        entries = [row for row in snapshot["entries"] if row.get("采集计划") == plan]
        plan_rows = [row for row in snapshot["items"] if row.get("采集计划") == plan]
        return {
            "采集计划": plan,
            "plan": plan_rows[0] if plan_rows else None,
            "entries": entries,
            "capacity": [
                row for row in snapshot["capacity"] if row.get("采集计划") == plan
            ],
        }
