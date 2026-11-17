"""分片覆盖准入的示例数据：起服务后能直接看到补洞、整段替换与容量不足三种结论。

只通过 ``CoverageService`` 的公开入口写入，保证示例数据与真实提交走同一套规则；
重复调用是幂等的：已经存在同名计划的覆盖清单时直接跳过。
"""
from __future__ import annotations

from app.services.coverage import CoverageService
from app.store import store

DEMO_PLANS = ("PLAN-CRAWL-001", "PLAN-CRAWL-002", "PLAN-CRAWL-003")


def bootstrap_coverage() -> None:
    if any(row.get("采集计划") in DEMO_PLANS for row in store.rows("coverage")):
        return
    service = CoverageService()
    # 计划 001：先窄后宽，演示「保留较窄范围并补洞」与整段替换两种处理顺序。
    service.admit({
        "采集计划": "PLAN-CRAWL-001",
        "分片范围": ["100-200"],
        "采集计划版本": 1,
        "来源": "计划初始下发",
        "容量版本": "CAP-2026-11",
        "分配节点": "NODE-001",
        "容量上限": 400,
    })
    service.admit({
        "采集计划": "PLAN-CRAWL-001",
        "分片范围": ["0-150"],
        "采集计划版本": 2,
        "来源": "计划调整单 ADJ-001",
        "冲突策略": "保留较窄范围并补洞",
        "容量版本": "CAP-2026-11",
        "分配节点": "NODE-001",
        "容量上限": 400,
    })

    # 计划 002：新范围超过节点容量，只保留装得下的部分，其余记录为未覆盖区间。
    service.admit({
        "采集计划": "PLAN-CRAWL-002",
        "分片范围": ["0-400"],
        "采集计划版本": 1,
        "来源": "计划初始下发",
        "容量版本": "CAP-2026-11",
        "分配节点": "NODE-002",
        "容量上限": 300,
    })
    # 计划 003：先完整覆盖，再由较新版本整段替换，演示旧条目转为已替换。
    service.admit({
        "采集计划": "PLAN-CRAWL-003",
        "分片范围": ["500-800"],
        "采集计划版本": 1,
        "来源": "计划初始下发",
        "容量版本": "CAP-2026-11",
        "分配节点": "NODE-003",
        "容量上限": 400,
    })
    service.admit({
        "采集计划": "PLAN-CRAWL-003",
        "分片范围": ["500-650"],
        "采集计划版本": 2,
        "来源": "计划调整单 ADJ-002",
        "冲突策略": "采用较新版本整段替换",
        "容量版本": "CAP-2026-11",
        "分配节点": "NODE-003",
        "容量上限": 400,
    })
