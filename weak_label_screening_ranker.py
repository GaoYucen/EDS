#!/usr/bin/env python3
"""Known-failure calibrated chip screening ranker.

This script builds a retrospective screening score from the existing
bridge_chip_risk_model output. It is intentionally simple and transparent:
signals are converted to within-module percentiles, then averaged.

The result is meant for review prioritization under the current confirmed
failure data condition, not for claiming externally validated failure prediction.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INPUT_CSV = ROOT / "result" / "bridge_chip_risk_model" / "risk_scores.csv"
OUT_DIR = ROOT / "result" / "weak_label_screening"

# Direction is +1 when larger raw value means higher screening risk, -1 when
# smaller raw value means higher screening risk.
SIGNALS = [
    ("global_consistency", "chip_raw_outlier_score", -1.0),
    ("IGE4_30_high", "IGE4_30", 1.0),
    ("IGE5_R30_high", "IGE5_R30", 1.0),
    ("ICES7_1330_high", "ICES7_1330", 1.0),
]


def to_float(value: object) -> float:
    try:
        out = float(str(value))
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def percentile_scores(rows: list[dict[str, object]], column: str, direction: float) -> dict[int, float]:
    scores: dict[int, float] = {}
    by_module: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_module[str(row["SN_ID"])].append(row)

    for module_rows in by_module.values():
        valid = [
            row
            for row in module_rows
            if math.isfinite(to_float(row.get(column)))
        ]
        ordered = sorted(
            valid,
            key=lambda row: (
                direction * to_float(row[column]),
                str(row["DBC_LOCATION"]),
                int(to_float(row["芯片位置"])),
            ),
        )
        denom = max(1, len(ordered) - 1)
        for idx, row in enumerate(ordered):
            scores[id(row)] = idx / denom
        for row in module_rows:
            scores.setdefault(id(row), 0.0)
    return scores


def rank_within_module(rows: list[dict[str, object]], score_column: str, rank_column: str) -> None:
    by_module: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_module[str(row["SN_ID"])].append(row)
    for module_rows in by_module.values():
        ordered = sorted(
            module_rows,
            key=lambda row: (
                -to_float(row[score_column]),
                str(row["DBC_LOCATION"]),
                int(to_float(row["芯片位置"])),
            ),
        )
        for rank, row in enumerate(ordered, start=1):
            row[rank_column] = rank


def evaluate(rows: list[dict[str, object]], score_column: str, rank_column: str) -> dict[str, float]:
    positives = [row for row in rows if int(to_float(row["label_bad"])) == 1]
    ranks = [int(row[rank_column]) for row in positives]
    ordered = sorted(rows, key=lambda row: -to_float(row[score_column]))
    hit = 0
    ap_total = 0.0
    for idx, row in enumerate(ordered, start=1):
        if int(to_float(row["label_bad"])) == 1:
            hit += 1
            ap_total += hit / idx
    positive_count = len(positives)
    return {
        "positive_count": float(positive_count),
        "mean_positive_rank": sum(ranks) / positive_count if positive_count else 0.0,
        "top1_recall": sum(1 for rank in ranks if rank <= 1) / positive_count if positive_count else 0.0,
        "top3_recall": sum(1 for rank in ranks if rank <= 3) / positive_count if positive_count else 0.0,
        "average_precision": ap_total / positive_count if positive_count else 0.0,
    }


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    base = [
        "SN_ID",
        "DBC_LOCATION",
        "芯片位置",
        "bridge_group",
        "label_bad",
        "screening_score",
        "screening_rank_in_module",
        "risk_score",
        "risk_rank_in_module",
    ]
    component_columns = [f"{name}_pct" for name, _column, _direction in SIGNALS]
    raw_columns = [column for _name, column, _direction in SIGNALS]
    columns = base + component_columns + raw_columns
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (str(r["SN_ID"]), int(r["screening_rank_in_module"]))):
            writer.writerow({column: row.get(column, "") for column in columns})


def write_report(rows: list[dict[str, object]], path: Path) -> None:
    screening = evaluate(rows, "screening_score", "screening_rank_in_module")
    original = evaluate(rows, "risk_score", "risk_rank_in_module")
    positives = sorted(
        [row for row in rows if int(to_float(row["label_bad"])) == 1],
        key=lambda row: str(row["SN_ID"]),
    )

    positive_lines = []
    for row in positives:
        positive_lines.append(
            "| {sn} | {dbc} | {pos} | {bridge} | {rank} | {score:.4f} | {old_rank} |".format(
                sn=row["SN_ID"],
                dbc=row["DBC_LOCATION"],
                pos=int(to_float(row["芯片位置"])),
                bridge=row["bridge_group"],
                rank=int(row["screening_rank_in_module"]),
                score=to_float(row["screening_score"]),
                old_rank=int(to_float(row["risk_rank_in_module"])),
            )
        )

    top_lines = []
    for sn in sorted({str(row["SN_ID"]) for row in rows}):
        module_rows = sorted(
            [row for row in rows if str(row["SN_ID"]) == sn],
            key=lambda row: int(row["screening_rank_in_module"]),
        )[:3]
        labels = ", ".join(
            f"{row['DBC_LOCATION']}{int(to_float(row['芯片位置']))}"
            f"{'*' if int(to_float(row['label_bad'])) == 1 else ''}"
            for row in module_rows
        )
        top_lines.append(f"- `{sn}`: {labels}")

    text = f"""# 已知失效筛查排序

## 目的

这是基于当前数据构建的回顾性已知失效筛查分数。目标是在正式监督建模样本仍偏少时，形成更有用的模块内复核优先级清单，同时避免把结果表述成已经外部验证的生产级失效预测模型。

## 分数定义

每个信号先转换为模块内百分位，再取平均：

- 较低的 `chip_raw_outlier_score`：避免泛化离群点主导排序
- 较高的 `IGE4_30`
- 较高的 `IGE5_R30`
- 较高的 `ICES7_1330`

```text
screening_score = mean(global_consistency_pct,
                       IGE4_30_high_pct,
                       IGE5_R30_high_pct,
                       ICES7_1330_high_pct)
```

## 回顾性评估

| 指标 | 原桥臂+芯片风险排序 | 已知失效筛查排序 |
|---|---:|---:|
| 平均坏芯片排名 / 18 | {original['mean_positive_rank']:.2f} | {screening['mean_positive_rank']:.2f} |
| Top-1 recall | {original['top1_recall']:.3f} | {screening['top1_recall']:.3f} |
| Top-3 recall | {original['top3_recall']:.3f} | {screening['top3_recall']:.3f} |
| Average precision | {original['average_precision']:.3f} | {screening['average_precision']:.3f} |

## 标色坏芯片排名明细

| SN_ID | DBC | 芯片位置 | 桥臂组 | 筛查排名 | 筛查分数 | 原排名 |
|---|---:|---:|---|---:|---:|---:|
{chr(10).join(positive_lines)}

## 模块 Top-3 复核清单

带 `*` 的芯片为当前确认失效芯片。

{chr(10).join(top_lines)}

## 重要限定

该结果使用当前确认失效芯片选择了紧凑筛查规则，因此应表述为回顾性复核优先级结果。它在当前数据上比未调权异常分数更有用，但尚不是经过外部验证的失效预测模型。
"""
    path.write_text(text, encoding="utf-8")


def write_svg(rows: list[dict[str, object]], path: Path) -> None:
    modules = sorted({str(row["SN_ID"]) for row in rows})
    width = 1080
    module_h = 165
    height = 70 + module_h * len(modules)
    left = 245
    bar_w = 650
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="30" y="34" font-family="Arial, sans-serif" font-size="22" font-weight="700">Known-Failure Screening Rank</text>',
        '<text x="30" y="55" font-family="Arial, sans-serif" font-size="12" fill="#555">Orange = confirmed failed chip; bar length = screening_score.</text>',
    ]
    for module_idx, sn in enumerate(modules):
        y0 = 88 + module_idx * module_h
        lines.append(f'<text x="30" y="{y0}" font-family="Arial, sans-serif" font-size="15" font-weight="700">{sn[-8:]}</text>')
        module_rows = sorted(
            [row for row in rows if str(row["SN_ID"]) == sn],
            key=lambda row: int(row["screening_rank_in_module"]),
        )
        for idx, row in enumerate(module_rows):
            yy = y0 + 20 + idx * 7
            score = to_float(row["screening_score"])
            color = "#d55e00" if int(to_float(row["label_bad"])) == 1 else "#4c78a8"
            label = f"{int(row['screening_rank_in_module']):>2}. {row['DBC_LOCATION']}{int(to_float(row['芯片位置']))} {row['bridge_group']}"
            lines.append(f'<text x="56" y="{yy + 4}" font-family="Arial, sans-serif" font-size="9" fill="#333">{label}</text>')
            lines.append(f'<rect x="{left}" y="{yy - 4}" width="{max(1, score * bar_w):.1f}" height="10" rx="1" fill="{color}" opacity="0.82"/>')
            lines.append(f'<text x="{left + bar_w + 10}" y="{yy + 4}" font-family="Arial, sans-serif" font-size="9" fill="#333">{score:.3f}</text>')
        lines.append(f'<line x1="30" y1="{y0 + 146}" x2="{width - 30}" y2="{y0 + 146}" stroke="#e6e6e6"/>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with INPUT_CSV.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    component_maps = {
        name: percentile_scores(rows, column, direction)
        for name, column, direction in SIGNALS
    }
    for row in rows:
        parts = []
        for name, _column, _direction in SIGNALS:
            value = component_maps[name][id(row)]
            row[f"{name}_pct"] = value
            parts.append(value)
        row["screening_score"] = sum(parts) / len(parts)

    rank_within_module(rows, "screening_score", "screening_rank_in_module")
    write_csv(rows, OUT_DIR / "screening_scores.csv")
    write_report(rows, OUT_DIR / "result.md")
    write_svg(rows, OUT_DIR / "rank_by_module.svg")
    print(f"Wrote {OUT_DIR / 'screening_scores.csv'}")
    print(f"Wrote {OUT_DIR / 'result.md'}")
    print(f"Wrote {OUT_DIR / 'rank_by_module.svg'}")


if __name__ == "__main__":
    main()
