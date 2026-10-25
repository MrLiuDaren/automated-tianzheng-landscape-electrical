#!/usr/bin/env python3
"""Validate landscape electrical registers without modifying drawings or source data."""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


LOAD_REQUIRED = {
    "record_id",
    "category",
    "description",
    "quantity",
    "unit_power_kw",
    "demand_factor",
    "circuit",
}
POINT_REQUIRED = {
    "point_id",
    "point_type",
    "description",
    "category",
    "serves",
    "circuit",
    "upstream_id",
    "junction_id",
    "status",
}
STYLE_REQUIRED = {
    "style_id",
    "object_type",
    "layer_name",
    "block_name",
    "usage",
}

POINT_ID_RE = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z0-9_-]*?)-(?P<number>\d+)$")
RANGE_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9_]*?)-(?P<start>\d+)"
    r"\s*[~～\-至]\s*"
    r"(?:(?P<end_prefix>[A-Za-z][A-Za-z0-9_]*?)-)?"
    r"(?P<end>\d+)$"
)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.lines: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def print(self) -> None:
        for line in self.lines:
            print(line)
        if self.warnings:
            print("\n警告：")
            for message in self.warnings:
                print(f"  - {message}")
        if self.errors:
            print("\n错误：")
            for message in self.errors:
                print(f"  - {message}")


def read_csv(path: Path, required: set[str], report: Report) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = set(reader.fieldnames or [])
            missing = sorted(required - fieldnames)
            if missing:
                report.error(f"{path.name} 缺少字段：{', '.join(missing)}")
                return []
            rows = []
            for row_number, row in enumerate(reader, start=2):
                cleaned = {
                    (key or "").strip(): (value or "").strip()
                    for key, value in row.items()
                }
                if any(cleaned.values()):
                    cleaned["_row_number"] = str(row_number)
                    rows.append(cleaned)
            return rows
    except FileNotFoundError:
        report.error(f"找不到文件：{path}")
    except UnicodeDecodeError as exc:
        report.error(f"{path.name} 不是可读取的 UTF-8 CSV：{exc}")
    except csv.Error as exc:
        report.error(f"{path.name} CSV 格式错误：{exc}")
    return []


def parse_positive_float(
    value: str, field: str, row_id: str, report: Report
) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        report.error(f"{row_id}: {field} 不是有效数字：{value!r}")
        return None
    if not math.isfinite(number) or number < 0:
        report.error(f"{row_id}: {field} 必须是非负有限数字：{value!r}")
        return None
    return number


def check_unique(
    rows: Iterable[dict[str, str]], key: str, label: str, report: Report
) -> None:
    values = [row[key] for row in rows if row.get(key)]
    for value, count in Counter(values).items():
        if count > 1:
            report.error(f"{label}重复 {count} 次：{value}")
    missing = sum(1 for row in rows if not row.get(key))
    if missing:
        report.error(f"{label}为空的行数：{missing}")


def validate_loads(path: Path, report: Report) -> dict[str, float]:
    rows = read_csv(path, LOAD_REQUIRED, report)
    if not rows:
        report.warn(f"{path.name} 没有数据行")
        return {"connected_kw": 0.0, "design_kw": 0.0}

    check_unique(rows, "record_id", "回路负荷记录编号", report)
    by_circuit: dict[str, dict[str, float]] = defaultdict(
        lambda: {"connected": 0.0, "design": 0.0}
    )
    total_connected = 0.0
    total_design = 0.0

    for row in rows:
        row_id = row["record_id"] or f"第{row['_row_number']}行"
        quantity = parse_positive_float(row["quantity"], "quantity", row_id, report)
        unit_power = parse_positive_float(
            row["unit_power_kw"], "unit_power_kw", row_id, report
        )
        demand_factor = parse_positive_float(
            row["demand_factor"], "demand_factor", row_id, report
        )
        if not row["category"]:
            report.error(f"{row_id}: category 为空")
        if not row["description"]:
            report.error(f"{row_id}: description 为空")
        if not row["circuit"]:
            report.error(f"{row_id}: circuit 为空")
        if demand_factor is not None and demand_factor > 1:
            report.error(f"{row_id}: demand_factor 不应大于 1")
        if quantity is None or unit_power is None or demand_factor is None:
            continue

        connected = quantity * unit_power
        design = connected * demand_factor
        total_connected += connected
        total_design += design
        circuit = row["circuit"] or "未分配"
        by_circuit[circuit]["connected"] += connected
        by_circuit[circuit]["design"] += design

    report.lines.append(
        f"负荷表：{len(rows)} 行，连接负荷 {total_connected:.3f} kW，"
        f"设计负荷 {total_design:.3f} kW"
    )
    report.lines.append("回路汇总：")
    report.lines.append(
        f"  {'回路':<12}{'连接 kW':>12}{'设计 kW':>12}{'需要系数':>12}"
    )
    for circuit in sorted(by_circuit):
        values = by_circuit[circuit]
        factor = (
            values["design"] / values["connected"] if values["connected"] else 0.0
        )
        report.lines.append(
            f"  {circuit:<12}{values['connected']:>12.3f}"
            f"{values['design']:>12.3f}{factor:>12.3f}"
        )
    return {"connected_kw": total_connected, "design_kw": total_design}


def parse_serves(value: str) -> set[str]:
    served: set[str] = set()
    for token in re.split(r"[,，;；\s]+", value):
        token = token.strip()
        if not token:
            continue
        range_match = RANGE_RE.fullmatch(token)
        if range_match:
            prefix = range_match.group("prefix")
            end_prefix = range_match.group("end_prefix")
            if end_prefix and end_prefix.upper() != prefix.upper():
                served.add(token)
                continue
            start = int(range_match.group("start"))
            end = int(range_match.group("end"))
            width = max(len(range_match.group("start")), len(range_match.group("end")))
            step = 1 if end >= start else -1
            for number in range(start, end + step, step):
                served.add(f"{prefix}-{number:0{width}d}")
        else:
            served.add(token)
    return served


def is_service_box(point_id: str, point_type: str) -> bool:
    text = f"{point_id} {point_type}"
    return "服务箱" in text or "服务插座箱" in text


def is_market_stall(row: dict[str, str]) -> bool:
    text = f"{row['point_type']} {row['category']} {row['description']}"
    return "摊位" in text or "外摆" in text


def validate_points(path: Path, report: Report) -> list[dict[str, str]]:
    rows = read_csv(path, POINT_REQUIRED, report)
    if not rows:
        report.warn(f"{path.name} 没有数据行")
        return []

    check_unique(rows, "point_id", "点位编号", report)
    ids = {row["point_id"] for row in rows if row["point_id"]}
    stall_ids = {row["point_id"] for row in rows if is_market_stall(row)}
    served_by: dict[str, list[str]] = defaultdict(list)
    service_rows = 0

    for row in rows:
        point_id = row["point_id"] or f"第{row['_row_number']}行"
        if not row["point_type"]:
            report.error(f"{point_id}: point_type 为空")
        if not row["description"]:
            report.error(f"{point_id}: description 为空")
        if not row["category"]:
            report.error(f"{point_id}: category 为空")
        upstream = row["upstream_id"]
        if upstream and upstream not in ids:
            report.warn(f"{point_id}: upstream_id 未在点位表中登记：{upstream}")
        junction = row["junction_id"]
        if junction and junction not in ids:
            report.warn(f"{point_id}: junction_id 未在点位表中登记：{junction}")

        if is_service_box(point_id, row["point_type"]):
            service_rows += 1
            served = parse_serves(row["serves"])
            unknown = sorted(served - ids)
            if unknown:
                report.error(
                    f"{point_id}: serves 中的点位未在点位表登记："
                    f"{', '.join(unknown)}"
                )
            stalls = served & stall_ids
            if stalls:
                for stall in stalls:
                    served_by[stall].append(point_id)

    if stall_ids and service_rows:
        missing = sorted(stall_ids - set(served_by))
        duplicated = {
            stall: boxes for stall, boxes in served_by.items() if len(boxes) > 1
        }
        if missing:
            report.error(f"未被服务箱覆盖的摊位：{', '.join(missing)}")
        for stall in sorted(duplicated):
            report.error(
                f"摊位被多个服务箱重复覆盖：{stall} -> {', '.join(duplicated[stall])}"
            )
        report.lines.append(
            f"摊位覆盖：{len(stall_ids)} 个摊位，{service_rows} 个服务箱，"
            f"未覆盖 {len(missing)}，重复覆盖 {len(duplicated)}"
        )
    else:
        report.lines.append(
            f"点位表：{len(rows)} 行；未执行摊位覆盖复核"
            "（缺少摊位类点位或服务箱记录）"
        )
    return rows


def validate_styles(path: Path, report: Report) -> None:
    rows = read_csv(path, STYLE_REQUIRED, report)
    if not rows:
        report.warn(f"{path.name} 没有数据行")
        return
    check_unique(rows, "style_id", "图面样式编号", report)
    for row in rows:
        style_id = row["style_id"] or f"第{row['_row_number']}行"
        for field in ("object_type", "layer_name", "usage"):
            if not row[field]:
                report.error(f"{style_id}: {field} 为空")
        height = row.get("text_height_mm", "")
        if height:
            parse_positive_float(height, "text_height_mm", style_id, report)
    report.lines.append(f"图面样式表：{len(rows)} 行")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验景观电气负荷、点位和样式 CSV 登记表。"
    )
    parser.add_argument("--load", required=True, type=Path, help="回路负荷登记表")
    parser.add_argument("--points", required=True, type=Path, help="点位登记表")
    parser.add_argument("--style", required=True, type=Path, help="图面样式登记表")
    parser.add_argument(
        "--supply-kw",
        type=float,
        help="可选：上游标称容量。仅按 kW 做基本比较，不替代正式 kVA 计算。",
    )
    args = parser.parse_args()

    report = Report()
    totals = validate_loads(args.load, report)
    validate_points(args.points, report)
    validate_styles(args.style, report)

    if args.supply_kw is not None:
        if not math.isfinite(args.supply_kw) or args.supply_kw <= 0:
            report.error(f"--supply-kw 必须为正数：{args.supply_kw}")
        else:
            report.lines.append(
                f"上游基本比较：标称 {args.supply_kw:.3f} kW，"
                f"设计负荷 {totals['design_kw']:.3f} kW，"
                f"差值 {args.supply_kw - totals['design_kw']:.3f} kW"
            )
            if totals["design_kw"] > args.supply_kw:
                report.warn(
                    "设计负荷已超过 kW 标称值；仍需结合功率因数、同时系数、"
                    "单相平衡和保护条件完成正式校核"
                )
            else:
                report.warn(
                    "仅按 kW 数值未超上游标称值；不得据此直接判定容量满足，"
                    "仍需完成 kVA、单相平衡、电压降和保护校核"
                )

    report.print()
    print(f"\n结果：{len(report.errors)} 个错误，{len(report.warnings)} 个警告")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
