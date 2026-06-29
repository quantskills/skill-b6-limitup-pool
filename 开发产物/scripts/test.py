#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BUILD-B6 涨停池动态管理 —— 测试脚本
覆盖：正常输入 / 空输入 / 缺字段 / 连板状态机 / 炸板（日线代理）/ 分钟级炸板回封 /
     动态状态机 / 渲染 / 真实数据（流量超限自动跳过）。
运行：python scripts/test.py
"""
from __future__ import annotations

import json

import pandas as pd

from build import (
    assemble_pool, backfill, board_type_of, build_streak, classify_special_pattern,
    compute_seal_metrics, compute_sentiment, limit_rate_of, maintain_daily, run,
    tag_concepts, to_standard_output, validate_input,
)
from render import render_markdown
from render_html import render_html


# ---------- 合成数据构造 ----------
def _daily_rows():
    """构造 3 只票 × 5 个交易日的日线：
       A: 连续 3 天主板涨停（首板→2连板→3连板）
       B: 创业板，前 2 天涨停后第 3 天炸板未封（日内摸板）
       C: 主板普通票，不涨停
    """
    dates = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"]
    rows = []
    # A 主板 10%：每日 close=limit_up，连板
    pa = 10.0
    for i, dt in enumerate(dates):
        lu = round(pa * 1.10, 2)
        is_lu = i <= 2  # 前 3 天涨停
        close = lu if is_lu else round(pa * 1.02, 2)
        rows.append(dict(trade_date=dt, ts_code="600000.SH", name="测试A", open=round(pa*1.03,2),
                         close=close, high=lu, low=round(pa*1.0,2), volume=1e6, amount=1e8,
                         pre_close=pa, limit_up=lu, limit_down=round(pa*0.9,2), trade_status=0))
        pa = close
    # B 创业板 20%：前 2 天涨停，第 3 天盘中摸板炸板未封（high=板, low 远离, close<板）
    pb = 20.0
    for i, dt in enumerate(dates):
        lu = round(pb * 1.20, 2)
        if i <= 1:
            close, high, low = lu, lu, round(pb*1.05,2)
        elif i == 2:
            close, high, low = round(pb*1.05,2), lu, round(pb*0.98,2)  # 摸板未封
        else:
            close, high, low = round(pb*1.0,2), round(pb*1.03,2), round(pb*0.97,2)
        rows.append(dict(trade_date=dt, ts_code="300001.SZ", name="测试B", open=round(pb*1.06,2),
                         close=close, high=high, low=low, volume=2e6, amount=2e8,
                         pre_close=pb, limit_up=lu, limit_down=round(pb*0.8,2), trade_status=0))
        pb = close
    # C 主板普通
    pc = 15.0
    for dt in dates:
        rows.append(dict(trade_date=dt, ts_code="600001.SH", name="测试C", open=pc, close=round(pc*1.01,2),
                         high=round(pc*1.02,2), low=round(pc*0.99,2), volume=5e5, amount=5e7,
                         pre_close=pc, limit_up=round(pc*1.10,2), limit_down=round(pc*0.9,2), trade_status=0))
        pc = round(pc*1.01,2)
    return rows


# ---------- 单元测试 ----------
def test_board_type_and_rate():
    assert board_type_of("600000.SH") == "沪主板"
    assert board_type_of("300001.SZ") == "创业板"
    assert board_type_of("688001.SH") == "科创板"
    assert board_type_of("830001.BJ") == "北交所"
    assert limit_rate_of("600000.SH") == 0.10
    assert limit_rate_of("300001.SZ") == 0.20
    assert limit_rate_of("830001.BJ") == 0.30
    assert limit_rate_of("600000.SH", "ST测试") == 0.05
    print("✅ test_board_type_and_rate")


def test_streak_machine():
    streak = build_streak(pd.DataFrame(_daily_rows()))
    a = streak[(streak["ts_code"] == "600000.SH")].sort_values("trade_date")
    assert list(a["limit_up_streak"]) == [1, 2, 3, 0, 0], list(a["limit_up_streak"])
    assert bool(a.iloc[0]["is_one_word"]) is False  # open<板，不是一字
    print("✅ test_streak_machine（首板→2→3连板，断板归零）")


def test_run_pool_and_status():
    out = run(_daily_rows(), config={"target_date": "2026-06-17"})
    assert not out.empty
    # 2026-06-17：A 应为 3连板·晋级；B 摸板未封
    a = out[out["ts_code"] == "600000.SH"].iloc[0]
    assert a["board_label"] == "3连板", a["board_label"]
    assert a["pool_status"] in ("晋级",), a["pool_status"]
    assert a["is_limit_up_close"]
    b = out[out["ts_code"] == "300001.SZ"]
    assert len(b) == 1 and not bool(b.iloc[0]["is_limit_up_close"])  # 炸板未封进池
    assert b.iloc[0]["board_label"] == "炸板未封"
    # 标准列 + result_json 可解析
    assert {"trade_date", "build_id", "target_id", "result_type", "result_value", "result_json"} <= set(out.columns)
    json.loads(out.iloc[0]["result_json"])
    print("✅ test_run_pool_and_status（晋级/炸板未封 + 标准列 + JSON 可解析）")


def test_first_board_status():
    out = run(_daily_rows(), config={"target_date": "2026-06-15"})
    a = out[out["ts_code"] == "600000.SH"].iloc[0]
    assert a["board_label"] == "首板" and a["is_first_board"]
    assert a["pool_status"] == "新晋首板", a["pool_status"]
    print("✅ test_first_board_status（首板=新晋首板）")


def test_minute_seal_metrics():
    """构造分钟线：某票 9:40 首封 → 10:00 炸开 → 10:30 回封，应得 blow_up=1, reseal=1。"""
    pool = run(_daily_rows(), config={"target_date": "2026-06-15"})
    target = pool[pool["ts_code"] == "600000.SH"].copy()
    # build_streak 给 eff_limit_up；这里从 pool 的 eff_limit_up 复原
    lu = 11.0  # A 首日 limit_up
    mins = []
    times = pd.date_range("2026-06-15 09:30", "2026-06-15 11:00", freq="1min")
    for i, t in enumerate(times):
        hhmm = t.strftime("%H:%M")
        # 9:40 起封；10:00-10:05 炸开；10:30 起回封
        if hhmm < "09:40":
            high = lu * 0.99
        elif "10:00" <= hhmm <= "10:05":
            high = lu * 0.985
        elif hhmm >= "10:30":
            high = lu
        else:
            high = lu
        mins.append(dict(ts_code="600000.SH", trade_date=pd.Timestamp("2026-06-15"),
                         datetime=t, high=round(high, 2), close=round(high, 2),
                         volume=1000, amount=10000, minute_idx=i + 1))
    minute = pd.DataFrame(mins)
    # 需要 eff_limit_up / touched_limit / is_limit_up_close / is_one_word 列
    streak = build_streak(pd.DataFrame(_daily_rows()))
    base = streak[(streak.ts_code == "600000.SH") & (streak.trade_date == "2026-06-15")].copy()
    base["prev_streak"] = 0
    metr = compute_seal_metrics(base, minute)
    r = metr.iloc[0]
    assert r["seal_metric_source"] == "minute", r["seal_metric_source"]
    assert int(r["blow_up_count"]) == 1, r["blow_up_count"]
    assert int(r["reseal_count"]) == 1, r["reseal_count"]
    assert r["first_seal_time"] == "09:40", r["first_seal_time"]
    assert r["final_seal_time"] == "11:00", r["final_seal_time"]
    print("✅ test_minute_seal_metrics（首封09:40 / 炸1次 / 回封1次）")


def test_empty_input():
    try:
        run([])
    except ValueError as e:
        assert "空表" in str(e)
        print("✅ test_empty_input")
        return
    raise AssertionError("空输入必须抛 ValueError")


def test_missing_column():
    try:
        run([{"trade_date": "2026-06-17", "ts_code": "600000.SH"}])
    except ValueError as e:
        assert "缺少必要字段" in str(e)
        print("✅ test_missing_column")
        return
    raise AssertionError("缺字段必须抛 ValueError")


def test_render():
    out = run(_daily_rows(), config={"target_date": "2026-06-17"})
    md = render_markdown(out)
    assert "连板梯队" in md and "3连板" in md
    htmls = render_html(out)
    assert "<html" in htmls and "涨停池" in htmls
    print("✅ test_render（Markdown + HTML 渲染）")


def _concepts_df():
    """概念成分：题材X含A+B(梯队2家)，题材Y只含C。in_date 早于测试区间。"""
    return pd.DataFrame([
        {"concept": "题材X", "ts_code": "600000.SH", "in_date": pd.Timestamp("2026-01-01")},
        {"concept": "题材X", "ts_code": "300001.SZ", "in_date": pd.Timestamp("2026-01-01")},
        {"concept": "题材Y", "ts_code": "600001.SH", "in_date": pd.Timestamp("2026-01-01")},
    ])


def test_concept_grouping():
    # 2026-06-16：A(2板) 和 B(2板) 都涨停，同属题材X → 梯队厚度 2，A/B 同为最高板=龙头
    out = run(_daily_rows(), config={"target_date": "2026-06-16", "concepts": _concepts_df()})
    stocks = out[out["result_type"] == "limitup_pool"]
    a = stocks[stocks["ts_code"] == "600000.SH"].iloc[0]
    assert a["lead_concept"] == "题材X", a["lead_concept"]
    assert int(a["concept_board_count"]) == 2, a["concept_board_count"]
    assert bool(a["is_concept_leader"]) is True
    assert "题材X" in json.loads(a["result_json"])["concepts"]
    md = render_markdown(out)
    assert "题材板块视图" in md and "题材X" in md
    print("✅ test_concept_grouping（题材X梯队2家/龙头标记/题材视图渲染）")


def test_pit_concept_filter():
    # in_date 晚于信号日 → 不应认这条概念（未来函数防护）
    future = pd.DataFrame([{"concept": "未来题材", "ts_code": "600000.SH", "in_date": pd.Timestamp("2026-12-31")}])
    out = run(_daily_rows(), config={"target_date": "2026-06-16", "concepts": future})
    a = out[out["ts_code"] == "600000.SH"].iloc[0]
    assert a["lead_concept"] is None, a["lead_concept"]
    print("✅ test_pit_concept_filter（in_date 晚于信号日被 PIT 滤除）")


def test_special_pattern():
    dates = ["2026-06-15"]
    rows = [
        # 地天板：盘中触跌停(low<=跌停9.0) 又涨停收盘(close=11)
        dict(trade_date="2026-06-15", ts_code="600000.SH", name="地天", open=9.0, close=11.0,
             high=11.0, low=9.0, volume=1e6, amount=1e8, pre_close=10.0, limit_up=11.0, limit_down=9.0, trade_status=0),
        # 天地板：盘中触涨停(high>=11) 却跌停收盘(close=9)
        dict(trade_date="2026-06-15", ts_code="600002.SH", name="天地", open=11.0, close=9.0,
             high=11.0, low=9.0, volume=1e6, amount=1e8, pre_close=10.0, limit_up=11.0, limit_down=9.0, trade_status=0),
        # 一字板：开高低收都=涨停
        dict(trade_date="2026-06-15", ts_code="600003.SH", name="一字", open=11.0, close=11.0,
             high=11.0, low=11.0, volume=1e6, amount=1e8, pre_close=10.0, limit_up=11.0, limit_down=9.0, trade_status=0),
    ]
    streak = build_streak(pd.DataFrame(rows))
    pool = assemble_pool(streak, pd.Timestamp("2026-06-15"))
    pat = pool.set_index("ts_code")["special_pattern"].to_dict()
    assert pat["600000.SH"] == "地天板", pat
    assert pat["600002.SH"] == "天地板", pat
    assert pat["600003.SH"] == "一字板", pat
    print("✅ test_special_pattern（地天板/天地板/一字板）")


def test_sentiment_row():
    out = run(_daily_rows(), config={"target_date": "2026-06-17"})
    mrow = out[out["result_type"] == "limitup_sentiment"]
    assert len(mrow) == 1, "应有 1 行市场情绪面 summary"
    s = json.loads(mrow.iloc[0]["result_json"])
    assert s["max_height"] == 3, s
    assert "2->3" in s["promote_rate_by_tier"], s["promote_rate_by_tier"]
    assert s["prev_limitup_premium"] is not None
    print(f"✅ test_sentiment_row（最高{s['max_height']}板/分层晋级率/赚钱效应{s['prev_limitup_premium']:+.1%}）")


def test_daily_proxy_clean_seal():
    """日线代理炸板口径（验收报告 B6 问题①）：低开拉板的干净涨停 blow_up 必须=0，
    不得误标炸板/反复板/烂板；盘中触板但未封住收盘仍记 1 次日线可见炸板。"""
    out = run(_daily_rows(), config={"target_date": "2026-06-17"})
    a = out[out["ts_code"] == "600000.SH"].iloc[0]   # 干净 3 连板：open<板、low 远离板、收盘封死
    ja = json.loads(a["result_json"])
    assert ja["seal_metric_source"] == "daily_proxy", ja["seal_metric_source"]
    assert int(ja["blow_up_count"]) == 0, ja["blow_up_count"]
    assert ja["seal_quality"] == "稳封", ja["seal_quality"]
    assert a["special_pattern"] not in ("炸1次回封", "反复板", "烂板"), a["special_pattern"]
    b = out[out["ts_code"] == "300001.SZ"].iloc[0]   # 摸板未封
    assert int(json.loads(b["result_json"])["blow_up_count"]) == 1
    assert b["board_label"] == "炸板未封"
    print("✅ test_daily_proxy_clean_seal（干净涨停代理炸板=0/稳封；摸板未封仍记炸1）")


def test_empty_limitup_day_market_row():
    """空涨停交易日（验收报告 B6 问题③）：当日无任何涨停/炸板，仍落 1 行 MARKET 情绪面（家数=0），
    保证情绪面时序不缺日。"""
    rows = []
    for dt in ["2026-06-15", "2026-06-16", "2026-06-17"]:
        p = 10.0
        rows.append(dict(trade_date=dt, ts_code="600000.SH", name="平票", open=p, close=round(p*1.001, 2),
                         high=round(p*1.01, 2), low=round(p*0.99, 2), volume=1e6, amount=1e8,
                         pre_close=p, limit_up=round(p*1.10, 2), limit_down=round(p*0.9, 2), trade_status=0))
    out = run(rows, config={"target_date": "2026-06-17"})
    assert len(out[out["result_type"] == "limitup_pool"]) == 0, "无涨停日不应有个股行"
    market = out[out["result_type"] == "limitup_sentiment"]
    assert len(market) == 1, "无涨停日仍应有 1 行 MARKET 情绪面"
    s = json.loads(market.iloc[0]["result_json"])
    assert s["n_limit_up"] == 0 and market.iloc[0]["trade_date"] == "2026-06-17", s
    print("✅ test_empty_limitup_day_market_row（空涨停日仍落 MARKET 家数=0，时序不缺日）")


def test_real_data_optional():
    """真实数据冒烟：流量超限/权限不足/服务异常 → 跳过（不判失败）。"""
    try:
        out = maintain_daily(with_minute=False)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if any(k in msg for k in ("500009", "单日总流量", "200103", "权限", "环境变量", "ServiceError",
                                   "504", "无法导入", "panda_data", "pip")):
            print(f"⏭️  test_real_data_optional 跳过（配额/权限/服务/凭证/未装 SDK）：{msg[:60]}")
            return
        raise
    if out.empty:
        print("⏭️  test_real_data_optional：当日涨停池为空（可能非交易日）")
        return
    assert {"trade_date", "build_id", "target_id"} <= set(out.columns)
    print(f"✅ test_real_data_optional（真实涨停池 {len(out)} 只）")


if __name__ == "__main__":
    test_board_type_and_rate()
    test_streak_machine()
    test_run_pool_and_status()
    test_first_board_status()
    test_minute_seal_metrics()
    test_empty_input()
    test_missing_column()
    test_concept_grouping()
    test_pit_concept_filter()
    test_special_pattern()
    test_sentiment_row()
    test_render()
    test_daily_proxy_clean_seal()
    test_empty_limitup_day_market_row()
    test_real_data_optional()
    print("\n🎉 全部测试通过")
