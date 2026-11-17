"""分片范围的统一口径：解析、合并、求补集与格式化。

覆盖核对、节点容量占用、聚合待归并条目都只认这里的区间表示，避免同一个范围
在不同模块里被理解成不同的数字。约定：

- 范围是整数半开区间 ``[start, end)``，与 ``start-end``、``start~end`` 写法等价；
  只写一个数字时按 ``[start, start+1)`` 处理。
- 端点允许为空的一端：``[0, 100)``、``[500, 末尾)``、``开头-20``。
- 解析失败直接抛 ``RangeFormatError``，由调用方转成可读的校验提示。
"""
from __future__ import annotations

import math
from typing import Iterable

OPEN_START = "开头"
OPEN_END = "末尾"


class RangeFormatError(ValueError):
    """分片范围写法无法解析。"""


Interval = tuple[int | None, int | None]


def _parse_point(token: str) -> int | None:
    token = token.strip()
    if token in {"", "*", "∞", "inf", "开头", "起点", "末尾", "终点"}:
        return None
    try:
        return int(token)
    except ValueError as exc:
        raise RangeFormatError(f"「{token}」不是整数边界") from exc


def normalize(text: object) -> str:
    """把任意写法归一成 ``[a, b)``，用于判重和落库展示。"""
    return format_interval(parse_interval(text))


def parse_interval(text: object) -> Interval:
    """解析一条分片范围；空值或无法识别的写法都按格式错误处理。"""
    raw = str(text or "").strip()
    if not raw:
        raise RangeFormatError("分片范围为空")
    cleaned = raw
    if cleaned[0] in "[(" and cleaned[-1] in ")]":
        cleaned = cleaned[1:-1]
    separators = [sep for sep in (",", "~", "-") if sep in cleaned]
    if not separators:
        # 纯数字视为单点：n 等价于 [n, n+1)。
        point = _parse_point(cleaned)
        if point is None:
            raise RangeFormatError("分片范围缺少起止边界")
        return point, point + 1
    separator = min(separators, key=cleaned.index)
    if separator == "-" and cleaned.startswith("-"):
        # 「-20」这种写法当作 [开头, 20)，而不是把负号当分隔符。
        left, right = "", cleaned[1:]
    else:
        left, right = cleaned.split(separator, 1)
    start = _parse_point(left)
    end = _parse_point(right)
    if start is not None and end is not None and start >= end:
        raise RangeFormatError(f"起始边界 {start} 不小于结束边界 {end}")
    return start, end


def format_interval(interval: Interval) -> str:
    start, end = interval
    left = OPEN_START if start is None else str(start)
    right = OPEN_END if end is None else str(end)
    return f"{left}-{right}"


def sort_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """按起点排序；起点为空的区间排在最前，代表最小的起点。"""
    return sorted(intervals, key=lambda item: (item[0] is not None, item[0] if item[0] is not None else 0))


def _touch(left: Interval, right: Interval) -> bool:
    """两个半开区间是否相邻或相交（左闭右开，端点相接视为连续）。"""
    left_start, left_end = left
    right_start, right_end = right
    if left_start is not None and right_end is not None and left_start >= right_end:
        return False
    if right_start is not None and left_end is not None and right_start > left_end:
        return False
    return True


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """合并有重叠或首尾相接的区间；开放端算作已覆盖到最远。"""
    merged: list[Interval] = []
    for current in sort_intervals(intervals):
        if not merged:
            merged.append(current)
            continue
        previous = merged[-1]
        if not _touch(previous, current):
            merged.append(current)
            continue
        prev_start, prev_end = previous
        cur_start, cur_end = current
        if prev_end is None or cur_end is None:
            new_end: int | None = None
        else:
            new_end = max(prev_end, cur_end)
        new_start: int | None
        if prev_start is None or cur_start is None:
            new_start = None
        else:
            new_start = min(prev_start, cur_start)
        merged[-1] = (new_start, new_end)
    return merged


def intersect(left: Interval, right: Interval) -> Interval | None:
    """两个区间的交集；没有交集返回 None。"""
    ls, le = left
    rs, re = right
    start = rs if ls is None else (max(ls, rs) if rs is not None else ls)
    end = le if re is None else (min(le, re) if le is not None else re)
    if start is not None and end is not None and start >= end:
        return None
    return start, end


def intersection_of(intervals: Iterable[Interval]) -> Interval | None:
    """多个区间的公共交集；任一方向为空即返回 None。"""
    result: Interval | None = None
    for item in intervals:
        if result is None:
            result = item
            continue
        result = intersect(result, item)
        if result is None:
            return None
    return result


def within(inner: Interval, outer: Interval) -> bool:
    """inner 是否完整落在 outer 之内。"""
    inner_start, inner_end = inner
    outer_start, outer_end = outer
    if outer_start is not None and (inner_start is None or inner_start < outer_start):
        return False
    if outer_end is not None and (inner_end is None or inner_end > outer_end):
        return False
    return True


def subtract(base: Interval, covered: Iterable[Interval]) -> list[Interval]:
    """从 base 里扣掉 covered 已覆盖的部分，返回仍然缺失的区间。

    开放端用正负无穷参与比较，输出时再换回 ``None``，这样
    〔开头, 20〕这类范围不会被当成「已经覆盖到最远」。
    """
    base_start, base_end = base
    low = -math.inf if base_start is None else base_start
    high = math.inf if base_end is None else base_end
    cursor = low
    pieces: list[tuple[float, float]] = []
    for cover_start, cover_end in merge_intervals(covered):
        c_low = -math.inf if cover_start is None else cover_start
        c_high = math.inf if cover_end is None else cover_end
        if c_high <= cursor:
            continue
        if c_low > cursor:
            pieces.append((cursor, min(c_low, high)))
        cursor = max(cursor, c_high)
        if cursor >= high:
            break
    if cursor < high:
        pieces.append((cursor, high))
    result: list[Interval] = []
    for start, end in pieces:
        piece_start = None if start == -math.inf else int(start)
        piece_end = None if end == math.inf else int(end)
        if valid((piece_start, piece_end)):
            result.append((piece_start, piece_end))
    return result


def valid(interval: Interval) -> bool:
    start, end = interval
    if start is None and end is None:
        return True
    if start is None or end is None:
        return True
    return start < end


def length(interval: Interval) -> int | None:
    """区间长度；开放端无法给出确定长度时返回 None。"""
    start, end = interval
    if start is None or end is None:
        return None
    return end - start


def contains_point(interval: Interval, point: int | None) -> bool:
    start, end = interval
    if point is None:
        return False
    if start is not None and point < start:
        return False
    if end is not None and point >= end:
        return False
    return True


def covered_by(inner: list[Interval], outer: list[Interval]) -> bool:
    """区间列表 inner 的每一段是否都被 outer 的并集完整覆盖。"""
    return not subtract_union(inner, outer)


def subtract_union(inner: list[Interval], outer: list[Interval]) -> list[Interval]:
    """inner 减去 outer 的并集后剩下的区间；为空说明 inner 已被完整覆盖。"""
    remaining: list[Interval] = []
    for interval in inner:
        remaining.extend(subtract(interval, outer))
    return remaining
