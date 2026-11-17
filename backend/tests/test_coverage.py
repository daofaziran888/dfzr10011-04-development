"""分片覆盖准入的可复核规则测试。

直接用标准库 unittest，起不了外部依赖也能跑：

    cd backend && PYTHONPATH=.venv python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest

from app.seed import SEED_ROWS
from app.services import intervals as iv
from app.services.coverage import CoverageService
from app.services.merge import MergeService
from app.services.shard import ShardService
from app.store import store

cov = CoverageService()
merge = MergeService()
shard = ShardService()


def reset_store() -> None:
    """每个用例从干净的示例数据开始；覆盖清单、容量与待归并表都是按需创建的。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    for module in ("coverage", "merge_pending", "coverage_capacity"):
        store._tables.setdefault(module, []).clear()


def active_entries(plan: str) -> list[dict]:
    return [
        row
        for row in store.rows("coverage")
        if row.get("采集计划") == plan
        and row.get("条目类型") != "批次"
        and row.get("条目状态") == "已确认"
    ]


def batches(plan: str) -> list[dict]:
    return [
        row
        for row in store.rows("coverage")
        if row.get("采集计划") == plan and row.get("条目类型") == "批次"
    ]


def pending(plan: str) -> list[dict]:
    return [
        row
        for row in store.rows("merge_pending")
        if row.get("采集计划") == plan and row.get("pending") and row.get("覆盖完整")
    ]


def admit(**values) -> tuple[dict | None, str]:
    return cov.admit(values)


class IntervalRuleTest(unittest.TestCase):
    def test_same_range_written_differently_is_one_interval(self):
        for text in ("0-100", "[0, 100)", "0~100", "0,100"):
            self.assertEqual(iv.parse_interval(text), (0, 100))
        self.assertEqual(iv.parse_interval("开头-20"), (None, 20))
        self.assertEqual(iv.parse_interval("500-末尾"), (500, None))

    def test_adjacent_ranges_are_continuous(self):
        merged = iv.merge_intervals([(0, 100), (100, 200)])
        self.assertEqual([iv.format_interval(item) for item in merged], ["0-200"])

    def test_missing_pieces_are_reported(self):
        missing = iv.subtract((0, 200), [(0, 100)])
        self.assertEqual([iv.format_interval(item) for item in missing], ["100-200"])

    def test_invalid_range_is_rejected(self):
        with self.assertRaises(iv.RangeFormatError):
            iv.parse_interval("100-10")


class AdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_batch_and_entries_are_reviewable(self):
        batch, message = admit(
            采集计划="PLAN-1",
            分片范围=["0-100", "100-200"],
            采集计划版本=1,
            来源="计划初始下发",
            容量上限=500,
            分配节点="NODE-1",
        )
        self.assertIsNotNone(batch, message)
        self.assertEqual(batch["条目状态"], "已准入")
        self.assertEqual(batch["覆盖目标"], "0-200")
        entries = active_entries("PLAN-1")
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["覆盖区间"], "0-200")
        self.assertEqual(entry["分配节点"], "NODE-1")
        self.assertEqual(entry["节点容量上限"], 500)
        self.assertEqual(entry["占用容量"], 200)
        self.assertEqual(entry["来源版本"], 1)

    def test_duplicate_submission_keeps_one_entry_and_one_capacity(self):
        values = dict(
            采集计划="PLAN-2",
            分片范围=["0-100"],
            采集计划版本=1,
            容量上限=100,
            分配节点="NODE-2",
        )
        first, _ = admit(**values)
        second, message = admit(**{**values, "来源": "重复提交"})
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIn("重复提交", message)
        self.assertEqual(len(active_entries("PLAN-2")), 1)
        capacity = [
            row for row in store.rows("coverage_capacity") if row.get("采集计划") == "PLAN-2"
        ]
        self.assertEqual(len(capacity), 1)
        self.assertEqual(capacity[0]["占用容量"], 100)

    def test_concurrent_assignment_keeps_one_coverage(self):
        results: list[tuple[dict | None, str]] = []

        def submit() -> None:
            results.append(admit(
                采集计划="PLAN-3",
                分片范围=["0-100"],
                采集计划版本=1,
                容量上限=100,
                分配节点="NODE-3",
            ))

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        accepted = [item for item in results if item[0] is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(active_entries("PLAN-3")), 1)
        self.assertEqual(
            len([row for row in store.rows("coverage_capacity") if row.get("采集计划") == "PLAN-3"]),
            1,
        )

    def test_one_capacity_version_per_plan(self):
        admit(采集计划="PLAN-4", 分片范围=["0-100"], 容量版本="CAP-A", 容量上限=100)
        batch, message = admit(
            采集计划="PLAN-4", 分片范围=["100-200"], 容量版本="CAP-B", 容量上限=100
        )
        self.assertIsNone(batch)
        self.assertIn("容量版本", message)

    def test_conflicting_capacity_limits_are_rejected(self):
        admit(采集计划="PLAN-5", 分片范围=["0-100"], 容量上限=200, 分配节点="NODE-5")
        batch, message = admit(
            采集计划="PLAN-5", 分片范围=["100-200"], 容量上限=300, 分配节点="NODE-5"
        )
        self.assertIsNone(batch)
        self.assertIn("不一致", message)

    def test_capacity_shortage_keeps_uncovered_range(self):
        batch, message = admit(
            采集计划="PLAN-6", 分片范围=["0-400"], 容量上限=300, 分配节点="NODE-6"
        )
        self.assertIsNotNone(batch, message)
        self.assertEqual(batch["条目状态"], "容量不足")
        self.assertEqual(batch["未覆盖区间"], ["300-400"])
        uncovered = [
            row
            for row in store.rows("coverage")
            if row.get("采集计划") == "PLAN-6" and row.get("条目类型") == "未覆盖"
        ]
        self.assertEqual(len(uncovered), 1)
        self.assertEqual(uncovered[0]["覆盖区间"], "300-400")
        blocker = [
            row for row in store.rows("merge_pending") if row.get("采集计划") == "PLAN-6"
        ]
        self.assertEqual(len(blocker), 1)
        self.assertFalse(blocker[0]["覆盖完整"])


class ConflictOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        admit(
            采集计划="PLAN-A",
            分片范围=["100-200"],
            采集计划版本=1,
            来源="旧版本",
            容量上限=500,
            分配节点="NODE-A",
        )

    def test_fill_policy_keeps_narrow_and_adds_fill_entry(self):
        batch, message = admit(
            采集计划="PLAN-A",
            分片范围=["0-150"],
            采集计划版本=2,
            来源="新版本",
            冲突策略="补洞",
            容量上限=500,
           分配节点="NODE-A",
        )
        self.assertIsNotNone(batch, message)
        self.assertEqual(batch["补洞条目数"], 1)
        self.assertEqual(batch["覆盖目标"], "0-200")
        entries = active_entries("PLAN-A")
        self.assertEqual(
            sorted(row["覆盖区间"] for row in entries), ["0-100", "100-200"]
        )
        # 已确认分片没有被挤出覆盖范围，待归并条目随新增分片增加。
        self.assertEqual(len(pending("PLAN-A")), 2)

    def test_replace_policy_swaps_whole_version(self):
        admit(
            采集计划="PLAN-A",
            分片范围=["0-150"],
            采集计划版本=2,
            来源="新版本",
            冲突策略="补洞",
            容量上限=500,
            分配节点="NODE-A",
        )
        batch, message = admit(
            采集计划="PLAN-A",
            分片范围=["0-120"],
            采集计划版本=3,
            来源="替换版本",
            冲突策略="整段替换",
            容量上限=500,
            分配节点="NODE-A",
        )
        self.assertIsNotNone(batch, message)
        self.assertEqual(batch["处理结论"], "整段替换")
        entries = active_entries("PLAN-A")
        self.assertEqual([row["覆盖区间"] for row in entries], ["0-120"])
        self.assertEqual([row["覆盖区间"] for row in pending("PLAN-A")], ["0-120"])
        superseded = [
            row
            for row in store.rows("coverage")
            if row.get("采集计划") == "PLAN-A" and row.get("条目状态") == "已替换"
        ]
        self.assertEqual(len(superseded), 2)

    def test_overlapping_versions_in_one_submission_are_split(self):
        batch, message = admit(
            采集计划="PLAN-B",
            分片范围=["100-200", "0-150"],
            采集计划版本=1,
            来源="新旧同时到达",
            容量上限=500,
        )
        self.assertIsNotNone(batch, message)
        self.assertEqual(batch["补洞条目数"], 1)
        self.assertEqual(
            sorted(row["覆盖区间"] for row in active_entries("PLAN-B")),
            ["0-100", "100-150", "150-200"],
        )
        self.assertEqual(len(pending("PLAN-B")), 3)

    def test_replace_without_capacity_keeps_old_coverage(self):
        admit(
            采集计划="PLAN-R",
            分片范围=["0-100"],
            采集计划版本=1,
            来源="旧版本",
            容量上限=120,
            分配节点="NODE-R",
        )
        batch, message = admit(
            采集计划="PLAN-R",
            分片范围=["0-200"],
            采集计划版本=2,
            来源="替换版本",
            冲突策略="整段替换",
            容量上限=120,
            分配节点="NODE-R",
        )
        self.assertIsNotNone(batch, message)
        self.assertIn("未生效", message)
        self.assertEqual([row["覆盖区间"] for row in active_entries("PLAN-R")], ["0-100"])
        self.assertEqual(batch["未覆盖区间"], ["120-200"])
        sources = {item["来源"] for item in batch["来源明细"]}
        self.assertEqual(sources, {"旧版本", "替换版本"})


class AggregateTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        admit(
            采集计划="PLAN-C",
            分片范围=["0-100"],
            采集计划版本=1,
            容量上限=100,
            分配节点="NODE-C",
        )
        admit(
            采集计划="PLAN-D",
            分片范围=["0-500"],
            容量上限=100,
            分配节点="NODE-D",
        )

    def test_pending_only_contains_complete_versions(self):
        self.assertTrue(all(row["覆盖完整"] for row in merge.pending_entries()))
        self.assertIn("PLAN-C", {row["采集计划"] for row in merge.pending_entries()})
        self.assertNotIn("PLAN-D", {row["采集计划"] for row in merge.pending_entries()})

    def test_incomplete_plan_is_not_visible_to_aggregate(self):
        self.assertFalse(any(row["采集计划"] == "PLAN-D" for row in merge.list_entries()[0]))
        entry, _missing = merge.create_entry(
            {"任务编号": "MERG-X", "采集计划": "PLAN-D", "分片总数": 1}
        )
        self.assertIsNone(entry)

    def test_building_pending_tasks_is_idempotent(self):
        first, _, _ = merge.build_pending_tasks()
        second, skipped, _ = merge.build_pending_tasks()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(skipped, 1)

    def test_shard_entry_rejects_repeat_without_new_capacity(self):
        entry, missing, _message = shard.create_entry(
            {"分片编号": "SHAR-X", "采集计划": "PLAN-E", "分片范围": "0-50", "容量上限": 100}
        )
        self.assertIsNotNone(entry)
        self.assertEqual(missing, [])
        repeat, _missing, message = shard.create_entry(
            {"分片编号": "SHAR-Y", "采集计划": "PLAN-E", "分片范围": "0-50", "容量上限": 100}
        )
        self.assertIsNone(repeat)
        self.assertIn("重复提交", message)
        self.assertEqual(
            len([row for row in store.rows("coverage_capacity") if row.get("采集计划") == "PLAN-E"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
