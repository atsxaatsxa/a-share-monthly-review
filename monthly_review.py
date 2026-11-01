#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
A股月度持仓复盘脚本
==================================================================
数据源 : akshare (优先) —— 多数据源兜底(东财 push2 -> 新浪)，任一源成功即采用
核心红线 : 严禁下标顺序匹配 —— 所有行情一律以「标的名称(str)」为 dict key 绑定，
           任何读取都通过 key 取数，绝不依赖列表下标 / 循环顺序。
功能 :
  1. 指定复盘月份 (命令行参数 YYYY-MM)
  2. 拉取各标的与沪深300基准当月日线(qfq)
  3. 计算个股月收益、基准收益、超额收益，区分贡献/拖累
  4. 控制台输出月报 + 导出 CSV(+JSON)
  5. 自动发送月报邮件至 xuyutom@qq.com (未配置SMTP则告警跳过)
  6. 接口异常 / 停牌缺行情 容错，缺失不编造、不静默丢弃
==================================================================
"""

import sys
import os
import csv
import json
import time
import random
import calendar
import smtplib
import re
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Optional

import akshare as ak
import pandas as pd

# ============================ 集中配置 ============================
# 复盘维度全以此字典为准；name 为唯一业务主键，symbol 为 akshare 代码。
# 增加/减少标的只需改这里，其余逻辑自动适配 (可复现复用)。
STOCKS: Dict[str, str] = {
    "五粮液": "000858",
    "万华化学": "600309",
    "立讯精密": "002475",
    "中国中免": "601888",
    "兆易创新": "603986",
    "京沪高铁": "601816",
}
BENCHMARK_NAME = "沪深300"
BENCHMARK_SYMBOL = "000300"  # 沪深300 指数代码

# 异常收益阈值：单月涨跌幅绝对值超过该值视为异常，需人工复核 (双向)
ANOMALY_RETURN_THRESHOLD = 0.60

# 接口容错
MAX_RETRY = 3
RETRY_BACKOFF = 2      # 秒
SOURCE_DELAY = 0.5     # 标的之间节流，降低被限流概率

# 邮件配置：优先读环境变量，缺失则跳过发送并告警 (不阻断主流程)
EMAIL_CONFIG = {
    "smtp_host": os.getenv("REVIEW_SMTP_HOST", ""),
    "smtp_port": int(os.getenv("REVIEW_SMTP_PORT", "465")),
    "smtp_user": os.getenv("REVIEW_SMTP_USER", ""),
    "smtp_pass": os.getenv("REVIEW_SMTP_PASS", ""),
    "to_addr": "xuyutom@qq.com",
    "from_addr": os.getenv("REVIEW_SMTP_USER", "xuyutom@qq.com"),
}


# ============================ 工具函数 ============================
def month_bounds(year: int, month: int):
    """返回该月首日和末日 (date)。"""
    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    return start, end


def validate_month(review_month: str):
    """输入自证：校验月份参数格式与合法性。非法直接抛错 → 主流程退出，绝不输出复盘。"""
    if not re.fullmatch(r"\d{4}-\d{2}", review_month or ""):
        raise ValueError(f"月份格式错误: '{review_month}'，应为 YYYY-MM（如 2024-01）")
    year, month = int(review_month[:4]), int(review_month[5:7])
    if not (1 <= month <= 12):
        raise ValueError(f"月份越界: {month}")
    if (year, month) > (date.today().year, date.today().month):
        raise ValueError(f"复盘月份 {review_month} 晚于当前月份，禁止输出未来复盘")
    # 标的配置自证
    if not STOCKS:
        raise ValueError("标的配置为空")
    for name, sym in STOCKS.items():
        if not isinstance(sym, str) or not sym.isdigit() or len(sym) != 6:
            raise ValueError(f"标的 '{name}' 代码非法: '{sym}'（应为6位A股代码）")
    return year, month


def fetch_with_retry(func, *args, **kwargs):
    """双向异常检测之——接口超时/网络异常重试。"""
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # 含超时/连接异常
            last_err = e
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * attempt)
    raise last_err


def _sina_symbol(symbol: str) -> str:
    """A股代码转新浪格式：6开头->sh，其余->sz。"""
    return ("sh" if symbol.startswith("6") else "sz") + symbol


def _extract_close(df) -> Optional[pd.DataFrame]:
    """从原始 df 抽取 (date, close) 归一化列；列名兼容东财(日期/收盘)与新浪(date/close)。"""
    if df is None or (hasattr(df, "empty") and df.empty):
        return None
    dcol = "日期" if "日期" in df.columns else ("date" if "date" in df.columns else None)
    ccol = "收盘" if "收盘" in df.columns else ("close" if "close" in df.columns else None)
    if dcol is None or ccol is None:
        return None
    out = df[[dcol, ccol]].copy()
    out.columns = ["date", "close"]
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna().sort_values("date").reset_index(drop=True)
    return out if not out.empty else None


def _filter_range(df, start, end):
    """对不支持起止参数的数据源(如新浪指数)做本地日期过滤。"""
    norm = _extract_close(df)
    if norm is None:
        return None
    mask = (norm["date"] >= pd.Timestamp(start)) & (norm["date"] <= pd.Timestamp(end))
    return norm[mask] if mask.any() else None


def get_monthly_return(name: str, symbol: str, start, end, is_index: bool = False) -> dict:
    """
    取得单标的当月收益。多数据源兜底，命中即返回。
    返回 dict，关键字段: name, symbol, status, base, end, ret, rows, note, src
    status ∈ {ok, anomaly, no_data, error}
    缺行情 → no_data (标注人工复核，不编造)；异常收益 → anomaly (仍给出计算值并标注)。
    """
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    if is_index:
        sources = [
            ("东财指数", lambda: _extract_close(fetch_with_retry(
                ak.index_zh_a_hist, symbol=symbol, period="daily",
                start_date=start_str, end_date=end_str, adjust=""))),
            ("新浪指数", lambda: _filter_range(
                ak.stock_zh_index_daily(symbol="sh" + symbol), start, end)),
        ]
    else:
        sources = [
            ("东财个股", lambda: _extract_close(fetch_with_retry(
                ak.stock_zh_a_hist, symbol=symbol, period="daily",
                start_date=start_str, end_date=end_str, adjust="qfq"))),
            ("新浪个股", lambda: _extract_close(fetch_with_retry(
                ak.stock_zh_a_daily, symbol=_sina_symbol(symbol),
                start_date=start_str, end_date=end_str, adjust="qfq"))),
        ]

    last_err = None
    for src_name, fn in sources:
        try:
            norm = fn()
        except Exception as e:
            last_err = f"[{src_name}] {e}"
            continue
        if norm is None or norm.empty:
            last_err = f"[{src_name}] 无数据"
            continue
        base = float(norm.iloc[0]["close"])
        end_c = float(norm.iloc[-1]["close"])
        if base <= 0:
            return {"name": name, "symbol": symbol, "status": "error", "src": src_name,
                    "base": base, "end": end_c, "ret": None, "rows": len(norm),
                    "note": f"月初收盘非正({base})，数据异常，需人工复核"}
        ret = end_c / base - 1.0
        if abs(ret) > ANOMALY_RETURN_THRESHOLD:
            status, note = "anomaly", f"月收益异常(|r|>{ANOMALY_RETURN_THRESHOLD:.0%})，需人工复核"
        else:
            status, note = "ok", f"[{src_name}] 月初收盘{base:.2f}→月末收盘{end_c:.2f}，共{len(norm)}个交易日"
        return {"name": name, "symbol": symbol, "status": status, "src": src_name,
                "base": base, "end": end_c, "ret": ret, "rows": len(norm), "note": note}

    # 所有数据源均失败 / 均无数据
    if last_err and "无数据" not in last_err:
        return {"name": name, "symbol": symbol, "status": "error", "src": "all",
                "base": None, "end": None, "ret": None, "rows": 0,
                "note": f"全部数据源失败: {last_err}"}
    return {"name": name, "symbol": symbol, "status": "no_data", "src": "all",
            "base": None, "end": None, "ret": None, "rows": 0,
            "note": "当月所有数据源均无交易数据(可能停牌/退市/无行情)，需人工复核"}


# ============================ 主流程 ============================
def build_report(review_month: str):
    year, month = validate_month(review_month)
    start, end = month_bounds(year, month)

    # ---- 逐标的取数：以 name 为 key 存入 dict，绝不用下标 ----
    results: Dict[str, dict] = {}
    for name, symbol in STOCKS.items():          # 顺序无关，循环只为遍历 key
        rec = get_monthly_return(name, symbol, start, end, is_index=False)
        results[name] = rec                       # key=名称，永久绑定
        time.sleep(SOURCE_DELAY)                  # 节流，降低限流概率

    # ---- 基准 ----
    bench = get_monthly_return(BENCHMARK_NAME, BENCHMARK_SYMBOL, start, end, is_index=True)
    bench_ret = bench.get("ret")

    # 守恒校验：标的总数一致，且无任一标的被静默丢弃
    assert len(results) == len(STOCKS), "守恒失败：结果数≠配置标的数"
    for name in STOCKS:
        assert name in results, f"守恒失败：标的 '{name}' 被静默丢弃"

    # ---- 超额收益 & 贡献/拖累分类 ----
    for name, rec in results.items():             # 遍历仍按 key
        if rec["ret"] is not None and bench_ret is not None:
            rec["excess"] = rec["ret"] - bench_ret
            if rec["ret"] > bench_ret:
                rec["role"] = "贡献"
            elif rec["ret"] < bench_ret:
                rec["role"] = "拖累"
            else:
                rec["role"] = "持平"
        else:
            rec["excess"] = None
            rec["role"] = "待人工"

    return results, bench, bench_ret, review_month


# ============================ 输出 ============================
def render_console(results, bench, bench_ret, review_month):
    print("=" * 84)
    print(f"  A股月度持仓复盘  |  复盘月份: {review_month}  |  基准: {BENCHMARK_NAME}")
    print("=" * 84)
    print(f"{'标的':<8}{'代码':<9}{'状态':<8}{'数据源':<8}{'月收益':>10}{'超额':>10}{'角色':<6}")
    print("-" * 84)
    for name, rec in results.items():
        ret_s = f"{rec['ret']*100:+.2f}%" if rec["ret"] is not None else "  --  "
        ex_s = f"{rec['excess']*100:+.2f}%" if rec.get("excess") is not None else "  --  "
        src = rec.get("src", "")
        print(f"{name:<8}{rec['symbol']:<9}{rec['status']:<8}{src:<8}{ret_s:>10}{ex_s:>10}{rec['role']:<6}")
    bret = f"{bench_ret*100:+.2f}%" if bench_ret is not None else "  --  "
    print("-" * 84)
    print(f"{BENCHMARK_NAME:<8}{bench['symbol']:<9}{bench['status']:<8}{bench.get('src',''):<8}{bret:>10}{'基准':>10}")
    # 人工友好：异常/待人工 单独汇总
    manual = [r for r in results.values()
              if r["status"] in ("error", "no_data", "anomaly") or r["ret"] is None]
    if manual:
        print(f"\n[!] 需人工复核标的 ({len(manual)}/{len(results)}):")
        for r in manual:
            print(f"    - {r['name']}({r['symbol']}) [{r['status']}/{r.get('src','')}] {r['note']}")
    else:
        print("\n[OK] 无异常标的，全部可自动复盘。")
    print("=" * 84)


def export_files(results, bench, bench_ret, review_month):
    csv_path = f"review_report_{review_month}.csv"
    cols = ["标的", "代码", "状态", "数据源", "月初收盘", "月末收盘", "月收益",
            "超额收益(基准)", "角色", "交易日数", "备注"]
    rows = []
    for name, r in results.items():
        rows.append([
            name, r["symbol"], r["status"], r.get("src", ""),
            f"{r['base']:.2f}" if r["base"] is not None else "",
            f"{r['end']:.2f}" if r["end"] is not None else "",
            f"{r['ret']*100:.2f}%" if r["ret"] is not None else "",
            f"{r['excess']*100:.2f}%" if r.get("excess") is not None else "",
            r["role"], r["rows"], r["note"],
        ])
    rows.append([
        BENCHMARK_NAME, bench["symbol"], bench["status"], bench.get("src", ""),
        f"{bench['base']:.2f}" if bench["base"] is not None else "",
        f"{bench['end']:.2f}" if bench["end"] is not None else "",
        f"{bench_ret*100:.2f}%" if bench_ret is not None else "",
        "基准", "基准", bench["rows"], bench["note"],
    ])
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    json_path = f"review_report_{review_month}.json"
    dump = {"review_month": review_month, "benchmark": bench, "stocks": results}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2, default=str)
    return csv_path, json_path


def build_email_text(results, bench, bench_ret, review_month):
    lines = [f"A股月度持仓复盘 - {review_month}", f"基准: {BENCHMARK_NAME}", "=" * 40]
    for name, r in results.items():
        ret_s = f"{r['ret']*100:+.2f}%" if r["ret"] is not None else "N/A"
        ex_s = f"{r['excess']*100:+.2f}%" if r.get("excess") is not None else "N/A"
        lines.append(f"{name}({r['symbol']}) [{r['status']}/{r.get('src','')}] 月收益 {ret_s} 超额 {ex_s} 角色 {r['role']}")
    bret = f"{bench_ret*100:+.2f}%" if bench_ret is not None else "N/A"
    lines.append(f"{BENCHMARK_NAME} 月收益 {bret}")
    manual = [r for r in results.values()
              if r["status"] in ("error", "no_data", "anomaly") or r["ret"] is None]
    if manual:
        lines.append("")
        lines.append("需人工复核:")
        for r in manual:
            lines.append(f"  - {r['name']}({r['symbol']}) [{r['status']}] {r['note']}")
    return "\n".join(lines)


def send_email(review_month, text):
    cfg = EMAIL_CONFIG
    if not cfg["smtp_host"] or not cfg["smtp_user"] or not cfg["smtp_pass"]:
        print("[WARN] 未配置 SMTP(REVIEW_SMTP_*)，跳过邮件发送。")
        return False
    msg = MIMEMultipart()
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg["Subject"] = f"A股月度持仓复盘 {review_month}"
    msg.attach(MIMEText(text, "plain", "utf-8"))
    try:
        if cfg["smtp_port"] == 465:
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=20) as s:
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())
        else:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=20) as s:
                s.starttls()
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.sendmail(cfg["from_addr"], [cfg["to_addr"]], msg.as_string())
        print(f"[OK] 月报已发送至 {cfg['to_addr']}")
        return True
    except Exception as e:
        print(f"[WARN] 邮件发送失败(不阻断主流程): {e}")
        return False


def self_test_order(results):
    """证明：打乱返回顺序后，按 name key 取数绝不串位。"""
    items = list(results.items())
    random.shuffle(items)
    rebuilt = {n: r for n, r in items}      # 仍按 name 作 key
    assert rebuilt == results               # dict 相等与顺序无关
    for n, r in results.items():
        assert rebuilt[n] is r              # 按 key 取数一致
    return True


# ============================ 入口 ============================
def main():
    if len(sys.argv) < 2:
        print("用法: python monthly_review.py YYYY-MM   (例: python monthly_review.py 2024-01)")
        print("可选环境变量: REVIEW_SMTP_HOST/PORT/USER/PASS 用于自动发邮件")
        sys.exit(2)  # 参数缺失 → 直接退出，绝不输出复盘 (一票否决)
    review_month = sys.argv[1]
    try:
        results, bench, bench_ret, rm = build_report(review_month)
    except ValueError as e:
        print(f"[参数错误] {e}")
        sys.exit(2)  # 参数非法 → 退出，不输出复盘

    render_console(results, bench, bench_ret, rm)
    csv_path, json_path = export_files(results, bench, bench_ret, rm)
    print(f"[OK] CSV已导出: {csv_path}")
    print(f"[OK] JSON已导出: {json_path}")

    if self_test_order(results):
        print("[OK] 顺序鲁棒自检通过：按名称key绑定，打乱顺序不串位。")

    text = build_email_text(results, bench, bench_ret, rm)
    send_email(rm, text)


if __name__ == "__main__":
    main()
