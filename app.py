
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

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
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None

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

def analyze_stock(symbol: str, adjust: str = "") -> Dict[str, Any]:
    symbol = normalize_symbol(symbol)
    adjust = adjust if adjust in ["", "qfq", "hfq"] else ""
    df = fetch_hist(symbol, adjust)
    cols = detect_columns(df)
    work = df.copy()

    for key in ["open", "close", "high", "low", "volume", "amount", "pct_chg"]:
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
    last_high = safe_float(last[cols["high"]])
    last_low = safe_float(last[cols["low"]])
    last_volume = safe_float(last[cols["volume"]])
    ma5 = safe_float(last["MA5"])
    ma10 = safe_float(last["MA10"])
    ma20 = safe_float(last["MA20"])
    vol_ma5 = safe_float(last["VOL_MA5"])

    if last_close is None or last_open is None or ma5 is None:
        raise ValueError("关键行情数据为空，无法分析")

    if "pct_chg" in cols and safe_float(last[cols["pct_chg"]]) is not None:
        pct_chg = safe_float(last[cols["pct_chg"]])
    else:
        prev_close = safe_float(prev[cols["close"]])
        pct_chg = ((last_close - prev_close) / prev_close * 100) if prev_close else None

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
    near_ma5 = distance_ma5_pct is not None and -1 <= distance_ma5_pct <= 3

    score = 0
    if rise_over_5: score += 2
    if volume_breakout: score += 2
    elif volume_above_avg: score += 1
    if big_yang: score += 2
    elif last_close > last_open: score += 1
    if above_ma5: score += 2
    if above_ma10 or above_ma20: score += 1
    if distance_ma5_pct is not None and 0 <= distance_ma5_pct <= 8: score += 1

    if not rise_over_5:
        level, action, position, risk, rank, category = "不看", "涨幅没有超过5%，不符合强势股第一条件。", "不买。", "没有明显主力进攻信号。", 6, "ignore"
    elif below_days >= 3:
        level, action, position, risk, rank, category = "清仓信号", "连续3天收盘没有站回5日线，短线趋势失效。", "已有仓位应清仓；没有仓位不进。", "趋势失效，不要幻想。", 5, "risk"
    elif not above_ma5:
        level, action, position, risk, rank, category = "风控信号", "股价跌破5日线，尾盘站不回先减一半仓。", "不加仓；已有仓位减半或观察到尾盘。", "连续3天站不回5日线，清仓。", 4, "risk"
    elif near_buy_point and score >= 7:
        level, action, position, risk, rank, category = "接近买点", "涨幅超过5%，站上5日线，且距离5日线不远，重点观察回踩不破机会。", "可按计划分批；先小仓确认，不能一次满仓。", "尾盘跌破5日线，减半仓。", 1, "near"
    elif far_from_ma5:
        level, action, position, risk, rank, category = "强势但远离5日线", "股价强势，但已经远离5日线，不适合满仓追高。", "最多半仓；等回踩5日线不破再接回来。", "远离5日线容易冲高回落。", 3, "far"
    elif score >= 8:
        level, action, position, risk, rank, category = "重点关注", "符合强势股条件，等回踩5日线附近不破。", "分批；远离5日线时不要满仓。", "尾盘跌破5日线，减半仓。", 2, "focus"
    elif score >= 6:
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
    elif near_ma5 and above_ma5: tags.append("靠近5日线")

    return {
        "symbol": symbol, "name": symbol, "data_points": int(len(work)), "rank": int(rank), "category": category, "score": int(score), "tags": tags,
        "quote": {"date": str(last[cols["date"]].date()), "open": last_open, "close": last_close, "high": last_high, "low": last_low, "volume": last_volume, "amount": safe_float(last[cols["amount"]]) if "amount" in cols else None, "pct_chg": pct_chg},
        "analysis": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "vol_ma5": vol_ma5, "distance_ma5_pct": distance_ma5_pct, "candle_body_pct": candle_body_pct, "below_ma5_days": below_days},
        "signals": {"rise_over_5": rise_over_5, "volume_breakout": volume_breakout, "volume_above_avg": volume_above_avg, "big_yang": big_yang, "above_ma5": above_ma5, "above_ma10": above_ma10, "above_ma20": above_ma20, "far_from_ma5": far_from_ma5, "near_ma5": near_ma5, "near_buy_point": near_buy_point},
        "advice": {"level": level, "action": action, "position": position, "risk": risk},
    }

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

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "stock-5day-system-v2", "version": "2.1.2-live-watch", "time": datetime.now().isoformat(timespec="seconds"), "message": "后端正常，可以开始分析股票代码"})

@app.route("/api/analyze")
def api_analyze():
    try:
        return jsonify(analyze_stock(request.args.get("symbol", ""), request.args.get("adjust", "")))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/batch_analyze", methods=["POST"])
def api_batch_analyze():
    try:
        payload = request.get_json(silent=True) or {}
        symbols = parse_symbols(payload.get("symbols", ""))
        adjust = payload.get("adjust", "")
        if not symbols:
            raise ValueError("没有识别到有效股票代码，请输入6位股票代码")
        if len(symbols) > 30:
            symbols = symbols[:30]
        results, errors = [], []
        for sym in symbols:
            try:
                results.append(analyze_stock(sym, adjust))
            except Exception as e:
                errors.append({"symbol": sym, "error": str(e)})
        results.sort(key=lambda x: (x.get("rank", 9), -int(x.get("score", 0)), -(x.get("quote", {}).get("pct_chg") or 0)))
        summary = {
            "total_input": len(symbols), "success": len(results), "failed": len(errors),
            "near": len([x for x in results if x["category"] == "near"]),
            "focus": len([x for x in results if x["category"] == "focus"]),
            "watch": len([x for x in results if x["category"] in ["watch", "far"]]),
            "far": len([x for x in results if x["category"] == "far"]),
            "risk": len([x for x in results if x["category"] == "risk"]),
            "ignore": len([x for x in results if x["category"] == "ignore"]),
        }
        return jsonify({"ok": True, "version": "2.1.2-live-watch", "summary": summary, "results": results, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
