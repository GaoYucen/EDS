"""
Generate lightweight SVG visualizations from the current CSV outputs.

The script uses only the Python standard library. It is intended for quick
inspection while the project is still in a weak-label, low-sample stage.
"""

from __future__ import annotations

import csv
import html
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"
VIS_DIR = OUTPUT_DIR / "visualizations"


def read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fnum(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def write_svg(path: Path, width: int, height: int, body: list[str]) -> None:
    content = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Arial,'Microsoft YaHei',sans-serif;fill:#1f2933}",
        ".title{font-size:22px;font-weight:700}",
        ".subtitle{font-size:12px;fill:#52606d}",
        ".axis{stroke:#cbd2d9;stroke-width:1}",
        ".label{font-size:12px}",
        ".small{font-size:10px;fill:#52606d}",
        "</style>",
        *body,
        "</svg>",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def short_module(module_id: str) -> str:
    return module_id[-8:] if len(module_id) > 8 else module_id


def draw_label_distribution(chip_rows: list[dict]) -> None:
    counts = Counter(row["label_role"] for row in chip_rows)
    items = [
        ("marked_damaged", counts.get("marked_damaged", 0), "#d64545"),
        ("candidate_negative", counts.get("candidate_negative", 0), "#2f80ed"),
    ]
    width, height = 760, 420
    left, top, bar_w, chart_h = 120, 90, 190, 240
    max_count = max(count for _, count, _ in items) or 1
    body = [
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="38" class="title">Chip Weak Label Distribution</text>',
        '<text x="32" y="60" class="subtitle">Uncolored chips are candidate negatives, not confirmed normal samples.</text>',
        f'<line x1="{left}" y1="{top + chart_h}" x2="{width - 60}" y2="{top + chart_h}" class="axis"/>',
    ]
    for idx, (name, count, color) in enumerate(items):
        x = left + idx * 260
        h = int((count / max_count) * chart_h)
        y = top + chart_h - h
        body.extend(
            [
                f'<rect x="{x}" y="{y}" width="{bar_w}" height="{h}" rx="4" fill="{color}"/>',
                f'<text x="{x + bar_w / 2}" y="{y - 10}" text-anchor="middle" class="label">{count}</text>',
                f'<text x="{x + bar_w / 2}" y="{top + chart_h + 24}" text-anchor="middle" class="label">{esc(name)}</text>',
            ]
        )
    write_svg(VIS_DIR / "01_chip_label_distribution.svg", width, height, body)


def draw_module_risk(module_rows: list[dict]) -> None:
    rows = sorted(module_rows, key=lambda row: fnum(row.get("process_anomaly_score")), reverse=True)
    width = 1080
    row_h = 56
    height = 110 + row_h * len(rows)
    left, top, bar_max_w = 190, 86, 690
    max_score = max((fnum(row.get("process_anomaly_score")) for row in rows), default=1.0) or 1.0
    body = [
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="38" class="title">Module Process Anomaly Ranking</text>',
        '<text x="32" y="60" class="subtitle">Scores describe process outlier strength, not validated failure probability.</text>',
    ]
    for idx, row in enumerate(rows):
        y = top + idx * row_h
        score = fnum(row.get("process_anomaly_score"))
        bar_w = int(score / max_score * bar_max_w)
        label = short_module(row.get("module_id", ""))
        body.extend(
            [
                f'<text x="32" y="{y + 24}" class="label">{esc(label)}</text>',
                f'<rect x="{left}" y="{y}" width="{bar_max_w}" height="30" rx="4" fill="#eef2f7"/>',
                f'<rect x="{left}" y="{y}" width="{bar_w}" height="30" rx="4" fill="#7b61ff"/>',
                f'<text x="{left + bar_max_w + 18}" y="{y + 21}" class="label">{score:.1f}</text>',
                f'<text x="{left}" y="{y + 46}" class="small">{esc(row.get("damage_source", ""))}</text>',
            ]
        )
    write_svg(VIS_DIR / "02_module_risk_ranking.svg", width, height, body)


def draw_chip_map(chip_rows: list[dict]) -> None:
    by_module: dict[str, list[dict]] = defaultdict(list)
    for row in chip_rows:
        by_module[row["module_id"]].append(row)

    modules = sorted(by_module)
    cell, gap = 34, 6
    width = 1060
    height = 110 + len(modules) * 72
    left, top = 210, 82
    max_score = max((fnum(row.get("process_anomaly_score")) for row in chip_rows), default=1.0) or 1.0
    body = [
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="38" class="title">Chip Position Map</text>',
        '<text x="32" y="60" class="subtitle">Red cells are colored rows. Blue intensity indicates chip-level anomaly score among all chips.</text>',
    ]
    header_cells = []
    for loc in ["U", "V", "W"]:
        for pos in range(1, 7):
            header_cells.append(f"{loc}{pos}")
    for idx, label in enumerate(header_cells):
        x = left + idx * (cell + gap)
        body.append(f'<text x="{x + cell / 2}" y="{top - 12}" text-anchor="middle" class="small">{label}</text>')

    for row_idx, module_id in enumerate(modules):
        y = top + row_idx * 72
        body.append(f'<text x="32" y="{y + 22}" class="label">{esc(short_module(module_id))}</text>')
        ordered = {
            (row.get("dbc_location", ""), row.get("chip_position", "")): row
            for row in by_module[module_id]
        }
        col_idx = 0
        for loc in ["U", "V", "W"]:
            for pos in range(1, 7):
                row = ordered.get((loc, str(pos)))
                x = left + col_idx * (cell + gap)
                if row:
                    is_marked = row.get("label_role") == "marked_damaged"
                    score_ratio = fnum(row.get("process_anomaly_score")) / max_score
                    blue = 238 - int(85 * score_ratio)
                    fill = "#d64545" if is_marked else f"rgb({blue},{blue + 8},247)"
                    stroke = "#922b2b" if is_marked else "#9fb3c8"
                    title = f"{short_module(module_id)} {loc}{pos}: {row.get('label_role')} score={fnum(row.get('process_anomaly_score')):.1f}"
                else:
                    fill, stroke, title = "#f5f7fa", "#d9e2ec", f"{short_module(module_id)} {loc}{pos}: missing"
                body.extend(
                    [
                        f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="4" fill="{fill}" stroke="{stroke}"/>',
                        f'<title>{esc(title)}</title>',
                        f'<text x="{x + cell / 2}" y="{y + 22}" text-anchor="middle" class="small">{pos}</text>',
                    ]
                )
                col_idx += 1
    body.extend(
        [
            f'<rect x="{left}" y="{height - 44}" width="18" height="18" rx="3" fill="#d64545"/>',
            f'<text x="{left + 26}" y="{height - 30}" class="small">marked_damaged</text>',
            f'<rect x="{left + 170}" y="{height - 44}" width="18" height="18" rx="3" fill="#b8c7e8" stroke="#9fb3c8"/>',
            f'<text x="{left + 196}" y="{height - 30}" class="small">candidate_negative</text>',
        ]
    )
    write_svg(VIS_DIR / "03_chip_position_map.svg", width, height, body)


def draw_chip_risk(chip_rows: list[dict]) -> None:
    rows = sorted(chip_rows, key=lambda row: fnum(row.get("process_anomaly_score")), reverse=True)[:24]
    width = 1120
    row_h = 34
    height = 100 + row_h * len(rows)
    left, top, bar_max_w = 250, 78, 660
    max_score = max((fnum(row.get("process_anomaly_score")) for row in rows), default=1.0) or 1.0
    body = [
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="38" class="title">Top Chip Anomaly Scores</text>',
        '<text x="32" y="60" class="subtitle">Top 24 chips by robust-Z anomaly score. Red marks colored damaged rows.</text>',
    ]
    for idx, row in enumerate(rows):
        y = top + idx * row_h
        score = fnum(row.get("process_anomaly_score"))
        bar_w = int(score / max_score * bar_max_w)
        color = "#d64545" if row.get("label_role") == "marked_damaged" else "#2f80ed"
        label = f"{short_module(row.get('module_id', ''))} {row.get('dbc_location')}{row.get('chip_position')}"
        body.extend(
            [
                f'<text x="32" y="{y + 20}" class="label">{esc(label)}</text>',
                f'<text x="150" y="{y + 20}" class="small">{esc(row.get("label_role", ""))}</text>',
                f'<rect x="{left}" y="{y + 3}" width="{bar_max_w}" height="20" rx="4" fill="#eef2f7"/>',
                f'<rect x="{left}" y="{y + 3}" width="{bar_w}" height="20" rx="4" fill="{color}"/>',
                f'<text x="{left + bar_max_w + 18}" y="{y + 20}" class="label">{score:.1f}</text>',
            ]
        )
    write_svg(VIS_DIR / "04_chip_risk_ranking.svg", width, height, body)


def draw_weak_model_predictions(prediction_rows: list[dict]) -> None:
    rows = sorted(prediction_rows, key=lambda row: fnum(row.get("weak_model_probability")), reverse=True)[:24]
    width = 1120
    row_h = 34
    height = 100 + row_h * len(rows)
    left, top, bar_max_w = 250, 78, 660
    max_score = max((fnum(row.get("weak_model_probability")) for row in rows), default=1.0) or 1.0
    body = [
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="38" class="title">Weak Model Prediction Ranking</text>',
        '<text x="32" y="60" class="subtitle">Grouped-CV probabilities from the temporary chip-independence weak-label model.</text>',
    ]
    for idx, row in enumerate(rows):
        y = top + idx * row_h
        score = fnum(row.get("weak_model_probability"))
        bar_w = int(score / max_score * bar_max_w) if max_score else 0
        color = "#d64545" if row.get("label_role") == "marked_damaged" else "#27a376"
        label = f"{short_module(row.get('module_id', ''))} {row.get('dbc_location')}{row.get('chip_position')}"
        body.extend(
            [
                f'<text x="32" y="{y + 20}" class="label">{esc(label)}</text>',
                f'<text x="150" y="{y + 20}" class="small">{esc(row.get("label_role", ""))}</text>',
                f'<rect x="{left}" y="{y + 3}" width="{bar_max_w}" height="20" rx="4" fill="#eef2f7"/>',
                f'<rect x="{left}" y="{y + 3}" width="{bar_w}" height="20" rx="4" fill="{color}"/>',
                f'<text x="{left + bar_max_w + 18}" y="{y + 20}" class="label">{score:.3f}</text>',
            ]
        )
    write_svg(VIS_DIR / "05_weak_model_prediction_ranking.svg", width, height, body)


def main() -> None:
    VIS_DIR.mkdir(parents=True, exist_ok=True)
    chip_rows = read_csv(OUTPUT_DIR / "chip_level_modeling_dataset.csv")
    module_rows = read_csv(OUTPUT_DIR / "risk_ranking.csv")

    draw_label_distribution(chip_rows)
    draw_module_risk(module_rows)
    draw_chip_map(chip_rows)
    draw_chip_risk(chip_rows)
    prediction_path = OUTPUT_DIR / "weak_chip_predictions.csv"
    if prediction_path.exists():
        draw_weak_model_predictions(read_csv(prediction_path))

    print(f"Wrote visualizations to: {VIS_DIR}")


if __name__ == "__main__":
    main()
