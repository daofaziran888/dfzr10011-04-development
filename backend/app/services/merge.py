"""结果聚合归并业务规则：状态流转、字段校验与筛选口径都收在这里。

聚合只读取分片覆盖清单里的「完整覆盖版本」：覆盖不完整的采集计划不会出现在
待归并清单里，也不能据此建立归并任务。判断口径统一走 ``CoverageService``，
本模块不自行计算区间或容量。
"""
from __future__ import annotations

from typing import Any

from app.services import intervals as iv
from app.services.coverage import CoverageService
from app.store import ADMISSION_LOCK, store

MODULE = "merge"
REQUIRED_FIELDS = ["任务编号", "采集计划", "分片总数"]
STATUS_ORDER = ["待归并", "归并中", "已完成", "归并失败"]
ACTION_RULES = {"启动归并": "待归并", "重试归并": "归并中", "归档任务": "归并失败"}
NEGATIVE_ACTIONS = []

coverage_service = CoverageService()


def _pending_code(plan: str, interval: str) -> str:
    return f"{plan}@{interval}"


class MergeService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = self._visible_rows()
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _visible_rows(self) -> list[dict[str, Any]]:
        """聚合只能看到完整覆盖版本：被覆盖清单标记为未覆盖的计划整体隐藏。"""
        incomplete = {
            str(row.get("采集计划"))
            for row in store.rows("merge_pending")
            if not row.get("覆盖完整")
        }
        return [
            row
            for row in store.rows(MODULE)
            if str(row.get("采集计划")) not in incomplete
        ]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        incomplete = {
            str(row.get("采集计划"))
            for row in store.rows("merge_pending")
            if not row.get("覆盖完整")
        }
        return None if str(entry.get("采集计划")) in incomplete else entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        plan = str(values.get("采集计划") or "").strip()
        if plan in self._incomplete_plans():
            return None, []
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"聚合任务 {entry_id} 不存在、已归档或所属计划覆盖不完整"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于结果聚合归并可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"聚合任务已{action}"

    # ------------------------------------------------------------ 覆盖清单读取
    def _incomplete_plans(self) -> set[str]:
        return {
            str(row.get("采集计划"))
            for row in store.rows("merge_pending")
            if not row.get("覆盖完整")
        }

    def admission_summary(self) -> dict[str, Any]:
        """新增分片覆盖准入汇总：批次、覆盖完整计划与待归并数。"""
        snapshot = coverage_service.coverage_snapshot()
        return {
            "summary": snapshot["summary"],
            "batches": snapshot["batches"],
            "plans": snapshot["items"],
            "entries": snapshot["entries"],
        }

    def coverage_entries(self) -> list[dict[str, Any]]:
        """分片覆盖清单：聚合侧读取的明细，与准入汇总同源。"""
        return coverage_service.coverage_snapshot()["entries"]

    def pending_entries(self) -> list[dict[str, Any]]:
        """覆盖完整的待归并条目：一条区间一行，未覆盖区间不出现。"""
        rows = coverage_service.complete_plans()
        return sorted(rows, key=lambda row: (str(row.get("采集计划")), str(row.get("覆盖区间"))))

    def coverage_view(self) -> dict[str, Any]:
        snapshot = coverage_service.coverage_snapshot()
        pending = self.pending_entries()
        incomplete = [
            row for row in store.rows("merge_pending") if not row.get("覆盖完整")
        ]
        return {
            "summary": snapshot["summary"],
            "admission": snapshot["batches"],
            "entries": snapshot["entries"],
            "pending": pending,
            "pending_total": len(pending),
            "blocked": incomplete,
            "capacity": snapshot["capacity"],
            "visible_tasks": len(self._visible_rows()),
        }

    def build_pending_tasks(self) -> tuple[int, int, str]:
        """把完整覆盖版本落成聚合待归并任务；重复执行不会生成第二个同范围任务。"""
        with ADMISSION_LOCK:
            rows = store.rows(MODULE)
            existing = {
                _pending_code(str(row.get("采集计划")), str(row.get("分片范围")))
                for row in rows
            }
            created = 0
            skipped = 0
            for item in self.pending_entries():
                plan = str(item.get("采集计划"))
                interval = iv.normalize(item.get("覆盖区间"))
                code = _pending_code(plan, interval)
                if code in existing:
                    skipped += 1
                    continue
                # 一个待归并条目对应覆盖清单里的一段完整覆盖区间，就是一片的归并范围。
                rows.append({
                    "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                    "任务编号": f"MERG-{len(rows) + 1:04d}",
                    "采集计划": plan,
                    "分片总数": 1,
                    "已归并数": 0,
                    "归并策略": "按覆盖区间逐片归并",
                    "输出路径": f"/data/merge/{plan}/{interval}",
                    "数据总量": 0,
                    "分片范围": interval,
                    "覆盖编号": item.get("覆盖编号"),
                    "来源版本": item.get("来源版本"),
                    "覆盖完整": True,
                    "status": "待归并",
                    "pending": True,
                    "abnormal": False,
                })
                existing.add(code)
                created += 1
            message = (
                f"已按完整覆盖清单建立 {created} 条待归并任务"
                if created
                else "没有新的完整覆盖区间，待归并任务保持不变"
            )
            if skipped:
                message += f"，跳过 {skipped} 条既有同范围任务"
            return created, skipped, message
