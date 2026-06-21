#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BUILD-B6 涨停池 —— 动态多维表格渲染（Markdown）
================================================================
把 build.py 产出的标准涨停池面板渲染成可直接贴给用户的多维表格：
  0. 情绪面概览（涨停家数 / 最高板 / 炸板率 / 分层晋级率 / 昨涨停今溢价=赚钱效应）
  1. 题材板块视图（按代表题材分组，梯队最厚在前，标题材龙头）
  2. 连板梯队（按 limit_up_streak 分层，高度板在前，带特殊形态）
  3. 炸板 & 回封明细
  4. 动态状态机（晋级 / 新晋首板 / 炸板出局）

输入：build.run(...) / maintain_daily(...) 返回的 DataFrame（含 1 行 MARKET 情绪面），
或读自 database.parquet。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

POOL_TYPE = "limitup_pool"
SENTIMENT_TYPE = "limitup_sentiment"


def _split_day(panel: pd.DataFrame, date: str | None):
    """返回 (当日个股池 DataFrame, 情绪面 dict, 日期字符串)。"""
    if panel.empty:
        return panel, {}, ""
    panel = panel.copy()
    panel["trade_date"] = panel["trade_date"].astype(str)
    day = date or panel["trade_date"].max()
    day = pd.to_datetime(day).strftime("%Y-%m-%d")
    d = panel[panel["trade_date"] == day]
    if "result_type" in d.columns:
        stocks = d[d["result_type"] == POOL_TYPE].copy()
        srow = d[d["result_type"] == SENTIMENT_TYPE]
        sentiment = json.loads(srow.iloc[0]["result_json"]) if len(srow) else {}
    else:
        stocks, sentiment = d.copy(), {}
    return stocks, sentiment, day


def _overview(d: pd.DataFrame, s: dict) -> str:
    n_lu = int(s.get("n_limit_up", int(d["is_limit_up_close"].sum())))
    n_blow = int(s.get("n_blow_open", len(d[~d["is_limit_up_close"] & d["touched_limit"]])))
    blow_rate = s.get("market_blow_rate")
    if blow_rate is None:
        denom = n_lu + n_blow
        blow_rate = (n_blow / denom) if denom else 0.0
    max_h = int(s.get("max_height", int(d["limit_up_streak"].max()) if not d.empty else 0))
    n_first = int(s.get("n_first_board", int((d["limit_up_streak"] == 1).sum())))
    n_lian = int(s.get("n_lianban", int((d["limit_up_streak"] >= 2).sum())))
    src = d["seal_metric_source"].mode()
    src_label = "分钟精确" if (len(src) and src.iloc[0] == "minute") else "日线代理"

    line1 = (f"**涨停 {n_lu} 家** ｜ 炸板未封 {n_blow} ｜ 炸板率 {blow_rate:.0%} ｜ "
             f"最高 {max_h} 板 ｜ 首板 {n_first} ｜ 连板 {n_lian} ｜ 封板数据：{src_label}")

    prem = s.get("prev_limitup_premium")
    prem_txt = f"昨涨停今溢价（赚钱效应）**{prem:+.1%}**" if prem is not None else "赚钱效应 N/A"
    tiers = s.get("promote_rate_by_tier") or {}
    tier_txt = " ｜ ".join(f"{k} 晋级 {v['rate']:.0%}({v['promoted']}/{v['base']})"
                          for k, v in sorted(tiers.items())) if tiers else "晋级率 N/A"
    return f"{line1}\n>\n> {prem_txt} ｜ {tier_txt}"


def _pattern_tag(r) -> str:
    p = r.get("special_pattern")
    return "" if p in (None, "实封", "未涨停") else f"·{p}"


def _concept_table(d: pd.DataFrame) -> str:
    sealed = d[d["is_limit_up_close"]].copy()
    if sealed.empty or "lead_concept" not in sealed.columns or sealed["lead_concept"].isna().all():
        return "_无题材数据（概念接口未返回或降级）。_"
    g = sealed.dropna(subset=["lead_concept"]).groupby("lead_concept")
    rows = []
    for concept, grp in g:
        cnt = int(grp["concept_board_count"].max())
        grp = grp.sort_values("limit_up_streak", ascending=False)
        leader = grp[grp["is_concept_leader"]]
        leader_name = leader.iloc[0]["name"] if len(leader) else grp.iloc[0]["name"]
        leader_streak = int((leader.iloc[0]["limit_up_streak"] if len(leader) else grp.iloc[0]["limit_up_streak"]))
        members = "，".join(f"{x['name']}({int(x['limit_up_streak'])}板)" for _, x in grp.head(8).iterrows())
        rows.append((cnt, concept, leader_name, leader_streak, len(grp), members))
    rows.sort(key=lambda x: x[0], reverse=True)
    lines = ["| 题材 | 涨停家数 | 题材龙头 | 梯队成员 |", "|---|---:|---|---|"]
    for cnt, concept, lname, lstreak, n, members in rows:
        if cnt < 2:  # 只列梯队（≥2 家涨停）的题材，单票不算板块效应
            continue
        lines.append(f"| **{concept}** | {cnt} | {lname}({lstreak}板) | {members} |")
    return "\n".join(lines) if len(lines) > 2 else "_当日无 ≥2 家涨停的题材梯队（个股分散）。_"


def _ladder_table(d: pd.DataFrame) -> str:
    sealed = d[d["is_limit_up_close"]].copy()
    if sealed.empty:
        return "_当日无涨停收盘标的。_"
    lines = ["| 板级 | 数量 | 标的（板块·形态·首封→回封） |", "|---|---|---|"]
    for s in sorted(sealed["limit_up_streak"].unique(), reverse=True):
        grp = sealed[sealed["limit_up_streak"] == s].sort_values("amount", ascending=False)
        label = "首板" if s == 1 else f"{s}连板"
        items = []
        for _, r in grp.iterrows():
            seal = ""
            if pd.notna(r.get("first_seal_time")):
                seal = f"·{r['first_seal_time']}"
                if pd.notna(r.get("final_seal_time")) and r.get("final_seal_time") != r.get("first_seal_time"):
                    seal += f"→{r['final_seal_time']}"
            items.append(f"{r['name']}({r['ts_code']},{r['board_type']}{_pattern_tag(r)}{seal})")
        lines.append(f"| **{label}** | {len(grp)} | {'，'.join(items)} |")
    return "\n".join(lines)


def _blowup_table(d: pd.DataFrame) -> str:
    blow = d[(d["blow_up_count"] > 0) | (~d["is_limit_up_close"] & d["touched_limit"])].copy()
    if blow.empty:
        return "_当日无炸板。_"
    blow = blow.sort_values(["blow_up_count", "limit_up_streak"], ascending=False)
    lines = [
        "| 标的 | 板块 | 板级 | 形态 | 炸板 | 回封 | 首封 | 回封时间 | 状态 |",
        "|---|---|---|---|---:|---:|---|---|---|",
    ]
    for _, r in blow.iterrows():
        lines.append(
            f"| {r['name']}({r['ts_code']}) | {r['board_type']} | {r['board_label']} | "
            f"{r.get('special_pattern') or '—'} | {int(r['blow_up_count'])} | {int(r['reseal_count'])} | "
            f"{r.get('first_seal_time') or '—'} | {r.get('final_seal_time') or '—'} | {r['pool_status']} |"
        )
    return "\n".join(lines)


def _status_table(d: pd.DataFrame) -> str:
    order = ["晋级", "维持", "新晋首板", "断板后重启", "炸板出局", "摸板未遂"]
    counts = d["pool_status"].value_counts().to_dict()
    rows = [(s, counts.get(s, 0)) for s in order if counts.get(s, 0) > 0]
    for s, c in counts.items():
        if s not in order:
            rows.append((s, c))
    if not rows:
        return ""
    lines = ["| 状态 | 数量 | 代表标的 |", "|---|---:|---|"]
    for s, c in rows:
        grp = d[d["pool_status"] == s].sort_values("limit_up_streak", ascending=False)
        names = "，".join(f"{r['name']}({int(r['limit_up_streak'])}板)" for _, r in grp.head(6).iterrows())
        lines.append(f"| {s} | {c} | {names} |")
    return "\n".join(lines)


def render_markdown(panel: pd.DataFrame, date: str | None = None) -> str:
    d, s, day = _split_day(panel, date)
    if d.empty:
        return f"# 涨停池 {day or ''}\n\n_当日涨停池为空。_\n"
    parts = [
        f"# 📊 涨停池动态管理 · {day}",
        "",
        f"> {_overview(d, s)}",
        "",
        "## 一、题材板块视图",
        "",
        _concept_table(d),
        "",
        "## 二、连板梯队",
        "",
        _ladder_table(d),
        "",
        "## 三、炸板 & 回封明细",
        "",
        _blowup_table(d),
        "",
        "## 四、动态状态机（vs 昨日涨停池）",
        "",
        _status_table(d),
        "",
        "---",
        "_BUILD-B6 涨停池动态管理 · 封板数据源见概览（分钟精确 / 日线代理）_",
    ]
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="B6 涨停池 Markdown 多维表格渲染")
    ap.add_argument("--parquet", default=str(Path(__file__).resolve().parents[2] /
                    "生产产物" / "database.parquet"))
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=None, help="输出 .md 文件路径（默认打印到 stdout）")
    args = ap.parse_args()
    panel = pd.read_parquet(args.parquet)
    md = render_markdown(panel, date=args.date)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"已写出 {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
