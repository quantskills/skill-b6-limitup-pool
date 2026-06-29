---
name: skill-b6-limitup-pool
license: GPL-3.0-only
metadata:
  organization: QuantSkills
  organization_url: https://github.com/quantskills
  repository: skill-b6-limitup-pool
  repository_url: https://github.com/quantskills/skill-b6-limitup-pool
  project_type: skill
  collection: limit-up-pool
  license: GPL-3.0-only
---

# skill-b6-limitup-pool · 涨停池动态管理（Limit-Up Pool）

> **项目状态：Community Project（社区项目）。** 本项目由社区成员创建，**未经 QuantSkills 官方审核、认证、验证或背书**，
> 也非生产可用认证项目。名称中的 `quantskills/` 仅表示托管组织，不代表任何官方身份。

A 股涨停池盘后维护工具：每日维护涨停池，标记 **首板 / 连板数 / 炸板次数 / 回封时间**，并做题材分组、
特殊形态（天地板/地天板/一字/秒板/烂板）、情绪面量化（分层晋级率/炸板率/赚钱效应），输出多维表格与 HTML 看板。

---

## 这个项目做什么（What）
把全市场日线（+ 涨停股分钟线）压成一张「涨停池盘面视图」：连板梯队、炸板回封、题材分组、市场情绪面。

## 怎么用（How）
- 自然语言：对接 agent 后说"跑今天的涨停池"即可（详见 [README.md](README.md)）。
- 命令行：`python 开发产物/scripts/build.py --mode daily`，再 `render_html.py` 出看板。
- 完整调用规则与字段表见 **[开发产物/SKILL.md](开发产物/SKILL.md)**（面向 agent 的详细声明）。

## 支持哪些场景（Scenarios）
盘后复盘 agent · 连板情绪类研究因子 · 人工复盘。

## 谁维护（Maintainer）
社区成员 [@ZLHad](https://github.com/ZLHad)。Issues / PR 欢迎。

## 重要限制（Limitations）
- 炸板次数/回封时间需分钟线，拉不到时**自动降级日线代理**（精度下降，`seal_metric_source=daily_proxy`）。
- 题材分组依赖概念接口，缺失时该维度留空。
- 依赖 PandaData 账号与流量额度，超限会中断当日维护。

---

## 量化项目边界声明（社区规则 §8）
- **数据来源**：PandaData（`panda_data` ≥ 0.0.9）；`get_stock_daily` / `get_concept_*` / `get_stock_min`。
- **假设条件**：涨停判定以接口 `limit_up` 为准，缺失时按板块 10/20/30% 兜底；连板在交易日序列上累加（停牌不算断板）。
- **参数**：回看窗口、股票池（全A/沪深300/中证1000/国证2000）、是否分钟精确，均可配置（见 api_guide）。
- **已知限制**：见上「重要限制」；题材标签为接口粗口径；特殊形态为规则判定。
- **风险边界**：输出为**研究/复盘用的客观统计**，不含买卖建议。
- **项目性质**：**仅供量化研究与教育示例**，不构成投资建议，不承诺任何收益，不暗示策略安全或保证盈利。

## 署名与许可（社区规则 §3 / §6）
- 许可证：**GPL-3.0-only**（见 [LICENSE](LICENSE)）。
- 涨停判定 / 连板状态机 / 一字板 / 分钟首封·炸板 口径借鉴自同作者的 alpha-A3 连板因子，保持同源以避免漂移。
- 第三方依赖：`panda_data`、`pandas`、`numpy`、`pyarrow`，均遵循各自许可证。

English declaration: see [README.en.md](README.en.md).
