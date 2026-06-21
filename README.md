# B6 · 涨停池动态管理

> 每日维护 A 股涨停池，标记**首板 / 连板数 / 炸板次数 / 回封时间**，并做**题材分组**、
> **特殊形态**（天地板/地天板/一字/秒板/烂板）、**情绪面量化**（分层晋级率/炸板率/赚钱效应）。
> 输出动态多维表格 / 暗色 HTML 看板，支持 Agent 问答。数据源：PandaData。

---

## 一句话定位
今天哪些票涨停、几板、炸没炸、什么题材、市场情绪怎样。

## 怎么说（自然语言触发，agent 自动调脚本/读 parquet）

| 你说 | 它做什么 |
|---|---|
| "跑一下今天的涨停池" / "维护涨停池" | 拉最新交易日，出连板梯队+炸板+题材+情绪面 |
| "今天最高几板？""炸板率多少？""赚钱效应？" | 读情绪面 summary 直接答 |
| "今天哪个题材最强 / 题材龙头是谁" | 按题材梯队厚度分组，标龙头 |
| "哪些票炸板了 / 谁尾盘回封的" | 列炸板次数、首封→回封时间 |
| "有没有天地板 / 地天板 / 烂板 / 一字板" | 按特殊形态筛 |
| "首板→2板晋级率多少" | 分层晋级率 |
| "出个涨停池 HTML 看板" | 生成暗色可视化看板 |
| "复盘 6月18号的涨停池" | 指定历史某天 |
| "回填 6 月这一个月" | 区间回填历史 |

## 可选参数（不说走默认）

| 参数 | 默认 | 怎么说 |
|---|---|---|
| 日期 | 最新交易日 | "复盘 6/18" |
| 股票池 | 全A | "只看沪深300 / 中证1000 / 国证2000" |
| 精度 | 分钟精确 | "快一点别拉分钟"→日线代理（炸板/回封精度降低） |
| 输出 | 看问法 | "口头答 / 给多维表格 / 出 HTML 看板" |

## 命令行（手动跑）

```bash
export PANDA_USERNAME=<86手机号>; export PANDA_PASSWORD=<密码>
python 开发产物/scripts/build.py --mode daily                 # 当日维护（含分钟，~16:00 后）
python 开发产物/scripts/build.py --mode daily --no-minute     # 仅日线代理（省流量）
python 开发产物/scripts/build.py --mode backfill --start 20260601 --end 20260619
python 开发产物/scripts/render.py                             # 打印多维表格
python 开发产物/scripts/render_html.py --out pool.html        # HTML 看板
```

无参数 `--mode daily` 自动定位最近交易日，结果**追加合并**进 `生产产物/database.parquet`（不覆盖历史）。

## 输出在哪
- 结果落地：`生产产物/database.parquet`（个股行 `result_type=limitup_pool` + 每日 1 行情绪面 `limitup_sentiment`）
- 看板样例：`生产产物/sample_pool.html`

## 目录
```
build-b6-limitup-pool/
├── 开发产物/  SKILL.md · skill.json · references/api_guide.md · scripts/{build,render,render_html,test}.py
└── 生产产物/  SKILL.md · database.parquet
```

详细字段口径见 [开发产物/SKILL.md](开发产物/SKILL.md) 与 [开发产物/references/api_guide.md](开发产物/references/api_guide.md)。
