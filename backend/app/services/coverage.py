"""分片覆盖准入核对业务规则：覆盖连续性、范围重复与节点容量都收在这一层。

处理链路：

1. 采集计划提交一个「分片范围版本」（可带多个区间、来源、容量版本、节点偏好）。
2. 对照同一计划下已确认的覆盖清单，按冲突策略处理新旧范围重叠：
   - ``补洞``：重叠处保留较窄范围，较宽的新范围只补进尚未覆盖的缺口，
     覆盖目标只增不减，已确认分片不会被计划调整挤出覆盖范围；
   - ``整段替换``：新版本整体替换旧版本；先确认新版本容量能完整装下才替换，
     容量不足时替换不生效，原覆盖原样保留，新版本未装下的区间登记为未覆盖。
3. 每条新增条目都按 ``[起点, 终点)`` 的统一口径占用一个容量版本下的节点容量；
   同计划同范围重复提交只保留一份覆盖结果，不重复占用容量。
4. 只有覆盖完整的计划才同步出聚合侧待归并条目；覆盖不完整时聚合侧看不到该计划。
5. 批次（准入汇总）与清单条目（明细）一起落库，逐条可复核区间、来源与容量上限。

范围、容量上限、宽窄比较与完整性判定全部复用 ``app.services.intervals``，
聚合与分片入口只读取这里的结果，不另算一套口径。
"""
from __future__ import annotations

from typing import Any, Iterable

from app.services import intervals as iv
from app.store import ADMISSION_LOCK, store

MODULE = "coverage"
PENDING_MODULE = "merge_pending"
CAPACITY_MODULE = "coverage_capacity"

ACTIVE = "已确认"
SUPERSEDED = "已替换"
UNCOVERED_STATUS = "未覆盖"
FILLED_STATUS = "已补齐"

TYPE_SHARD = "分片"
TYPE_FILL = "补洞"
TYPE_UNCOVERED = "未覆盖"
TYPE_BATCH = "批次"

POLICY_FILL = "补洞"
POLICY_REPLACE = "整段替换"
_POLICY_ALIASES = {
    "补洞": POLICY_FILL,
    "保留较窄范围": POLICY_FILL,
    "保留较窄范围并补洞": POLICY_FILL,
    "fill": POLICY_FILL,
    "narrow": POLICY_FILL,
    "整段替换": POLICY_REPLACE,
    "替换": POLICY_REPLACE,
    "采用较新版本整段替换": POLICY_REPLACE,
    "replace": POLICY_REPLACE,
}

DEFAULT_CAPACITY_VERSION = "CAP-2026-11"
DEFAULT_NODE_CAPACITY = 300
DEFAULT_OPEN_WEIGHT = 1

BATCH_ADMITTED = "已准入"
BATCH_SHORT = "容量不足"
BATCH_DUP = "重复提交"
BATCH_NOOP = "无需新增"


class CoverageError(ValueError):
    """准入请求的字段或范围不合法。"""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _next_code(rows: Iterable[dict[str, Any]], prefix: str) -> str:
    used = [
        int(str(row.get("覆盖编号", "")).rsplit("-", 1)[-1])
        for row in rows
        if str(row.get("覆盖编号", "")).startswith(f"{prefix}-")
        and str(row.get("覆盖编号", "")).rsplit("-", 1)[-1].isdigit()
    ]
    return f"{prefix}-{max(used, default=0) + 1:04d}"


def _next_id(rows: Iterable[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _looks_like_range(text: str) -> bool:
    try:
        iv.parse_interval(text)
    except iv.RangeFormatError:
        return False
    return True


def _interval_key(interval: iv.Interval) -> tuple[int, int]:
    start, _ = interval
    return (0 if start is None else 1, start or 0)


class CoverageService:
    """分片覆盖准入：批次汇总、覆盖清单、容量占用与聚合待归并同步。"""

    # ---------------------------------------------------------------- 读取口径
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        plan: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [row for row in store.rows(MODULE) if row.get("条目类型") != TYPE_BATCH]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("覆盖编号", ""))
                or keyword in str(row.get("采集计划", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("条目状态") == status]
        if plan:
            rows = [row for row in rows if row.get("采集计划") == plan]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None or entry.get("条目类型") == TYPE_BATCH:
            return None
        return entry

    def batches(
        self, *, plan: str | None = None, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [row for row in store.rows(MODULE) if row.get("条目类型") == TYPE_BATCH]
        if plan:
            rows = [row for row in rows if row.get("采集计划") == plan]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def capacity_rows(self) -> list[dict[str, Any]]:
        return store.rows(CAPACITY_MODULE)

    def complete_plans(self) -> list[dict[str, Any]]:
        """覆盖完整、可以被聚合读取的待归并条目；聚合只认这一份结论。"""
        return [
            row
            for row in store.rows(PENDING_MODULE)
            if row.get("pending") and row.get("覆盖完整")
        ]

    def coverage_snapshot(self) -> dict[str, Any]:
        """给其它业务入口（聚合、分片、看板）读取的同一份汇总与清单。"""
        plans = self._plan_summaries()
        return {
            "summary": self._summary(plans),
            "items": plans,
            "batches": [row for row in store.rows(MODULE) if row.get("条目类型") == TYPE_BATCH],
            "entries": [row for row in store.rows(MODULE) if row.get("条目类型") != TYPE_BATCH],
            "capacity": self.capacity_rows(),
            "pending": store.rows(PENDING_MODULE),
        }

    def _summary(self, plans: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "计划数": len(plans),
            "覆盖完整计划数": sum(1 for item in plans if item["覆盖完整"]),
            "分片条目数": sum(int(item["分片条目"]) for item in plans),
            "补洞条目数": sum(int(item["补洞条目"]) for item in plans),
            "未覆盖计划数": sum(1 for item in plans if item["未覆盖区间"]),
            "未覆盖区间数": sum(len(item["未覆盖区间"]) for item in plans),
            "待归并条目数": sum(int(item["待归并条目"]) for item in plans),
        }

    def _plan_summaries(self) -> list[dict[str, Any]]:
        pending_by_plan: dict[str, list[dict[str, Any]]] = {}
        blocked = self._blocked_by_plan()
        for row in store.rows(PENDING_MODULE):
            if row.get("pending") and row.get("覆盖完整"):
                pending_by_plan.setdefault(str(row.get("采集计划")), []).append(row)
        plans: dict[str, dict[str, Any]] = {}
        for row in store.rows(MODULE):
            if row.get("条目类型") == TYPE_BATCH:
                continue
            if row.get("条目状态") not in {ACTIVE, UNCOVERED_STATUS}:
                continue
            plan = str(row.get("采集计划"))
            bucket = plans.setdefault(
                plan,
                {
                    "采集计划": plan,
                    "范围版本": row.get("范围版本"),
                    "容量版本": row.get("容量版本"),
                    "分片条目": 0,
                    "补洞条目": 0,
                    "容量占用": 0,
                    "节点容量上限": row.get("节点容量上限"),
                    "覆盖区间": [],
                    "未覆盖区间": [],
                    "待归并条目": 0,
                    "覆盖完整": False,
                },
            )
            bucket.setdefault("覆盖目标区间", []).append(str(row.get("覆盖目标")))
            bucket["范围版本"] = max(int(row.get("范围版本") or 1), int(bucket.get("范围版本") or 1))
            if row.get("条目类型") == TYPE_FILL:
                bucket["补洞条目"] += 1
            elif row.get("条目类型") != TYPE_UNCOVERED:
                bucket["分片条目"] += 1
            if row.get("条目状态") == ACTIVE:
                bucket["容量占用"] += int(row.get("占用容量") or 0)
                bucket["覆盖区间"].append(str(row.get("覆盖区间")))
            pending = pending_by_plan.get(plan, [])
            bucket["待归并条目"] = len(pending)
            bucket["覆盖完整"] = bool(pending) and all(item.get("覆盖完整") for item in pending)
        uncovered = self._uncovered_by_plan()
        for plan, bucket in plans.items():
            bucket["未覆盖区间"] = uncovered.get(plan, blocked.get(plan, []))
            bucket["覆盖完整"] = bucket["覆盖完整"] and not bucket["未覆盖区间"]
            bucket["覆盖目标"] = "、".join(
                iv.format_interval(item)
                for item in iv.merge_intervals(
                    iv.parse_interval(text) for text in bucket.pop("覆盖目标区间")
                )
            )
            bucket["覆盖区间"] = [
                iv.format_interval(item)
                for item in iv.merge_intervals(
                    iv.parse_interval(text) for text in bucket["覆盖区间"]
                )
            ]
        return sorted(plans.values(), key=lambda item: item["采集计划"])

    def _blocked_by_plan(self) -> dict[str, list[str]]:
        return {
            str(row.get("采集计划")): list(row.get("未覆盖区间") or [])
            for row in store.rows(PENDING_MODULE)
            if not row.get("覆盖完整")
        }

    def _uncovered_by_plan(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for row in store.rows(MODULE):
            if row.get("条目类型") == TYPE_UNCOVERED and row.get("条目状态") == UNCOVERED_STATUS:
                result.setdefault(str(row.get("采集计划")), []).append(str(row.get("覆盖区间")))
        return result

    # -------------------------------------------------------------- 准入主流程
    def admit(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """准入一个分片范围版本，落一批覆盖清单条目与批次汇总。"""
        with ADMISSION_LOCK:
            try:
                request = self._parse_request(values)
            except CoverageError as exc:
                return None, str(exc)
            return self._admit_locked(request)

    def sync_from_shard(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """分片任务登记入口：把计划、范围与节点交给准入核对，不绕过覆盖清单。"""
        plan = str(values.get("采集计划") or "").strip()
        if not plan:
            return None, "缺少必填字段：采集计划"
        ranges = [str(item) for item in _as_list(values.get("分片范围")) if str(item).strip()]
        if not ranges:
            return None, "缺少必填字段：分片范围"
        for text in ranges:
            if not _looks_like_range(text):
                return None, f"分片范围「{text}」不是可核对的区间写法，需形如 0-100 或 [0, 100)"
        return self.admit(values)

    def _parse_request(self, values: dict[str, Any]) -> dict[str, Any]:
        plan = str(values.get("采集计划") or "").strip()
        if not plan:
            raise CoverageError("缺少必填字段：采集计划")
        texts = [str(item) for item in _as_list(values.get("分片范围")) if str(item).strip()]
        if not texts:
            raise CoverageError("缺少必填字段：分片范围")
        intervals: list[iv.Interval] = []
        for text in texts:
            try:
                intervals.append(iv.parse_interval(text))
            except iv.RangeFormatError as exc:
                raise CoverageError(f"分片范围「{text}」不合法：{exc}") from exc
        policy_raw = str(values.get("冲突策略") or POLICY_FILL).strip()
        policy = _POLICY_ALIASES.get(policy_raw)
        if policy is None:
            raise CoverageError(f"冲突策略「{policy_raw}」不在允许范围内：补洞 / 整段替换")
        try:
            version = int(values.get("采集计划版本") if values.get("采集计划版本") is not None else 1)
        except (TypeError, ValueError) as exc:
            raise CoverageError("采集计划版本必须是正整数") from exc
        if version < 1:
            raise CoverageError("采集计划版本必须是正整数")
        try:
            capacity_cap = int(values.get("容量上限") or 0) or None
        except (TypeError, ValueError) as exc:
            raise CoverageError("容量上限必须是正整数") from exc
        if capacity_cap is not None and capacity_cap <= 0:
            raise CoverageError("容量上限必须是正整数")
        try:
            open_weight = int(values.get("开放端权重") or DEFAULT_OPEN_WEIGHT)
        except (TypeError, ValueError) as exc:
            raise CoverageError("开放端权重必须是正整数") from exc
        if open_weight <= 0:
            raise CoverageError("开放端权重必须是正整数")
        return {
            "plan": plan,
            "intervals": intervals,
            "policy": policy,
            "policy_raw": policy_raw,
            "version": version,
            "source": str(values.get("来源") or "计划提交").strip(),
            "capacity_version": str(values.get("容量版本") or DEFAULT_CAPACITY_VERSION).strip(),
            "capacity_cap": capacity_cap,
            "node": str(values.get("分配节点") or "").strip() or None,
            "open_weight": open_weight,
            "remark": str(values.get("remark") or "").strip(),
        }

    def _admit_locked(self, request: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        plan = request["plan"]
        active = self._active_entries(plan)
        stage = store.rows(CAPACITY_MODULE)
        occupied_versions = sorted(
            {str(row.get("容量版本")) for row in stage if row.get("采集计划") == plan}
        )
        if occupied_versions and request["capacity_version"] not in occupied_versions:
            return None, (
                f"采集计划 {plan} 已占用容量版本 {'、'.join(occupied_versions)}，"
                f"本次提交的容量版本 {request['capacity_version']} 不能重复占用"
            )
        target_before = self._coverage_target(self._plan_entries(plan))
        current_version = max((int(row.get("范围版本") or 1) for row in active), default=0)
        submitted = iv.merge_intervals(request["intervals"])
        outstanding = self._missing(active, target_before)
        if (
            active
            and request["version"] == current_version
            and iv.covered_by(submitted, target_before)
            and not outstanding
        ):
            # 同版本同范围且已无缺口：不再生成第二个同范围分片，也不重复占容量。
            return self._duplicate(request, active)
        if request["policy"] == POLICY_REPLACE and active:
            return self._admit_replace(request, active, stage, submitted, target_before)
        return self._admit_fill(request, active, stage, request["intervals"], target_before)

    def _active_entries(self, plan: str) -> list[dict[str, Any]]:
        return [
            row
            for row in store.rows(MODULE)
            if row.get("条目类型") != TYPE_BATCH
            and row.get("采集计划") == plan
            and row.get("条目状态") == ACTIVE
        ]

    def _plan_entries(self, plan: str) -> list[dict[str, Any]]:
        """已确认 + 未覆盖：未覆盖条目的目标区间也算计划覆盖目标，不能在重试时被抹掉。"""
        return [
            row
            for row in store.rows(MODULE)
            if row.get("条目类型") != TYPE_BATCH
            and row.get("采集计划") == plan
            and row.get("条目状态") in {ACTIVE, UNCOVERED_STATUS}
        ]

    # ---------------------------------------------------------------- 补洞口径
    def _admit_fill(
        self,
        request: dict[str, Any],
        active: list[dict[str, Any]],
        stage: list[dict[str, Any]],
        raw_submitted: list[iv.Interval],
        target_before: list[iv.Interval],
    ) -> tuple[dict[str, Any] | None, str]:
        """保留较窄范围并补洞：重叠段留给较窄范围，其余区间补进缺口。"""
        submitted = iv.merge_intervals(raw_submitted)
        covered = [iv.parse_interval(row.get("覆盖区间")) for row in active]
        candidates: list[dict[str, Any]] = []
        for interval, depth in self._submitted_pieces(raw_submitted):
            conflicted = any(iv.intersect(interval, item) is not None for item in covered)
            for piece in iv.subtract(interval, covered):
                candidates.append({
                    "interval": piece,
                    "entry_type": TYPE_FILL if conflicted or depth > 1 else TYPE_SHARD,
                    "target": interval,
                    "reason": (
                        "新旧范围局部重叠，保留较窄范围并为缺口补洞"
                        if conflicted or depth > 1
                        else "新增覆盖范围"
                    ),
                })
        candidates.sort(key=lambda item: _interval_key(item["interval"]))
        if not candidates:
            # 新版本已被既有覆盖包住：补洞口径下不新增分片，只留一条批次记录。
            return self._noop(request, active, target_before, self._missing(active, target_before))

        target = self._coverage_target_from(target_before, submitted)
        node = request["node"] or self._pick_node(stage, request["plan"])
        cap, cap_error = self._resolve_cap(stage, request, node)
        if cap is None:
            return None, cap_error
        used = self._used_capacity(node, request["capacity_version"])
        # 覆盖清单按目标登记全量缺口，容量不足的部分再转为未覆盖条目，覆盖范围不静默缩水。
        admitted, shortage = self._split_by_capacity(candidates, used, cap, request, node)
        entries = [self._write_entry(request, item, node) for item in admitted]
        for item, entry in zip(admitted, entries):
            self._write_capacity(request, entry, item, node)
        for item in shortage:
            self._write_uncovered(request, item, node)
        active_after = active + entries
        missing = self._missing(active_after, target)
        batch = self._write_batch(
            request,
            active,
            entries,
            missing,
            policy=POLICY_FILL,
            status=BATCH_SHORT if missing else BATCH_ADMITTED,
            target=target,
            check_note=(
                "节点容量不足，保留未覆盖区间 "
                f"{'、'.join(iv.format_interval(i) for i in missing)}，不静默启动聚合"
                if missing
                else "重叠区间保留较窄范围，缺口以补洞条目补齐"
            ),
        )
        self._sync_pending(request, active_after, batch, missing)
        self._close_uncovered(request["plan"], active_after, target)
        message = f"分片覆盖准入完成：新增 {len(entries)} 条覆盖条目"
        if batch["补洞条目数"]:
            message += f"（其中补洞 {batch['补洞条目数']} 条）"
        if missing:
            message += (
                f"，{len(shortage)} 条因容量不足保留为未覆盖区间 "
                f"{'、'.join(iv.format_interval(i) for i in missing)}"
            )
        else:
            message += "，覆盖范围完整"
        return batch, message

    def _submitted_pieces(
        self, submitted: list[iv.Interval]
    ) -> list[tuple[iv.Interval, int]]:
        """把一次提交的多个区间拆成互不重叠的片段，并标出每段被几个区间覆盖。

        一次提交里同时出现新旧两个范围版本时，重叠段（被 2 个区间覆盖）就是
        冲突区间，按「保留较窄范围」的口径登记为补洞条目；首尾相接但不重叠的
        区间仍合成一段，不额外拆片。
        """
        merged = iv.merge_intervals(submitted)
        conflicting = {
            index
            for index, interval in enumerate(submitted)
            if any(
                iv.intersect(interval, other) is not None
                for position, other in enumerate(submitted)
                if position != index
            )
        }
        if not conflicting:
            # 区间之间只首尾相接，没有重叠：按合并后的连续范围处理，不拆片也不标补洞。
            return [
                (
                    interval,
                    1 if interval[0] is not None or interval[1] is not None else 0,
                )
                for interval in merged
            ]
        boundaries = sorted(
            {
                point
                for index in conflicting
                for point in submitted[index]
                if point is not None
            }
        )
        pieces: list[tuple[iv.Interval, int]] = []
        for interval in merged:
            left, right = interval
            cuts = [
                left,
                *[
                    point
                    for point in boundaries
                    if left is not None and right is not None and left < point < right
                ],
                right,
            ]
            for start, end in zip(cuts, cuts[1:]):
                if start is not None and end is not None and start >= end:
                    continue
                depth = sum(
                    1
                    for candidate in submitted
                    if iv.intersect((start, end), candidate) is not None
                )
                pieces.append(((start, end), depth))
        return pieces

    def _duplicate(
        self,
        request: dict[str, Any],
        active: list[dict[str, Any]],
    ) -> tuple[None, str]:
        target = self._coverage_target(self._plan_entries(request["plan"]))
        batch = self._write_batch(
            request,
            active,
            [],
            self._missing(active, target),
            policy=request["policy"],
            status=BATCH_DUP,
            target=target,
            check_note="同计划同范围重复提交，覆盖清单与节点容量保持不变",
        )
        return None, (
            f"采集计划 {request['plan']} 的覆盖范围 {batch['覆盖目标']} 已存在，"
            "重复提交不会生成第二个同范围分片，也不重复占用节点容量"
        )

    def _noop(
        self,
        request: dict[str, Any],
        active: list[dict[str, Any]],
        target: list[iv.Interval],
        missing: list[iv.Interval],
    ) -> tuple[dict[str, Any] | None, str]:
        batch = self._write_batch(
            request,
            active,
            [],
            missing,
            policy=POLICY_FILL,
            status=BATCH_SHORT if missing else BATCH_NOOP,
            target=target,
            check_note=(
                "新范围没有带来可用容量，保留未覆盖区间 "
                f"{'、'.join(iv.format_interval(i) for i in missing)}"
                if missing
                else "新范围已被既有覆盖完整包含，保留较窄范围，无新增分片与新增容量占用"
            ),
        )
        self._sync_pending(request, active, batch, missing)
        if missing:
            return batch, (
                f"没有新增覆盖条目：{'、'.join(iv.format_interval(i) for i in missing)} "
                "仍未覆盖，需先释放节点容量再提交"
            )
        return batch, (
            f"保留较窄范围并补洞：范围 {batch['覆盖目标']} 已被既有分片完整覆盖，"
            "覆盖清单与待归并条目保持不变"
        )

    # -------------------------------------------------------------- 整段替换口径
    def _admit_replace(
        self,
        request: dict[str, Any],
        active: list[dict[str, Any]],
        stage: list[dict[str, Any]],
        submitted: list[iv.Interval],
        target_before: list[iv.Interval],
    ) -> tuple[dict[str, Any] | None, str]:
        node = request["node"] or self._pick_node(stage, request["plan"])
        cap, cap_error = self._resolve_cap(stage, request, node)
        if cap is None:
            return None, cap_error
        # 替换成功时旧版本占用的容量同步释放；先确认新版本能完整装下，再动旧清单。
        released = sum(
            int(row.get("占用容量") or 0)
            for row in stage
            if row.get("采集计划") == request["plan"]
            and row.get("条目状态") == "占用中"
        )
        used_by_others = max(self._used_capacity(node, request["capacity_version"]) - released, 0)
        candidates = [
            {
                "interval": interval,
                "entry_type": TYPE_SHARD,
                "target": interval,
                "reason": "采用较新版本整段替换",
            }
            for interval in submitted
        ]
        admitted, shortage = self._split_by_capacity(
            candidates, used_by_others, cap, request, node
        )
        if shortage:
            # 替换不生效：新版本装不下的部分登记为未覆盖；已装下的部分不落清单，避免与旧版本并存。
            for item in shortage:
                self._write_uncovered(request, item, node)
            missing_old = self._missing(active, target_before)
            uncovered = self._merge_texts(missing_old + [item["interval"] for item in shortage])
            batch = self._write_batch(
                request,
                active,
                [],
                missing_old + [item["interval"] for item in shortage],
                policy=POLICY_REPLACE,
                status=BATCH_SHORT,
                target=target_before,
                check_note=(
                    "整段替换容量不足，旧版本覆盖保留未动；"
                    f"新版本来源 {request['source']} 提交的 "
                    f"{'、'.join(iv.format_interval(i) for i in submitted)} 中装不下的部分"
                    f"（{'、'.join(iv.format_interval(i['interval']) for i in shortage)}）保留为未覆盖区间"
                ),
                submitted=submitted,
            )
            self._sync_pending(request, active, batch, missing_old)
            return batch, (
                f"整段替换未生效：节点容量不足，保留原覆盖并记录未覆盖区间 {'、'.join(uncovered)}；"
                "新旧两个范围版本的来源均已留痕"
            )

        for row in active:
            row["条目状态"] = SUPERSEDED
            row["替换说明"] = f"由范围版本 {request['version']}（{request['source']}）整段替换"
        for row in stage:
            if row.get("采集计划") == request["plan"] and row.get("条目状态") == "占用中":
                row["条目状态"] = "已释放"
                row["释放说明"] = f"由范围版本 {request['version']} 整段替换"
        entries = [self._write_entry(request, item, node) for item in admitted]
        for item, entry in zip(admitted, entries):
            self._write_capacity(request, entry, item, node)
        target = iv.merge_intervals(submitted)
        batch = self._write_batch(
            request,
            active,
            entries,
            [],
            policy=POLICY_REPLACE,
            status=BATCH_ADMITTED,
            target=target,
            check_note="按较新范围版本整段替换旧版本分片，旧版本条目转为已替换并保留来源",
        )
        self._sync_pending(request, entries, batch, [])
        self._close_uncovered(request["plan"], entries, target)
        return batch, (
            f"分片覆盖准入完成：范围版本 {request['version']} 整段替换旧版本，"
            f"新增 {len(entries)} 条覆盖条目，覆盖范围完整"
        )

    # ---------------------------------------------------------------- 容量口径
    def _resolve_cap(
        self, stage: list[dict[str, Any]], request: dict[str, Any], node: str
    ) -> tuple[int | None, str]:
        existing = None
        for row in stage:
            if row.get("容器") == node and row.get("容量版本") == request["capacity_version"]:
                existing = int(row.get("节点容量上限") or DEFAULT_NODE_CAPACITY)
                break
        requested = request["capacity_cap"]
        if existing and requested and existing != requested:
            # 同一节点同一容量版本只认一个上限，前后不一致时明确拦下而不是各算一套。
            return None, (
                f"节点 {node} 在容量版本 {request['capacity_version']} 下的上限已确定为 {existing}，"
                f"本次提交的 {requested} 与既有口径不一致"
            )
        return existing or requested or DEFAULT_NODE_CAPACITY, ""

    def _used_capacity(self, node: str, capacity_version: str) -> int:
        return sum(
            int(row.get("占用容量") or 0)
            for row in store.rows(CAPACITY_MODULE)
            if row.get("容器") == node
            and row.get("容量版本") == capacity_version
            and row.get("条目状态") == "占用中"
        )

    def _split_by_capacity(
        self,
        candidates: list[dict[str, Any]],
        used: int,
        cap: int,
        request: dict[str, Any],
        node: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按统一口径逐条占用容量，返回（准入条目、容量不足条目），顺序保持不变。"""
        admitted: list[dict[str, Any]] = []
        shortage: list[dict[str, Any]] = []
        remaining = max(cap - used, 0)
        for item in candidates:
            interval: iv.Interval = item["interval"]
            cost = self._cost(interval, request["open_weight"])
            if cost <= remaining:
                remaining -= cost
                item["capacity_version"] = request["capacity_version"]
                item["node"] = node
                item["cost"] = cost
                item["capacity_cap"] = cap
                admitted.append(item)
                continue
            # 容量只够覆盖区间的前一段：装得下的部分先占位，剩余区间保留为未覆盖。
            start, end = interval
            if start is not None and end is not None and remaining > 0:
                split = (start, start + remaining)
                admitted.append({
                    **item,
                    "interval": split,
                    "capacity_version": request["capacity_version"],
                    "node": node,
                    "cost": int(remaining),
                    "capacity_cap": cap,
                    "reason": f"{item['reason']}（按节点容量上限截断）",
                })
                shortage.append({**item, "interval": (start + remaining, end), "cost": end - start - remaining})
                remaining = 0
                continue
            shortage.append({**item, "cost": cost})
        return admitted, shortage

    def _coverage_target_from(
        self, target_before: list[iv.Interval], submitted: list[iv.Interval]
    ) -> list[iv.Interval]:
        """覆盖目标只增不减：计划调整不会把已确认分片挤出覆盖范围。"""
        return iv.merge_intervals(target_before + submitted)

    def _cost(self, interval: iv.Interval, open_weight: int) -> int:
        """统一容量口径：有限区间按长度，开放端按权重计，两个方向可比较。"""
        width = iv.length(interval)
        return open_weight if width is None else width

    def _pick_node(self, stage: list[dict[str, Any]], plan: str) -> str:
        for row in stage:
            if row.get("采集计划") == plan and row.get("容器"):
                return str(row["容器"])
        used_nodes = {row.get("容器") for row in stage}
        index = 1
        while f"NODE-{index:03d}" in used_nodes:
            index += 1
        return f"NODE-{index:03d}"

    # ---------------------------------------------------------------- 覆盖判定
    def _coverage_target(self, entries: list[dict[str, Any]]) -> list[iv.Interval]:
        """覆盖目标：所有已确认条目声明的目标区间并集，只增不减地累加。"""
        return iv.merge_intervals(
            iv.parse_interval(row.get("覆盖目标")) for row in entries
        )

    def _missing(
        self, entries: list[dict[str, Any]], target: list[iv.Interval]
    ) -> list[iv.Interval]:
        """目标里仍未被已确认条目覆盖的区间；为空才视为完整覆盖。"""
        covered = [iv.parse_interval(row.get("覆盖区间")) for row in entries]
        missing: list[iv.Interval] = []
        for interval in target:
            missing.extend(iv.subtract(interval, covered))
        return iv.merge_intervals(missing)

    # ---------------------------------------------------------------- 落库收口
    def _write_entry(
        self, request: dict[str, Any], item: dict[str, Any], node: str
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry = {
            "id": _next_id(rows),
            "覆盖编号": _next_code(rows, "COVR"),
            "条目类型": item["entry_type"],
            "采集计划": request["plan"],
            "范围版本": request["version"],
            "容量版本": item.get("capacity_version", request["capacity_version"]),
            "覆盖区间": iv.format_interval(item["interval"]),
            "覆盖目标": iv.format_interval(item["target"]),
            "区间起点": item["interval"][0],
            "区间终点": item["interval"][1],
            "分配节点": node,
            "占用容量": int(item.get("cost", 0)),
            "节点容量上限": item.get("capacity_cap") or request["capacity_cap"],
            "来源": request["source"],
            "来源版本": request["version"],
            "冲突策略": request["policy_raw"],
            "条目状态": ACTIVE if item["entry_type"] != TYPE_UNCOVERED else UNCOVERED_STATUS,
            "判定说明": item["reason"],
            "pending": item["entry_type"] == TYPE_UNCOVERED,
            "abnormal": item["entry_type"] == TYPE_UNCOVERED,
        }
        rows.append(entry)
        return entry

    def _write_uncovered(
        self, request: dict[str, Any], item: dict[str, Any], node: str
    ) -> dict[str, Any] | None:
        """未覆盖区间同一计划同一段只留一条，重复提交不会堆出多条同范围记录。"""
        text = iv.format_interval(item["interval"])
        for row in store.rows(MODULE):
            if (
                row.get("采集计划") == request["plan"]
                and row.get("条目类型") == TYPE_UNCOVERED
                and row.get("条目状态") == UNCOVERED_STATUS
                and row.get("覆盖区间") == text
            ):
                return row
        return self._write_entry(request, {**item, "entry_type": TYPE_UNCOVERED}, node)

    def _write_capacity(
        self,
        request: dict[str, Any],
        entry: dict[str, Any],
        item: dict[str, Any],
        node: str,
    ) -> None:
        store.rows(CAPACITY_MODULE).append({
            "id": _next_id(store.rows(CAPACITY_MODULE)),
            "采集计划": request["plan"],
            "覆盖编号": entry["覆盖编号"],
            "容器": node,
            "容量版本": item["capacity_version"],
            "节点容量上限": item["capacity_cap"],
            "占用容量": item["cost"],
            "覆盖区间": entry["覆盖区间"],
            "条目类型": entry["条目类型"],
            "条目状态": "占用中",
            "pending": True,
            "abnormal": False,
        })

    def _write_batch(
        self,
        request: dict[str, Any],
        active: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        uncovered: list[iv.Interval],
        *,
        policy: str,
        status: str,
        target: list[iv.Interval],
        check_note: str,
        submitted: list[iv.Interval] | None = None,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        old_versions = {int(row.get("范围版本") or 1) for row in active}
        new_versions = {request["version"]}
        source_versions = sorted(old_versions | new_versions)
        uncovered_texts = self._merge_texts(uncovered)
        batch = {
            "id": _next_id(rows),
            "覆盖编号": _next_code(rows, "COVB"),
            "条目类型": TYPE_BATCH,
            "采集计划": request["plan"],
            "范围版本": request["version"],
            "容量版本": request["capacity_version"],
            "冲突策略": request["policy_raw"],
            "处理结论": policy,
            "覆盖目标": "、".join(iv.format_interval(item) for item in target),
            "提交范围": "、".join(
                iv.format_interval(item)
                for item in iv.merge_intervals(submitted or request["intervals"])
            ),
            "新增条目数": len(entries),
            "补洞条目数": sum(1 for row in entries if row["条目类型"] == TYPE_FILL),
            "新增覆盖条目": [row["覆盖编号"] for row in entries],
            "未覆盖区间": uncovered_texts,
            "未覆盖容量": sum(self._cost(item, request["open_weight"]) for item in uncovered),
            "占用容量": sum(int(row.get("占用容量") or 0) for row in entries),
            "节点容量上限": (entries[0].get("节点容量上限") if entries else request["capacity_cap"]),
            "分配节点": (entries[0].get("分配节点") if entries else request["node"]),
            "来源版本": source_versions,
            "来源明细": self._source_detail(active, request),
            "来源": request["source"],
            "条目状态": status,
            "判定说明": check_note,
            "pending": status in {BATCH_SHORT, BATCH_DUP, BATCH_NOOP},
            "abnormal": status == BATCH_SHORT,
        }
        rows.append(batch)
        return batch

    def _source_detail(
        self, active: list[dict[str, Any]], request: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """两个范围版本同时到达时逐版本记录来源，复核时能看出谁覆盖了哪一段。"""
        detail: dict[int, dict[str, Any]] = {}

        def bucket(version: int, source: Any) -> dict[str, Any]:
            return detail.setdefault(version, {
                "范围版本": version,
                "来源": source,
                "覆盖区间": [],
            })

        for row in active:
            version = int(row.get("来源版本") or row.get("范围版本") or 1)
            bucket(version, row.get("来源"))["覆盖区间"].append(str(row.get("覆盖区间")))
        bucket(request["version"], request["source"])["覆盖区间"].extend(
            iv.format_interval(item) for item in iv.merge_intervals(request["intervals"])
        )
        for item in detail.values():
            item["覆盖区间"] = [
                iv.format_interval(interval)
                for interval in iv.merge_intervals(
                    iv.parse_interval(text) for text in item["覆盖区间"]
                )
            ]
        return [detail[key] for key in sorted(detail)]

    def _merge_texts(self, intervals: Iterable[iv.Interval]) -> list[str]:
        return [
            iv.format_interval(item)
            for item in iv.merge_intervals(list(intervals))
        ]

    def _sync_pending(
        self,
        request: dict[str, Any],
        active: list[dict[str, Any]],
        batch: dict[str, Any],
        missing: list[iv.Interval],
    ) -> None:
        """聚合侧待归并：只按完整覆盖版本的清单条目逐条建立，重复提交不新增。"""
        plan = request["plan"]
        rows = store.rows(PENDING_MODULE)
        existing = {
            str(row.get("覆盖编号")): row
            for row in rows
            if row.get("采集计划") == plan
        }
        if missing:
            # 覆盖不完整：聚合只能看到完整覆盖版本，撤下该计划的待归并条目并登记阻塞原因。
            for row in existing.values():
                if row.get("阻塞标记"):
                    continue
                row["pending"] = False
                row["abnormal"] = True
                row["条目状态"] = "未覆盖"
                row["未覆盖区间"] = self._merge_texts(missing)
                row["覆盖完整"] = False
            blocker = self._blocker(rows, plan)
            blocker.update({
                "覆盖编号": batch["覆盖编号"],
                "阻塞标记": True,
                "范围版本": request["version"],
                "覆盖目标": batch["覆盖目标"],
                "覆盖区间": "",
                "未覆盖区间": self._merge_texts(missing),
                "覆盖完整": False,
                "来源": request["source"],
                "来源版本": request["version"],
                "条目状态": "未覆盖",
                "pending": True,
                "abnormal": True,
            })
            return
        target_text = batch["覆盖目标"]
        rows[:] = [
            row
            for row in rows
            if not (row.get("采集计划") == plan and row.get("阻塞标记"))
        ]
        wanted = {str(row.get("覆盖编号")): row for row in active}
        for code, row in existing.items():
            if code in wanted:
                row.update({
                    "范围版本": wanted[code].get("范围版本"),
                    "条目类型": wanted[code].get("条目类型"),
                    "覆盖区间": wanted[code].get("覆盖区间"),
                    "覆盖目标": target_text,
                    "未覆盖区间": [],
                    "覆盖完整": True,
                    "来源": wanted[code].get("来源"),
                    "来源版本": wanted[code].get("来源版本"),
                    "条目状态": "待归并",
                    "pending": True,
                    "abnormal": False,
                })
            else:
                row["pending"] = False
                row["abnormal"] = False
                row["条目状态"] = "已撤下"
                row["撤下说明"] = f"由范围版本 {request['version']} 调整覆盖范围"
        for code, entry in wanted.items():
            if code in existing:
                continue
            rows.append({
                "id": _next_id(rows),
                "采集计划": plan,
                "覆盖编号": code,
                "范围版本": entry.get("范围版本"),
                "条目类型": entry.get("条目类型"),
                "覆盖区间": entry.get("覆盖区间"),
                "覆盖目标": target_text,
                "未覆盖区间": [],
                "覆盖完整": True,
                "来源": entry.get("来源"),
                "来源版本": entry.get("来源版本"),
                "条目状态": "待归并",
                "pending": True,
                "abnormal": False,
            })

    def _blocker(self, rows: list[dict[str, Any]], plan: str) -> dict[str, Any]:
        """每个计划保留一条阻塞记录，聚合据此隐藏不完整覆盖版本。"""
        for row in rows:
            if row.get("采集计划") == plan and row.get("阻塞标记"):
                return row
        row = {
            "id": _next_id(rows),
            "采集计划": plan,
            "覆盖编号": f"BLOCK-{plan}",
            "阻塞标记": True,
            "pending": True,
            "abnormal": True,
        }
        rows.append(row)
        return row

    def _close_uncovered(
        self,
        plan: str,
        active: list[dict[str, Any]],
        target: list[iv.Interval],
    ) -> None:
        """后续版本补齐后把旧的未覆盖条目置为已补齐，便于复核闭环。"""
        covered = [iv.parse_interval(row.get("覆盖区间")) for row in active]
        still_missing = bool(self._missing(active, target))
        for row in store.rows(MODULE):
            if (
                row.get("采集计划") == plan
                and row.get("条目类型") == TYPE_UNCOVERED
                and row.get("条目状态") == UNCOVERED_STATUS
            ):
                interval = iv.parse_interval(row.get("覆盖区间"))
                if not still_missing and not iv.subtract(interval, covered):
                    row["条目状态"] = FILLED_STATUS
                    row["pending"] = False
                    row["abnormal"] = False
