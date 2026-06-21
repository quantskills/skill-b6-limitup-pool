#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BUILD-B6 涨停池 —— 暗色玻璃拟态单文件 HTML 看板
================================================================
自包含 HTML（无外部依赖），可视化：
  - 顶部 KPI（涨停 / 最高板 / 炸板率 / 分层晋级率 / 昨涨停今溢价）
  - 题材板块视图（按代表题材分组）
  - 连板梯队卡片（带特殊形态）
  - 炸板回封表
"""
from __future__ import annotations

import argparse
import html as _html
import json
from pathlib import Path

import pandas as pd

POOL_TYPE = "limitup_pool"
SENTIMENT_TYPE = "limitup_sentiment"


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None and not (isinstance(s, float) and pd.isna(s)) else ""


def _split_day(panel: pd.DataFrame, date: str | None):
    panel = panel.copy()
    panel["trade_date"] = panel["trade_date"].astype(str)
    day = date or (panel["trade_date"].max() if not panel.empty else "")
    if day:
        day = pd.to_datetime(day).strftime("%Y-%m-%d")
    d = panel[panel["trade_date"] == day]
    if "result_type" in d.columns:
        stocks = d[d["result_type"] == POOL_TYPE].copy()
        srow = d[d["result_type"] == SENTIMENT_TYPE]
        sentiment = json.loads(srow.iloc[0]["result_json"]) if len(srow) else {}
    else:
        stocks, sentiment = d.copy(), {}
    return stocks, sentiment, day


def _kpis(d: pd.DataFrame, s: dict) -> list[tuple[str, str]]:
    n_lu = int(s.get("n_limit_up", int(d["is_limit_up_close"].sum())))
    n_blow = int(s.get("n_blow_open", len(d[~d["is_limit_up_close"] & d["touched_limit"]])))
    denom = n_lu + n_blow
    blow_rate = s.get("market_blow_rate", (n_blow / denom) if denom else 0.0)
    max_h = int(s.get("max_height", int(d["limit_up_streak"].max()) if not d.empty else 0))
    prem = s.get("prev_limitup_premium")
    kpis = [
        ("涨停", f"{n_lu}"), ("最高板", f"{max_h} 板"),
        ("炸板未封", f"{n_blow}"), ("炸板率", f"{blow_rate:.0%}"),
    ]
    kpis.append(("昨涨停今溢价", f"{prem:+.1%}" if prem is not None else "N/A"))
    return kpis


def _tier_bar(s: dict) -> str:
    tiers = s.get("promote_rate_by_tier") or {}
    if not tiers:
        return ""
    chips = "".join(
        f'<span class="tier">{_esc(k)} <b>{v["rate"]:.0%}</b> '
        f'<em>{v["promoted"]}/{v["base"]}</em></span>'
        for k, v in sorted(tiers.items()))
    return f'<div class="tiers"><span class="tl">分层晋级率</span>{chips}</div>'


def _pattern_cls(p) -> str:
    if p in ("一字板", "地天板", "秒板"):
        return " strong"
    if p in ("天地板", "烂板", "炸板未封", "反复板"):
        return " blow"
    return ""


def _concept_cards(d: pd.DataFrame) -> str:
    sealed = d[d["is_limit_up_close"]].copy()
    if sealed.empty or "lead_concept" not in sealed.columns or sealed["lead_concept"].isna().all():
        return '<p class="empty">无题材数据（概念接口未返回或降级）。</p>'
    g = sealed.dropna(subset=["lead_concept"]).groupby("lead_concept")
    cards = []
    for concept, grp in g:
        cnt = int(grp["concept_board_count"].max())
        if cnt < 2:
            continue
        grp = grp.sort_values("limit_up_streak", ascending=False)
        chips = []
        for _, r in grp.head(12).iterrows():
            lead = " lead" if bool(r.get("is_concept_leader")) else ""
            chips.append(f'<span class="chip{lead}">{_esc(r["name"])} '
                         f'<em>{int(r["limit_up_streak"])}板</em></span>')
        cards.append(f'<div class="ladder"><div class="ladder-head">'
                     f'<span class="bn">{_esc(concept)}</span><span class="cnt">{cnt} 家</span></div>'
                     f'<div class="chips">{"".join(chips)}</div></div>')
    return "".join(cards) if cards else '<p class="empty">当日无 ≥2 家涨停的题材梯队。</p>'


def _ladder_cards(d: pd.DataFrame) -> str:
    sealed = d[d["is_limit_up_close"]].copy()
    if sealed.empty:
        return '<p class="empty">当日无涨停收盘标的。</p>'
    cards = []
    for s in sorted(sealed["limit_up_streak"].unique(), reverse=True):
        grp = sealed[sealed["limit_up_streak"] == s].sort_values("amount", ascending=False)
        label = "首板" if s == 1 else f"{s}连板"
        chips = []
        for _, r in grp.iterrows():
            p = r.get("special_pattern")
            seal = r.get("first_seal_time") or ""
            chips.append(f'<span class="chip{_pattern_cls(p)}" title="{_esc(r["board_type"])}·{_esc(p)}">'
                         f'{_esc(r["name"])} <em>{_esc(seal)}</em></span>')
        cards.append(f'<div class="ladder"><div class="ladder-head"><span class="bn">{_esc(label)}</span>'
                     f'<span class="cnt">{len(grp)}</span></div>'
                     f'<div class="chips">{"".join(chips)}</div></div>')
    return "".join(cards)


def _blow_rows(d: pd.DataFrame) -> str:
    blow = d[(d["blow_up_count"] > 0) | (~d["is_limit_up_close"] & d["touched_limit"])].copy()
    if blow.empty:
        return '<tr><td colspan="8" class="empty">当日无炸板。</td></tr>'
    blow = blow.sort_values(["blow_up_count", "limit_up_streak"], ascending=False)
    rows = []
    for _, r in blow.iterrows():
        rows.append(
            "<tr>"
            f"<td>{_esc(r['name'])}<span class='code'>{_esc(r['ts_code'])}</span></td>"
            f"<td>{_esc(r['board_type'])}</td><td>{_esc(r['board_label'])}</td>"
            f"<td>{_esc(r.get('special_pattern') or '—')}</td>"
            f"<td class='num'>{int(r['blow_up_count'])}</td>"
            f"<td>{_esc(r.get('first_seal_time') or '—')}</td>"
            f"<td>{_esc(r.get('final_seal_time') or '—')}</td>"
            f"<td>{_esc(r['pool_status'])}</td>"
            "</tr>"
        )
    return "".join(rows)


_TPL = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>涨停池动态管理 · {day}</title>
<style>
:root{{--bg:#0b0e14;--card:rgba(255,255,255,.04);--line:rgba(255,255,255,.08);
--txt:#e6e9ef;--mut:#8b93a7;--red:#ff5d6c;--gold:#ffce6b;--green:#4fd1a6;--blue:#6ba8ff;}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(1200px 600px at 70% -10%,#161b2b,#0b0e14);
color:var(--txt);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:28px}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--mut);margin-bottom:18px;font-size:13px}}
.kpis{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:14px}}
.kpi{{flex:1;min-width:110px;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;backdrop-filter:blur(12px)}}
.kpi .v{{font-size:25px;font-weight:700}}.kpi .k{{color:var(--mut);font-size:12px;margin-top:2px}}
.tiers{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:22px}}
.tl{{color:var(--mut);font-size:12px;margin-right:4px}}
.tier{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:4px 10px;font-size:12px}}
.tier b{{color:var(--green)}}.tier em{{color:var(--mut);font-style:normal;font-size:11px}}
h2{{font-size:15px;margin:24px 0 12px;color:var(--mut);font-weight:600}}
.ladder{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px}}
.ladder-head{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
.bn{{font-weight:700;color:var(--gold)}}.cnt{{color:var(--mut);font-size:12px}}
.chips{{display:flex;flex-wrap:wrap;gap:7px}}
.chip{{background:rgba(107,168,255,.12);border:1px solid rgba(107,168,255,.25);color:var(--blue);
border-radius:9px;padding:4px 9px;font-size:12px}}
.chip.strong{{background:rgba(255,93,108,.16);border-color:rgba(255,93,108,.35);color:var(--red)}}
.chip.blow{{background:rgba(255,206,107,.12);border-color:rgba(255,206,107,.3);color:var(--gold)}}
.chip.lead{{box-shadow:0 0 0 1px var(--gold) inset;color:var(--gold)}}
.chip em{{font-style:normal;opacity:.6;font-size:11px;margin-left:3px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:14px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}}
th{{color:var(--mut);font-weight:600;background:rgba(255,255,255,.02)}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.code{{color:var(--mut);font-size:11px;margin-left:6px}}
.empty{{color:var(--mut);text-align:center;padding:18px}}
.foot{{color:var(--mut);font-size:11px;margin-top:24px;text-align:center}}
</style></head><body>
<h1>📊 涨停池动态管理</h1><div class="sub">{day} ｜ 封板数据源：{src}</div>
<div class="kpis">{kpis}</div>{tiers}
<h2>题材板块视图</h2>{concepts}
<h2>连板梯队</h2>{ladders}
<h2>炸板 &amp; 回封明细</h2>
<table><thead><tr><th>标的</th><th>板块</th><th>板级</th><th>形态</th><th>炸板</th>
<th>首封</th><th>回封时间</th><th>状态</th></tr></thead><tbody>{blows}</tbody></table>
<div class="foot">BUILD-B6 涨停池动态管理 · PandaData 驱动</div>
</body></html>"""


def render_html(panel: pd.DataFrame, date: str | None = None) -> str:
    d, s, day = _split_day(panel, date)
    if d.empty:
        return _TPL.format(day=day, src="—", kpis="", tiers="",
                           concepts='<p class="empty">当日涨停池为空。</p>',
                           ladders='<p class="empty">当日涨停池为空。</p>',
                           blows='<tr><td colspan="8" class="empty">无</td></tr>')
    src = d["seal_metric_source"].mode()
    src = "分钟精确" if (len(src) and src.iloc[0] == "minute") else "日线代理"
    kpis = "".join(f'<div class="kpi"><div class="v">{v}</div><div class="k">{k}</div></div>'
                   for k, v in _kpis(d, s))
    return _TPL.format(day=day, src=src, kpis=kpis, tiers=_tier_bar(s),
                       concepts=_concept_cards(d), ladders=_ladder_cards(d), blows=_blow_rows(d))


def main() -> None:
    ap = argparse.ArgumentParser(description="B6 涨停池暗色 HTML 看板")
    ap.add_argument("--parquet", default=str(Path(__file__).resolve().parents[2] /
                    "生产产物" / "database.parquet"))
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default="limitup_pool.html")
    args = ap.parse_args()
    panel = pd.read_parquet(args.parquet)
    Path(args.out).write_text(render_html(panel, date=args.date), encoding="utf-8")
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
