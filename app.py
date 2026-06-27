
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
        "version": "3.1-batch-deep",
        "hint": "后端异常已被捕获。建议降低分批深度分析数量，或先用单股分析。",
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

    if not rise_over_5:
        level, action, position, risk, rank, category = "不看", "涨幅没有超过5%，不符合强势股第一条件。", "不买。", "没有明显主力进攻信号。", 6, "ignore"
    elif below_days >= 3:
        level, action, position, risk, rank, category = "清仓信号", "连续3天收盘没有站回5日线，短线趋势失效。", "已有仓位应清仓；没有仓位不进。", "趋势失效，不要幻想。", 5, "risk"
    elif not above_ma5:
        level, action, position, risk, rank, category = "风控信号", "股价跌破5日线，尾盘站不回先减一半仓。", "不加仓；已有仓位减半或观察到尾盘。", "连续3天站不回5日线，清仓。", 4, "risk"
    elif near_buy_point and smart_score >= 65:
        level, action, position, risk, rank, category = "接近买点", "涨幅超过5%，站上5日线，且距离5日线不远。", "可分批；先小仓确认，不能一次满仓。", "尾盘跌破5日线，减半仓。", 1, "near"
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
    if amount and amount >= 1_000_000_000: tags.append("成交额活跃")
    if turnover and turnover >= 8: tags.append("高换手")

    return {
        "symbol": symbol,
        "name": name or symbol,
        "data_points": int(len(work)),
        "rank": rank,
        "category": category,
        "score": int(score10),
        "smart_score": int(smart_score),
        "tags": tags,
        "quote": {
            "date": str(last[cols["date"]].date()),
            "open": last_open,
            "close": last_close,
            "volume": last_volume,
            "amount": amount,
            "turnover": turnover,
            "pct_chg": pct_chg,
        },
        "analysis": {
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "vol_ma5": vol_ma5,
            "distance_ma5_pct": distance_ma5_pct,
            "below_ma5_days": below_days,
        },
        "advice": {"level": level, "action": action, "position": position, "risk": risk},
    }


def quick_score_from_spot(item: Dict[str, Any]) -> Dict[str, Any]:
    pct = item.get("pct_chg") or 0
    amount = item.get("amount") or 0
    turnover = item.get("turnover") or 0
    score = 0
    score += min(max(pct, 0), 10) * 4
    if amount >= 5_000_000_000:
        score += 30
    elif amount >= 1_000_000_000:
        score += 22
    elif amount >= 300_000_000:
        score += 12
    if turnover >= 15:
        score += 25
    elif turnover >= 8:
        score += 16
    elif turnover >= 3:
        score += 8
    score = int(min(score, 100))
    category = "watch" if pct >= 5 else "ignore"
    level = "热门候选" if pct >= 5 else "只看热度"
    return {
        "symbol": item["symbol"],
        "name": item.get("name") or item["symbol"],
        "category": category,
        "rank": 3 if category == "watch" else 6,
        "score": 0,
        "smart_score": score,
        "tags": [
            "热门候选" if pct >= 5 else "涨幅不足5%",
            "成交额活跃" if amount >= 1_000_000_000 else "成交额一般",
            "高换手" if turnover >= 8 else "换手一般",
        ],
        "quote": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "open": None,
            "close": item.get("price"),
            "volume": None,
            "amount": amount,
            "turnover": turnover,
            "pct_chg": pct,
        },
        "analysis": {"ma5": None, "ma10": None, "ma20": None, "vol_ma5": None, "distance_ma5_pct": None, "below_ma5_days": None},
        "advice": {
            "level": level,
            "action": "快速筛选结果，只说明热度，不代表买点。可点分批深度分析确认5日线。",
            "position": "先观察，不直接追。",
            "risk": "快速候选没有做5日线深度分析，不能直接当买入信号。",
        },
        "quick_only": True,
    }


def market_prefix(code: str) -> str:
    if code.startswith(("60", "68")):
        return "1."
    return "0."


def fetch_eastmoney_spot(page_size: int = 80, sort_field: str = "f3") -> List[Dict[str, Any]]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": sort_field,
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f6,f8",
        "_": str(int(datetime.now().timestamp() * 1000)),
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=12)
    r.raise_for_status()
    data = r.json()
    rows = (((data or {}).get("data") or {}).get("diff") or [])
    out = []
    for row in rows:
        code = safe_str(row.get("f12"))
        if len(code) != 6 or not code.startswith(("00", "30", "60", "68")):
            continue
        out.append({
            "symbol": code,
            "name": safe_str(row.get("f14")),
            "price": safe_float(row.get("f2")),
            "pct_chg": safe_float(row.get("f3")),
            "amount": safe_float(row.get("f6")),
            "turnover": safe_float(row.get("f8")),
            "secid": market_prefix(code) + code,
        })
    return out


def get_spot_candidates(limit: int = 60) -> Dict[str, Any]:
    all_rows: List[Dict[str, Any]] = []
    errors = []
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
    candidates.sort(key=lambda x: (-(x.get("pct_chg") or -999), -(x.get("amount") or 0), -(x.get("turnover") or 0)))
    return {"candidates": candidates[:limit], "source_count": len(candidates), "errors": errors}


def try_fetch_theme_board(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(limit), "po": "1", "np": "1", "fltt": "2", "invt": "2",
            "fid": "f3", "fs": "m:90+t:3", "fields": "f12,f14,f3",
            "_": str(int(datetime.now().timestamp() * 1000)),
        }
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
        if not x.strip():
            continue
        try:
            s = normalize_symbol(x)
            if s not in seen:
                seen.add(s); result.append(s)
        except Exception:
            pass
    return result


def build_summary(results: List[Dict[str, Any]], total_input: int, failed: int) -> Dict[str, Any]:
    return {
        "total_input": total_input,
        "success": len(results),
        "failed": failed,
        "near": len([x for x in results if x.get("category") == "near"]),
        "focus": len([x for x in results if x.get("category") == "focus"]),
        "watch": len([x for x in results if x.get("category") in ["watch", "far"]]),
        "far": len([x for x in results if x.get("category") == "far"]),
        "risk": len([x for x in results if x.get("category") == "risk"]),
        "ignore": len([x for x in results if x.get("category") == "ignore"]),
        "quick_only": len([x for x in results if x.get("quick_only")]),
    }


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "service": "stock-5day-system-v2",
        "version": "3.1-batch-deep",
        "time": datetime.now().isoformat(timespec="seconds"),
        "message": "后端正常，支持热门候选分批深度分析"
    })


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
    return jsonify({"ok": True, "version": "3.1-batch-deep", "summary": build_summary(results, len(symbols), len(errors)), "results": results, "errors": errors})


@app.route("/api/smart_hot", methods=["POST", "GET"])
def api_smart_hot():
    payload = request.get_json(silent=True) if request.method == "POST" else request.args
    payload = payload or {}
    min_pct = float(payload.get("min_pct", 5))
    quick_limit = max(20, min(int(payload.get("quick_limit", 35)), 60))
    deep_limit = max(0, min(int(payload.get("deep_limit", 0)), 8))
    adjust = payload.get("adjust", "")

    try:
        spot_data = get_spot_candidates(limit=quick_limit)
    except Exception as e:
        return jsonify({
            "ok": False, "error": str(e), "type": e.__class__.__name__, "where": "eastmoney_spot",
            "hint": "东方财富实时行情接口暂时不可用。稍后重试，或先用单股分析。",
            "version": "3.1-batch-deep", "summary": build_summary([], 0, 1), "themes": [], "results": [], "errors": [{"error": str(e)}],
        }), 200

    candidates = spot_data["candidates"]
    quick_results = [quick_score_from_spot(x) for x in candidates]
    quick_results.sort(key=lambda x: (-(x.get("quote", {}).get("pct_chg") or 0), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("amount") or 0)))

    deep_candidates = [x for x in candidates if (x.get("pct_chg") or 0) >= min_pct]
    deep_candidates = sorted(deep_candidates, key=lambda x: (-(x.get("pct_chg") or 0), -(x.get("amount") or 0)))[:deep_limit]

    deep_results, errors = [], []
    for item in deep_candidates:
        try:
            meta = {"pct_chg": item.get("pct_chg"), "amount": item.get("amount"), "turnover": item.get("turnover")}
            deep_results.append(analyze_stock(item["symbol"], adjust=adjust, name=item.get("name") or item["symbol"], spot_meta=meta))
        except Exception as e:
            errors.append({"symbol": item.get("symbol"), "name": item.get("name"), "error": str(e)})

    deep_symbols = {x["symbol"] for x in deep_results}
    merged = deep_results + [x for x in quick_results if x["symbol"] not in deep_symbols]
    merged.sort(key=lambda x: (x.get("rank", 9), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("pct_chg") or 0)))
    merged = merged[:quick_limit]

    summary = build_summary(merged, len(candidates), len(errors))
    summary["source_count"] = spot_data.get("source_count", 0)
    summary["candidate_count"] = len(candidates)
    summary["deep_analyzed"] = len(deep_results)

    return jsonify({
        "ok": True, "version": "3.1-batch-deep", "mode": "eastmoney_quick_first",
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary, "themes": try_fetch_theme_board(limit=20), "results": merged,
        "errors": errors + [{"info": x} for x in spot_data.get("errors", [])],
    })


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

    return jsonify({
        "ok": True,
        "version": "3.1-batch-deep",
        "offset": offset,
        "size": size,
        "next_offset": next_offset,
        "done": done,
        "total": len(symbols),
        "summary": build_summary(results, len(batch), len(errors)),
        "results": results,
        "errors": errors,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
