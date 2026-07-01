# 桥臂-芯片风险排序模型说明

本项目基于 `bridge_chip_risk_model.py` 实现了一个“模块桥臂异常 + 芯片相对离群”的风险排序模型。模型目标不是直接给出校准后的好/坏二分类结论，而是在每个模块内部对 18 颗芯片进行风险排序，辅助优先定位需要复核的芯片和桥臂。

## 文件结构

```text
.
├── bridge_chip_risk_model.py
├── failure_prediction_model.py
├── 模块层级制程数据【加颜色标注】.xlsx
├── 芯片层级制程数据【加颜色标注】.xlsx
└── result/
    ├── failure_prediction/
    │   ├── failure_predictions.csv
    │   ├── result.md
    │   └── prediction_by_module.svg
    └── bridge_chip_risk_model/
        ├── risk_scores.csv
        ├── model_report.md
        └── rank_by_module.svg
```

## 数据情况

模型使用两个 Excel 文件作为输入：

- `模块层级制程数据【加颜色标注】.xlsx`：模块层级制程/测试参数数据。脚本会解析各 sheet 中带有桥臂信息的参数名，并映射到 6 个桥臂组：`U_H`、`U_L`、`V_H`、`V_L`、`W_H`、`W_L`。
- `芯片层级制程数据【加颜色标注】.xlsx`：芯片层级测试数据，当前使用 sheet `IGBT`。脚本读取每颗芯片的模块编号、DBC 位置、芯片位置和 7 个芯片指标。

芯片级输入指标包括：

```text
IGE4_30, IGE5_R30, VCE5_200, BVCE3_1m, ICES7_1330, RG, VTH5_10m
```

当前数据检查结果：

- 有效芯片样本数：72
- 模块数：4
- 每个模块芯片数：18
- 每个模块桥臂组数：6
- 每个桥臂组芯片数：3
- 橙色标注坏芯片标签数：4
- 当前按非失效评估芯片数：68

标签使用方式：

- 模块层级 Excel 中的黄色单元格不作为模型输入。
- 芯片层级 Excel 中的橙色单元格用于生成 `label_bad`，表示确认失效芯片。
- 由于当前确认失效芯片只有 4 个，当前模型应表述为现有数据内高召回预警，不能视为已经跨批次验证的生产级精准判废模型。

## 模型方法

模型由三部分分数组成：

1. `bridge_anomaly_score`：桥臂级异常分数  
   对每个模块的每个桥臂组，使用留一模块法构造参考集。脚本对模块层级参数计算 robust z-score，并用分位数和最大异常程度合成桥臂异常分数。

2. `chip_relative_score`：芯片相对离群分数  
   对每颗芯片的 7 个芯片级指标，分别计算其在同模块、同 DBC、同桥臂内的相对偏离程度，并合成芯片相对异常分数。

3. `chip_raw_outlier_score`：芯片原始跨模块离群分数  
   使用其他模块芯片作为参考，衡量该芯片原始指标值在全局上的离群程度。

最终风险分数公式为：

```text
risk_score = 0.45 * bridge_anomaly_score
           + 0.45 * chip_relative_score
           + 0.10 * chip_raw_outlier_score
```

其中 `risk_score` 越高，表示该芯片越应优先复核。脚本会在每个模块内按 `risk_score` 从高到低生成 `risk_rank_in_module`。

桥臂映射规则：

```text
芯片位置 1/2/3 -> H 侧
芯片位置 4/5/6 -> L 侧
DBC_LOCATION + H/L -> bridge_group
```

例如：

- `DBC_LOCATION = U` 且 `芯片位置 = 1`，映射为 `U_H`
- `DBC_LOCATION = V` 且 `芯片位置 = 4`，映射为 `V_L`

## 运行方式

脚本只使用 Python 标准库，不依赖 pandas、openpyxl 或 scikit-learn。

```bash
python3 bridge_chip_risk_model.py
```

运行后会写入：

- `result/bridge_chip_risk_model/risk_scores.csv`：每颗芯片的风险分数、模块内排名、桥臂异常分数、芯片异常分数、关键贡献指标等。
- `result/bridge_chip_risk_model/model_report.md`：模型摘要、数据检查、评估指标和坏芯片排名明细。
- `result/bridge_chip_risk_model/rank_by_module.svg`：按模块展示的风险排序可视化图。

## 当前结果

当前已生成的模型报告显示：

- 平均坏芯片排名：10.00 / 18
- Top-1 recall：0.000
- Top-3 recall：0.250
- Average precision：0.084
- 内置数据一致性检查：通过

4 个确认失效芯片的模块内排名如下：

| SN_ID | DBC | 芯片位置 | 桥臂组 | 模块内排名 | 风险分数 | 主要芯片指标 |
|---|---:|---:|---|---:|---:|---|
| E3003161ABV40298001001K21200697 | V | 4 | V_L | 3 | 0.6234 | RG |
| E3003161ABV40298001001K21201308 | U | 1 | U_H | 12 | 0.5308 | IGE4_30 |
| E3003161ABV40298001001K21800896 | W | 3 | W_H | 12 | 0.5074 | IGE4_30 |
| E3003161ABV40298001001K22002764 | U | 1 | U_H | 13 | 0.5203 | RG |

结果解读：

- 模型成功将其中 1 个确认失效芯片排入所在模块 Top-3。
- 其余 3 个确认失效芯片排名靠后，说明原始桥臂+芯片风险排序尚不足以稳定识别所有已标注异常。
- 由于样本量和标签条件有限，本模型更适合作为异常排查和工艺诊断的辅助排序工具，而不是最终判废模型。

## 优化版失效二分类模型

为了在现有数据条件下形成更直接的预测输出，项目新增 `failure_prediction_model.py`。该脚本读取 `result/bridge_chip_risk_model/risk_scores.csv`，保留基线全局阈值模型，同时新增优化版稀疏加权评分 `optimized_fail_score` 和 `optimized_predicted_fail` 二分类结果。两套规则都不假设每个模块一定有失效芯片。

基线预测分数使用：

```text
fail_score = mean(global_consistency_pct,
                  IGE4_30_high_pct,
                  IGE5_R30_high_pct,
                  ICES7_1330_high_pct)
```

当前结果：

- 基线规则：`predicted_fail = 1 if fail_score >= global_threshold else 0`
- 优化规则：`optimized_predicted_fail = 1 if optimized_fail_score >= optimized_global_threshold else 0`
- 基线 TP / FP / TN / FN：4 / 12 / 56 / 0
- 优化版 TP / FP / TN / FN：4 / 1 / 67 / 0
- 基线 Precision / Recall / Accuracy：0.250 / 1.000 / 0.833
- 优化版 Precision / Recall / Accuracy：0.800 / 1.000 / 0.986
- 优化版 Predicted failure count：5
- 优化版已知失效芯片平均模块内排名：1.00 / 18
- 优化版 Average precision：0.950

输出文件：

- `result/failure_prediction/failure_predictions.csv`
- `result/failure_prediction/result.md`
- `result/failure_prediction/prediction_by_module.svg`

该结果适合表述为“现有数据内增强版失效预警二分类模型”。由于优化版基于当前 4 个确认失效芯片筛选形成，leave-one-module 检查仍提示泛化风险，不能表述为已经完成跨批次验证的生产级精准判废模型。

## 输出字段说明

`risk_scores.csv` 中核心字段包括：

- `SN_ID`：模块编号。
- `DBC_LOCATION`：DBC 位置，取值如 `U`、`V`、`W`。
- `芯片位置`：芯片在 DBC 内的位置。
- `bridge_group`：桥臂组，例如 `U_H`、`V_L`。
- `label_bad`：由芯片层级 Excel 橙色标注生成的确认失效标签，`1` 表示确认失效芯片。
- `bridge_anomaly_score`：模块桥臂异常分数。
- `chip_relative_score`：芯片在模块、DBC、桥臂内部的相对离群分数。
- `chip_raw_outlier_score`：芯片相对其他模块样本的原始离群分数。
- `risk_score`：最终风险分数。
- `risk_rank_in_module`：模块内风险排名，`1` 表示该模块内最高风险。
- `bridge_top_feature`：对桥臂异常贡献最大的模块层级参数。
- `chip_top_metric`：对芯片相对离群贡献最大的芯片级指标。

## 注意事项

- 当前数据规模较小，不适合训练复杂监督模型。
- 当前失效芯片标签来自橙色标注，按确认失效标签处理。
- 模型使用 robust z-score 降低极端值和小样本波动影响，但每个桥臂组只有 3 颗芯片，局部排序仍可能不稳定。
- 后续若补充更多模块和跨批次数据，可以进一步验证权重、引入正式监督模型，并评估跨批次泛化能力。
