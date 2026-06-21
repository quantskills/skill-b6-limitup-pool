# B6 涨停池动态管理 —— 数据接口与字段说明

## 数据来源

正式生产使用 PandaData 数据拉取库（`panda_data` ≥ 0.0.9，默认网关
`http://pandadata.pandaaiquant.com`）。两个接口：

| 接口 | 用途 | B6 中的角色 | 实测速度（见 PandaData_实战使用经验.md） |
|---|---|---|---|
| `get_stock_daily` | 全市场日线 OHLC + `limit_up`/`limit_down`/`trade_status` | 涨停判定、连板状态机、日线炸板代理、天地板/地天板 | 全市场 1 月 ~3s（30 天/段） |
| `get_stock_min` | 涨停股 1m 分钟线 | 精确炸板次数、首封/回封时间、秒板 | 单股 1 天 ~0.7s |
| `get_concept_list` + `get_concept_constituents` | 概念列表 + 成分（遍历，带 `in_date`） | 题材分组（代表题材/梯队厚度/题材龙头） | 列表 ~0.1s；成分单概念 ~0.1s，遍历 ~300 个 |

不得使用来源不明、字段不稳定、手工整理的临时表作为正式输入。
数据源 / 字段口径 / 接口权限不明确时，先咨询项目工作人员（BUILD 规则 §1）。

## 凭证（环境变量）

```bash
export PANDA_USERNAME=<86开头手机号>      # 兼容 PANDA_DATA_USERNAME
export PANDA_PASSWORD=<密码>              # 兼容 PANDA_DATA_PASSWORD
# 可选：export PANDA_BASE_URL=...         # 覆盖默认网关
```

## 字段口径

### 板块与涨停幅度（`board_type_of` / `limit_rate_of`）

| 板块 | 代码段 | 涨停幅度（兜底用） |
|---|---|---|
| 沪主板 | 600/601/603/605.SH | 10%（ST 5%） |
| 深主板 | 000/001/002/003.SZ | 10%（ST 5%） |
| 创业板 | 300/301.SZ | 20% |
| 科创板 | 688/689.SH | 20% |
| 北交所 | *.BJ | 30% |

> `limit_rate` 仅在接口 `limit_up` 缺失时作兜底；优先用接口实际涨停价。
> 涨停池**纳入全部板块涨停股并打 `board_type` 标签**（用户口径），剔除 ST。

### 涨停判定（口径同 alpha-A3）

```text
eff_limit_up = limit_up if limit_up > 0 else round(pre_close * (1+limit_rate), 2)
is_limit_up_close = (trade_status == 0) & (close >= eff_limit_up * 0.999)
touched_limit     = (high >= eff_limit_up * 0.999) & (trade_status == 0)   # 盘中曾摸板
```

### 连板数（`limit_up_streak`）

连续涨停收盘的**交易日**数。仅在交易日序列上累加：停牌日不算断板，
复牌后接续上一交易日板数。`is_first_board = (streak == 1)`。

### 炸板次数 / 回封时间（分钟级，缺则日线代理）

分钟级（`seal_metric_source="minute"`）：

```text
sealed(t) = minute.high(t) >= eff_limit_up * 0.999
blow_up_count = #( sealed: True -> False )          # 封后被砸开的次数
first_seal_time = 第一根 sealed 分钟的时刻
final_seal_time = 最后一根 sealed 分钟的时刻        # 最终回封/封死时刻
reseal_count = #( sealed: False -> True ) - 1       # 砸开后再封的次数
```

日线代理（`seal_metric_source="daily_proxy"`，分钟不可用时）：

```text
intraday_open_back = (eff_limit_up - low) / eff_limit_up   # 日内自涨停价回落
blow_up_count ≈ 1 if (非一字 且 intraday_open_back >= 2%) else 0
first_seal_time / final_seal_time = 空（日线无法定位时刻）
```

### 动态状态机（`pool_status`，对比前一交易日）

| 状态 | 条件 |
|---|---|
| 晋级 | 今连板数 > 昨连板数（且昨日涨停） |
| 维持 | 昨日涨停、今涨停、板数未升 |
| 新晋首板 | 昨日未涨停、今首板 |
| 断板后重启 | 昨日未涨停、今 ≥2 板（断板后再起） |
| 炸板出局 | 昨日涨停、今未封住 |
| 摸板未遂 | 昨日未涨停、今摸板未封 |

## 流量管理（关键）

- `get_stock_min` 仅对**当日涨停池（含炸板未封）**的 `(ts_code, trade_date)` 拉取，
  当日池子约 50–150 只，约 1–2 分钟，流量可控。
- 分钟线连续 ≥3 次配额/服务失败（`500009`/`200103`/`ServiceError`/`504`）→ 整体降级日线代理，主流程不中断。
- 回填历史（`backfill`）默认 **不拉分钟线**（流量大）；如需精确炸板史，按日单独 `maintain_daily`。
- 错误码速查：`500009` 单日流量超限（等 0:00 重置）；`600003` 结果集超限（拆分）；
  `200103` 权限不足（套餐未开实时/分钟，自动降级）；`504` 网关超时（重试/缩段）。

## 增强维度口径（v1.1）

### 题材分组（`tag_concepts`，PIT）

```text
PIT 过滤：仅认 in_date <= 信号日 的概念成分（杜绝未来函数，口径同 A3）
concept_board_count = 该概念当日涨停收盘家数（梯队厚度，只数封住的）
lead_concept        = 该票所属概念中 concept_board_count 最大者（并列取板更高）
is_concept_leader   = 该票 limit_up_streak == 代表题材内最高板（且最高板>0）
concepts            = 该票 PIT 后所属全部概念（JSON 列表）
```

题材数据不可用（接口降级 / 配额超限）→ 上述字段留空，主流程不中断。

### 特殊形态（`classify_special_pattern`，优先级从高到低）

```text
地天板  = 盘中触及跌停(low<=eff_limit_down) 且 涨停收盘
天地板  = 盘中触及涨停(high>=eff_limit_up) 且 跌停收盘
一字板  = 开/低/收 均贴涨停
秒板    = 涨停收盘 且 首封时间 <= 09:31（需分钟）
炸板未封 = 盘中摸板但未涨停收盘
烂板    = 涨停收盘 且 (炸板≥3 次 或 (炸板≥1 且 最终回封 >= 14:30))
          注：必须炸开过(≥1)才算"尾盘回封"，否则全天稳封的 final_seal≈15:00 会误判为烂板
反复板  = 涨停收盘 且 炸板 1-2 次（非尾盘回封）
实封    = 涨停收盘 且 炸板 0 次（非一字非秒）
```

### 情绪面（`compute_sentiment`，每日 1 行 MARKET summary）

```text
market_blow_rate     = 炸板未封数 / (涨停收盘数 + 炸板未封数)
max_height           = 当日最高连板数（空间板）
promote_rate_by_tier = {N->N+1: 今日(N+1)板且昨为N板数 / 昨日N板数}   # 分层晋级率
prev_limitup_premium = 昨日涨停股今日涨幅均值（赚钱效应）
```

落地为 `target_id=MARKET` / `result_type=limitup_sentiment` 的一行，`result_json` 存完整 dict。

## 异常处理

- 输入为空 / 缺 `trade_date/ts_code/close/pre_close` → 抛明确 `ValueError`。
- 当日无涨停 → 返回空表，`check_quality` 提示「涨停池为空」。
- 分钟线 / 概念等辅助数据缺失 → 优雅降级，不中断主流程。
- `result_json` 必须为合法 JSON（`check_quality` 校验）。
