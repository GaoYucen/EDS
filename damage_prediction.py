"""
Build module-level damage-analysis datasets from the Excel workbooks in this folder.

This script intentionally uses only the Python standard library so it can run in
the current environment without installing pandas/openpyxl/scikit-learn.

Outputs are written to ./outputs:
  - modeling_dataset.csv
  - chip_level_modeling_dataset.csv
  - risk_ranking.csv
  - chip_level_risk_ranking.csv
  - top_damage_indicators.csv
  - damage_prediction_summary.json
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def col_to_num(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref)
    if not letters:
        return 0
    value = 0
    for char in letters.group(0):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value


def row_num(cell_ref: str) -> int:
    digits = re.search(r"\d+", cell_ref)
    return int(digits.group(0)) if digits else 0


def clean_id(value: object) -> str:
    return str(value or "").strip()


def to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


class XlsxReader:
    def __init__(self, path: Path):
        self.path = path
        self.archive = zipfile.ZipFile(path)
        self.shared_strings = self._load_shared_strings()
        self.fills, self.style_fill_ids = self._load_styles()
        self.sheet_entries = self._load_sheet_entries()

    def close(self) -> None:
        self.archive.close()

    def __enter__(self) -> "XlsxReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _read_xml(self, entry_name: str) -> ET.Element:
        with self.archive.open(entry_name) as handle:
            return ET.parse(handle).getroot()

    def _load_shared_strings(self) -> List[str]:
        try:
            root = self._read_xml("xl/sharedStrings.xml")
        except KeyError:
            return []

        strings: List[str] = []
        for si in root.iter():
            if local_name(si.tag) != "si":
                continue
            parts = [
                node.text or ""
                for node in si.iter()
                if local_name(node.tag) == "t"
            ]
            strings.append("".join(parts))
        return strings

    def _load_styles(self) -> Tuple[List[dict], List[int]]:
        try:
            root = self._read_xml("xl/styles.xml")
        except KeyError:
            return [], []

        fills: List[dict] = []
        style_fill_ids: List[int] = []

        for child in root:
            if local_name(child.tag) == "fills":
                for fill in child:
                    fill_info = {"patternType": "", "fgRgb": "", "fgIndexed": "", "fgTheme": ""}
                    for pattern_fill in fill:
                        if local_name(pattern_fill.tag) != "patternFill":
                            continue
                        fill_info["patternType"] = pattern_fill.attrib.get("patternType", "")
                        for color_node in pattern_fill:
                            if local_name(color_node.tag) == "fgColor":
                                fill_info["fgRgb"] = color_node.attrib.get("rgb", "")
                                fill_info["fgIndexed"] = color_node.attrib.get("indexed", "")
                                fill_info["fgTheme"] = color_node.attrib.get("theme", "")
                    fills.append(fill_info)

            if local_name(child.tag) == "cellXfs":
                for xf in child:
                    style_fill_ids.append(int(xf.attrib.get("fillId", "0")))

        return fills, style_fill_ids

    def _load_sheet_entries(self) -> List[Tuple[str, str]]:
        workbook = self._read_xml("xl/workbook.xml")
        rels = self._read_xml("xl/_rels/workbook.xml.rels")

        rel_targets = {}
        for rel in rels:
            if local_name(rel.tag) == "Relationship":
                rel_targets[rel.attrib["Id"]] = rel.attrib["Target"].replace("\\", "/")

        sheet_entries: List[Tuple[str, str]] = []
        rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for sheets_node in workbook.iter():
            if local_name(sheets_node.tag) != "sheets":
                continue
            for sheet in sheets_node:
                if local_name(sheet.tag) != "sheet":
                    continue
                name = sheet.attrib["name"]
                rel_id = sheet.attrib[rel_ns]
                target = rel_targets[rel_id]
                if target.startswith("/"):
                    entry_name = target.lstrip("/")
                elif target.startswith("xl/"):
                    entry_name = target
                else:
                    entry_name = "xl/" + target
                sheet_entries.append((name, entry_name))
        return sheet_entries

    def cell_value(self, cell: ET.Element) -> str:
        cell_type = cell.attrib.get("t", "")
        if cell_type == "inlineStr":
            text_parts = [
                node.text or ""
                for node in cell.iter()
                if local_name(node.tag) == "t"
            ]
            return "".join(text_parts)

        raw = ""
        for child in cell:
            if local_name(child.tag) == "v":
                raw = child.text or ""
                break

        if not raw:
            return ""
        if cell_type == "s":
            index = int(raw)
            return self.shared_strings[index] if index < len(self.shared_strings) else ""
        if cell_type == "b":
            return "TRUE" if raw == "1" else "FALSE"
        return raw

    def cell_fill_id(self, cell: ET.Element) -> int:
        style_id = int(cell.attrib.get("s", "0") or "0")
        if style_id >= len(self.style_fill_ids):
            return 0
        return self.style_fill_ids[style_id]

    def is_colored_cell(self, cell: ET.Element) -> bool:
        fill_id = self.cell_fill_id(cell)
        if fill_id <= 1:
            return False
        if fill_id >= len(self.fills):
            return True
        fill = self.fills[fill_id]
        return bool(fill.get("fgRgb") or fill.get("fgIndexed") or fill.get("fgTheme"))

    def read_sheet(self, sheet_entry: str) -> Tuple[List[List[str]], Dict[Tuple[int, int], int]]:
        root = self._read_xml(sheet_entry)
        rows: Dict[int, Dict[int, str]] = defaultdict(dict)
        colors: Dict[Tuple[int, int], int] = {}
        max_row = 0
        max_col = 0

        for row in root.iter():
            if local_name(row.tag) != "row":
                continue
            for cell in row:
                if local_name(cell.tag) != "c":
                    continue
                ref = cell.attrib.get("r", "")
                r = row_num(ref)
                c = col_to_num(ref)
                if not r or not c:
                    continue
                rows[r][c] = self.cell_value(cell)
                max_row = max(max_row, r)
                max_col = max(max_col, c)
                if self.is_colored_cell(cell):
                    colors[(r, c)] = self.cell_fill_id(cell)

        table: List[List[str]] = []
        for r in range(1, max_row + 1):
            table.append([rows[r].get(c, "") for c in range(1, max_col + 1)])
        return table, colors


def locate_workbooks() -> Tuple[Path, Path]:
    search_dirs = [DATA_DIR, ROOT] if DATA_DIR.exists() else [ROOT]
    files = []
    seen = set()
    for directory in search_dirs:
        for path in directory.glob("*.xlsx"):
            if path.name.startswith("~$") or path in seen:
                continue
            files.append(path)
            seen.add(path)
    if len(files) < 2:
        raise FileNotFoundError("Expected at least two .xlsx files in ./data or the current folder.")

    chip_candidates = [p for p in files if "芯片" in p.name]
    module_candidates = [p for p in files if "模块" in p.name]
    if chip_candidates and module_candidates:
        return chip_candidates[0], module_candidates[0]

    # Fallback for filename encoding or renamed files: chip workbook is much larger.
    files = sorted(files, key=lambda path: path.stat().st_size)
    return files[-1], files[0]


def add_numeric_stats(features: dict, prefix: str, values: List[float]) -> None:
    if not values:
        return
    features[f"{prefix}_count"] = len(values)
    features[f"{prefix}_mean"] = sum(values) / len(values)
    features[f"{prefix}_min"] = min(values)
    features[f"{prefix}_max"] = max(values)
    features[f"{prefix}_range"] = max(values) - min(values)
    features[f"{prefix}_std"] = statistics.pstdev(values) if len(values) > 1 else 0.0


def extract_chip_features(chip_path: Path) -> Tuple[Dict[str, dict], Dict[str, List[dict]], List[dict]]:
    module_rows: Dict[str, List[dict]] = defaultdict(list)
    damage_sources: Dict[str, List[dict]] = defaultdict(list)
    chip_level_rows: List[dict] = []

    with XlsxReader(chip_path) as workbook:
        sheet_name, sheet_entry = workbook.sheet_entries[0]
        table, colors = workbook.read_sheet(sheet_entry)

    if not table:
        return {}, {}, []

    headers = [clean_id(value) for value in table[0]]
    numeric_headers = headers[3:]

    for row_index, row in enumerate(table[1:], start=2):
        if not row:
            continue
        module_id = clean_id(row[0] if len(row) > 0 else "")
        if not module_id:
            continue

        row_is_colored = any((row_index, col) in colors for col in range(2, len(headers) + 1))
        location = clean_id(row[1] if len(row) > 1 else "")
        chip_position = clean_id(row[2] if len(row) > 2 else "")

        row_record = {
            "row": row_index,
            "location": location,
            "chip_position": chip_position,
            "is_colored": row_is_colored,
            "values": row,
        }
        module_rows[module_id].append(row_record)

        chip_level_row = {
            "sample_id": f"{module_id}__row_{row_index}",
            "module_id": module_id,
            "group_module_id": module_id,
            "source_row": row_index,
            "dbc_location": location,
            "chip_position": chip_position,
            "label_damaged": 1 if row_is_colored else 0,
            "label_role": "marked_damaged" if row_is_colored else "candidate_negative",
            "label_note": (
                "colored chip row; treated as temporary damaged label"
                if row_is_colored
                else "uncolored chip row; temporary candidate negative under chip-independence assumption"
            ),
        }
        for offset, header in enumerate(numeric_headers, start=3):
            value = row[offset] if offset < len(row) else ""
            number = to_float(value)
            if number is None:
                continue
            safe_header = re.sub(r"[^\w]+", "_", header).strip("_") or f"col_{offset + 1}"
            chip_level_row[f"chip_{safe_header}"] = number
        chip_level_rows.append(chip_level_row)

        if row_is_colored:
            damage_sources[module_id].append(
                {
                    "row": row_index,
                    "dbc_location": location,
                    "chip_position": chip_position,
                }
            )

    features_by_module: Dict[str, dict] = {}
    for module_id, rows in module_rows.items():
        features = {
            "module_id": module_id,
            "chip_count": len(rows),
            "abnormal_chip_count": sum(1 for row in rows if row["is_colored"]),
        }
        features["abnormal_chip_ratio"] = (
            features["abnormal_chip_count"] / features["chip_count"]
            if features["chip_count"]
            else 0.0
        )

        location_counter = Counter(row["location"] for row in rows if row["location"])
        abnormal_location_counter = Counter(
            row["location"] for row in rows if row["is_colored"] and row["location"]
        )
        for location in sorted(location_counter):
            safe_location = re.sub(r"\W+", "_", location)
            features[f"chip_location_{safe_location}_count"] = location_counter[location]
            features[f"abnormal_location_{safe_location}_count"] = abnormal_location_counter[location]

        for offset, header in enumerate(numeric_headers, start=3):
            values = []
            for row_record in rows:
                row = row_record["values"]
                if offset < len(row):
                    number = to_float(row[offset])
                    if number is not None:
                        values.append(number)
            safe_header = re.sub(r"[^\w]+", "_", header).strip("_") or f"col_{offset + 1}"
            add_numeric_stats(features, f"chip_{safe_header}", values)

        features_by_module[module_id] = features

    return features_by_module, damage_sources, chip_level_rows


def extract_module_features(module_path: Path) -> Dict[str, dict]:
    features_by_module: Dict[str, dict] = defaultdict(dict)

    with XlsxReader(module_path) as workbook:
        for sheet_name, sheet_entry in workbook.sheet_entries:
            table, colors = workbook.read_sheet(sheet_entry)
            if not table:
                continue

            headers = [clean_id(value) for value in table[0]]
            module_ids = headers[1:]

            for column_offset, module_id in enumerate(module_ids, start=2):
                if not module_id:
                    continue
                features_by_module[module_id]["module_id"] = module_id
                colored_count = 0

                for row_index, row in enumerate(table[1:], start=2):
                    if not row:
                        continue
                    parameter = clean_id(row[0])
                    if not parameter:
                        continue
                    value = row[column_offset - 1] if column_offset - 1 < len(row) else ""
                    number = to_float(value)
                    safe_sheet = re.sub(r"[^\w]+", "_", sheet_name).strip("_")
                    safe_parameter = re.sub(r"[^\w]+", "_", parameter).strip("_")
                    feature_name = f"module_{safe_sheet}_{safe_parameter}"
                    if number is not None:
                        features_by_module[module_id][feature_name] = number
                    if (row_index, column_offset) in colors:
                        colored_count += 1

                safe_sheet = re.sub(r"[^\w]+", "_", sheet_name).strip("_")
                features_by_module[module_id][f"module_{safe_sheet}_colored_cell_count"] = colored_count

    return dict(features_by_module)


def median(values: List[float]) -> float:
    return statistics.median(values)


def robust_scale(values: List[float]) -> Optional[Tuple[float, float]]:
    unique = {value for value in values}
    if len(values) < 3 or len(unique) < 2:
        return None
    center = median(values)
    absolute_deviations = [abs(value - center) for value in values]
    mad = median(absolute_deviations)
    if mad > 0:
        return center, 1.4826 * mad
    std = statistics.pstdev(values)
    if std > 0:
        return center, std
    return None


def add_anomaly_scores(rows: List[dict], label_keys: Iterable[str]) -> None:
    label_keys = set(label_keys)
    numeric_columns = []
    for key in sorted({key for row in rows for key in row.keys()}):
        if key in label_keys or key == "module_id":
            continue
        values = [row.get(key) for row in rows]
        numeric_values = [value for value in values if isinstance(value, (int, float))]
        if len(numeric_values) >= 3:
            numeric_columns.append(key)

    scales = {}
    for key in numeric_columns:
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        scale = robust_scale(values)
        if scale:
            scales[key] = scale

    for row in rows:
        z_values = []
        top_features = []
        for key, (center, scale) in scales.items():
            value = row.get(key)
            if not isinstance(value, (int, float)):
                continue
            z = abs((value - center) / scale)
            capped = min(z, 10.0)
            z_values.append(capped)
            if z >= 2.0:
                top_features.append((key, z))

        if z_values:
            z_values_sorted = sorted(z_values, reverse=True)
            top_n = z_values_sorted[: min(10, len(z_values_sorted))]
            top_mean = sum(top_n) / len(top_n)
            max_z = z_values_sorted[0]
            high_count = sum(1 for value in z_values if value >= 2.0)
            row["process_anomaly_score"] = round(35.0 * top_mean + 45.0 * max_z + 5.0 * high_count, 4)
            row["process_anomaly_max_z"] = round(max_z, 4)
            row["process_anomaly_feature_count_z_ge_2"] = high_count
            row["top_anomaly_features"] = "; ".join(
                f"{key}:{z:.2f}" for key, z in sorted(top_features, key=lambda item: item[1], reverse=True)[:8]
            )
        else:
            row["process_anomaly_score"] = 0.0
            row["process_anomaly_max_z"] = 0.0
            row["process_anomaly_feature_count_z_ge_2"] = 0
            row["top_anomaly_features"] = ""


def build_indicator_table(rows: List[dict]) -> List[dict]:
    damaged = [row for row in rows if row.get("label_damaged") == 1]
    normal = [row for row in rows if row.get("label_damaged") == 0]
    if not damaged or not normal:
        return []

    indicators = []
    feature_names = sorted(
        key
        for key in {key for row in rows for key in row.keys()}
        if key not in {"module_id", "label_damaged", "damage_source"}
    )

    for key in feature_names:
        damaged_values = [row[key] for row in damaged if isinstance(row.get(key), (int, float))]
        normal_values = [row[key] for row in normal if isinstance(row.get(key), (int, float))]
        if not damaged_values or not normal_values:
            continue

        damaged_mean = sum(damaged_values) / len(damaged_values)
        normal_mean = sum(normal_values) / len(normal_values)
        all_values = damaged_values + normal_values
        std = statistics.pstdev(all_values) if len(all_values) > 1 else 0.0
        effect = (damaged_mean - normal_mean) / std if std else 0.0
        indicators.append(
            {
                "feature": key,
                "damaged_mean": damaged_mean,
                "normal_mean": normal_mean,
                "standardized_difference": effect,
                "absolute_standardized_difference": abs(effect),
                "damaged_count": len(damaged_values),
                "normal_count": len(normal_values),
            }
        )

    indicators.sort(key=lambda row: row["absolute_standardized_difference"], reverse=True)
    return indicators


def write_csv(path: Path, rows: List[dict], columns: Optional[List[str]] = None) -> None:
    if columns is None:
        leading = ["sample_id", "module_id", "group_module_id", "label_damaged", "label_role", "damage_source"]
        all_columns = sorted({key for row in rows for key in row.keys()})
        columns = [key for key in leading if key in all_columns]
        columns += [key for key in all_columns if key not in columns]

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    chip_path, module_path = locate_workbooks()
    OUTPUT_DIR.mkdir(exist_ok=True)

    chip_features, damage_sources, chip_level_rows = extract_chip_features(chip_path)
    module_features = extract_module_features(module_path)

    all_module_ids = sorted(set(chip_features) | set(module_features))
    rows = []
    for module_id in all_module_ids:
        row = {}
        row.update(chip_features.get(module_id, {}))
        row.update(module_features.get(module_id, {}))
        row["module_id"] = module_id
        row["label_damaged"] = 1 if module_id in damage_sources else 0
        row["damage_source"] = "; ".join(
            f"chip_row={item['row']},location={item['dbc_location']},position={item['chip_position']}"
            for item in damage_sources.get(module_id, [])
        )
        rows.append(row)

    add_anomaly_scores(
        rows,
        label_keys={
            "label_damaged",
            "damage_source",
            "abnormal_chip_count",
            "abnormal_chip_ratio",
            "top_anomaly_features",
        },
    )
    add_anomaly_scores(
        chip_level_rows,
        label_keys={
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
        },
    )

    indicators = build_indicator_table(rows)

    modeling_path = OUTPUT_DIR / "modeling_dataset.csv"
    chip_modeling_path = OUTPUT_DIR / "chip_level_modeling_dataset.csv"
    ranking_path = OUTPUT_DIR / "risk_ranking.csv"
    chip_ranking_path = OUTPUT_DIR / "chip_level_risk_ranking.csv"
    indicators_path = OUTPUT_DIR / "top_damage_indicators.csv"
    summary_path = OUTPUT_DIR / "damage_prediction_summary.json"

    write_csv(modeling_path, rows)
    write_csv(chip_modeling_path, chip_level_rows)

    ranking_columns = [
        "module_id",
        "label_damaged",
        "process_anomaly_score",
        "process_anomaly_max_z",
        "process_anomaly_feature_count_z_ge_2",
        "chip_count",
        "abnormal_chip_count",
        "abnormal_chip_ratio",
        "damage_source",
        "top_anomaly_features",
    ]
    ranking_rows = sorted(rows, key=lambda row: row.get("process_anomaly_score", 0), reverse=True)
    write_csv(ranking_path, ranking_rows, columns=[col for col in ranking_columns if any(col in row for row in rows)])

    chip_ranking_columns = [
        "sample_id",
        "module_id",
        "group_module_id",
        "source_row",
        "dbc_location",
        "chip_position",
        "label_damaged",
        "label_role",
        "process_anomaly_score",
        "process_anomaly_max_z",
        "process_anomaly_feature_count_z_ge_2",
        "top_anomaly_features",
        "label_note",
    ]
    chip_ranking_rows = sorted(chip_level_rows, key=lambda row: row.get("process_anomaly_score", 0), reverse=True)
    write_csv(
        chip_ranking_path,
        chip_ranking_rows,
        columns=[col for col in chip_ranking_columns if any(col in row for row in chip_level_rows)],
    )

    write_csv(
        indicators_path,
        indicators[:100],
        columns=[
            "feature",
            "damaged_mean",
            "normal_mean",
            "standardized_difference",
            "absolute_standardized_difference",
            "damaged_count",
            "normal_count",
        ],
    )

    damaged_modules = sorted(damage_sources.keys())
    modules_with_module_level_data = sorted(module_features.keys())
    normal_count = sum(1 for row in rows if row["label_damaged"] == 0)
    damaged_count = sum(1 for row in rows if row["label_damaged"] == 1)
    damaged_chip_count = sum(1 for row in chip_level_rows if row["label_damaged"] == 1)
    candidate_negative_chip_count = sum(1 for row in chip_level_rows if row["label_damaged"] == 0)

    summary = {
        "chip_workbook": chip_path.name,
        "module_workbook": module_path.name,
        "module_count": len(rows),
        "damaged_module_count": damaged_count,
        "normal_module_count": normal_count,
        "chip_level_sample_count": len(chip_level_rows),
        "damaged_chip_count": damaged_chip_count,
        "candidate_negative_chip_count": candidate_negative_chip_count,
        "damaged_modules": damaged_modules,
        "modules_with_module_level_data": modules_with_module_level_data,
        "outputs": {
            "modeling_dataset": str(modeling_path),
            "chip_level_modeling_dataset": str(chip_modeling_path),
            "risk_ranking": str(ranking_path),
            "chip_level_risk_ranking": str(chip_ranking_path),
            "top_damage_indicators": str(indicators_path),
        },
        "chip_level_modeling_note": (
            "Uncolored chip rows are temporary candidate negatives under the assumption that chips "
            "do not interfere with each other. They are weak labels, not confirmed normal samples. "
            "Use group_module_id for grouped train/validation splits."
        ),
        "modeling_note": (
            "This dataset has very few damaged labels. Treat process_anomaly_score as a "
            "descriptive risk score, not a validated failure-prediction model. Add more "
            "historical normal and damaged modules before training a supervised classifier."
        ),
    }

    with summary_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print(f"Read chip workbook: {chip_path.name}")
    print(f"Read module workbook: {module_path.name}")
    print(f"Modules: {len(rows)}; damaged: {damaged_count}; normal: {normal_count}")
    print(
        "Chip-level samples: "
        f"{len(chip_level_rows)}; damaged: {damaged_chip_count}; "
        f"candidate_negative: {candidate_negative_chip_count}"
    )
    print(f"Wrote: {modeling_path}")
    print(f"Wrote: {chip_modeling_path}")
    print(f"Wrote: {ranking_path}")
    print(f"Wrote: {chip_ranking_path}")
    print(f"Wrote: {indicators_path}")
    print(f"Wrote: {summary_path}")
    if damaged_count < 10:
        print("Warning: damaged samples are too few for a reliable supervised prediction model.")


if __name__ == "__main__":
    main()
