# Result Index

当前结果分为三类：一个是面向对外汇报的优化版失效二分类模型，一个是未使用坏芯片标签调权的桥臂-芯片异常排序，一个是基于确认失效芯片做复核优先级优化的筛查排序。

## 推荐汇报口径

- 正式结论：现有数据可以形成优化版失效二分类模型，但仍不足以声明已经建立跨批次验证的生产级精准判废模型。
- 可交付结果：`failure_prediction` 同时输出基线 `fail_score`/`predicted_fail` 和优化版 `optimized_fail_score`/`optimized_predicted_fail`。
- 当前优化版规则为 `optimized_fail_score >= optimized_global_threshold` 判失效，不假设每个模块一定有失效芯片；当前阈值 0.7768，覆盖 4/4 个确认失效芯片，判失效 5 颗芯片，Precision 为 0.800，Recall 为 1.000，Accuracy 为 0.986。

## 结果文件

- [Optimized Failure Model Explanation](failure_prediction/optimized_model_explanation.md)
- [Failure Prediction Model](failure_prediction/result.md)
- [Bridge Chip Risk Model](bridge_chip_risk_model/model_report.md)
- [Known-Failure Screening Ranker](weak_label_screening/result.md)

## 注意

橙色芯片按确认失效芯片处理。`failure_prediction` 适合表述为“现有数据内增强版失效预警二分类模型”。优化版由当前数据筛选得到，leave-one-module 检查仍提示泛化风险，不应表述为已经外部验证的生产级精准判废模型。
