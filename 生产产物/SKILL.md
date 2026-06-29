---
name: build-b6-limitup-pool-production
description: 当需要读取涨停池动态管理（B6）的生产结果时，使用此 skill。该 skill 读取已生成的 Parquet 涨停池历史，不重复执行重计算流程。
tags: [quant, build, production, monitor, limit-up]
---

# 涨停池动态管理生产结果（B6）

## 工具定位

- 工具类型：结果型 BUILD
- 服务对象：盘后复盘 agent / 连板情绪 Alpha（如 A3）/ 人工复盘
- 是否可被 Alpha 调用：是

## 结果文件

- 文件路径：`database.parquet`
- 数据格式：Parquet
- 更新频率：每日收盘后，由开发产物 `maintain_daily()` 生成并 `save_parquet()` 追加
- 生成任务：`python build-b6-limitup-pool/开发产物/scripts/build.py --mode daily --date <交易日>`
- 历史回填：`--mode backfill --start <起> --end <止>`

> 注：`database.parquet` 由生产任务在 PandaData 配额可用时生成；本仓库不内置合成数据，
> 避免把来源不明的数据当作生产结果（见 BUILD开发与生产规则V2.md §1、§14）。
> **随包样例溯源**：当前随包 `database.parquet` 为真实 PandaData 复盘样例，覆盖 **2026-06-18 ~ 2026-06-24**
> （`data_version=pandadata-limitup-pool-v1`），由 `maintain_daily` 真实抓取生成、可用 `--mode backfill`
> 全量重建，非测试桩/合成数据。

## 主键

- `trade_date`
- `build_id`
- `target_id`
- `result_type`

## 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| trade_date | string | 结果所属交易日（YYYY-MM-DD） |
| build_id | string | `B6` |
| build_name | string | `涨停池动态管理` |
| target_id / ts_code | string | 股票代码 |
| result_type | string | `limitup_pool`（个股行）/ `limitup_sentiment`（每日 1 行 MARKET 情绪面） |
| result_value | string | 板级标签：`首板` / `N连板` / `炸板未封`；MARKET 行为 `N涨停/M板` |
| name / board_type | string | 名称 / 板块（沪主板·深主板·创业板·科创板·北交所） |
| board_label | string | 同 result_value |
| limit_up_streak / prev_streak | int | 今/昨连板数 |
| is_first_board | bool | 是否首板 |
| pool_status | string | 晋级 / 维持 / 新晋首板 / 断板后重启 / 炸板出局 / 摸板未遂 |
| seal_quality | string | 一字板 / 稳封 / 炸N次回封 / 炸板未封 |
| special_pattern | string | 地天板 / 天地板 / 一字板 / 秒板 / 炸板未封 / 烂板 / 反复板 / 实封 |
| lead_concept | string | 代表题材（所属概念中当日涨停家数最多者） |
| concept_board_count | int | 代表题材当日涨停家数（梯队厚度） |
| is_concept_leader | bool | 是否代表题材内连板最高（题材龙头） |
| concepts | string(json) | 该票 PIT 后所属全部概念列表 |
| blow_up_count / reseal_count | int | 炸板次数 / 回封次数 |
| first_seal_time / final_seal_time | string | 首封 / 最终回封时刻（HH:MM；日线代理时空） |
| is_one_word / is_limit_up_close / touched_limit | bool | 一字板 / 涨停收盘 / 盘中摸板 |
| pct_chg / close / open / high / low / amount / eff_limit_up | float | 行情 |
| seal_metric_source | string | `minute`（分钟精确）/ `daily_proxy`（日线代理） |
| result_json | string | 全状态 JSON（合法可解析）；MARKET 行存情绪面 dict（炸板率/最高板/分层晋级率/赚钱效应） |
| data_version / update_time | string | 版本 / 生成时间 |

> **MARKET 行**：每个交易日额外一行 `target_id=MARKET` / `result_type=limitup_sentiment`，
> `result_json` 含 `n_limit_up` / `market_blow_rate` / `max_height` /
> `promote_rate_by_tier`（分层晋级率）/ `prev_limitup_premium`（昨涨停今溢价=赚钱效应）。

## 读取规则

```python
import pandas as pd, json
df = pd.read_parquet("build-b6-limitup-pool/生产产物/database.parquet")
day = df[df["trade_date"] == df["trade_date"].max()]            # 默认取最新交易日
stocks = day[day["result_type"] == "limitup_pool"]             # 个股池
sentiment = json.loads(day[day["result_type"] == "limitup_sentiment"].iloc[0]["result_json"])
stocks.sort_values("limit_up_streak", ascending=False)         # 连板梯队
stocks[stocks["blow_up_count"] > 0]                            # 今日炸板
stocks.groupby("lead_concept")["concept_board_count"].max()    # 题材梯队厚度
sentiment["promote_rate_by_tier"], sentiment["prev_limitup_premium"]  # 情绪面
```

agent 按 `result_type` 先拆个股/情绪面，再按 `trade_date` / `ts_code` / `pool_status` /
`limit_up_streak` / `lead_concept` / `special_pattern` 查询。
读取后展示可调用开发产物 `render.render_markdown` / `render_html.render_html`。
回答里需注明 `seal_metric_source` 以说明炸板/回封精度。

## 禁止行为

- 不允许多人查询时重复触发重计算（重算走开发产物 `maintain_daily`）。
- 不允许手工修改 Parquet 结果。
- 生产结果异常时必须提示数据日期和异常原因（如分钟降级、当日无涨停）。
