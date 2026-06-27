
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
import traceback

import pandas as pd
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({
        "ok": False,
        "error": str(e),
        "type": e.__class__.__name__,
        "version": "3.4.1-no-688",
        "hint": "后端异常已被捕获。建议降低每批数量，或先用单股分析。",
        "trace_tail": traceback.format_exc()[-1000:],
    }), 500


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().lower()
    for x in ["sh", "sz", "bj"]:
        s = s.replace(x, "")
    s = "".join(ch for ch in s if ch.isdigit())
    if len(s) != 6:
        raise ValueError("股票代码必须是6位数字，例如 300592、000001、600519")
    return s


def safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "-" or v == "":
            return None
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        if pd.isna(v):
            return ""
        return str(v)
    except Exception:
        return ""


def is_st_stock(name: str) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n or "退" in n


def is_bj_stock(symbol: str) -> bool:
    # 北交所常见 8、4 开头；本系统默认主做沪深 A 股
    s = normalize_symbol(symbol)
    return s.startswith(("8", "4"))


def is_star_market_stock(symbol: str) -> bool:
    # 科创板 688 开头，默认过滤，避免进入普通5日线强势股池
    s = normalize_symbol(symbol)
    return s.startswith("688")


def detect_columns(df: pd.DataFrame) -> Dict[str, str]:
    candidates = {
        "date": ["日期", "date", "交易日"],
        "open": ["开盘", "open", "开盘价"],
        "close": ["收盘", "close", "收盘价"],
        "high": ["最高", "high", "最高价"],
        "low": ["最低", "low", "最低价"],
        "volume": ["成交量", "volume", "成交量(手)"],
        "amount": ["成交额", "amount", "成交额(元)"],
        "pct_chg": ["涨跌幅", "pct_chg", "涨幅"],
        "turnover": ["换手率", "turnover", "换手"],
    }
    result = {}
    cols = list(df.columns)
    for key, names in candidates.items():
        for name in names:
            if name in cols:
                result[key] = name
                break
    missing = [k for k in ["date", "open", "close", "high", "low", "volume"] if k not in result]
    if missing:
        raise ValueError(f"行情字段不完整，缺少：{missing}，当前字段：{cols}")
    return result


def fetch_hist(symbol: str, adjust: str = "") -> pd.DataFrame:
    import akshare as ak
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust or "")
    if df is None or df.empty:
        raise ValueError("没有获取到行情数据，可能是代码错误、停牌或 AKShare 数据源暂时不可用")
    return df



def build_trade_plan(close_price, ma5, ma10, pct_chg, distance_ma5_pct, category, level, risk_flags):
    """生成5日线纪律交易执行计划。仅作辅助，不是买卖指令。"""
    if close_price is None or ma5 is None:
        return {"enabled": False, "summary": "缺少收盘价或5日线，无法生成交易计划。", "execution_steps": ["先完成深度分析，获得 MA5 / MA10 / MA20 后再判断。"]}
    risk_text = "、".join(risk_flags) if risk_flags else "无明显风险标签"
    watch_price = round(ma5, 2)
    half_position_price = round(ma5 * 1.01, 2)
    add_back_price = round(ma5 * 1.005, 2)
    stop_loss_price = round(ma5 * 0.985, 2)
    check_1450_price = round(ma5, 2)
    take_profit_1 = round(close_price * 1.05, 2)
    take_profit_2 = round(close_price * 1.10, 2)
    if category == "safe_near":
        summary = "安全接近买点：站上5日线且距离不远，未触发主要风险过滤。"
        position_plan = "可进入实盘观察计划：先小仓/半仓，不能一次满仓。"
        steps = [f"观察价：MA5 {watch_price} 附近重点观察。", f"半仓参考：回踩不破 {half_position_price} 附近，可考虑小仓/半仓。", f"回踩接回：回踩5日线后重新拉起，参考 {add_back_price}。", f"2:50检查：尾盘跌破 {check_1450_price}，先减半仓。", f"止损线：有效跌破 {stop_loss_price}，严格风控。", "连续3天收盘站不回5日线，果断清仓。"]
    elif category == "near":
        summary = "普通接近买点：位置接近，但仍需确认风险标签。"
        position_plan = "只适合轻仓观察；有风险标签，不建议直接满仓。"
        steps = [f"先看风险标签：{risk_text}。", f"观察价：MA5 {watch_price} 附近。", f"轻仓参考：回踩不破 {half_position_price} 才考虑。", f"2:50检查：跌破 {check_1450_price} 先减仓。", "风险标签未消除前，不做重仓。"]
    elif category == "focus":
        summary = "重点关注：强度较好，但还不是最优进场点。"
        position_plan = "重点观察，等待回踩5日线；不追高。"
        steps = [f"等待回踩 MA5 {watch_price}。", f"回踩不破后，观察是否重新站上 {add_back_price}。", "没有回踩，不追高。"]
    elif category == "far":
        summary = "强势但远离5日线：容易冲高回落。"
        position_plan = "最多观察或小仓，不能满仓追高。"
        steps = [f"不追现价，等待回踩 MA5 {watch_price}。", f"回踩不破后，参考 {add_back_price} 接回。", f"尾盘跌破 {check_1450_price} 不接。"]
    elif category in ["riskfilter", "risk", "ignore"]:
        summary = "风险过滤/不看：不符合当前5日线纪律。"
        position_plan = "不纳入实盘计划。"
        steps = [f"风险标签：{risk_text}。", "不买入，不加仓。", "等待重新站上5日线并解除风险标签后再分析。"]
    else:
        summary = "信号不足，等待更明确的5日线结构。"
        position_plan = "观察为主。"
        steps = [f"观察 MA5 {watch_price} 是否获得支撑。", "没有超过5个点的票不看。", "没有主力大阳线不主动进场。"]
    return {"enabled": True, "summary": summary, "watch_price": watch_price, "half_position_price": half_position_price, "add_back_price": add_back_price, "stop_loss_price": stop_loss_price, "check_1450_price": check_1450_price, "take_profit_1": take_profit_1, "take_profit_2": take_profit_2, "clear_condition": "连续3天收盘站不回5日线，清仓。", "position_plan": position_plan, "execution_steps": steps, "risk_note": risk_text}


def analyze_stock(symbol: str, adjust: str = "", name: str = "", spot_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol)
    df = fetch_hist(symbol, adjust if adjust in ["", "qfq", "hfq"] else "")
    cols = detect_columns(df)
    work = df.copy()

    for key in ["open", "close", "high", "low", "volume", "amount", "pct_chg", "turnover"]:
        if key in cols:
            work[cols[key]] = pd.to_numeric(work[cols[key]], errors="coerce")

    work[cols["date"]] = pd.to_datetime(work[cols["date"]])
    work = work.sort_values(cols["date"]).reset_index(drop=True)

    if len(work) < 20:
        raise ValueError("历史数据不足20个交易日，无法稳定计算均线")

    close = work[cols["close"]]
    volume = work[cols["volume"]]
    work["MA5"] = close.rolling(5).mean()
    work["MA10"] = close.rolling(10).mean()
    work["MA20"] = close.rolling(20).mean()
    work["VOL_MA5"] = volume.rolling(5).mean()

    last = work.iloc[-1]
    prev = work.iloc[-2]

    last_close = safe_float(last[cols["close"]])
    last_open = safe_float(last[cols["open"]])
    last_volume = safe_float(last[cols["volume"]])
    ma5 = safe_float(last["MA5"])
    ma10 = safe_float(last["MA10"])
    ma20 = safe_float(last["MA20"])
    vol_ma5 = safe_float(last["VOL_MA5"])
    amount = safe_float(last[cols["amount"]]) if "amount" in cols else None
    turnover = safe_float(last[cols["turnover"]]) if "turnover" in cols else None

    if last_close is None or last_open is None or ma5 is None:
        raise ValueError("关键行情数据为空，无法分析")

    if "pct_chg" in cols and safe_float(last[cols["pct_chg"]]) is not None:
        pct_chg = safe_float(last[cols["pct_chg"]])
    else:
        prev_close = safe_float(prev[cols["close"]])
        pct_chg = ((last_close - prev_close) / prev_close * 100) if prev_close else None

    if spot_meta:
        if spot_meta.get("pct_chg") is not None:
            pct_chg = spot_meta.get("pct_chg")
        if spot_meta.get("amount") is not None:
            amount = spot_meta.get("amount")
        if spot_meta.get("turnover") is not None:
            turnover = spot_meta.get("turnover")

    distance_ma5_pct = ((last_close - ma5) / ma5 * 100) if ma5 else None

    below_days = 0
    for _, row in work.iloc[::-1].iterrows():
        c = safe_float(row[cols["close"]])
        m = safe_float(row["MA5"])
        if c is not None and m is not None and c < m:
            below_days += 1
        else:
            break

    rise_over_5 = pct_chg is not None and pct_chg >= 5
    volume_breakout = last_volume is not None and vol_ma5 is not None and last_volume >= vol_ma5 * 1.5
    volume_above_avg = last_volume is not None and vol_ma5 is not None and last_volume >= vol_ma5
    candle_body_pct = ((last_close - last_open) / last_open * 100) if last_open else 0
    big_yang = candle_body_pct >= 3 and last_close > last_open
    above_ma5 = last_close >= ma5
    above_ma10 = ma10 is not None and last_close >= ma10
    above_ma20 = ma20 is not None and last_close >= ma20
    far_from_ma5 = distance_ma5_pct is not None and distance_ma5_pct >= 6
    near_buy_point = rise_over_5 and above_ma5 and distance_ma5_pct is not None and 0 <= distance_ma5_pct <= 3

    risk_flags = []
    name_text = name or symbol

    if is_st_stock(name_text):
        risk_flags.append("ST/*ST风险")
    if is_bj_stock(symbol):
        risk_flags.append("北交所过滤")
    if is_star_market_stock(symbol):
        risk_flags.append("688科创板过滤")
    if is_star_market_stock(symbol):
        risk_flags.append("688科创板过滤")
    if last_close is not None and last_close < 3:
        risk_flags.append("低价股风险")
    if amount is not None and amount < 100_000_000:
        risk_flags.append("成交额不足1亿")
    if turnover is not None and turnover > 30:
        risk_flags.append("换手率过高")
    if pct_chg is not None and pct_chg >= 19:
        risk_flags.append("接近20cm涨停/高波动")
    if distance_ma5_pct is not None and distance_ma5_pct > 10:
        risk_flags.append("严重远离5日线")

    hard_risk = any(x in risk_flags for x in ["ST/*ST风险", "北交所过滤", "688科创板过滤", "成交额不足1亿"])
    soft_risk = any(x in risk_flags for x in ["低价股风险", "换手率过高", "接近20cm涨停/高波动", "严重远离5日线"])

    score10 = 0
    if rise_over_5: score10 += 2
    if volume_breakout: score10 += 2
    elif volume_above_avg: score10 += 1
    if big_yang: score10 += 2
    elif last_close > last_open: score10 += 1
    if above_ma5: score10 += 2
    if above_ma10 or above_ma20: score10 += 1
    if distance_ma5_pct is not None and 0 <= distance_ma5_pct <= 8: score10 += 1

    smart_score = min(100, int(score10 * 8 + min(max((pct_chg or 0), 0), 10) * 2))
    if amount and amount >= 1_000_000_000:
        smart_score = min(100, smart_score + 8)
    if turnover and turnover >= 8:
        smart_score = min(100, smart_score + 6)

    # 风险扣分
    if hard_risk:
        smart_score = max(0, smart_score - 25)
    elif soft_risk:
        smart_score = max(0, smart_score - 10)

    safe_near = near_buy_point and not hard_risk and not soft_risk and amount is not None and amount >= 200_000_000 and turnover is not None and 3 <= turnover <= 20

    if hard_risk:
        level, action, position, risk, rank, category = "风险过滤", "触发硬风险过滤，不进入普通买点池。", "不买；仅观察或直接排除。", "、".join(risk_flags), 7, "riskfilter"
    elif not rise_over_5:
        level, action, position, risk, rank, category = "不看", "涨幅没有超过5%，不符合强势股第一条件。", "不买。", "没有明显主力进攻信号。", 6, "ignore"
    elif below_days >= 3:
        level, action, position, risk, rank, category = "清仓信号", "连续3天收盘没有站回5日线，短线趋势失效。", "已有仓位应清仓；没有仓位不进。", "趋势失效，不要幻想。", 5, "risk"
    elif not above_ma5:
        level, action, position, risk, rank, category = "风控信号", "股价跌破5日线，尾盘站不回先减一半仓。", "不加仓；已有仓位减半或观察到尾盘。", "连续3天站不回5日线，清仓。", 4, "risk"
    elif safe_near and smart_score >= 65:
        level, action, position, risk, rank, category = "安全接近买点", "涨幅超过5%，站上5日线，距离5日线不远，且未触发主要风险过滤。", "可分批；先小仓确认，不能一次满仓。", "尾盘跌破5日线，减半仓。", 1, "safe_near"
    elif near_buy_point and smart_score >= 65:
        level, action, position, risk, rank, category = "接近买点", "涨幅超过5%，站上5日线，距离5日线不远，但仍有风险标签。", "只可轻仓观察；严格看尾盘5日线。", "、".join(risk_flags) if risk_flags else "尾盘跌破5日线，减半仓。", 2, "near"
    elif far_from_ma5:
        level, action, position, risk, rank, category = "强势但远离5日线", "股价强势，但已经远离5日线，不适合满仓追高。", "最多半仓；等回踩5日线不破再接回来。", "远离5日线容易冲高回落。", 3, "far"
    elif smart_score >= 80:
        level, action, position, risk, rank, category = "重点关注", "强势评分较高，等待5日线附近确认。", "分批；远离5日线时不要满仓。", "尾盘跌破5日线，减半仓。", 2, "focus"
    elif smart_score >= 65:
        level, action, position, risk, rank, category = "加入自选", "有一定强度，但还要等5日线附近确认。", "轻仓或等待；回踩不破再考虑。", "强度还不够，避免追高。", 3, "watch"
    else:
        level, action, position, risk, rank, category = "暂不操作", "条件不完整，先观察。", "不买或轻仓观察。", "信号不足，容易买到假突破。", 6, "ignore"

    tags = [
        "涨幅超过5%" if rise_over_5 else "涨幅不足5%",
        "明显放量" if volume_breakout else ("量能高于5日均量" if volume_above_avg else "未明显放量"),
        "大阳线" if big_yang else "非大阳线",
        "站上5日线" if above_ma5 else "跌破5日线",
    ]
    if far_from_ma5: tags.append("远离5日线")
    if near_buy_point: tags.append("接近买点")
    if safe_near: tags.append("安全接近买点")
    if amount and amount >= 1_000_000_000: tags.append("成交额活跃")
    if turnover and turnover >= 8: tags.append("高换手")
    tags.extend(risk_flags)

    return {
        "symbol": symbol, "name": name or symbol, "data_points": int(len(work)), "rank": rank,
        "category": category, "score": int(score10), "smart_score": int(smart_score), "tags": tags,
        "risk_flags": risk_flags, "hard_risk": hard_risk, "soft_risk": soft_risk,
        "quote": {"date": str(last[cols["date"]].date()), "open": last_open, "close": last_close, "volume": last_volume, "amount": amount, "turnover": turnover, "pct_chg": pct_chg},
        "analysis": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "vol_ma5": vol_ma5, "distance_ma5_pct": distance_ma5_pct, "below_ma5_days": below_days},
        "advice": {"level": level, "action": action, "position": position, "risk": risk},
        "trade_plan": build_trade_plan(last_close, ma5, ma10, pct_chg, distance_ma5_pct, category, level, risk_flags),
    }


def quick_score_from_spot(item: Dict[str, Any], enable_risk_filter: bool = True) -> Dict[str, Any]:
    pct = item.get("pct_chg") or 0
    amount = item.get("amount") or 0
    turnover = item.get("turnover") or 0
    name = item.get("name") or item["symbol"]
    symbol = item["symbol"]

    risk_flags = []
    if is_st_stock(name):
        risk_flags.append("ST/*ST风险")
    if is_bj_stock(symbol):
        risk_flags.append("北交所过滤")
    if is_star_market_stock(symbol):
        risk_flags.append("688科创板过滤")
    if is_star_market_stock(symbol):
        risk_flags.append("688科创板过滤")
    if item.get("price") is not None and item.get("price") < 3:
        risk_flags.append("低价股风险")
    if amount and amount < 100_000_000:
        risk_flags.append("成交额不足1亿")
    if turnover and turnover > 30:
        risk_flags.append("换手率过高")
    if pct >= 19:
        risk_flags.append("接近20cm涨停/高波动")

    hard_risk = any(x in risk_flags for x in ["ST/*ST风险", "北交所过滤", "688科创板过滤", "成交额不足1亿"])

    score = 0
    score += min(max(pct, 0), 10) * 4
    if amount >= 5_000_000_000: score += 30
    elif amount >= 1_000_000_000: score += 22
    elif amount >= 300_000_000: score += 12
    if turnover >= 15: score += 25
    elif turnover >= 8: score += 16
    elif turnover >= 3: score += 8
    score = int(min(score, 100))
    if hard_risk:
        score = max(0, score - 25)

    if enable_risk_filter and hard_risk:
        category = "riskfilter"
        level = "风险过滤"
    else:
        category = "watch" if pct >= 5 else "ignore"
        level = "热门候选" if pct >= 5 else "只看热度"

    tags = ["热门候选" if pct >= 5 else "涨幅不足5%"]
    tags.append("成交额活跃" if amount >= 1_000_000_000 else "成交额一般")
    tags.append("高换手" if turnover >= 8 else "换手一般")
    tags.extend(risk_flags)

    return {
        "symbol": symbol, "name": name, "category": category,
        "rank": 7 if category == "riskfilter" else (3 if category == "watch" else 6),
        "score": 0, "smart_score": score, "tags": tags,
        "risk_flags": risk_flags, "hard_risk": hard_risk,
        "quote": {"date": datetime.now().strftime("%Y-%m-%d"), "open": None, "close": item.get("price"), "volume": None, "amount": amount, "turnover": turnover, "pct_chg": pct},
        "analysis": {"ma5": None, "ma10": None, "ma20": None, "vol_ma5": None, "distance_ma5_pct": None, "below_ma5_days": None},
        "advice": {
            "level": level,
            "action": "触发风险过滤，先排除。" if category == "riskfilter" else "快速筛选结果，只说明热度，不代表买点。可自动连续分批分析确认5日线。",
            "position": "不买。" if category == "riskfilter" else "先观察，不直接追。",
            "risk": "、".join(risk_flags) if risk_flags else "快速候选没有做5日线深度分析，不能直接当买入信号。",
        },
        "trade_plan": {"enabled": False, "summary": "快速候选尚未计算5日线，不能生成实盘交易计划。请先深度分析。", "execution_steps": ["点击自动连续分析，计算 MA5 / MA10 / MA20 后再生成计划。"]},
        "quick_only": True,
    }


def market_prefix(code: str) -> str:
    return "1." if code.startswith(("60", "68")) else "0."


def fetch_eastmoney_spot(page_size: int = 80, sort_field: str = "f3") -> List[Dict[str, Any]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {"pn": "1", "pz": str(page_size), "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": sort_field, "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12,f14,f2,f3,f6,f8", "_": str(int(datetime.now().timestamp() * 1000))}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=12)
    r.raise_for_status()
    rows = (((r.json() or {}).get("data") or {}).get("diff") or [])
    out = []
    for row in rows:
        code = safe_str(row.get("f12"))
        if len(code) != 6 or not code.startswith(("00", "30", "60", "8", "4")):
            continue
        out.append({"symbol": code, "name": safe_str(row.get("f14")), "price": safe_float(row.get("f2")), "pct_chg": safe_float(row.get("f3")), "amount": safe_float(row.get("f6")), "turnover": safe_float(row.get("f8")), "secid": market_prefix(code) + code})
    return out


def get_spot_candidates(limit: int = 60, enable_risk_filter: bool = True, include_risk: bool = True) -> Dict[str, Any]:
    all_rows, errors = [], []
    for sort_field in ["f3", "f6", "f8"]:
        try:
            all_rows.extend(fetch_eastmoney_spot(page_size=limit, sort_field=sort_field))
        except Exception as e:
            errors.append(f"{sort_field}:{e}")
    if not all_rows:
        raise ValueError("东方财富实时行情接口不可用：" + " | ".join(errors))
    seen = {}
    for item in all_rows:
        seen[item["symbol"]] = item
    candidates = list(seen.values())

    if enable_risk_filter and not include_risk:
        candidates = [x for x in candidates if not is_st_stock(x.get("name", "")) and not is_bj_stock(x.get("symbol", "")) and not is_star_market_stock(x.get("symbol", "")) and (x.get("amount") or 0) >= 100_000_000]

    candidates.sort(key=lambda x: (-(x.get("pct_chg") or -999), -(x.get("amount") or 0), -(x.get("turnover") or 0)))
    return {"candidates": candidates[:limit], "source_count": len(candidates), "errors": errors}


def try_fetch_theme_board(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": "1", "pz": str(limit), "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f3", "fs": "m:90+t:3", "fields": "f12,f14,f3", "_": str(int(datetime.now().timestamp() * 1000))}
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        rows = (((r.json() or {}).get("data") or {}).get("diff") or [])
        return [{"theme": safe_str(x.get("f14")), "pct_chg": safe_float(x.get("f3"))} for x in rows[:limit]]
    except Exception:
        return []


def parse_symbols(raw: str) -> List[str]:
    text = (raw or "").replace("，", ",").replace("、", ",").replace("\n", ",").replace(" ", ",")
    result, seen = [], set()
    for x in text.split(","):
        if not x.strip(): continue
        try:
            s = normalize_symbol(x)
            if s not in seen:
                seen.add(s); result.append(s)
        except Exception:
            pass
    return result


def build_summary(results: List[Dict[str, Any]], total_input: int, failed: int) -> Dict[str, Any]:
    return {
        "total_input": total_input, "success": len(results), "failed": failed,
        "safe_near": len([x for x in results if x.get("category") == "safe_near"]),
        "near": len([x for x in results if x.get("category") == "near"]),
        "focus": len([x for x in results if x.get("category") == "focus"]),
        "watch": len([x for x in results if x.get("category") in ["watch", "far"]]),
        "far": len([x for x in results if x.get("category") == "far"]),
        "risk": len([x for x in results if x.get("category") == "risk"]),
        "riskfilter": len([x for x in results if x.get("category") == "riskfilter"]),
        "ignore": len([x for x in results if x.get("category") == "ignore"]),
        "quick_only": len([x for x in results if x.get("quick_only")]),
    }


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "stock-5day-system-v2", "version": "3.4.1-no-688", "time": datetime.now().isoformat(timespec="seconds"), "message": "后端正常，支持过滤688科创板、风险过滤增强、安全接近买点与实盘交易计划"})


@app.route("/api/analyze")
def api_analyze():
    try:
        return jsonify(analyze_stock(request.args.get("symbol", ""), request.args.get("adjust", "")))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 400


@app.route("/api/batch_analyze", methods=["POST"])
def api_batch_analyze():
    payload = request.get_json(silent=True) or {}
    symbols = parse_symbols(payload.get("symbols", ""))[:15]
    if not symbols:
        return jsonify({"ok": False, "error": "没有识别到有效股票代码"}), 400
    results, errors = [], []
    for sym in symbols:
        try:
            results.append(analyze_stock(sym, payload.get("adjust", "")))
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})
    results.sort(key=lambda x: (x.get("rank", 9), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("pct_chg") or 0)))
    return jsonify({"ok": True, "version": "3.4.1-no-688", "summary": build_summary(results, len(symbols), len(errors)), "results": results, "errors": errors})


@app.route("/api/smart_hot", methods=["POST", "GET"])
def api_smart_hot():
    payload = request.get_json(silent=True) if request.method == "POST" else request.args
    payload = payload or {}
    quick_limit = max(20, min(int(payload.get("quick_limit", 35)), 60))
    enable_risk_filter = str(payload.get("enable_risk_filter", "true")).lower() != "false"
    include_risk = str(payload.get("include_risk", "true")).lower() != "false"

    try:
        spot_data = get_spot_candidates(limit=quick_limit, enable_risk_filter=enable_risk_filter, include_risk=include_risk)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__, "where": "eastmoney_spot", "hint": "东方财富实时行情接口暂时不可用。稍后重试，或先用单股分析。", "version": "3.4.1-no-688", "summary": build_summary([], 0, 1), "themes": [], "results": [], "errors": [{"error": str(e)}]}), 200
    candidates = spot_data["candidates"]
    quick_results = [quick_score_from_spot(x, enable_risk_filter=enable_risk_filter) for x in candidates]
    quick_results.sort(key=lambda x: (x.get("rank", 9), -(x.get("quote", {}).get("pct_chg") or 0), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("amount") or 0)))
    summary = build_summary(quick_results[:quick_limit], len(candidates), 0)
    summary["source_count"] = spot_data.get("source_count", 0)
    summary["candidate_count"] = len(candidates)
    summary["deep_analyzed"] = 0
    return jsonify({"ok": True, "version": "3.4.1-no-688", "mode": "risk_filter_quick_first", "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "summary": summary, "themes": try_fetch_theme_board(limit=20), "results": quick_results[:quick_limit], "errors": [{"info": x} for x in spot_data.get("errors", [])]})


@app.route("/api/deep_batch", methods=["POST"])
def api_deep_batch():
    payload = request.get_json(silent=True) or {}
    raw_symbols = payload.get("symbols", [])
    if isinstance(raw_symbols, str):
        symbols = parse_symbols(raw_symbols)
    else:
        symbols = []
        for x in raw_symbols:
            try:
                symbols.append(normalize_symbol(str(x)))
            except Exception:
                pass
    names = payload.get("names", {}) or {}
    spot = payload.get("spot", {}) or {}
    offset = int(payload.get("offset", 0))
    size = max(1, min(int(payload.get("size", 3)), 5))
    adjust = payload.get("adjust", "")
    if not symbols:
        return jsonify({"ok": False, "error": "没有收到需要深度分析的股票代码"}), 400
    batch = symbols[offset:offset + size]
    results, errors = [], []
    for sym in batch:
        try:
            meta = spot.get(sym) if isinstance(spot, dict) else None
            results.append(analyze_stock(sym, adjust=adjust, name=names.get(sym, sym), spot_meta=meta))
        except Exception as e:
            errors.append({"symbol": sym, "name": names.get(sym, sym), "error": str(e)})
    next_offset = offset + size
    done = next_offset >= len(symbols)
    results.sort(key=lambda x: (x.get("rank", 9), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("pct_chg") or 0)))
    return jsonify({"ok": True, "version": "3.4.1-no-688", "offset": offset, "size": size, "next_offset": next_offset, "done": done, "total": len(symbols), "summary": build_summary(results, len(batch), len(errors)), "results": results, "errors": errors})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
