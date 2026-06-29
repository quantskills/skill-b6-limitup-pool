#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BUILD-B6: 涨停池动态管理
================================================================
工具定位（见 BUILD开发与生产规则V2.md §3）：监控预警型 + 数据处理型混合 BUILD。

核心能力：
  1. 每日维护涨停池：拉当日（含回看窗口算连板）行情，圈出所有触及涨停的标的。
  2. 标记四件套：
       - 首板          (is_first_board / board_label="首板")
       - 连板数        (limit_up_streak：连续涨停收盘的交易日数)
       - 炸板次数      (blow_up_count：日内封板后被砸开的次数，分钟级；缺分钟用日线代理)
       - 回封时间      (final_seal_time：最后一次重新封死的时刻；首封 first_seal_time)
  3. 动态管理：对比昨日涨停池，给出 晋级 / 新晋首板 / 炸板未封 / 淘汰 状态机。
  4. 输出涨停池状态：标准 run() 即时调用 + parquet 落地，配 render（多维表格 / HTML）。

数据源：PandaData（panda_data ≥ 0.0.9）。凭证用环境变量
  PANDA_USERNAME / PANDA_PASSWORD（兼容 PANDA_DATA_USERNAME / PANDA_DATA_PASSWORD）。
分钟线优雅降级：拉不到（流量超限 / 套餐未开 / 服务异常）时，炸板次数用日线回撤代理，
回封时间字段置空，主流程不中断。

复用说明：涨停判定 / 连板状态机 / 一字板口径 / 分钟首封·炸板口径，
与 alpha-A3（量枢院/alpha-a3-streak-leader-relay）保持同一事实来源，避免口径漂移。
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# ============================================================
# 常量 & 默认配置
# ============================================================
BUILD_ID = "B6"
BUILD_NAME = "涨停池动态管理"
RESULT_TYPE = "limitup_pool"
DEFAULT_DATA_VERSION = "pandadata-limitup-pool-v1"

# 回看窗口：算 N 连板至少需要 N 个交易日历史；默认 45 自然日 ≈ 30 交易日，覆盖到 ~30 连板
LOOKBACK_CALENDAR_DAYS = int(os.getenv("B6_LOOKBACK_DAYS", "45"))
DAILY_CHUNK_DAYS = int(os.getenv("B6_DAILY_CHUNK_DAYS", "30"))
LIMIT_TOL = 0.999  # 贴板容差（接口价偶有 0.01 抖动）

# 涨停池行的标准必填字段（run 输入校验用）
REQUIRED_COLUMNS = {"trade_date", "ts_code", "close", "pre_close"}

# 输出目录（生产 parquet 落地点）：scripts -> 开发产物 -> build-b6-limitup-pool -> 生产产物
DEV_ROOT = Path(__file__).resolve().parents[1]            # 开发产物
BUILD_ROOT = DEV_ROOT.parent                              # build-b6-limitup-pool
DEFAULT_OUT = BUILD_ROOT / "生产产物" / "database.parquet"


# ============================================================
# 通用工具
# ============================================================
def _compact(s: Any) -> str:
    return str(s).strip().replace("-", "").replace("/", "")[:8]


def _norm_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.astype(str).str.replace(r"\.0$", "", regex=True), errors="coerce")


def _is_quota_or_service_error(exc: Exception) -> bool:
    t = str(exc)
    return any(k in t for k in ("500009", "单日总流量超限", "200103", "权限不足",
                                 "ServiceError", "空 detail", "504", "Gateway Time-out"))


def _is_retryable(exc: Exception) -> bool:
    t = str(exc)
    return any(k in t for k in ("600003", "超过套餐限额", "查询结果为空", "429", "503", "504", "Gateway Time-out"))


def board_type_of(ts_code: str) -> str:
    """按交易所代码段判定板块。"""
    c = str(ts_code).upper()
    if c.endswith(".BJ"):
        return "北交所"
    if c.startswith(("688", "689")) and c.endswith(".SH"):
        return "科创板"
    if c.startswith(("300", "301")) and c.endswith(".SZ"):
        return "创业板"
    if c.startswith(("600", "601", "603", "605")) and c.endswith(".SH"):
        return "沪主板"
    if c.startswith(("000", "001", "002", "003")) and c.endswith(".SZ"):
        return "深主板"
    return "其他"


def limit_rate_of(ts_code: str, name: str = "") -> float:
    """涨停幅度（仅作 limit_up 缺失时的兜底；优先用接口 limit_up）。
    主板 10%、创业板/科创 20%、北交 30%、主板 ST 5%。"""
    bt = board_type_of(ts_code)
    is_st = "ST" in str(name).upper()
    if bt in ("创业板", "科创板"):
        return 0.20
    if bt == "北交所":
        return 0.30
    # 主板 / 其他
    return 0.05 if is_st else 0.10


def _consecutive_true_streak(flags: pd.Series) -> pd.Series:
    """连续 True 计数：[F,T,T,F,T] -> [0,1,2,0,1]。"""
    f = flags.fillna(False).astype(bool)
    grp = f.ne(f.shift(fill_value=False)).cumsum()
    streak = f.groupby(grp).cumcount().add(1)
    return streak.where(f, 0).astype(int)


# ============================================================
# 数据层（PandaData 封装）
# ============================================================
def init_panda() -> Any:
    try:
        import panda_data
    except ModuleNotFoundError as exc:
        raise RuntimeError("无法导入 panda_data，请先 `pip install --upgrade panda_data`（需 ≥0.0.9）") from exc
    user = os.getenv("PANDA_USERNAME") or os.getenv("PANDA_DATA_USERNAME")
    pwd = os.getenv("PANDA_PASSWORD") or os.getenv("PANDA_DATA_PASSWORD")
    if not (user and pwd):
        raise RuntimeError("缺少 PANDA_USERNAME / PANDA_PASSWORD 环境变量")
    base_url = os.getenv("PANDA_BASE_URL")
    if base_url:
        panda_data.init_token(username=user, password=pwd, base_url=base_url)
    else:
        panda_data.init_token(username=user, password=pwd)
    return panda_data


def resolve_trade_window(end_date: str, pd_api: Any) -> tuple[str, str, str]:
    """给定目标日 end_date，返回 (start, end, 实际最新交易日)。
    end_date 非交易日时，用交易日历退到 <= end_date 的最近交易日。"""
    end_c = _compact(end_date)
    last = end_c
    try:
        cal_start = _compact((pd.to_datetime(end_c) - timedelta(days=15)).strftime("%Y%m%d"))
        cal = pd_api.get_trade_cal(start_date=cal_start, end_date=end_c, is_trading_day=1)
        if cal is not None and not cal.empty:
            col = "date" if "date" in cal.columns else cal.columns[0]
            days = sorted(_compact(x) for x in cal[col].tolist() if _compact(x) <= end_c)
            if days:
                last = days[-1]
    except Exception:
        pass
    start_c = _compact((pd.to_datetime(last) - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y%m%d"))
    return start_c, last, last


def _fetch_daily_once(pd_api: Any, start: str, end: str, indicator: str = "") -> pd.DataFrame:
    return pd_api.get_stock_daily(
        start_date=_compact(start), end_date=_compact(end),
        symbol=[], fields=[], st=False, indicator=indicator,
    )


def load_daily(start: str, end: str, pd_api: Any | None = None, indicator: str = "") -> pd.DataFrame:
    """全市场日线，分段拉取 + 超限重试。字段口径与 A3 load_daily 一致。"""
    pd_api = pd_api or init_panda()
    frames, cur = [], pd.to_datetime(_compact(start)).date()
    end_d = pd.to_datetime(_compact(end)).date()
    while cur <= end_d:
        seg_end = min(cur + timedelta(days=DAILY_CHUNK_DAYS - 1), end_d)
        try:
            f = _fetch_daily_once(pd_api, cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"), indicator)
        except Exception as exc:  # noqa: BLE001
            if _is_retryable(exc) and DAILY_CHUNK_DAYS > 7:
                sub, f_list = cur, []
                while sub <= seg_end:
                    se = min(sub + timedelta(days=6), seg_end)
                    try:
                        ff = _fetch_daily_once(pd_api, sub.strftime("%Y%m%d"), se.strftime("%Y%m%d"), indicator)
                        if ff is not None and not ff.empty:
                            f_list.append(ff)
                    except Exception:
                        pass
                    sub = se + timedelta(days=1)
                f = pd.concat(f_list, ignore_index=True) if f_list else pd.DataFrame()
            else:
                raise
        if f is not None and not f.empty:
            frames.append(f)
        cur = seg_end + timedelta(days=1)
    if not frames:
        raise ValueError("get_stock_daily 未返回任何行情")
    df = pd.concat(frames, ignore_index=True)
    return _normalize_daily(df)


def _normalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """把 PandaData / 调用方传入的日线统一成标准列。"""
    df = df.rename(columns={"symbol": "ts_code", "date": "trade_date"})
    keep = ["trade_date", "ts_code", "name", "open", "close", "high", "low",
            "volume", "amount", "pre_close", "limit_up", "limit_down", "trade_status"]
    for c in keep:
        if c not in df.columns:
            df[c] = np.nan
    df = df[keep].copy()
    df["trade_date"] = _norm_date(df["trade_date"])
    for c in ["open", "close", "high", "low", "volume", "amount", "pre_close", "limit_up", "limit_down"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_status"] = pd.to_numeric(df["trade_status"], errors="coerce").fillna(0).astype(int)
    df["name"] = df["name"].fillna("").astype(str)
    df = df.dropna(subset=["trade_date", "ts_code", "close"])
    return df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def load_minute_for_pool(pool_pairs: pd.DataFrame, pd_api: Any | None = None) -> Optional[pd.DataFrame]:
    """对涨停池（含炸板）的 (ts_code, trade_date) 拉 1m 分钟线。
    pool_pairs: 必含 ts_code, trade_date。失败 / 流量超限 → 返回 None（上游降级到日线代理）。"""
    if pool_pairs is None or pool_pairs.empty:
        return None
    try:
        pd_api = pd_api or init_panda()
        if not hasattr(pd_api, "get_stock_min"):
            print("  [warn] SDK 无 get_stock_min，炸板次数/回封时间用日线代理")
            return None
        pairs = pool_pairs[["ts_code", "trade_date"]].drop_duplicates()
        print(f"  [info] 分钟线：对 {len(pairs)} 个 (股票×日) 拉 1m ...")
        frames, n_done, n_quota_fail = [], 0, 0
        for _, row in pairs.iterrows():
            d = pd.Timestamp(row["trade_date"]).strftime("%Y%m%d")
            try:
                f = pd_api.get_stock_min(
                    symbol=row["ts_code"], start_date=d, end_date=d,
                    fields=["symbol", "date", "datetime", "high", "close", "volume", "amount"],
                    frequency="1m",
                )
                if f is not None and not f.empty:
                    frames.append(f)
            except Exception as exc:  # noqa: BLE001
                if _is_quota_or_service_error(exc):
                    n_quota_fail += 1
                    if n_quota_fail >= 3:  # 连续配额/服务失败，整体降级
                        print(f"  [warn] 分钟线连续 {n_quota_fail} 次配额/服务失败，整体降级到日线代理")
                        return None
                continue
            n_done += 1
            if n_done % 50 == 0:
                print(f"  [info]   ...分钟线已拉 {n_done}/{len(pairs)}")
        if not frames:
            print("  [warn] 分钟线全空，回退日线代理")
            return None
        m = pd.concat(frames, ignore_index=True).rename(columns={"symbol": "ts_code", "date": "trade_date"})
        m["trade_date"] = _norm_date(m["trade_date"])
        m = m.sort_values(["ts_code", "trade_date", "datetime"]).reset_index(drop=True)
        m["minute_idx"] = m.groupby(["ts_code", "trade_date"]).cumcount() + 1
        for c in ["high", "close", "volume", "amount"]:
            m[c] = pd.to_numeric(m[c], errors="coerce")
        return m
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 分钟线不可用，炸板次数/回封时间降级: {str(exc)[:80]}")
        return None


def load_concepts(asof: str | None = None, pd_api: Any | None = None,
                  max_concepts: int = 300) -> Optional[pd.DataFrame]:
    """概念成分（题材分组用，口径同 A3 load_concepts）。
    get_concept_list 拿列表 → 按概念逐个拉成分（避开全量 600003 限额）。
    保留 in_date 供 PIT 过滤。失败 / 流量超限 → 返回 None（题材分组优雅降级）。"""
    try:
        pd_api = pd_api or init_panda()
        clist = pd_api.get_concept_list()
        if clist is None or clist.empty:
            return None
        concepts = clist["name"].dropna().unique().tolist()[:max_concepts]
        print(f"  [info] 题材：遍历 {len(concepts)} 个概念拉成分 ...")
        frames = []
        for i, name in enumerate(concepts):
            try:
                df = pd_api.get_concept_constituents(
                    concept=name, date=_compact(asof) if asof else "",
                    concept_stock="", fields=["concept", "concept_stock", "date"],
                )
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception as exc:  # noqa: BLE001
                if _is_quota_or_service_error(exc):
                    print(f"  [warn] 概念拉取触发配额/服务异常，题材分组降级: {str(exc)[:50]}")
                    return None if not frames else _finalize_concepts(frames)
                continue
            if (i + 1) % 100 == 0:
                print(f"  [info]   ...概念已拉 {i+1}/{len(concepts)}")
        return _finalize_concepts(frames) if frames else None
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 题材数据不可用，concept 分组降级: {str(exc)[:80]}")
        return None


def _finalize_concepts(frames: list) -> Optional[pd.DataFrame]:
    df = pd.concat(frames, ignore_index=True).rename(columns={"concept_stock": "ts_code", "date": "in_date"})
    df = df[["concept", "ts_code", "in_date"]].dropna(subset=["concept", "ts_code"]).drop_duplicates()
    df["in_date"] = pd.to_datetime(df["in_date"], errors="coerce")
    print(f"  [info] 概念成分: {len(df)} 条 / {df['concept'].nunique()} 概念 / {df['ts_code'].nunique()} 标的")
    return df


# ============================================================
# 连板状态机 + 日线特征（口径同 A3 build_streak）
# ============================================================
def build_streak(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])  # 防御：直接调用方可能传字符串日期
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    g = df.groupby("ts_code", sort=False)

    df["board_type"] = df["ts_code"].map(board_type_of)
    df["limit_rate"] = [limit_rate_of(c, n) for c, n in zip(df["ts_code"], df["name"])]

    pre = df["pre_close"].where(df["pre_close"].notna(), g["close"].shift(1))
    fallback = (pre * (1.0 + df["limit_rate"])).round(2)
    df["eff_limit_up"] = df["limit_up"].where(df["limit_up"].gt(0), fallback)
    # 跌停价（天地板/地天板识别用）：接口 limit_down 优先，缺则 pre*(1-rate) 兜底
    fallback_dn = (pre * (1.0 - df["limit_rate"])).round(2)
    df["eff_limit_down"] = df["limit_down"].where(df["limit_down"].gt(0), fallback_dn)

    df["is_tradable"] = df["trade_status"].eq(0)
    df["is_limit_up_close"] = (
        df["is_tradable"] & df["close"].notna() & df["eff_limit_up"].notna()
        & df["close"].ge(df["eff_limit_up"] * LIMIT_TOL)
    )
    df["is_limit_down_close"] = (
        df["is_tradable"] & df["close"].notna() & df["eff_limit_down"].notna()
        & df["close"].le(df["eff_limit_down"] * (2 - LIMIT_TOL))
    )

    # 连板：仅在交易日序列上累加（停牌不算断板，复牌接续）
    trad = df[df["is_tradable"]]
    trad_streak = trad.groupby("ts_code")["is_limit_up_close"].transform(_consecutive_true_streak)
    df["limit_up_streak"] = trad_streak.reindex(df.index)
    df["limit_up_streak"] = df.groupby("ts_code")["limit_up_streak"].ffill().fillna(0).astype(int)

    # 一字板
    df["is_one_word"] = (
        df["open"].ge(df["eff_limit_up"] * LIMIT_TOL) & df["low"].ge(df["eff_limit_up"] * LIMIT_TOL)
        & df["is_limit_up_close"]
    )
    # 触及涨停（盘中曾摸板，可能未封住）：用于"炸板未封"识别
    df["touched_limit"] = df["high"].ge(df["eff_limit_up"] * LIMIT_TOL) & df["is_tradable"]
    # 盘中曾触及跌停（地天板/天地板识别用）
    df["touched_limit_down"] = df["low"].le(df["eff_limit_down"] * (2 - LIMIT_TOL)) & df["is_tradable"]
    # 日内自涨停价回落幅度（诊断字段；注意低开拉板的干净涨停同样会"low 远离板"，
    # 故不能据此判炸板——见 compute_seal_metrics 的保守日线代理，本字段已不参与炸板判定）
    df["intraday_open_back"] = np.where(
        df["touched_limit"],
        ((df["eff_limit_up"] - df["low"]) / df["eff_limit_up"]).clip(0, 0.30),
        0.0,
    )
    df["pct_chg"] = (df["close"] / pre - 1.0)
    return df


# ============================================================
# 分钟级封板指标（炸板次数 / 首封·回封时间）
# ============================================================
def _minute_to_seconds(dt_val: Any) -> Optional[str]:
    try:
        t = pd.Timestamp(dt_val)
        return t.strftime("%H:%M")
    except Exception:
        return None


def compute_seal_metrics(pool: pd.DataFrame, minute: Optional[pd.DataFrame]) -> pd.DataFrame:
    """对涨停池每行补：first_seal_time / final_seal_time / blow_up_count / reseal_count /
    seal_quality / seal_metric_source。minute 为空时全部走日线代理。"""
    df = pool.copy()
    # 默认（日线代理）
    df["first_seal_time"] = None
    df["final_seal_time"] = None
    df["blow_up_count"] = 0
    df["reseal_count"] = 0
    df["seal_metric_source"] = "daily_proxy"

    # 日线代理（保守）：日线 OHLC 无法区分"全天稳封"与"封→炸→回封"——两者都满足
    # high=close=板、low 可远离板（低开拉板的干净涨停 low 同样远离板）。故收盘封死的涨停一律记
    # blow_up=0（不臆造炸板），只把"盘中触板但未封住收盘"(touched_limit 且 未涨停收盘) 记 1 次
    # 日线可见炸板（高触板、尾盘回落）。修复验收报告 B6 问题①：低开拉板干净涨停被误标炸板/反复板。
    proxy_blow = np.where(df["touched_limit"] & ~df["is_limit_up_close"], 1, 0)
    df["blow_up_count"] = proxy_blow.astype(int)

    if minute is not None and not minute.empty:
        lim = df[["trade_date", "ts_code", "eff_limit_up"]].drop_duplicates().copy()
        m = minute.copy()
        # 统一 merge 键 dtype（外部调用方可能传字符串日期）
        lim["trade_date"] = pd.to_datetime(lim["trade_date"])
        m["trade_date"] = pd.to_datetime(m["trade_date"])
        m["ts_code"] = m["ts_code"].astype(str)
        lim["ts_code"] = lim["ts_code"].astype(str)
        m = m.merge(lim, on=["trade_date", "ts_code"], how="inner")
        if not m.empty:
            m = m.sort_values(["ts_code", "trade_date", "minute_idx"])
            m["sealed"] = m["high"].ge(m["eff_limit_up"] * LIMIT_TOL)
            grp = m.groupby(["ts_code", "trade_date"], sort=False)
            # 首封时间
            first = m[m["sealed"]].groupby(["ts_code", "trade_date"])["datetime"].min().reset_index(name="dt_first")
            # 最后一根封板分钟 = 最终回封/封死时间
            last = m[m["sealed"]].groupby(["ts_code", "trade_date"])["datetime"].max().reset_index(name="dt_last")
            # 炸板次数：sealed True->False 的下降沿数
            m["prev_sealed"] = grp["sealed"].shift(1).fillna(False)
            m["blow"] = (~m["sealed"]) & m["prev_sealed"]
            blow = grp["blow"].sum().reset_index(name="blow_up_count_min")
            # 回封次数：False->True 的上升沿数减首封（即砸开后再封的次数）
            m["reseal"] = m["sealed"] & (~grp["sealed"].shift(1).fillna(False))
            reseal = grp["reseal"].sum().reset_index(name="rise_edges")

            metr = first.merge(last, on=["ts_code", "trade_date"], how="outer") \
                        .merge(blow, on=["ts_code", "trade_date"], how="outer") \
                        .merge(reseal, on=["ts_code", "trade_date"], how="outer")
            metr["first_seal_time"] = metr["dt_first"].map(_minute_to_seconds)
            metr["final_seal_time"] = metr["dt_last"].map(_minute_to_seconds)
            metr["blow_up_count_min"] = metr["blow_up_count_min"].fillna(0).astype(int)
            # 回封次数 = 上升沿数 - 1（第一次封不算"回"封）；无封则 0
            metr["reseal_count_min"] = (metr["rise_edges"].fillna(0).astype(int) - 1).clip(lower=0)

            df = df.merge(
                metr[["ts_code", "trade_date", "first_seal_time", "final_seal_time",
                      "blow_up_count_min", "reseal_count_min"]],
                on=["ts_code", "trade_date"], how="left", suffixes=("", "_m"),
            )
            has_min = df["first_seal_time_m"].notna() | df["final_seal_time_m"].notna()
            df["first_seal_time"] = df["first_seal_time_m"].where(has_min, df["first_seal_time"])
            df["final_seal_time"] = df["final_seal_time_m"].where(has_min, df["final_seal_time"])
            df["blow_up_count"] = np.where(has_min, df["blow_up_count_min"].fillna(0), df["blow_up_count"]).astype(int)
            df["reseal_count"] = np.where(has_min, df["reseal_count_min"].fillna(0), df["reseal_count"]).astype(int)
            df["seal_metric_source"] = np.where(has_min, "minute", "daily_proxy")
            df = df.drop(columns=[c for c in df.columns if c.endswith("_m")], errors="ignore")

    # 封板质量标签
    def _quality(r) -> str:
        if not r["is_limit_up_close"]:
            return "炸板未封" if r["touched_limit"] else "未涨停"
        if r.get("is_one_word", False):
            return "一字板"
        bu = int(r.get("blow_up_count", 0) or 0)
        if bu == 0:
            return "稳封"
        if bu == 1:
            return "炸1次回封"
        return f"炸{bu}次回封"

    df["seal_quality"] = df.apply(_quality, axis=1)
    return df


# ============================================================
# 题材/概念分组（PIT）
# ============================================================
def tag_concepts(pool: pd.DataFrame, concepts: Optional[pd.DataFrame],
                 target_date: pd.Timestamp) -> pd.DataFrame:
    """给涨停池每只票打题材标签：
       lead_concept        —— 代表题材（该票所属概念中当日涨停家数最多的那个）
       concept_board_count —— 代表题材当日涨停家数（梯队厚度）
       is_concept_leader   —— 是否该代表题材内连板最高（题材龙头）
       concepts            —— 该票所属全部概念（PIT 过滤后）JSON 列表
    无概念数据 → 字段留空，优雅降级。"""
    pool = pool.copy()
    pool["lead_concept"] = None
    pool["concept_board_count"] = 0
    pool["is_concept_leader"] = False
    pool["concepts"] = "[]"
    if concepts is None or concepts.empty:
        return pool
    c = concepts.copy()
    # PIT：仅认信号日当天已纳入该概念的成分，杜绝"未来才纳入"回填历史
    c = c[c["in_date"].isna() | (c["in_date"] <= target_date)]
    c = c[c["ts_code"].isin(set(pool["ts_code"]))]
    if c.empty:
        return pool
    st = pool[["ts_code", "limit_up_streak", "is_limit_up_close"]].drop_duplicates("ts_code")
    cm = c.merge(st, on="ts_code", how="left")
    # 每概念当日涨停家数（梯队厚度，只数封住的）
    cbc = cm[cm["is_limit_up_close"].fillna(False)].groupby("concept")["ts_code"].nunique().rename("cbc")
    cm = cm.merge(cbc, on="concept", how="left")
    cm["cbc"] = cm["cbc"].fillna(0).astype(int)
    # 题材内最高板 → 龙头身份
    cmax = cm.groupby("concept")["limit_up_streak"].transform("max")
    cm["is_leader_in_c"] = cm["limit_up_streak"].fillna(0).ge(cmax) & (cmax > 0)
    # 每票代表题材 = 梯队最厚的概念（并列取板更高）
    cm = cm.sort_values(["ts_code", "cbc", "limit_up_streak"], ascending=[True, False, False])
    lead = cm.groupby("ts_code", as_index=False).first()[["ts_code", "concept", "cbc", "is_leader_in_c"]]
    lead = lead.rename(columns={"concept": "lead_concept", "cbc": "concept_board_count",
                                "is_leader_in_c": "is_concept_leader"})
    clist = (cm.groupby("ts_code")["concept"]
             .apply(lambda s: json.dumps(sorted(set(s)), ensure_ascii=False)).rename("concepts").reset_index())
    pool = pool.drop(columns=["lead_concept", "concept_board_count", "is_concept_leader", "concepts"])
    pool = pool.merge(lead, on="ts_code", how="left").merge(clist, on="ts_code", how="left")
    pool["concept_board_count"] = pool["concept_board_count"].fillna(0).astype(int)
    pool["is_concept_leader"] = pool["is_concept_leader"].fillna(False).astype(bool)
    pool["concepts"] = pool["concepts"].fillna("[]")
    return pool


# ============================================================
# 特殊形态标记
# ============================================================
def classify_special_pattern(pool: pd.DataFrame) -> pd.DataFrame:
    """识别特殊形态（优先级从高到低）：
       地天板（盘中触跌停又涨停收盘）> 天地板（盘中触涨停却跌停收盘）> 一字板 >
       秒板（开盘即封）> 炸板未封 > 烂板（炸≥3 次或尾盘才回封）> 反复板（炸 1-2 次）> 实封 > 普通"""
    pool = pool.copy()

    def _p(r) -> str:
        lu = bool(r.get("is_limit_up_close", False))
        ld = bool(r.get("is_limit_down_close", False))
        t_up = bool(r.get("touched_limit", False))
        t_dn = bool(r.get("touched_limit_down", False))
        bu = int(r.get("blow_up_count", 0) or 0)
        fst, fin = r.get("first_seal_time"), r.get("final_seal_time")
        if lu and t_dn:
            return "地天板"
        if ld and t_up:
            return "天地板"
        if bool(r.get("is_one_word", False)):
            return "一字板"
        if lu and isinstance(fst, str) and fst <= "09:31":
            return "秒板"
        if not lu:
            return "炸板未封" if t_up else "未涨停"
        # 烂板：反复炸板≥3 次，或 炸开过(≥1)且尾盘(>=14:30)才最终回封。
        # 注意必须 bu>=1 才算"尾盘回封"——否则全天稳封的 final_seal≈15:00 会误判。
        if bu >= 3 or (bu >= 1 and isinstance(fin, str) and fin >= "14:30"):
            return "烂板"
        if bu >= 1:
            return "反复板"
        return "实封"

    pool["special_pattern"] = pool.apply(_p, axis=1)
    return pool


# ============================================================
# 情绪面量化（市场层面，按日一行 summary）
# ============================================================
def compute_sentiment(streak: pd.DataFrame, target_date: pd.Timestamp) -> dict:
    """当日市场情绪面：涨停/炸板/最高板/分层晋级率/昨涨停今溢价（赚钱效应）。"""
    all_dates = sorted(streak["trade_date"].dropna().unique())
    if target_date not in all_dates:
        return {}
    ti = all_dates.index(target_date)
    prev_date = all_dates[ti - 1] if ti > 0 else None
    today = streak[streak["trade_date"] == target_date]

    n_lu = int(today["is_limit_up_close"].sum())
    n_blow_open = int((today["touched_limit"] & ~today["is_limit_up_close"]).sum())
    denom = n_lu + n_blow_open
    s = {
        "n_limit_up": n_lu,
        "n_blow_open": n_blow_open,
        "market_blow_rate": round(n_blow_open / denom, 4) if denom else 0.0,
        "max_height": int(today["limit_up_streak"].max()) if len(today) else 0,
        "n_first_board": int((today["limit_up_streak"] == 1).sum()),
        "n_lianban": int((today["limit_up_streak"] >= 2).sum()),
        "prev_limitup_premium": None,
        "promote_rate_by_tier": {},
    }
    if prev_date is not None:
        prev = streak[streak["trade_date"] == prev_date]
        prev_lu_codes = set(prev[prev["is_limit_up_close"]]["ts_code"])
        if prev_lu_codes:
            td = today[today["ts_code"].isin(prev_lu_codes)]
            if len(td):
                s["prev_limitup_premium"] = round(float(td["pct_chg"].mean()), 4)
        prev_smap = prev.set_index("ts_code")["limit_up_streak"]
        tmap = today.set_index("ts_code")["limit_up_streak"]
        max_n = int(prev_smap.max()) if len(prev_smap) else 0
        for n in range(1, max_n + 1):
            codes_n = prev_smap[prev_smap == n].index
            base = len(codes_n)
            if base == 0:
                continue
            promoted = int((tmap.reindex(codes_n).fillna(0) >= n + 1).sum())
            s["promote_rate_by_tier"][f"{n}->{n+1}"] = {
                "rate": round(promoted / base, 3), "promoted": promoted, "base": base,
            }
    return s


# ============================================================
# 涨停池组装 + 动态状态机（晋级 / 新晋 / 淘汰）
# ============================================================
def assemble_pool(streak: pd.DataFrame, target_date: pd.Timestamp,
                  minute: Optional[pd.DataFrame] = None,
                  concepts: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """组装 target_date 当日涨停池（含当日炸板未封股），并对比前一交易日做动态状态机。"""
    all_dates = sorted(streak["trade_date"].dropna().unique())
    if target_date not in all_dates:
        # 容错：target 不在数据里时取最近 <= target 的交易日
        prior = [d for d in all_dates if d <= target_date]
        if not prior:
            return pd.DataFrame()
        target_date = prior[-1]
    ti = all_dates.index(target_date)
    prev_date = all_dates[ti - 1] if ti > 0 else None

    today = streak[streak["trade_date"] == target_date].copy()
    # 涨停池成员 = 当日涨停收盘 或 当日触及涨停后炸板未封（动态管理需要看炸板）
    pool = today[today["is_limit_up_close"] | today["touched_limit"]].copy()
    if pool.empty:
        # 空涨停日（该交易日有数据但无涨停/炸板）：仍计算并挂载情绪面，供 to_standard_output
        # 落 1 行 MARKET（家数=0），保证情绪面时序不缺日（修复验收报告 B6 问题③）。
        pool.attrs["sentiment"] = compute_sentiment(streak, target_date)
        pool.attrs["target_date"] = target_date
        return pool

    # 昨日连板数（for 晋级判定）
    if prev_date is not None:
        prev = streak[streak["trade_date"] == prev_date][["ts_code", "limit_up_streak", "is_limit_up_close"]]
        prev = prev.rename(columns={"limit_up_streak": "prev_streak", "is_limit_up_close": "prev_limit_up"})
        pool = pool.merge(prev, on="ts_code", how="left")
    else:
        pool["prev_streak"] = 0
        pool["prev_limit_up"] = False
    pool["prev_streak"] = pool["prev_streak"].fillna(0).astype(int)
    pool["prev_limit_up"] = pool["prev_limit_up"].fillna(False).astype(bool)

    # 分钟封板指标
    pool = compute_seal_metrics(pool, minute)

    # 首板 / 板级标签
    pool["is_first_board"] = (pool["limit_up_streak"] == 1)

    def _board_label(r) -> str:
        s = int(r["limit_up_streak"])
        if s <= 0:
            return "炸板未封" if r["touched_limit"] else "未涨停"
        if s == 1:
            return "首板"
        return f"{s}连板"
    pool["board_label"] = pool.apply(_board_label, axis=1)

    # 动态状态机
    def _status(r) -> str:
        s = int(r["limit_up_streak"])
        ps = int(r["prev_streak"])
        if s == 0:  # 今天没封住
            return "炸板出局" if r["prev_limit_up"] else "摸板未遂"
        if not r["prev_limit_up"]:
            return "新晋首板" if s == 1 else "断板后重启"
        if s > ps:
            return "晋级"  # 连板升级
        return "维持"
    pool["pool_status"] = pool.apply(_status, axis=1)

    # 题材分组 + 特殊形态
    pool = tag_concepts(pool, concepts, target_date)
    pool = classify_special_pattern(pool)

    pool["target_date"] = target_date
    pool = pool.reset_index(drop=True)
    # 市场情绪面（按日一行 summary，挂在 attrs 上供 to_standard_output 落一行 MARKET）
    pool.attrs["sentiment"] = compute_sentiment(streak, target_date)
    return pool


# ============================================================
# 标准化输出
# ============================================================
OUTPUT_COLS = [
    "trade_date", "build_id", "build_name", "target_id", "result_type",
    "ts_code", "name", "board_type",
    "board_label", "limit_up_streak", "prev_streak", "is_first_board",
    "pool_status", "seal_quality", "special_pattern",
    "lead_concept", "concept_board_count", "is_concept_leader", "concepts",
    "first_seal_time", "final_seal_time", "blow_up_count", "reseal_count",
    "is_one_word", "is_limit_up_close", "touched_limit",
    "pct_chg", "close", "open", "high", "low", "amount", "eff_limit_up",
    "seal_metric_source", "result_value", "result_json",
    "data_version", "update_time",
]


def _market_row(sentiment: dict, trade_date: str, data_version: str, now_iso: str) -> dict:
    """构造 1 行市场情绪面 MARKET 记录（target_id=MARKET, result_type=limitup_sentiment）。"""
    mrow = {c: None for c in OUTPUT_COLS}
    mrow.update({
        "trade_date": trade_date, "build_id": BUILD_ID, "build_name": BUILD_NAME,
        "target_id": "MARKET", "result_type": "limitup_sentiment",
        "ts_code": "MARKET", "name": "市场情绪面",
        "limit_up_streak": 0, "prev_streak": 0, "blow_up_count": 0, "reseal_count": 0,
        "is_first_board": False, "is_one_word": False,
        "is_limit_up_close": False, "touched_limit": False, "is_concept_leader": False,
        "concept_board_count": 0,
        "result_value": f"{sentiment.get('n_limit_up', 0)}涨停/{sentiment.get('max_height', 0)}板",
        "result_json": json.dumps(sentiment, ensure_ascii=False),
        "data_version": data_version, "update_time": now_iso,
    })
    return mrow


def to_standard_output(pool: pd.DataFrame, data_version: str = DEFAULT_DATA_VERSION) -> pd.DataFrame:
    """转 BUILD 生产规则 §11 标准列（trade_date/build_id/target_id/result_type/result_value/result_json...）。"""
    if pool.empty:
        # 空涨停交易日：若已挂载情绪面，仍落 1 行 MARKET（家数=0），保证情绪面时序不缺日。
        sentiment = pool.attrs.get("sentiment") or {}
        tgt = pool.attrs.get("target_date")
        if sentiment and tgt is not None:
            now_iso = datetime.now().isoformat(timespec="seconds")
            trade_date = pd.to_datetime(tgt).strftime("%Y-%m-%d")
            return pd.DataFrame([_market_row(sentiment, trade_date, data_version, now_iso)],
                                columns=OUTPUT_COLS)
        return pd.DataFrame(columns=OUTPUT_COLS)
    df = pool.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df["build_id"] = BUILD_ID
    df["build_name"] = BUILD_NAME
    df["result_type"] = RESULT_TYPE
    df["target_id"] = df["ts_code"].astype(str)
    df["result_value"] = df["board_label"].astype(str)  # 核心结果 = 板级标签

    for c in ["is_first_board", "is_one_word", "is_limit_up_close", "touched_limit"]:
        df[c] = df[c].fillna(False).astype(bool)
    for c in ["limit_up_streak", "prev_streak", "blow_up_count", "reseal_count"]:
        df[c] = df[c].fillna(0).astype(int)

    def _row_json(r) -> str:
        return json.dumps({
            "board_type": r["board_type"],
            "board_label": r["board_label"],
            "limit_up_streak": int(r["limit_up_streak"]),
            "prev_streak": int(r["prev_streak"]),
            "is_first_board": bool(r["is_first_board"]),
            "pool_status": r["pool_status"],
            "seal_quality": r["seal_quality"],
            "special_pattern": r.get("special_pattern"),
            "lead_concept": r.get("lead_concept"),
            "concept_board_count": int(r.get("concept_board_count", 0) or 0),
            "is_concept_leader": bool(r.get("is_concept_leader", False)),
            "concepts": json.loads(r.get("concepts") or "[]"),
            "first_seal_time": r.get("first_seal_time"),
            "final_seal_time": r.get("final_seal_time"),
            "blow_up_count": int(r["blow_up_count"]),
            "reseal_count": int(r["reseal_count"]),
            "is_one_word": bool(r["is_one_word"]),
            "pct_chg": round(float(r["pct_chg"]), 4) if pd.notna(r["pct_chg"]) else None,
            "amount": round(float(r["amount"]), 0) if pd.notna(r["amount"]) else None,
            "seal_metric_source": r["seal_metric_source"],
        }, ensure_ascii=False)
    df["result_json"] = df.apply(_row_json, axis=1)

    df["data_version"] = data_version
    now_iso = datetime.now().isoformat(timespec="seconds")
    df["update_time"] = now_iso
    for c in OUTPUT_COLS:
        if c not in df.columns:
            df[c] = None
    out = df[OUTPUT_COLS].sort_values(
        ["limit_up_streak", "amount"], ascending=[False, False]
    ).reset_index(drop=True)

    # 追加一行市场情绪面 summary（target_id=MARKET, result_type=limitup_sentiment）
    sentiment = pool.attrs.get("sentiment") or {}
    if sentiment:
        trade_date = out["trade_date"].iloc[0] if len(out) else \
            pd.to_datetime(pool["trade_date"].iloc[0]).strftime("%Y-%m-%d")
        mrow = _market_row(sentiment, trade_date, data_version, now_iso)
        mdf = pd.DataFrame([mrow]).reindex(columns=out.columns).astype(out.dtypes.to_dict(), errors="ignore")
        out = pd.concat([out, mdf], ignore_index=True)
    return out


def check_quality(panel: pd.DataFrame) -> list[str]:
    errs = []
    if panel.empty:
        return ["涨停池为空（当日可能无涨停，或数据未返回）"]
    dup = panel.duplicated(subset=["trade_date", "build_id", "target_id", "result_type"], keep=False)
    if dup.any():
        errs.append(f"主键重复 {int(dup.sum())} 条")
    for c in ["trade_date", "build_id", "target_id", "result_type", "result_value"]:
        if panel[c].isnull().any():
            errs.append(f"必填字段 '{c}' 存在空值")
    bad = panel[~panel["result_json"].apply(_is_json)]
    if len(bad):
        errs.append(f"result_json 不可解析 {len(bad)} 条")
    if (panel["limit_up_streak"] < 0).any():
        errs.append("limit_up_streak 出现负值")
    return errs


def _is_json(s: Any) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


# ============================================================
# 标准入口
# ============================================================
def validate_input(input_data: Any) -> pd.DataFrame:
    """校验调用方传入的标准结构化日线数据。"""
    if input_data is None:
        raise ValueError("input_data 不能为空")
    df = pd.DataFrame(input_data)
    if df.empty:
        raise ValueError("input_data 不能为空表")
    df = df.rename(columns={"symbol": "ts_code", "date": "trade_date"})
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"input_data 缺少必要字段: {sorted(missing)}")
    return df


def run(input_data: Any, config: dict | None = None) -> pd.DataFrame:
    """标准调用入口（BUILD 规则 §6）。

    Args:
        input_data: 调用方传入的标准结构化日线数据（含回看窗口，用于算连板）。
            必含 trade_date/ts_code/close/pre_close，建议附 open/high/low/amount/limit_up/
            trade_status/name 以获得完整指标。
        config: {
            "target_date": "YYYY-MM-DD" 或 "YYYYMMDD"（默认数据中最大日期）,
            "minute": 可选，预拉好的分钟线 DataFrame（含 ts_code/trade_date/datetime/high...）,
            "concepts": 可选，概念成分 DataFrame（concept/ts_code/in_date），用于题材分组,
            "data_version": str,
        }
    Returns:
        标准化涨停池 DataFrame（OUTPUT_COLS）+ 1 行市场情绪面 summary。
    """
    config = config or {}
    raw = validate_input(input_data)
    daily = _normalize_daily(raw)
    streak = build_streak(daily)

    tgt = config.get("target_date")
    target_date = pd.to_datetime(_compact(tgt)) if tgt else streak["trade_date"].max()

    pool = assemble_pool(streak, target_date, minute=config.get("minute"),
                         concepts=config.get("concepts"))
    out = to_standard_output(pool, data_version=str(config.get("data_version", DEFAULT_DATA_VERSION)))
    return out


def maintain_daily(end_date: str | None = None, with_minute: bool = True,
                   indicator: str = "", pd_api: Any | None = None) -> pd.DataFrame:
    """每日维护：自动拉数据 + 组装当日涨停池（生产任务调用）。

    Args:
        end_date: 目标日（默认今天，非交易日自动退到最近交易日）。
        with_minute: 是否拉分钟线精确算炸板次数/回封时间（默认 True，失败自动降级日线代理）。
        indicator: 股票池 ""=全A / "000300" / "000852" / "399303"。
    """
    pd_api = pd_api or init_panda()
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    start_c, end_c, last_trade = resolve_trade_window(end_date, pd_api)
    print(f"[1/4] 拉日线（回看 {start_c} ~ {end_c}，目标日={last_trade}）...")
    daily = load_daily(start_c, end_c, pd_api, indicator=indicator)
    print(f"      universe: {daily['ts_code'].nunique()} 标的, {daily['trade_date'].nunique()} 交易日")

    print("[2/5] 连板状态机 ...")
    streak = build_streak(daily)
    target_date = pd.to_datetime(last_trade)

    print("[3/5] 题材成分（概念分组）...")
    concepts = load_concepts(asof=end_c, pd_api=pd_api)

    minute = None
    if with_minute:
        print("[4/5] 涨停池分钟线（首封/炸板/回封）...")
        prelim = assemble_pool(streak, target_date, minute=None)
        if not prelim.empty:
            pairs = prelim[["ts_code", "trade_date"]].drop_duplicates()
            minute = load_minute_for_pool(pairs, pd_api)
    else:
        print("[4/5] 跳过分钟线（with_minute=False，用日线代理）")

    print("[5/5] 组装涨停池 + 标准化 ...")
    pool = assemble_pool(streak, target_date, minute=minute, concepts=concepts)
    out = to_standard_output(pool)
    errs = check_quality(out)
    print("  [PASS] 质量检查通过" if not errs else "  [FAIL] " + "; ".join(errs))
    return out


def backfill(start_date: str, end_date: str, with_minute: bool = False,
             indicator: str = "", pd_api: Any | None = None) -> pd.DataFrame:
    """区间回填：对 [start_date, end_date] 每个交易日生成涨停池历史，纵向拼接。
    回填默认不拉分钟线（流量大）；如需精确炸板史，单独按日 maintain_daily。"""
    pd_api = pd_api or init_panda()
    look_start = _compact((pd.to_datetime(_compact(start_date)) - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y%m%d"))
    print(f"[backfill] 拉日线 {look_start} ~ {_compact(end_date)} ...")
    daily = load_daily(look_start, end_date, pd_api, indicator=indicator)
    streak = build_streak(daily)
    concepts = load_concepts(asof=end_date, pd_api=pd_api)  # 概念快照（PIT 按各日 in_date 过滤）
    dates = [d for d in sorted(streak["trade_date"].dropna().unique())
             if pd.to_datetime(_compact(start_date)) <= d <= pd.to_datetime(_compact(end_date))]
    frames = []
    for d in dates:
        pool = assemble_pool(streak, d, minute=None, concepts=concepts)
        out_d = to_standard_output(pool)   # 空涨停日也会回 1 行 MARKET（情绪面时序不缺日）
        if not out_d.empty:
            frames.append(out_d)
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLS)
    return pd.concat(frames, ignore_index=True)


def save_parquet(panel: pd.DataFrame, out_path: str | Path = DEFAULT_OUT, append: bool = True) -> Path:
    """落地 parquet。append=True 时按 (trade_date,target_id) 去重合并历史。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if append and out_path.exists():
        try:
            old = pd.read_parquet(out_path)
            panel = pd.concat([old, panel], ignore_index=True)
            panel = panel.drop_duplicates(subset=["trade_date", "build_id", "target_id", "result_type"], keep="last")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 读取旧 parquet 失败，改为覆盖写: {exc}")
    panel = panel.sort_values(["trade_date", "limit_up_streak", "amount"], ascending=[True, False, False])
    panel.to_parquet(out_path, index=False)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="BUILD-B6 涨停池动态管理")
    ap.add_argument("--mode", choices=["daily", "backfill"], default="daily")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="daily 模式目标日")
    ap.add_argument("--start", default=None, help="backfill 起始日")
    ap.add_argument("--end", default=None, help="backfill 结束日")
    ap.add_argument("--no-minute", action="store_true", help="daily 模式不拉分钟线（仅日线代理）")
    ap.add_argument("--indicator", default="", choices=["", "000300", "000852", "399303"])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-append", action="store_true", help="覆盖写而非合并历史")
    args = ap.parse_args()

    print("=" * 64)
    print(f"BUILD-B6 涨停池动态管理 | mode={args.mode}")
    print("=" * 64)
    if args.mode == "daily":
        panel = maintain_daily(args.date, with_minute=not args.no_minute, indicator=args.indicator)
    else:
        if not (args.start and args.end):
            raise SystemExit("backfill 模式需 --start 和 --end")
        panel = backfill(args.start, args.end, indicator=args.indicator)

    if panel.empty:
        print("涨停池为空，退出。")
        return
    path = save_parquet(panel, args.out, append=not args.no_append)
    print(f"\n已保存 {len(panel)} 条 → {path}")

    latest = panel[panel["trade_date"] == panel["trade_date"].max()]
    stocks = latest[latest["result_type"] == RESULT_TYPE]
    market = latest[latest["result_type"] == "limitup_sentiment"]
    print(f"\n最新交易日 {panel['trade_date'].max()} | 涨停池 {len(stocks)} 只")
    if len(market):
        print(f"情绪面: {market.iloc[0]['result_json']}")
    show = ["ts_code", "name", "board_type", "board_label", "special_pattern",
            "lead_concept", "pool_status", "blow_up_count", "first_seal_time", "final_seal_time"]
    show = [c for c in show if c in stocks.columns]
    print(stocks.head(15)[show].to_string(index=False))


if __name__ == "__main__":
    main()
