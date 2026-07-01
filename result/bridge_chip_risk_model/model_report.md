# Bridge-Level Anomaly + Chip-Level Ranking Model

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

- Valid chip rows: 72
- Bad-chip labels: 4
- Candidate negatives: 68
- Modules: 4
- Chips per module: 18
- Bridge groups per module: 6

## Module Bridge Parsing

- `U_H`: 408 module-level rows parsed
- `U_L`: 408 module-level rows parsed
- `V_H`: 408 module-level rows parsed
- `V_L`: 408 module-level rows parsed
- `W_H`: 408 module-level rows parsed
- `W_L`: 408 module-level rows parsed

## Evaluation

- Mean bad-chip rank: 10.00 / 18
- Top-1 recall: 0.000
- Top-3 recall: 0.250
- Average precision: 0.084

## Bad-Chip Ranking Detail

| SN_ID | DBC | Chip Position | Bridge Group | Rank in Module | Risk Score | Top Chip Metric |
|---|---:|---:|---|---:|---:|---|
| E3003161ABV40298001001K21200697 | V | 4 | V_L | 3 | 0.6234 | RG |
| E3003161ABV40298001001K21201308 | U | 1 | U_H | 12 | 0.5308 | IGE4_30 |
| E3003161ABV40298001001K21800896 | W | 3 | W_H | 12 | 0.5074 | IGE4_30 |
| E3003161ABV40298001001K22002764 | U | 1 | U_H | 13 | 0.5203 | RG |

## Implementation Notes

- `1/2/3 -> H`; `4/5/6 -> L`.
- Each bridge group score is estimated with leave-one-module-out robust z-scores.
- Chip relative scores use same-module, same-DBC, and same-bridge robust deviations.
- Scores are intended for prioritizing review, not for claiming production classification accuracy.

## Sanity Tests

All checks passed.
