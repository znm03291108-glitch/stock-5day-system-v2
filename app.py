
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
from collections import Counter, defaultdict

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


def safe_str(v: Any) -> str:
    try:
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
    start_date = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust=adjust or "",
    )
    if df is None or df.empty:
        raise ValueError("没有获取到行情数据，可能是代码错误、停牌或 AKShare 数据源暂时不可用")
    return df


def analyze_stock(symbol: str, adjust: str = "", name: str = "", spot_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol)
    adjust = adjust if adjust in ["", "qfq", "hfq"] else ""

    df = fetch_hist(symbol, adjust)
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
    last_high = safe_float(last[cols["high"]])
    last_low = safe_float(last[cols["low"]])
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
    near_ma5 = distance_ma5_pct is not None and -1 <= distance_ma5_pct <= 3

    # 100分智能评分：交易纪律 + 热门强度
    score10 = 0
    if rise_over_5: score10 += 2
    if volume_breakout: score10 += 2
    elif volume_above_avg: score10 += 1
    if big_yang: score10 += 2
    elif last_close > last_open: score10 += 1
    if above_ma5: score10 += 2
    if above_ma10 or above_ma20: score10 += 1
    if distance_ma5_pct is not None and 0 <= distance_ma5_pct <= 8: score10 += 1

    smart_score = 0
    smart_score += min(max((pct_chg or 0), 0), 10) * 2          # 涨幅最多20
    smart_score += 20 if volume_breakout else (12 if volume_above_avg else 4)
    smart_score += 20 if (above_ma5 and above_ma10 and above_ma20) else (12 if above_ma5 else 0)
    smart_score += 15 if big_yang else (8 if last_close > last_open else 0)
    smart_score += 15 if near_buy_point else (8 if not far_from_ma5 and above_ma5 else 2)
    if amount:
        # 成交额越大，给少量热度分，amount 通常为元
        if amount >= 5_000_000_000:
            smart_score += 10
        elif amount >= 1_000_000_000:
            smart_score += 7
        elif amount >= 300_000_000:
            smart_score += 4
    smart_score = int(min(round(smart_score), 100))

    if not rise_over_5:
        level, action, position, risk, rank, category = "不看", "涨幅没有超过5%，不符合强势股第一条件。", "不买。", "没有明显主力进攻信号。", 6, "ignore"
    elif below_days >= 3:
        level, action, position, risk, rank, category = "清仓信号", "连续3天收盘没有站回5日线，短线趋势失效。", "已有仓位应清仓；没有仓位不进。", "趋势失效，不要幻想。", 5, "risk"
    elif not above_ma5:
        level, action, position, risk, rank, category = "风控信号", "股价跌破5日线，尾盘站不回先减一半仓。", "不加仓；已有仓位减半或观察到尾盘。", "连续3天站不回5日线，清仓。", 4, "risk"
    elif near_buy_point and smart_score >= 65:
        level, action, position, risk, rank, category = "接近买点", "涨幅超过5%，站上5日线，且距离5日线不远，重点观察回踩不破机会。", "可按计划分批；先小仓确认，不能一次满仓。", "尾盘跌破5日线，减半仓。", 1, "near"
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
    elif near_ma5 and above_ma5: tags.append("靠近5日线")
    if amount and amount >= 1_000_000_000: tags.append("成交额活跃")
    if turnover and turnover >= 8: tags.append("高换手")

    return {
        "symbol": symbol, "name": name or symbol, "data_points": int(len(work)), "rank": int(rank), "category": category,
        "score": int(score10), "smart_score": int(smart_score), "tags": tags,
        "quote": {
            "date": str(last[cols["date"]].date()), "open": last_open, "close": last_close, "high": last_high, "low": last_low,
            "volume": last_volume, "amount": amount, "turnover": turnover, "pct_chg": pct_chg
        },
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


def get_spot_candidates(limit: int = 60) -> Dict[str, Any]:
    import akshare as ak
    spot = ak.stock_zh_a_spot_em()
    if spot is None or spot.empty:
        raise ValueError("没有获取到 A 股实时行情")

    cols = list(spot.columns)
    code_col = "代码" if "代码" in cols else None
    name_col = "名称" if "名称" in cols else None
    pct_col = "涨跌幅" if "涨跌幅" in cols else None
    amount_col = "成交额" if "成交额" in cols else None
    turnover_col = "换手率" if "换手率" in cols else None

    if not code_col or not pct_col:
        raise ValueError(f"实时行情字段不完整，当前字段：{cols}")

    df = spot.copy()
    df[pct_col] = pd.to_numeric(df[pct_col], errors="coerce")
    if amount_col:
        df[amount_col] = pd.to_numeric(df[amount_col], errors="coerce")
    if turnover_col:
        df[turnover_col] = pd.to_numeric(df[turnover_col], errors="coerce")

    # 过滤北交所/风险项可以按需扩展，这里先保留主流 00/30/60/68
    df[code_col] = df[code_col].astype(str)
    df = df[df[code_col].str.len() == 6]
    df = df[df[code_col].str.startswith(("00", "30", "60", "68"))]

    gainers = df.sort_values(pct_col, ascending=False).head(limit)
    amount_top = df.sort_values(amount_col, ascending=False).head(limit) if amount_col else df.head(0)
    turnover_top = df.sort_values(turnover_col, ascending=False).head(limit) if turnover_col else df.head(0)

    pool = pd.concat([gainers, amount_top, turnover_top], ignore_index=True).drop_duplicates(subset=[code_col]).head(limit * 2)

    candidates = []
    for _, row in pool.iterrows():
        code = safe_str(row[code_col])
        if len(code) != 6:
            continue
        candidates.append({
            "symbol": code,
            "name": safe_str(row[name_col]) if name_col else code,
            "pct_chg": safe_float(row[pct_col]) if pct_col else None,
            "amount": safe_float(row[amount_col]) if amount_col else None,
            "turnover": safe_float(row[turnover_col]) if turnover_col else None,
        })

    return {
        "candidates": candidates,
        "source_count": int(len(df)),
        "gainers_count": int(len(gainers)),
        "amount_count": int(len(amount_top)),
        "turnover_count": int(len(turnover_top)),
    }


def try_fetch_theme_board(limit: int = 30) -> List[Dict[str, Any]]:
    try:
        import akshare as ak
        board = ak.stock_board_concept_name_em()
        if board is None or board.empty:
            return []
        cols = list(board.columns)
        name_col = "板块名称" if "板块名称" in cols else ("名称" if "名称" in cols else None)
        pct_col = "涨跌幅" if "涨跌幅" in cols else None
        hot_col = "总市值" if "总市值" in cols else None
        if not name_col:
            return []
        if pct_col:
            board[pct_col] = pd.to_numeric(board[pct_col], errors="coerce")
            board = board.sort_values(pct_col, ascending=False)
        out = []
        for _, row in board.head(limit).iterrows():
            out.append({
                "theme": safe_str(row[name_col]),
                "pct_chg": safe_float(row[pct_col]) if pct_col else None,
                "hot_value": safe_float(row[hot_col]) if hot_col else None,
            })
        return out
    except Exception:
        return []


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "service": "stock-5day-system-v2", "version": "3.0-smart-theme", "time": datetime.now().isoformat(timespec="seconds"), "message": "后端正常，可以开始智能筛选"})


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

        results.sort(key=lambda x: (x.get("rank", 9), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("pct_chg") or 0)))
        summary = build_summary(results, len(symbols), len(errors))
        return jsonify({"ok": True, "version": "3.0-smart-theme", "summary": summary, "results": results, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def build_summary(results: List[Dict[str, Any]], total_input: int, failed: int) -> Dict[str, Any]:
    return {
        "total_input": total_input,
        "success": len(results),
        "failed": failed,
        "near": len([x for x in results if x["category"] == "near"]),
        "focus": len([x for x in results if x["category"] == "focus"]),
        "watch": len([x for x in results if x["category"] in ["watch", "far"]]),
        "far": len([x for x in results if x["category"] == "far"]),
        "risk": len([x for x in results if x["category"] == "risk"]),
        "ignore": len([x for x in results if x["category"] == "ignore"]),
    }


@app.route("/api/smart_hot", methods=["POST", "GET"])
def api_smart_hot():
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
        else:
            payload = request.args

        limit = int(payload.get("limit", 50))
        analyze_limit = int(payload.get("analyze_limit", 35))
        adjust = payload.get("adjust", "")
        min_pct = float(payload.get("min_pct", 5))

        limit = max(20, min(limit, 120))
        analyze_limit = max(10, min(analyze_limit, 60))

        spot_data = get_spot_candidates(limit=limit)
        candidates = spot_data["candidates"]

        # 优先分析涨幅超过 min_pct 的，再补充成交额/换手热门
        strong = [x for x in candidates if (x.get("pct_chg") or 0) >= min_pct]
        rest = [x for x in candidates if x not in strong]
        selected = (strong + rest)[:analyze_limit]

        results, errors = [], []
        for item in selected:
            try:
                meta = {"pct_chg": item.get("pct_chg"), "amount": item.get("amount"), "turnover": item.get("turnover")}
                results.append(analyze_stock(item["symbol"], adjust=adjust, name=item.get("name") or item["symbol"], spot_meta=meta))
            except Exception as e:
                errors.append({"symbol": item.get("symbol"), "name": item.get("name"), "error": str(e)})

        results.sort(key=lambda x: (x.get("rank", 9), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("amount") or 0), -(x.get("quote", {}).get("pct_chg") or 0)))

        # 简易题材：优先获取概念板块热度；若失败，则给出热门股名称池
        themes = try_fetch_theme_board(limit=20)

        summary = build_summary(results, len(selected), len(errors))
        summary["source_count"] = spot_data.get("source_count", 0)
        summary["candidate_count"] = len(candidates)
        summary["analyzed_count"] = len(results)

        return jsonify({
            "ok": True,
            "version": "3.0-smart-theme",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "themes": themes,
            "results": results,
            "errors": errors,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
