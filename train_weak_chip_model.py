"""
Train a weak-label chip-level baseline model.

Assumptions for this temporary baseline:
  - Chips are treated as independent samples.
  - Colored chip rows are positive labels.
  - Uncolored chip rows are candidate negatives, not confirmed normal samples.

The implementation uses only the Python standard library. It writes:
  - outputs/weak_chip_predictions.csv
  - outputs/weak_chip_model.json
  - outputs/weak_chip_model_metrics.json
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"
DATASET_PATH = OUTPUT_DIR / "chip_level_modeling_dataset.csv"


META_COLUMNS = {
    "sample_id",
    "module_id",
    "group_module_id",
    "source_row",
    "dbc_location",
    "chip_position",
    "label_damaged",
    "label_role",
    "label_note",
    "top_anomaly_features",
}


def read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fnum(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def sigmoid(value: float) -> float:
    value = max(min(value, 35.0), -35.0)
    return 1.0 / (1.0 + math.exp(-value))


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def pstdev(values: Iterable[float]) -> float:
    values = list(values)
    if len(values) < 2:
        return 1.0
    center = mean(values)
    var = sum((value - center) ** 2 for value in values) / len(values)
    std = math.sqrt(var)
    return std if std > 1e-12 else 1.0


def discover_numeric_features(rows: list[dict]) -> list[str]:
    features = []
    for key in rows[0].keys():
        if key in META_COLUMNS:
            continue
        if key.startswith("process_anomaly_"):
            continue
        if key == "process_anomaly_score":
            continue
        values = [fnum(row.get(key)) for row in rows]
        if any(value is not None for value in values):
            features.append(key)
    return features


def build_feature_spec(rows: list[dict]) -> dict:
    numeric_features = discover_numeric_features(rows)
    numeric_stats = {}
    for feature in numeric_features:
        values = [fnum(row.get(feature)) for row in rows]
        clean_values = [value for value in values if value is not None]
        numeric_stats[feature] = {
            "mean": mean(clean_values),
            "std": pstdev(clean_values),
        }

    locations = sorted({row.get("dbc_location", "") for row in rows if row.get("dbc_location", "")})
    positions = sorted({row.get("chip_position", "") for row in rows if row.get("chip_position", "")}, key=lambda x: fnum(x) or 0)

    feature_names = []
    feature_names.extend(numeric_features)
    feature_names.extend(f"dbc_location={value}" for value in locations)
    feature_names.extend(f"chip_position={value}" for value in positions)

    return {
        "numeric_features": numeric_features,
        "numeric_stats": numeric_stats,
        "dbc_locations": locations,
        "chip_positions": positions,
        "feature_names": feature_names,
    }


def transform_row(row: dict, spec: dict) -> list[float]:
    values = []
    for feature in spec["numeric_features"]:
        number = fnum(row.get(feature))
        stats = spec["numeric_stats"][feature]
        if number is None:
            values.append(0.0)
        else:
            values.append((number - stats["mean"]) / stats["std"])

    location = row.get("dbc_location", "")
    values.extend(1.0 if location == item else 0.0 for item in spec["dbc_locations"])

    position = row.get("chip_position", "")
    values.extend(1.0 if position == item else 0.0 for item in spec["chip_positions"])
    return values


def transform_rows(rows: list[dict], spec: dict) -> tuple[list[list[float]], list[int]]:
    x_values = [transform_row(row, spec) for row in rows]
    y_values = [int(fnum(row.get("label_damaged")) or 0) for row in rows]
    return x_values, y_values


def class_weights(y_values: list[int]) -> list[float]:
    total = len(y_values)
    positives = sum(y_values)
    negatives = total - positives
    pos_weight = total / (2.0 * positives) if positives else 1.0
    neg_weight = total / (2.0 * negatives) if negatives else 1.0
    return [pos_weight if y else neg_weight for y in y_values]


def train_logistic_regression(
    x_values: list[list[float]],
    y_values: list[int],
    *,
    epochs: int = 3000,
    learning_rate: float = 0.04,
    l2: float = 0.02,
) -> dict:
    if not x_values:
        raise ValueError("No training rows.")
    feature_count = len(x_values[0])
    weights = [0.0] * feature_count
    positives = sum(y_values)
    negatives = len(y_values) - positives
    if positives and negatives:
        bias = math.log(positives / negatives)
    else:
        bias = 0.0

    sample_weights = class_weights(y_values)
    total_weight = sum(sample_weights)

    for epoch in range(epochs):
        grad_w = [0.0] * feature_count
        grad_b = 0.0
        lr = learning_rate / (1.0 + 0.0008 * epoch)

        for x_row, y, sample_weight in zip(x_values, y_values, sample_weights):
            z = bias + sum(weight * value for weight, value in zip(weights, x_row))
            pred = sigmoid(z)
            error = (pred - y) * sample_weight
            grad_b += error
            for idx, value in enumerate(x_row):
                grad_w[idx] += error * value

        for idx in range(feature_count):
            grad = grad_w[idx] / total_weight + l2 * weights[idx]
            weights[idx] -= lr * grad
        bias -= lr * grad_b / total_weight

    return {
        "weights": weights,
        "bias": bias,
        "epochs": epochs,
        "learning_rate": learning_rate,
        "l2": l2,
    }


def predict_probabilities(x_values: list[list[float]], model: dict) -> list[float]:
    weights = model["weights"]
    bias = model["bias"]
    return [sigmoid(bias + sum(weight * value for weight, value in zip(weights, x_row))) for x_row in x_values]


def auc_roc(y_true: list[int], scores: list[float]) -> float | None:
    positives = [score for y, score in zip(y_true, scores) if y == 1]
    negatives = [score for y, score in zip(y_true, scores) if y == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    total = len(positives) * len(negatives)
    for pos_score in positives:
        for neg_score in negatives:
            if pos_score > neg_score:
                wins += 1.0
            elif pos_score == neg_score:
                wins += 0.5
    return wins / total


def average_precision(y_true: list[int], scores: list[float]) -> float | None:
    positive_count = sum(y_true)
    if positive_count == 0:
        return None
    ranked = sorted(zip(scores, y_true), reverse=True)
    hits = 0
    precision_sum = 0.0
    for rank, (_, y) in enumerate(ranked, start=1):
        if y == 1:
            hits += 1
            precision_sum += hits / rank
    return precision_sum / positive_count


def threshold_metrics(y_true: list[int], scores: list[float], threshold: float = 0.5) -> dict:
    preds = [1 if score >= threshold else 0 for score in scores]
    tp = sum(1 for y, pred in zip(y_true, preds) if y == 1 and pred == 1)
    fp = sum(1 for y, pred in zip(y_true, preds) if y == 0 and pred == 1)
    tn = sum(1 for y, pred in zip(y_true, preds) if y == 0 and pred == 0)
    fn = sum(1 for y, pred in zip(y_true, preds) if y == 1 and pred == 0)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "threshold": threshold,
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
    }


def module_positive_ranks(rows: list[dict], scores: list[float]) -> list[dict]:
    by_module: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for row, score in zip(rows, scores):
        by_module[row["group_module_id"]].append((row, score))

    rank_rows = []
    for module_id, items in sorted(by_module.items()):
        ranked = sorted(items, key=lambda item: item[1], reverse=True)
        positives = [
            (idx, row, score)
            for idx, (row, score) in enumerate(ranked, start=1)
            if int(fnum(row.get("label_damaged")) or 0) == 1
        ]
        if not positives:
            continue
        rank, row, score = positives[0]
        rank_rows.append(
            {
                "module_id": module_id,
                "positive_sample_id": row["sample_id"],
                "positive_location": f"{row.get('dbc_location', '')}{row.get('chip_position', '')}",
                "positive_rank_in_module": rank,
                "module_chip_count": len(ranked),
                "positive_score": score,
            }
        )
    return rank_rows


def leave_one_module_out(rows: list[dict]) -> tuple[list[dict], dict]:
    groups = sorted({row["group_module_id"] for row in rows})
    predictions = []

    for group in groups:
        train_rows = [row for row in rows if row["group_module_id"] != group]
        test_rows = [row for row in rows if row["group_module_id"] == group]
        spec = build_feature_spec(train_rows)
        x_train, y_train = transform_rows(train_rows, spec)
        model = train_logistic_regression(x_train, y_train)
        x_test, _ = transform_rows(test_rows, spec)
        scores = predict_probabilities(x_test, model)
        for row, score in zip(test_rows, scores):
            predictions.append(
                {
                    **row,
                    "weak_model_probability": score,
                    "validation_fold": f"leave_module_out:{group}",
                    "prediction_source": "grouped_cv",
                }
            )

    y_true = [int(fnum(row["label_damaged"]) or 0) for row in predictions]
    scores = [fnum(row["weak_model_probability"]) or 0.0 for row in predictions]
    rank_rows = module_positive_ranks(predictions, scores)

    metrics = {
        "validation": "leave_one_group_module_out",
        "sample_count": len(predictions),
        "positive_count": sum(y_true),
        "candidate_negative_count": len(y_true) - sum(y_true),
        "roc_auc": auc_roc(y_true, scores),
        "average_precision": average_precision(y_true, scores),
        "threshold_0_5": threshold_metrics(y_true, scores, 0.5),
        "positive_rank_by_module": rank_rows,
        "mean_positive_rank_in_module": mean(row["positive_rank_in_module"] for row in rank_rows),
        "top1_module_recall": mean(1.0 if row["positive_rank_in_module"] == 1 else 0.0 for row in rank_rows),
    }
    return predictions, metrics


def train_final_model(rows: list[dict]) -> tuple[dict, dict]:
    spec = build_feature_spec(rows)
    x_values, y_values = transform_rows(rows, spec)
    model = train_logistic_regression(x_values, y_values)
    scores = predict_probabilities(x_values, model)
    weights = model["weights"]
    coefficients = sorted(
        [
            {
                "feature": feature,
                "coefficient": coefficient,
                "absolute_coefficient": abs(coefficient),
            }
            for feature, coefficient in zip(spec["feature_names"], weights)
        ],
        key=lambda item: item["absolute_coefficient"],
        reverse=True,
    )
    model_artifact = {
        "model_type": "standard_library_logistic_regression",
        "label_definition": {
            "positive": "colored chip row / marked_damaged",
            "negative": "uncolored chip row / candidate_negative",
            "warning": "Candidate negatives are weak labels, not confirmed normal samples.",
        },
        "assumption": "Temporary chip-independence assumption.",
        "feature_spec": spec,
        "bias": model["bias"],
        "weights": weights,
        "top_coefficients": coefficients[:20],
        "training_params": {
            "epochs": model["epochs"],
            "learning_rate": model["learning_rate"],
            "l2": model["l2"],
        },
    }
    final_training_metrics = {
        "training_sample_count": len(rows),
        "training_positive_count": sum(y_values),
        "training_candidate_negative_count": len(y_values) - sum(y_values),
        "training_roc_auc": auc_roc(y_values, scores),
        "training_average_precision": average_precision(y_values, scores),
        "training_threshold_0_5": threshold_metrics(y_values, scores, 0.5),
        "training_positive_rank_by_module": module_positive_ranks(rows, scores),
    }
    return model_artifact, final_training_metrics


def main() -> None:
    rows = read_csv(DATASET_PATH)
    if not rows:
        raise ValueError(f"No rows found in {DATASET_PATH}")

    predictions, cv_metrics = leave_one_module_out(rows)
    predictions.sort(key=lambda row: fnum(row.get("weak_model_probability")) or 0.0, reverse=True)

    model_artifact, training_metrics = train_final_model(rows)
    metrics = {
        "model_status": "temporary_weak_label_baseline",
        "important_warning": (
            "This is not a validated production failure model. It assumes chip independence "
            "and treats uncolored chips as candidate negatives."
        ),
        "cross_validation": cv_metrics,
        "final_training": training_metrics,
    }

    prediction_columns = [
        "sample_id",
        "module_id",
        "group_module_id",
        "source_row",
        "dbc_location",
        "chip_position",
        "label_damaged",
        "label_role",
        "weak_model_probability",
        "validation_fold",
        "prediction_source",
        "process_anomaly_score",
        "label_note",
    ]
    write_csv(OUTPUT_DIR / "weak_chip_predictions.csv", predictions, prediction_columns)

    with (OUTPUT_DIR / "weak_chip_model.json").open("w", encoding="utf-8-sig") as handle:
        json.dump(model_artifact, handle, ensure_ascii=False, indent=2)
    with (OUTPUT_DIR / "weak_chip_model_metrics.json").open("w", encoding="utf-8-sig") as handle:
        json.dump(metrics, handle, ensure_ascii=False, indent=2)

    print(f"Rows: {len(rows)}")
    print(f"Cross-validation ROC-AUC: {cv_metrics['roc_auc']}")
    print(f"Cross-validation average precision: {cv_metrics['average_precision']}")
    print(f"Mean positive rank in module: {cv_metrics['mean_positive_rank_in_module']}")
    print(f"Wrote: {OUTPUT_DIR / 'weak_chip_predictions.csv'}")
    print(f"Wrote: {OUTPUT_DIR / 'weak_chip_model.json'}")
    print(f"Wrote: {OUTPUT_DIR / 'weak_chip_model_metrics.json'}")


if __name__ == "__main__":
    main()
