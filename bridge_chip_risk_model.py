#!/usr/bin/env python3
"""Bridge-level anomaly + chip-level ranking model.

This script intentionally uses only the Python standard library so it can run in
the current workspace without installing Excel/data-science packages.
"""

from __future__ import annotations

import csv
import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
MODULE_XLSX = ROOT / "模块层级制程数据【加颜色标注】.xlsx"
CHIP_XLSX = ROOT / "芯片层级制程数据【加颜色标注】.xlsx"
OUT_DIR = ROOT / "result" / "bridge_chip_risk_model"

NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
RELNS = {"pr": "http://schemas.openxmlformats.org/package/2006/relationships"}

CHIP_METRICS = [
    "IGE4_30",
    "IGE5_R30",
    "VCE5_200",
    "BVCE3_1m",
    "ICES7_1330",
    "RG",
    "VTH5_10m",
]
BRIDGE_GROUPS = ["U_H", "U_L", "V_H", "V_L", "W_H", "W_L"]


def col_to_index(ref: str) -> tuple[int, int]:
    match = re.match(r"([A-Z]+)([0-9]+)", ref)
    if not match:
        raise ValueError(f"Invalid cell reference: {ref}")
    letters, row = match.groups()
    idx = 0
    for ch in letters:
        idx = idx * 26 + ord(ch) - 64
    return idx, int(row)


def shared_text(si: ET.Element) -> str:
    return "".join(t.text or "" for t in si.iter(f"{{{NS['a']}}}t"))


def to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        val = float(str(value).strip())
    except ValueError:
        return None
    if math.isfinite(val):
        return val
    return None


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.median(values)


def percentile(values: list[float], q: float) -> float:
    clean = sorted(v for v in values if math.isfinite(v))
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return clean[lo]
    return clean[lo] * (hi - pos) + clean[hi] * (pos - lo)


def robust_z(value: float, reference: list[float]) -> float:
    clean = [v for v in reference if math.isfinite(v)]
    if not clean:
        return 0.0
    med = statistics.median(clean)
    deviations = [abs(v - med) for v in clean]
    mad = statistics.median(deviations)
    if mad > 1e-12:
        return (value - med) / (1.4826 * mad)
    if len(clean) >= 2:
        sd = statistics.pstdev(clean)
        if sd > 1e-12:
            return (value - med) / sd
    return 0.0 if abs(value - med) <= 1e-12 else math.copysign(3.0, value - med)


def score_from_abs_z(abs_z: float) -> float:
    abs_z = max(0.0, min(abs_z, 12.0))
    return abs_z / (abs_z + 3.0)


def safe_mean(values: list[float]) -> float:
    clean = [v for v in values if math.isfinite(v)]
    return sum(clean) / len(clean) if clean else 0.0


class SimpleXlsx:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.zip = ZipFile(path)
        self.shared_strings = self._load_shared_strings()
        self.fills, self.xf_fills = self._load_styles()
        self.sheet_paths = self._load_sheet_paths()

    def _load_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.zip.namelist():
            return []
        root = ET.fromstring(self.zip.read("xl/sharedStrings.xml"))
        return [shared_text(si) for si in root.findall("a:si", NS)]

    def _load_styles(self) -> tuple[list[tuple[str | None, str | None]], list[int]]:
        root = ET.fromstring(self.zip.read("xl/styles.xml"))
        fills: list[tuple[str | None, str | None]] = []
        fills_el = root.find("a:fills", NS)
        if fills_el is not None:
            for fill in fills_el.findall("a:fill", NS):
                pattern = fill.find("a:patternFill", NS)
                pattern_type = None
                rgb = None
                if pattern is not None:
                    pattern_type = pattern.attrib.get("patternType")
                    fg = pattern.find("a:fgColor", NS)
                    bg = pattern.find("a:bgColor", NS)
                    if fg is not None:
                        rgb = fg.attrib.get("rgb")
                    elif bg is not None:
                        rgb = bg.attrib.get("rgb")
                fills.append((pattern_type, rgb))

        xf_fills: list[int] = []
        xfs_el = root.find("a:cellXfs", NS)
        if xfs_el is not None:
            for xf in xfs_el.findall("a:xf", NS):
                xf_fills.append(int(xf.attrib.get("fillId", "0")))
        return fills, xf_fills

    def _load_sheet_paths(self) -> dict[str, str]:
        workbook = ET.fromstring(self.zip.read("xl/workbook.xml"))
        rels = ET.fromstring(self.zip.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall("pr:Relationship", RELNS)
        }
        paths: dict[str, str] = {}
        sheets = workbook.find("a:sheets", NS)
        if sheets is None:
            return paths
        for sheet in sheets.findall("a:sheet", NS):
            rid = sheet.attrib[f"{{{NS['r']}}}id"]
            target = rid_to_target[rid]
            paths[sheet.attrib["name"]] = (
                "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
            )
        return paths

    def read_sheet(self, sheet_name: str) -> tuple[dict[int, dict[int, object]], dict[int, dict[int, str]], int, int]:
        root = ET.fromstring(self.zip.read(self.sheet_paths[sheet_name]))
        values: dict[int, dict[int, object]] = defaultdict(dict)
        colors: dict[int, dict[int, str]] = defaultdict(dict)
        max_row = 0
        max_col = 0

        for cell in root.iter(f"{{{NS['a']}}}c"):
            ref = cell.attrib.get("r")
            if not ref:
                continue
            col, row = col_to_index(ref)
            max_row = max(max_row, row)
            max_col = max(max_col, col)

            value = None
            cell_type = cell.attrib.get("t")
            v_el = cell.find("a:v", NS)
            is_el = cell.find("a:is", NS)
            if v_el is not None:
                raw = v_el.text
                if cell_type == "s" and raw is not None:
                    value = self.shared_strings[int(raw)]
                else:
                    value = raw
            elif is_el is not None:
                value = shared_text(is_el)
            values[row][col] = value

            style_id = int(cell.attrib.get("s", "0"))
            if style_id:
                fill_id = self.xf_fills[style_id] if style_id < len(self.xf_fills) else 0
                if fill_id < len(self.fills):
                    pattern_type, rgb = self.fills[fill_id]
                    if pattern_type not in (None, "none") and rgb:
                        colors[row][col] = rgb

        return values, colors, max_row, max_col


def clean_sn(sn: object) -> str:
    return str(sn or "").strip()


def side_from_position(position: int) -> str:
    if position in (1, 2, 3):
        return "H"
    if position in (4, 5, 6):
        return "L"
    raise ValueError(f"Unexpected chip position: {position}")


def bridge_group(dbc: str, position: int) -> str:
    return f"{dbc}_{side_from_position(position)}"


def parse_bridge_from_param(param: str) -> str | None:
    body = param.replace("PAR:", "")
    match = re.search(r"(?:^|_)([UVW])_([HL])(?:_|$)", body)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    match = re.search(r"(?:^|_)([UVW])([HL])(?:_|$)", body)
    if match:
        return f"{match.group(1)}_{match.group(2)}"
    return None


def param_family(param: str) -> str:
    body = param.replace("PAR:", "")
    body = re.sub(r"_(?:[UVW])_?[HL]_\d+$", "", body)
    body = re.sub(r"_\d+$", "", body)
    body = re.sub(r"_(?:[UVW])_?[HL]$", "", body)
    parts = body.split("_")
    return "_".join(parts[:2]) if len(parts) >= 2 else parts[0]


def parse_chips() -> list[dict[str, object]]:
    workbook = SimpleXlsx(CHIP_XLSX)
    values, colors, max_row, max_col = workbook.read_sheet("IGBT")
    headers = [str(values[1].get(col, "")).strip() for col in range(1, max_col + 1)]
    chips: list[dict[str, object]] = []

    for row in range(2, max_row + 1):
        if not values[row].get(4):
            continue
        item = {headers[col - 1]: values[row].get(col) for col in range(1, max_col + 1)}
        sn = clean_sn(item.get("SN_ID"))
        dbc = str(item.get("DBC_LOCATION") or "").strip()
        position_raw = item.get("芯片位置")
        if not sn or not dbc or position_raw in (None, ""):
            continue
        position = int(float(str(position_raw)))
        item["SN_ID"] = sn
        item["DBC_LOCATION"] = dbc
        item["芯片位置"] = position
        item["side"] = side_from_position(position)
        item["bridge_group"] = bridge_group(dbc, position)
        item["label_bad"] = 1 if any(colors[row].get(col) == "FFFFC000" for col in range(1, max_col + 1)) else 0
        item["_source_row"] = row
        for metric in CHIP_METRICS:
            item[metric] = to_float(item.get(metric))
        chips.append(item)
    return chips


def parse_module_values() -> tuple[dict[tuple[str, str, str, str], float], Counter[str]]:
    workbook = SimpleXlsx(MODULE_XLSX)
    feature_values: dict[tuple[str, str, str, str], float] = {}
    bridge_counts: Counter[str] = Counter()

    for sheet_name in workbook.sheet_paths:
        values, _colors, max_row, max_col = workbook.read_sheet(sheet_name)
        module_cols = {
            col: clean_sn(values[1].get(col))
            for col in range(2, max_col + 1)
            if clean_sn(values[1].get(col))
        }
        for row in range(2, max_row + 1):
            param = str(values[row].get(1) or "").strip()
            group = parse_bridge_from_param(param)
            if group not in BRIDGE_GROUPS:
                continue
            family = param_family(param)
            bridge_counts[group] += 1
            feature_name = f"{sheet_name}|{family}|{param}"
            for col, sn in module_cols.items():
                value = to_float(values[row].get(col))
                if value is not None:
                    feature_values[(sn, group, feature_name, sheet_name)] = value
    return feature_values, bridge_counts


def q75_max_score(abs_z_values: list[float]) -> tuple[float, float, float]:
    if not abs_z_values:
        return 0.0, 0.0, 0.0
    q75 = percentile(abs_z_values, 0.75)
    max_z = max(abs_z_values)
    mean_z = safe_mean(abs_z_values)
    raw = 0.70 * q75 + 0.20 * max_z + 0.10 * mean_z
    return score_from_abs_z(raw), q75, max_z


def compute_bridge_scores(
    chips: list[dict[str, object]],
    module_values: dict[tuple[str, str, str, str], float],
) -> dict[tuple[str, str], dict[str, float | str]]:
    module_ids = sorted({str(chip["SN_ID"]) for chip in chips})
    features_by_group: dict[str, set[str]] = defaultdict(set)
    for _sn, group, feature, _sheet in module_values:
        features_by_group[group].add(feature)

    scores: dict[tuple[str, str], dict[str, float | str]] = {}
    for test_sn in module_ids:
        train_sns = [sn for sn in module_ids if sn != test_sn]
        for group in BRIDGE_GROUPS:
            abs_z_values: list[float] = []
            contributors: list[tuple[float, str]] = []
            for feature in sorted(features_by_group.get(group, [])):
                test_value = module_values.get((test_sn, group, feature, feature.split("|", 1)[0]))
                if test_value is None:
                    continue
                ref = [
                    module_values[(sn, group, feature, feature.split("|", 1)[0])]
                    for sn in train_sns
                    if (sn, group, feature, feature.split("|", 1)[0]) in module_values
                ]
                if not ref:
                    continue
                abs_z = abs(robust_z(test_value, ref))
                abs_z_values.append(abs_z)
                contributors.append((abs_z, feature))
            score, q75, max_z = q75_max_score(abs_z_values)
            top_feature = max(contributors, default=(0.0, ""))[1]
            scores[(test_sn, group)] = {
                "bridge_anomaly_score": score,
                "bridge_abs_z_q75": q75,
                "bridge_abs_z_max": max_z,
                "bridge_feature_count": float(len(abs_z_values)),
                "bridge_top_feature": top_feature,
            }
    return scores


def group_values(
    chips: list[dict[str, object]],
    metric: str,
    predicate,
) -> list[float]:
    return [float(chip[metric]) for chip in chips if predicate(chip) and chip.get(metric) is not None]


def rank_desc(values: list[tuple[int, float]]) -> dict[int, int]:
    ordered = sorted(values, key=lambda item: (-item[1], item[0]))
    return {idx: rank + 1 for rank, (idx, _value) in enumerate(ordered)}


def compute_chip_scores(chips: list[dict[str, object]]) -> list[dict[str, object]]:
    module_ids = sorted({str(chip["SN_ID"]) for chip in chips})
    rows: list[dict[str, object]] = []

    for test_sn in module_ids:
        train = [chip for chip in chips if chip["SN_ID"] != test_sn]
        test = [chip for chip in chips if chip["SN_ID"] == test_sn]

        # Precompute within-test relative values. These are available at scoring
        # time because all chip measurements in the module are known.
        metric_scores_by_chip: dict[int, list[float]] = defaultdict(list)
        raw_scores_by_chip: dict[int, list[float]] = defaultdict(list)
        top_metric_by_chip: dict[int, tuple[float, str]] = defaultdict(lambda: (0.0, ""))
        relative_details: dict[int, dict[str, float]] = defaultdict(dict)

        for metric in CHIP_METRICS:
            raw_ref = group_values(train, metric, lambda _chip: True)
            for idx, chip in enumerate(test):
                value = chip.get(metric)
                if value is None:
                    continue
                raw_abs_z = abs(robust_z(float(value), raw_ref))
                raw_scores_by_chip[idx].append(raw_abs_z)

                module_ref = group_values(test, metric, lambda _chip: True)
                dbc_ref = group_values(test, metric, lambda other, chip=chip: other["DBC_LOCATION"] == chip["DBC_LOCATION"])
                bridge_ref = group_values(test, metric, lambda other, chip=chip: other["bridge_group"] == chip["bridge_group"])
                module_abs_z = abs(robust_z(float(value), module_ref))
                dbc_abs_z = abs(robust_z(float(value), dbc_ref))
                bridge_abs_z = abs(robust_z(float(value), bridge_ref))
                metric_score = 0.30 * module_abs_z + 0.25 * dbc_abs_z + 0.45 * bridge_abs_z
                metric_scores_by_chip[idx].append(metric_score)
                if metric_score > top_metric_by_chip[idx][0]:
                    top_metric_by_chip[idx] = (metric_score, metric)
                relative_details[idx][f"{metric}_module_abs_z"] = module_abs_z
                relative_details[idx][f"{metric}_dbc_abs_z"] = dbc_abs_z
                relative_details[idx][f"{metric}_bridge_abs_z"] = bridge_abs_z

            bridge_abs_for_rank = []
            for idx, chip in enumerate(test):
                value = chip.get(metric)
                if value is None:
                    continue
                bridge_ref = group_values(test, metric, lambda other, chip=chip: other["bridge_group"] == chip["bridge_group"])
                bridge_abs_for_rank.append((idx, abs(robust_z(float(value), bridge_ref))))
            ranks = rank_desc(bridge_abs_for_rank)
            for idx, rank in ranks.items():
                relative_details[idx][f"{metric}_bridge_rank"] = float(rank)

        for idx, chip in enumerate(test):
            chip_relative_score, chip_q75, chip_max = q75_max_score(metric_scores_by_chip[idx])
            chip_raw_outlier_score, raw_q75, raw_max = q75_max_score(raw_scores_by_chip[idx])
            row = dict(chip)
            row.update(relative_details[idx])
            row.update(
                {
                    "chip_relative_score": chip_relative_score,
                    "chip_relative_abs_z_q75": chip_q75,
                    "chip_relative_abs_z_max": chip_max,
                    "chip_raw_outlier_score": chip_raw_outlier_score,
                    "chip_raw_abs_z_q75": raw_q75,
                    "chip_raw_abs_z_max": raw_max,
                    "chip_top_metric": top_metric_by_chip[idx][1],
                    "chip_top_metric_score": top_metric_by_chip[idx][0],
                }
            )
            rows.append(row)

    return rows


def attach_scores_and_ranks(
    rows: list[dict[str, object]],
    bridge_scores: dict[tuple[str, str], dict[str, float | str]],
) -> list[dict[str, object]]:
    for row in rows:
        key = (str(row["SN_ID"]), str(row["bridge_group"]))
        row.update(bridge_scores.get(key, {}))
        row["risk_score"] = (
            0.45 * float(row.get("bridge_anomaly_score", 0.0))
            + 0.45 * float(row.get("chip_relative_score", 0.0))
            + 0.10 * float(row.get("chip_raw_outlier_score", 0.0))
        )

    for sn in sorted({str(row["SN_ID"]) for row in rows}):
        module_rows = [row for row in rows if row["SN_ID"] == sn]
        ordered = sorted(
            module_rows,
            key=lambda row: (-float(row["risk_score"]), str(row["DBC_LOCATION"]), int(row["芯片位置"])),
        )
        for rank, row in enumerate(ordered, start=1):
            row["risk_rank_in_module"] = rank
    return rows


def average_precision(rows: list[dict[str, object]]) -> float:
    ordered = sorted(rows, key=lambda row: -float(row["risk_score"]))
    positives = sum(int(row["label_bad"]) for row in ordered)
    if positives == 0:
        return 0.0
    hit = 0
    total = 0.0
    for idx, row in enumerate(ordered, start=1):
        if int(row["label_bad"]):
            hit += 1
            total += hit / idx
    return total / positives


def evaluate(rows: list[dict[str, object]]) -> dict[str, float]:
    positives = [row for row in rows if int(row["label_bad"]) == 1]
    ranks = [int(row["risk_rank_in_module"]) for row in positives]
    return {
        "sample_count": float(len(rows)),
        "positive_count": float(len(positives)),
        "candidate_negative_count": float(len(rows) - len(positives)),
        "mean_bad_rank": safe_mean([float(rank) for rank in ranks]),
        "top1_recall": safe_mean([1.0 if rank <= 1 else 0.0 for rank in ranks]),
        "top3_recall": safe_mean([1.0 if rank <= 3 else 0.0 for rank in ranks]),
        "average_precision": average_precision(rows),
    }


def run_tests(chips: list[dict[str, object]], rows: list[dict[str, object]]) -> list[str]:
    errors: list[str] = []
    if len(chips) != 72:
        errors.append(f"Expected 72 valid chips, got {len(chips)}")
    if sum(int(chip["label_bad"]) for chip in chips) != 4:
        errors.append(f"Expected 4 bad chips, got {sum(int(chip['label_bad']) for chip in chips)}")
    per_module = Counter(str(chip["SN_ID"]) for chip in chips)
    if any(count != 18 for count in per_module.values()) or len(per_module) != 4:
        errors.append(f"Expected four modules with 18 chips each, got {dict(per_module)}")
    per_bridge = Counter((str(chip["SN_ID"]), str(chip["bridge_group"])) for chip in chips)
    if any(count != 3 for count in per_bridge.values()) or len(per_bridge) != 24:
        errors.append("Expected each module to have six bridge groups with three chips each")
    for chip in chips:
        pos = int(chip["芯片位置"])
        expected = bridge_group(str(chip["DBC_LOCATION"]), pos)
        if chip["bridge_group"] != expected:
            errors.append(f"Bridge mapping mismatch for {chip}")
            break
    for row in rows:
        score = float(row["risk_score"])
        if not math.isfinite(score):
            errors.append(f"Non-finite risk score for {row}")
            break
    for sn in per_module:
        if sum(1 for row in rows if row["SN_ID"] == sn) != 18:
            errors.append(f"Expected 18 output rows for {sn}")
    return errors


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    base_columns = [
        "SN_ID",
        "DBC_LOCATION",
        "芯片位置",
        "side",
        "bridge_group",
        "label_bad",
        *CHIP_METRICS,
        "bridge_anomaly_score",
        "chip_relative_score",
        "chip_raw_outlier_score",
        "risk_score",
        "risk_rank_in_module",
        "bridge_abs_z_q75",
        "bridge_abs_z_max",
        "bridge_feature_count",
        "bridge_top_feature",
        "chip_relative_abs_z_q75",
        "chip_relative_abs_z_max",
        "chip_raw_abs_z_q75",
        "chip_raw_abs_z_max",
        "chip_top_metric",
        "chip_top_metric_score",
    ]
    extra_columns = sorted({
        col
        for row in rows
        for col in row.keys()
        if col not in base_columns and not col.startswith("_")
    })
    columns = base_columns + extra_columns
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (str(r["SN_ID"]), int(r["risk_rank_in_module"]))):
            writer.writerow({col: row.get(col, "") for col in columns})


def escape_xml(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_svg(rows: list[dict[str, object]], path: Path) -> None:
    modules = sorted({str(row["SN_ID"]) for row in rows})
    width = 1150
    module_h = 170
    height = 70 + module_h * len(modules)
    left = 245
    bar_w = 720
    bar_h = 10
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="30" y="34" font-family="Arial, sans-serif" font-size="22" font-weight="700">Bridge + Chip Risk Ranking</text>',
        '<text x="30" y="55" font-family="Arial, sans-serif" font-size="12" fill="#555">Orange marker = original bad-chip label; bar length = risk_score.</text>',
    ]
    y0 = 88
    for module_idx, sn in enumerate(modules):
        module_rows = sorted(
            [row for row in rows if row["SN_ID"] == sn],
            key=lambda row: int(row["risk_rank_in_module"]),
        )
        y = y0 + module_idx * module_h
        short_sn = sn[-8:]
        lines.append(f'<text x="30" y="{y}" font-family="Arial, sans-serif" font-size="15" font-weight="700">{escape_xml(short_sn)}</text>')
        for idx, row in enumerate(module_rows):
            yy = y + 20 + idx * 7
            score = float(row["risk_score"])
            bw = max(1, score * bar_w)
            color = "#d55e00" if int(row["label_bad"]) else "#4c78a8"
            label = f'{row["risk_rank_in_module"]:>2}. {row["DBC_LOCATION"]}{row["芯片位置"]} {row["bridge_group"]}'
            lines.append(f'<text x="56" y="{yy + 4}" font-family="Arial, sans-serif" font-size="9" fill="#333">{escape_xml(label)}</text>')
            lines.append(f'<rect x="{left}" y="{yy - 4}" width="{bw:.1f}" height="{bar_h}" rx="1" fill="{color}" opacity="0.82"/>')
            lines.append(f'<text x="{left + bar_w + 10}" y="{yy + 4}" font-family="Arial, sans-serif" font-size="9" fill="#333">{score:.3f}</text>')
        lines.append(f'<line x1="30" y1="{y + 146}" x2="{width - 30}" y2="{y + 146}" stroke="#e6e6e6"/>')
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_report(
    rows: list[dict[str, object]],
    metrics: dict[str, float],
    bridge_counts: Counter[str],
    test_errors: list[str],
    path: Path,
) -> None:
    positives = sorted(
        [row for row in rows if int(row["label_bad"])],
        key=lambda row: str(row["SN_ID"]),
    )
    bad_lines = [
        f'| {row["SN_ID"]} | {row["DBC_LOCATION"]} | {row["芯片位置"]} | {row["bridge_group"]} | {row["risk_rank_in_module"]} | {float(row["risk_score"]):.4f} | {row["chip_top_metric"]} |'
        for row in positives
    ]
    bridge_lines = [f"- `{group}`: {bridge_counts.get(group, 0)} module-level rows parsed" for group in BRIDGE_GROUPS]
    test_section = "All checks passed." if not test_errors else "\n".join(f"- {err}" for err in test_errors)
    text = f"""# Bridge-Level Anomaly + Chip-Level Ranking Model

## Summary

This is a risk-ranking model, not a calibrated good/bad classifier. It combines a bridge-level module anomaly score with chip-level relative outlier scores.

Formula:

```text
risk_score = 0.45 * bridge_anomaly_score
           + 0.45 * chip_relative_score
           + 0.10 * chip_raw_outlier_score
```

Yellow module-cell fills are not used as model inputs. Orange chip fills are treated as confirmed failed-chip labels and used to build `label_bad` for current-dataset evaluation.

## Data Checks

- Valid chip rows: {int(metrics["sample_count"])}
- Bad-chip labels: {int(metrics["positive_count"])}
- Candidate negatives: {int(metrics["candidate_negative_count"])}
- Modules: 4
- Chips per module: 18
- Bridge groups per module: 6

## Module Bridge Parsing

{chr(10).join(bridge_lines)}

## Evaluation

- Mean bad-chip rank: {metrics["mean_bad_rank"]:.2f} / 18
- Top-1 recall: {metrics["top1_recall"]:.3f}
- Top-3 recall: {metrics["top3_recall"]:.3f}
- Average precision: {metrics["average_precision"]:.3f}

## Bad-Chip Ranking Detail

| SN_ID | DBC | Chip Position | Bridge Group | Rank in Module | Risk Score | Top Chip Metric |
|---|---:|---:|---|---:|---:|---|
{chr(10).join(bad_lines)}

## Implementation Notes

- `1/2/3 -> H`; `4/5/6 -> L`.
- Each bridge group score is estimated with leave-one-module-out robust z-scores.
- Chip relative scores use same-module, same-DBC, and same-bridge robust deviations.
- Scores are intended for prioritizing review, not for claiming production classification accuracy.

## Sanity Tests

{test_section}
"""
    path.write_text(text, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chips = parse_chips()
    module_values, bridge_counts = parse_module_values()
    bridge_scores = compute_bridge_scores(chips, module_values)
    rows = compute_chip_scores(chips)
    rows = attach_scores_and_ranks(rows, bridge_scores)
    metrics = evaluate(rows)
    test_errors = run_tests(chips, rows)

    write_csv(rows, OUT_DIR / "risk_scores.csv")
    write_svg(rows, OUT_DIR / "rank_by_module.svg")
    write_report(rows, metrics, bridge_counts, test_errors, OUT_DIR / "model_report.md")

    if test_errors:
        raise SystemExit("\n".join(test_errors))
    print(f"Wrote {OUT_DIR / 'risk_scores.csv'}")
    print(f"Wrote {OUT_DIR / 'model_report.md'}")
    print(f"Wrote {OUT_DIR / 'rank_by_module.svg'}")
    print(
        "metrics:",
        {
            key: round(value, 4)
            for key, value in metrics.items()
        },
    )


if __name__ == "__main__":
    main()
