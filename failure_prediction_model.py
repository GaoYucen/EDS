#!/usr/bin/env python3
"""Known-failure chip prediction model.

This script converts the existing bridge+chip risk output into a high-recall
failure warning model. Orange chip labels are treated as confirmed failures in
the current dataset. The default prediction rule uses one global fail_score
threshold calibrated to cover all known failures in the current dataset.
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INPUT_CSV = ROOT / "result" / "bridge_chip_risk_model" / "risk_scores.csv"
OUT_DIR = ROOT / "result" / "failure_prediction"

# Direction is +1 when larger raw value means higher failure risk, -1 when
# smaller raw value means higher failure risk.
SIGNALS = [
    ("global_consistency", "chip_raw_outlier_score", -1.0),
    ("IGE4_30_high", "IGE4_30", 1.0),
    ("IGE5_R30_high", "IGE5_R30", 1.0),
    ("ICES7_1330_high", "ICES7_1330", 1.0),
]

# Optimized sparse score selected from the current dataset under a no-recall-loss
# objective. Keep the baseline model alongside it because only four positives
# are available and the optimized score can overfit this batch.
OPTIMIZED_SIGNALS = [
    ("chip_raw_abs_z_max_low", "chip_raw_abs_z_max", -1.0, "module", 3.0),
    ("chip_raw_outlier_score_low", "chip_raw_outlier_score", -1.0, "module", 1.0),
    ("VTH5_10m_dbc_abs_z_low", "VTH5_10m_dbc_abs_z", -1.0, "module", 3.0),
    ("IGE5_R30_module_abs_z_low", "IGE5_R30_module_abs_z", -1.0, "global", 2.0),
]


def to_float(value: object) -> float:
    try:
        out = float(str(value))
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def to_int(value: object) -> int:
    value_float = to_float(value)
    return int(value_float) if math.isfinite(value_float) else 0


def percentile_scores(
    rows: list[dict[str, object]],
    column: str,
    direction: float,
    scope: str = "module",
) -> dict[int, float]:
    scores: dict[int, float] = {}
    by_module: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = str(row["SN_ID"]) if scope == "module" else "__global__"
        by_module[key].append(row)

    for module_rows in by_module.values():
        valid = [row for row in module_rows if math.isfinite(to_float(row.get(column)))]
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


def rank_within_module(
    rows: list[dict[str, object]],
    score_column: str = "fail_score",
    rank_column: str = "rank_in_module",
) -> None:
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


def compute_global_threshold(rows: list[dict[str, object]]) -> float:
    return compute_threshold(rows, "fail_score")


def compute_threshold(rows: list[dict[str, object]], score_column: str) -> float:
    known_failure_scores = [
        to_float(row[score_column])
        for row in rows
        if to_int(row["label_bad"]) == 1 and math.isfinite(to_float(row[score_column]))
    ]
    if not known_failure_scores:
        return math.inf
    return min(known_failure_scores)


def attach_predictions(rows: list[dict[str, object]]) -> float:
    threshold = compute_global_threshold(rows)
    for row in rows:
        score = to_float(row["fail_score"])
        predicted = 1 if math.isfinite(score) and score >= threshold else 0
        row["predicted_fail"] = predicted
        row["global_threshold"] = threshold
        row["prediction_band"] = "high_risk" if predicted else "low_risk"
        row["prediction_rule"] = "global_threshold"
    return threshold


def attach_optimized_predictions(rows: list[dict[str, object]]) -> float:
    threshold = compute_threshold(rows, "optimized_fail_score")
    for row in rows:
        score = to_float(row["optimized_fail_score"])
        predicted = 1 if math.isfinite(score) and score >= threshold else 0
        row["optimized_predicted_fail"] = predicted
        row["optimized_global_threshold"] = threshold
        row["optimized_prediction_band"] = "high_risk" if predicted else "low_risk"
        row["optimized_prediction_rule"] = "optimized_global_threshold"
    return threshold


def evaluate(
    rows: list[dict[str, object]],
    score_column: str = "fail_score",
    prediction_column: str = "predicted_fail",
    rank_column: str = "rank_in_module",
) -> dict[str, float]:
    tp = fp = tn = fn = 0
    for row in rows:
        actual = to_int(row["label_bad"])
        predicted = to_int(row[prediction_column])
        if actual and predicted:
            tp += 1
        elif not actual and predicted:
            fp += 1
        elif not actual and not predicted:
            tn += 1
        else:
            fn += 1

    positives = [row for row in rows if to_int(row["label_bad"]) == 1]
    ranks = [int(row[rank_column]) for row in positives]
    ordered = sorted(rows, key=lambda row: -to_float(row[score_column]))
    hit = 0
    ap_total = 0.0
    for idx, row in enumerate(ordered, start=1):
        if to_int(row["label_bad"]) == 1:
            hit += 1
            ap_total += hit / idx

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    positive_count = len(positives)
    return {
        "sample_count": float(len(rows)),
        "positive_count": float(positive_count),
        "negative_count": float(len(rows) - positive_count),
        "predicted_positive_count": float(tp + fp),
        "tp": float(tp),
        "fp": float(fp),
        "tn": float(tn),
        "fn": float(fn),
        "precision": precision,
        "recall": recall,
        "accuracy": (tp + tn) / len(rows) if rows else 0.0,
        "mean_positive_rank": sum(ranks) / positive_count if positive_count else 0.0,
        "average_precision": ap_total / positive_count if positive_count else 0.0,
    }


def run_checks(
    rows: list[dict[str, object]],
    metrics: dict[str, float],
    optimized_metrics: dict[str, float],
) -> list[str]:
    errors: list[str] = []
    if len(rows) != 72:
        errors.append(f"Expected 72 chip rows, got {len(rows)}")
    if sum(to_int(row["label_bad"]) for row in rows) != 4:
        errors.append("Expected 4 known failed chips")
    per_module = Counter(str(row["SN_ID"]) for row in rows)
    if len(per_module) != 4 or any(count != 18 for count in per_module.values()):
        errors.append(f"Expected four modules with 18 chips each, got {dict(per_module)}")
    failed_rows = [row for row in rows if to_int(row["label_bad"]) == 1]
    if any(to_int(row["predicted_fail"]) != 1 for row in failed_rows):
        errors.append("Expected all known failed chips to be predicted high_risk by baseline")
    if any(to_int(row["optimized_predicted_fail"]) != 1 for row in failed_rows):
        errors.append("Expected all known failed chips to be predicted high_risk by optimized model")
    thresholds = {to_float(row["global_threshold"]) for row in rows}
    if len(thresholds) != 1:
        errors.append(f"Expected one global threshold, got {thresholds}")
    threshold = next(iter(thresholds), math.nan)
    if not math.isfinite(threshold) or abs(threshold - 0.6323529411764706) > 1e-9:
        errors.append(f"Expected global_threshold=0.6323529411764706, got {threshold}")
    for row in rows:
        expected_predicted = 1 if to_float(row["fail_score"]) >= threshold else 0
        if to_int(row["predicted_fail"]) != expected_predicted:
            errors.append("Expected predicted_fail to be based only on global_threshold")
            break
    optimized_thresholds = {to_float(row["optimized_global_threshold"]) for row in rows}
    if len(optimized_thresholds) != 1:
        errors.append(f"Expected one optimized global threshold, got {optimized_thresholds}")
    optimized_threshold = next(iter(optimized_thresholds), math.nan)
    for row in rows:
        expected_predicted = 1 if to_float(row["optimized_fail_score"]) >= optimized_threshold else 0
        if to_int(row["optimized_predicted_fail"]) != expected_predicted:
            errors.append("Expected optimized_predicted_fail to be based only on optimized_global_threshold")
            break
    expected = {
        "tp": 4.0,
        "fp": 12.0,
        "tn": 56.0,
        "fn": 0.0,
        "predicted_positive_count": 16.0,
        "precision": 0.25,
        "recall": 1.0,
        "accuracy": 5.0 / 6.0,
    }
    for key, value in expected.items():
        if abs(metrics[key] - value) > 1e-9:
            errors.append(f"Expected baseline {key}={value}, got {metrics[key]}")
    optimized_expected = {
        "tp": 4.0,
        "fp": 1.0,
        "tn": 67.0,
        "fn": 0.0,
        "predicted_positive_count": 5.0,
        "precision": 0.8,
        "recall": 1.0,
        "accuracy": 71.0 / 72.0,
    }
    for key, value in optimized_expected.items():
        if abs(optimized_metrics[key] - value) > 1e-9:
            errors.append(f"Expected optimized {key}={value}, got {optimized_metrics[key]}")
    return errors


def load_rows() -> list[dict[str, object]]:
    with INPUT_CSV.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def compute_scores(rows: list[dict[str, object]]) -> None:
    component_maps = {
        name: percentile_scores(rows, column, direction)
        for name, column, direction in SIGNALS
    }
    optimized_component_maps = {
        name: percentile_scores(rows, column, direction, scope)
        for name, column, direction, scope, _weight in OPTIMIZED_SIGNALS
    }
    optimized_weight_total = sum(weight for *_signal, weight in OPTIMIZED_SIGNALS)
    for row in rows:
        parts = []
        for name, _column, _direction in SIGNALS:
            value = component_maps[name][id(row)]
            row[f"{name}_pct"] = value
            parts.append(value)
        row["fail_score"] = sum(parts) / len(parts)

        optimized_total = 0.0
        for name, _column, _direction, scope, weight in OPTIMIZED_SIGNALS:
            value = optimized_component_maps[name][id(row)]
            row[f"{name}_{scope}_pct"] = value
            optimized_total += weight * value
        row["optimized_fail_score"] = optimized_total / optimized_weight_total


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    base = [
        "SN_ID",
        "DBC_LOCATION",
        "芯片位置",
        "bridge_group",
        "label_bad",
        "fail_score",
        "predicted_fail",
        "global_threshold",
        "prediction_band",
        "prediction_rule",
        "optimized_fail_score",
        "optimized_predicted_fail",
        "optimized_global_threshold",
        "optimized_prediction_band",
        "optimized_prediction_rule",
        "rank_in_module",
        "optimized_rank_in_module",
        "risk_score",
        "risk_rank_in_module",
    ]
    component_columns = [f"{name}_pct" for name, _column, _direction in SIGNALS]
    optimized_component_columns = [
        f"{name}_{scope}_pct"
        for name, _column, _direction, scope, _weight in OPTIMIZED_SIGNALS
    ]
    raw_columns = [column for _name, column, _direction in SIGNALS]
    optimized_raw_columns = [
        column
        for _name, column, _direction, _scope, _weight in OPTIMIZED_SIGNALS
        if column not in raw_columns
    ]
    columns = base + component_columns + optimized_component_columns + raw_columns + optimized_raw_columns
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (str(r["SN_ID"]), int(r["rank_in_module"]))):
            writer.writerow({column: row.get(column, "") for column in columns})


def prediction_lines(
    rows: list[dict[str, object]],
    prediction_column: str,
    score_column: str,
) -> list[str]:
    lines = []
    for sn in sorted({str(row["SN_ID"]) for row in rows}):
        module_rows = sorted(
            [
                row
                for row in rows
                if str(row["SN_ID"]) == sn and to_int(row[prediction_column]) == 1
            ],
            key=lambda row: (
                -to_float(row[score_column]),
                str(row["DBC_LOCATION"]),
                int(to_float(row["芯片位置"])),
            ),
        )
        if module_rows:
            labels = ", ".join(
                f"{row['DBC_LOCATION']}{int(to_float(row['芯片位置']))}"
                f"{'*' if to_int(row['label_bad']) == 1 else ''}"
                f"({to_float(row[score_column]):.4f})"
                for row in module_rows
            )
        else:
            labels = "无"
        lines.append(f"- `{sn}`: {labels}")
    return lines


def leave_one_module_lines(rows: list[dict[str, object]], score_column: str) -> list[str]:
    lines = []
    modules = sorted({str(row["SN_ID"]) for row in rows})
    for module in modules:
        train_positive_scores = [
            to_float(row[score_column])
            for row in rows
            if str(row["SN_ID"]) != module
            and to_int(row["label_bad"]) == 1
            and math.isfinite(to_float(row[score_column]))
        ]
        threshold = min(train_positive_scores) if train_positive_scores else math.inf
        module_rows = [row for row in rows if str(row["SN_ID"]) == module]
        tp = sum(
            1
            for row in module_rows
            if to_int(row["label_bad"]) == 1 and to_float(row[score_column]) >= threshold
        )
        fp = sum(
            1
            for row in module_rows
            if to_int(row["label_bad"]) == 0 and to_float(row[score_column]) >= threshold
        )
        fn = sum(
            1
            for row in module_rows
            if to_int(row["label_bad"]) == 1 and to_float(row[score_column]) < threshold
        )
        lines.append(f"| {module} | {threshold:.4f} | {tp} | {fp} | {fn} |")
    return lines


def write_report(
    rows: list[dict[str, object]],
    metrics: dict[str, float],
    optimized_metrics: dict[str, float],
    global_threshold: float,
    optimized_global_threshold: float,
    test_errors: list[str],
    path: Path,
) -> None:
    positives = sorted(
        [row for row in rows if to_int(row["label_bad"]) == 1],
        key=lambda row: str(row["SN_ID"]),
    )
    positive_lines = [
        "| {sn} | {dbc} | {pos} | {bridge} | {base_score:.4f} | {base_pred} | {opt_score:.4f} | {opt_pred} | {rank} |".format(
            sn=row["SN_ID"],
            dbc=row["DBC_LOCATION"],
            pos=int(to_float(row["芯片位置"])),
            bridge=row["bridge_group"],
            base_score=to_float(row["fail_score"]),
            base_pred=to_int(row["predicted_fail"]),
            opt_score=to_float(row["optimized_fail_score"]),
            opt_pred=to_int(row["optimized_predicted_fail"]),
            rank=int(row["optimized_rank_in_module"]),
        )
        for row in positives
    ]

    baseline_lines = prediction_lines(rows, "predicted_fail", "fail_score")
    optimized_lines = prediction_lines(rows, "optimized_predicted_fail", "optimized_fail_score")
    lomo_lines = leave_one_module_lines(rows, "optimized_fail_score")

    test_section = "All checks passed." if not test_errors else "\n".join(f"- {error}" for error in test_errors)

    text = f"""# 优化版芯片失效二分类模型

## 目的

本模型将橙色芯片作为确认失效芯片，基于当前 72 颗芯片数据生成基线 `fail_score` 和优化版 `optimized_fail_score`。基线模型保留为可回退参考；优化版在当前数据内以“不降低已知失效召回”为约束减少误报。

## 输入特征和预测规则

基线分数使用 4 个信号的模块内百分位平均：

```text
fail_score = mean(global_consistency_pct,
                  IGE4_30_high_pct,
                  IGE5_R30_high_pct,
                  ICES7_1330_high_pct)
```

优化版分数使用当前数据内筛选出的稀疏加权信号：

```text
optimized_fail_score = (3*a + 1*b + 3*c + 2*d) / 9
a = chip_raw_abs_z_max_low_module_pct
b = chip_raw_outlier_score_low_module_pct
c = VTH5_10m_dbc_abs_z_low_module_pct
d = IGE5_R30_module_abs_z_low_global_pct
```

两套模型都采用全局阈值判定，不假设每个模块一定有失效芯片：

```text
predicted_fail = 1 if fail_score >= global_threshold else 0
optimized_predicted_fail = 1 if optimized_fail_score >= optimized_global_threshold else 0
```

当前基线阈值为 `{global_threshold:.4f}`，优化版阈值为 `{optimized_global_threshold:.4f}`。模块内排名仅作为解释字段保留，不参与最终二分类判定。

## 数据检查

- 样本数：{int(metrics['sample_count'])}
- 确认失效芯片数：{int(metrics['positive_count'])}
- 当前按非失效评估芯片数：{int(metrics['negative_count'])}
- 模块数：4
- 每个模块芯片数：18

## 基线 vs 优化版评估

| 指标 | 基线模型 | 优化版 |
|---|---:|---:|
| TP | {int(metrics['tp'])} | {int(optimized_metrics['tp'])} |
| FP | {int(metrics['fp'])} | {int(optimized_metrics['fp'])} |
| TN | {int(metrics['tn'])} | {int(optimized_metrics['tn'])} |
| FN | {int(metrics['fn'])} | {int(optimized_metrics['fn'])} |
| Predicted failure count | {int(metrics['predicted_positive_count'])} | {int(optimized_metrics['predicted_positive_count'])} |
| Threshold | {global_threshold:.4f} | {optimized_global_threshold:.4f} |
| Recall | {metrics['recall']:.3f} | {optimized_metrics['recall']:.3f} |
| Precision | {metrics['precision']:.3f} | {optimized_metrics['precision']:.3f} |
| Accuracy | {metrics['accuracy']:.3f} | {optimized_metrics['accuracy']:.3f} |
| Average precision | {metrics['average_precision']:.3f} | {optimized_metrics['average_precision']:.3f} |

## 排序辅助指标

| 指标 | 数值 |
|---|---:|
| 基线已知失效芯片平均模块内排名 / 18 | {metrics['mean_positive_rank']:.2f} |
| 优化版已知失效芯片平均模块内排名 / 18 | {optimized_metrics['mean_positive_rank']:.2f} |

## 已知失效芯片明细

| SN_ID | DBC | 芯片位置 | 桥臂组 | 基线分数 | 基线判定 | 优化分数 | 优化判定 | 优化排名 |
|---|---:|---:|---|---:|---:|---:|---:|---:|
{chr(10).join(positive_lines)}

## 基线判失效清单

以下为每个模块中 `fail_score >= global_threshold` 的芯片。带 `*` 的芯片为确认失效芯片。

{chr(10).join(baseline_lines)}

## 优化版判失效清单

以下为每个模块中 `optimized_fail_score >= optimized_global_threshold` 的芯片。带 `*` 的芯片为确认失效芯片。

{chr(10).join(optimized_lines)}

## Leave-One-Module 风险检查

该检查每次留出一个模块，用其他模块的确认失效芯片确定优化版阈值，再评估留出模块。它用于提示小样本泛化风险。

| 留出模块 | 训练阈值 | TP | FP | FN |
|---|---:|---:|---:|---:|
{chr(10).join(lomo_lines)}

## 重要限定

优化版是基于当前 4 个已知失效样本筛选出的回顾性稀疏加权规则。它在当前数据内减少了误报，但 leave-one-module 检查显示仍可能漏掉个别已知失效芯片，因此不能声明已经完成跨批次验证的生产级精准判废模型。当前可以表述为“现有数据内增强版失效预警二分类”。

## Sanity Tests

{test_section}
"""
    path.write_text(text, encoding="utf-8")


def write_svg(rows: list[dict[str, object]], path: Path) -> None:
    modules = sorted({str(row["SN_ID"]) for row in rows})
    width = 1120
    module_h = 165
    height = 70 + module_h * len(modules)
    left = 245
    bar_w = 660
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="30" y="34" font-family="Arial, sans-serif" font-size="22" font-weight="700">Optimized Failure Classifier</text>',
        '<text x="30" y="55" font-family="Arial, sans-serif" font-size="12" fill="#555">Orange = confirmed failed chip; green = optimized predicted fail; red line = optimized global threshold; bar length = optimized_fail_score.</text>',
    ]
    optimized_global_threshold = to_float(rows[0]["optimized_global_threshold"]) if rows else 0.0
    threshold_x = left + optimized_global_threshold * bar_w
    for module_idx, sn in enumerate(modules):
        y0 = 88 + module_idx * module_h
        module_rows = sorted(
            [row for row in rows if str(row["SN_ID"]) == sn],
            key=lambda row: int(row["optimized_rank_in_module"]),
        )
        lines.append(f'<text x="30" y="{y0}" font-family="Arial, sans-serif" font-size="15" font-weight="700">{sn[-8:]}</text>')
        lines.append(f'<line x1="{threshold_x:.1f}" y1="{y0 + 8}" x2="{threshold_x:.1f}" y2="{y0 + 142}" stroke="#c43c39" stroke-width="1.2" stroke-dasharray="4 3"/>')
        for idx, row in enumerate(module_rows):
            yy = y0 + 20 + idx * 7
            score = to_float(row["optimized_fail_score"])
            is_failed = to_int(row["label_bad"]) == 1
            is_predicted = to_int(row["optimized_predicted_fail"]) == 1
            color = "#d55e00" if is_failed else ("#5f8f3f" if is_predicted else "#4c78a8")
            label = f"{int(row['optimized_rank_in_module']):>2}. {row['DBC_LOCATION']}{int(to_float(row['芯片位置']))} {row['bridge_group']}"
            lines.append(f'<text x="56" y="{yy + 4}" font-family="Arial, sans-serif" font-size="9" fill="#333">{label}</text>')
            lines.append(f'<rect x="{left}" y="{yy - 4}" width="{max(1, score * bar_w):.1f}" height="10" rx="1" fill="{color}" opacity="0.82"/>')
            lines.append(f'<text x="{left + bar_w + 10}" y="{yy + 4}" font-family="Arial, sans-serif" font-size="9" fill="#333">{score:.3f}</text>')
        lines.append(f'<line x1="30" y1="{y0 + 146}" x2="{width - 30}" y2="{y0 + 146}" stroke="#e6e6e6"/>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    compute_scores(rows)
    rank_within_module(rows)
    rank_within_module(rows, "optimized_fail_score", "optimized_rank_in_module")
    global_threshold = attach_predictions(rows)
    optimized_global_threshold = attach_optimized_predictions(rows)
    metrics = evaluate(rows)
    optimized_metrics = evaluate(
        rows,
        "optimized_fail_score",
        "optimized_predicted_fail",
        "optimized_rank_in_module",
    )
    test_errors = run_checks(rows, metrics, optimized_metrics)

    write_csv(rows, OUT_DIR / "failure_predictions.csv")
    write_report(
        rows,
        metrics,
        optimized_metrics,
        global_threshold,
        optimized_global_threshold,
        test_errors,
        OUT_DIR / "result.md",
    )
    write_svg(rows, OUT_DIR / "prediction_by_module.svg")

    if test_errors:
        raise SystemExit("\n".join(test_errors))
    print(f"Wrote {OUT_DIR / 'failure_predictions.csv'}")
    print(f"Wrote {OUT_DIR / 'result.md'}")
    print(f"Wrote {OUT_DIR / 'prediction_by_module.svg'}")
    print(
        "metrics:",
        {
            key: round(value, 4)
            for key, value in metrics.items()
        },
    )
    print(
        "optimized_metrics:",
        {
            key: round(value, 4)
            for key, value in optimized_metrics.items()
        },
    )


if __name__ == "__main__":
    main()
