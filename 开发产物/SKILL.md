---
name: build-b6-limitup-pool
description: 当需要每日维护 A 股涨停池、标记首板/连板数/炸板次数/回封时间并输出涨停池动态多维表格时，使用此 skill。该 BUILD 提供涨停池动态管理能力，可被复盘 agent 或 Alpha 调用。
tags: [quant, build, development, monitor, limit-up]
---

# 涨停池动态管理 BUILD（B6）

## 工具定位

- 工具类型：监控预警型 + 数据处理型 混合 BUILD
- 解决问题：把全市场日线（+涨停股分钟线）转成稳定的「涨停池状态」——每日谁在池中、几板、炸没炸、何时回封、相对昨日是晋级还是出局
- 使用对象：盘后复盘 agent / 连板情绪类 Alpha（如 A3 连板龙头接力）/ 人工复盘

## 适用场景

- 复盘 agent 需要「今日涨停池 + 连板梯队 + 炸板回封」一张多维表
- Alpha 需要稳定的 `limit_up_streak` / `is_first_board` / `blow_up_count` 输入而不想各自重算
- 人工想问「今天最高几板」「哪些票炸板了」「昨天的连板今天晋级率多少」

## 核心字段（四件套）

| 标记 | 字段 | 口径 |
|---|---|---|
| 首板 | `is_first_board` / `board_label="首板"` | `limit_up_streak == 1` |
| 连板数 | `limit_up_streak` | 连续涨停收盘的交易日数（停牌不算断板，复牌接续） |
| 炸板次数 | `blow_up_count` | 日内封板后被砸开的次数；分钟级精确，缺分钟用日线回撤代理 |
| 回封时间 | `first_seal_time` / `final_seal_time` | 首次封板 / 最后一次封死的时刻（HH:MM，分钟级；日线代理时为空） |

另含：`board_type`（板块）、`pool_status`（晋级/新晋首板/维持/炸板出局/摸板未遂）、
`seal_quality`（一字板/稳封/炸N次回封/炸板未封）、`reseal_count`、`seal_metric_source`（minute / daily_proxy）。

## 增强维度（v1.1，从"个股清单"升级为"盘面视图"）

| 维度 | 字段 | 口径 |
|---|---|---|
| 题材分组 | `lead_concept` / `concept_board_count` / `is_concept_leader` / `concepts` | 代表题材（所属概念中当日涨停家数最多者）、梯队厚度、是否题材内连板最高、全部所属概念。PIT 按 `in_date<=信号日` 过滤，杜绝未来函数 |
| 特殊形态 | `special_pattern` | 地天板 > 天地板 > 一字板 > 秒板 > 炸板未封 > 烂板(炸≥3或尾盘回封) > 反复板(炸1-2) > 实封 |
| 情绪面 | 每日 1 行 `target_id=MARKET` / `result_type=limitup_sentiment` | `result_json` 含 涨停家数 / 炸板率 / 最高板 / **分层晋级率**(1→2,2→3…) / **昨涨停今溢价**(赚钱效应) |

## 输入

输入来自 PandaData（`get_stock_daily` 全市场日线 + `get_stock_min` 涨停股分钟线），
或调用方传入的标准结构化日线数据。不得使用来源不明、字段不稳定的临时表。

`run()` 输入（DataFrame / records，**含回看窗口**用于算连板）：

| 字段 | 类型 | 必须 | 说明 |
|---|---|---|---|
| trade_date | date/string | 是 | 交易日期 |
| ts_code | string | 是 | 股票代码（带 .SH/.SZ/.BJ 后缀） |
| close | float | 是 | 收盘价 |
| pre_close | float | 是 | 前收盘价 |
| open/high/low | float | 否 | 缺则一字板/炸板代理退化 |
| amount | float | 否 | 成交额（梯队排序用） |
| limit_up | float | 否 | 交易所涨停价；缺则按板块 10/20/30% 兜底 |
| trade_status | int | 否 | 0=正常，非 0=停牌 |
| name | string | 否 | 含 ST 影响兜底涨停幅度 |

## 输出

标准化涨停池面板（BUILD 生产规则 §11 兼容）。主键 `(trade_date, build_id, target_id, result_type)`。

| 字段 | 类型 | 说明 |
|---|---|---|
| trade_date | string | 结果所属交易日 |
| build_id / build_name | string | `B6` / `涨停池动态管理` |
| target_id / ts_code | string | 股票代码 |
| result_type | string | `limitup_pool`（个股行）/ `limitup_sentiment`（每日 1 行 MARKET 情绪面） |
| result_value | string | 核心结果 = 板级标签（首板 / N连板 / 炸板未封）；MARKET 行为 `N涨停/M板` |
| result_json | string | 全状态 JSON（板块/连板/首板/状态/炸板/首封回封/质量/题材/形态…）；MARKET 行为情绪面 dict |
| board_label / limit_up_streak / is_first_board | 见上 | 连板状态 |
| blow_up_count / reseal_count / first_seal_time / final_seal_time | 见上 | 封板质量 |
| special_pattern | string | 特殊形态（地天板/天地板/一字板/秒板/烂板/反复板/实封…） |
| lead_concept / concept_board_count / is_concept_leader / concepts | string/int/bool/json | 题材分组 |
| pool_status / seal_quality / seal_metric_source | string | 动态状态/质量/数据源 |
| data_version / update_time | string | 版本与生成时间 |

> 读取时按 `result_type` 区分：个股池 `df[df.result_type=="limitup_pool"]`，情绪面 `df[df.result_type=="limitup_sentiment"]`。

## 调用方式（支持多种模式，可交互选择）

```python
# 模式 A：调用型——调用方已有标准日线（含回看窗口），即时算当日涨停池
from scripts.build import run
pool = run(daily_records, config={"target_date": "2026-06-19"})

# 模式 B：每日维护——自动拉数据（含涨停股分钟线，失败自动降级日线代理）
from scripts.build import maintain_daily, save_parquet
pool = maintain_daily("2026-06-19", with_minute=True)   # indicator="" 全A
save_parquet(pool)                                       # 追加合并进 production/database.parquet

# 模式 C：区间回填历史涨停池（默认不拉分钟线，省流量）
from scripts.build import backfill, save_parquet
hist = backfill("2026-01-01", "2026-06-19")
save_parquet(hist)

# 输出展示（可多选）
from scripts.render import render_markdown          # 动态多维表格（连板梯队/炸板回封/状态机）
from scripts.render_html import render_html         # 暗色玻璃单文件 HTML 看板
md = render_markdown(pool)
html = render_html(pool)
```

命令行：

```bash
export PANDA_USERNAME=<86手机号>; export PANDA_PASSWORD=<密码>
python scripts/build.py --mode daily --date 20260619              # 当日维护（含分钟）
python scripts/build.py --mode daily --date 20260619 --no-minute  # 仅日线代理（省流量）
python scripts/build.py --mode backfill --start 20260101 --end 20260619
python scripts/render.py --date 2026-06-19                        # 打印多维表格
python scripts/render_html.py --date 2026-06-19 --out pool.html   # 生成 HTML 看板
```

## Agent 执行规则（含 Q&A / 提问整理）

1. 若调用方已传标准日线，优先 `run(input_data, config=...)`，不重复拉数据。
2. 每日生产维护用 `maintain_daily()`，结果 `save_parquet()` 落地，供他人直接读，不重算。
3. **Agent 问答 / 提问整理**：读取 `database.parquet`，先按 `result_type` 拆分，再用 pandas 回答：
   - 「今天最高几板/炸板率/晋级率/赚钱效应」→ 读 MARKET 行 `df[df.result_type=="limitup_sentiment"]` 的 `result_json`（含 `max_height`/`market_blow_rate`/`promote_rate_by_tier`/`prev_limitup_premium`）
   - 「哪些票炸板了」→ `df[df.blow_up_count > 0]`
   - 「今天哪个题材最强/题材龙头是谁」→ 按 `lead_concept` 分组取 `concept_board_count` 最大；`is_concept_leader==True` 即龙头
   - 「有没有天地板/烂板」→ `df[df.special_pattern.isin(["天地板","烂板"])]`
   - 「连板梯队 / 题材视图」→ 直接 `render_markdown(df)`
   回答时务必带上「封板数据源」（minute / daily_proxy）以说明精度。
4. 必须先 `python scripts/test.py` 全绿；真实数据因流量超限/权限不足会自动跳过（不判失败）。

## 成功标准

- `result_value` 为板级标签；`result_json` 可解析且含 `blow_up_count`/`first_seal_time` 等。
- `limit_up_streak` 单调正确（连板累加、断板归零、停牌不断板）。
- 分钟可用时 `seal_metric_source="minute"` 且炸板次数/回封时间非空；不可用时自动 `daily_proxy`，主流程不中断。

## 可被 Alpha 调用

- 是
- 调用限制：输入须含 `trade_date/ts_code/close/pre_close`，且区间需覆盖回看窗口（算 N 连板需 ≥N 个交易日历史）
- 依赖数据：日频行情（必）+ 涨停股 1m 分钟线（选，用于精确炸板/回封）

## 是否需要生产结果

- 是否生成 `database.parquet`：是（结果型，盘后统一计算，多人复用）
- 更新频率：每日收盘后 `maintain_daily()` 追加
- 字段结构：见 `../生产产物/SKILL.md`

## 依赖

- panda_data ≥ 0.0.9（默认网关 pandadata.pandaaiquant.com）
- pandas、numpy、pyarrow（写 parquet）
- 凭证环境变量：`PANDA_USERNAME` / `PANDA_PASSWORD`（兼容 `PANDA_DATA_*`）
- 数据源不明确时，先咨询项目工作人员（见 BUILD开发与生产规则V2.md §1）
