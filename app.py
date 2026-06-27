
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
import traceback

try:
    import pandas as pd
except Exception:
    pd = None
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
        "version": "3.7.1.1-railway-startup-fix",
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
        if pd is not None and pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        if pd is not None and pd.isna(v):
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


def detect_columns(df) -> Dict[str, str]:
    if pd is None:
        raise RuntimeError("pandas 未成功加载，请检查 Railway requirements 安装日志")
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
    return mark_validity(result)


def fetch_hist(symbol: str, adjust: str = "") -> pd.DataFrame:
    import akshare as ak
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust=adjust or "")
    if df is None or df.empty:
        raise ValueError("没有获取到行情数据，可能是代码错误、停牌或 AKShare 数据源暂时不可用")
    return df




POSITIVE_KEYWORDS = ["业绩预增", "扭亏", "中标", "签订合同", "回购", "增持", "并购", "重组", "新产品", "涨价", "政策支持", "订单增长", "人工智能", "机器人", "算力", "芯片", "新能源", "低空经济", "军工"]
NEGATIVE_KEYWORDS = ["减持", "立案", "问询函", "业绩下滑", "亏损", "退市", "商誉减值", "股东质押", "解禁", "处罚", "诉讼", "风险警示"]



def _first_existing_col(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for n in names:
        if n in df.columns:
            return n
    return None


def _clean_numeric(v: Any) -> Optional[float]:
    try:
        if v is None or pd.isna(v):
            return None
        s = str(v).replace("%", "").replace(",", "").replace("万元", "").replace("亿元", "").strip()
        if s in ["", "-", "nan", "None"]:
            return None
        return float(s)
    except Exception:
        return None



def _parse_report_date(v: Any) -> Optional[pd.Timestamp]:
    try:
        if v is None or pd.isna(v):
            return None
        s = str(v).strip()
        if not s or s in ["-", "nan", "None"]:
            return None
        # 兼容 2024-12-31 / 20241231 / 2024年12月31日
        s = s.replace("年", "-").replace("月", "-").replace("日", "")
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        return None


def _pick_latest_finance_row(df: pd.DataFrame) -> Dict[str, Any]:
    """
    从财务表中自动识别报告期字段，并按报告期倒序取最新一行。
    避免 AKShare 返回 2012 年等旧数据时直接使用第一行。
    """
    if df is None or df.empty:
        return {"row": None, "date_col": None, "latest_date": None, "sorted": False}

    date_col = _first_existing_col(df, ["报告期", "日期", "公告日期", "REPORT_DATE", "report_date", "截止日期", "报表日期"])
    work = df.copy()

    if date_col:
        work["_report_dt"] = work[date_col].apply(_parse_report_date)
        work = work.dropna(subset=["_report_dt"])
        if not work.empty:
            work = work.sort_values("_report_dt", ascending=False).reset_index(drop=True)
            row = work.iloc[0]
            return {"row": row, "date_col": date_col, "latest_date": row["_report_dt"], "sorted": True}

    # 如果没有日期字段，保守返回第一行，但标记未排序
    return {"row": df.iloc[0], "date_col": date_col, "latest_date": None, "sorted": False}


def _report_is_stale(report_dt: Optional[pd.Timestamp], max_days: int = 500) -> bool:
    try:
        if report_dt is None or pd.isna(report_dt):
            return True
        now = pd.Timestamp(datetime.now().date())
        return (now - report_dt).days > max_days
    except Exception:
        return True


def fetch_real_financial_profile(symbol: str) -> Dict[str, Any]:
    """
    尝试读取真实财务指标。
    使用 AKShare，若接口字段变化/限流/超时，则返回可解释的降级结果。
    """
    profile = {
        "ok": False,
        "source": "akshare",
        "annual_profit": None,
        "quarter_profit": None,
        "revenue_yoy": None,
        "profit_yoy": None,
        "roe": None,
        "gross_margin": None,
        "report_date": "",
        "report_is_stale": True,
        "report_sort_status": "未排序",
        "performance_text": "",
        "errors": [],
    }
    try:
        import akshare as ak
        # 优先尝试财务摘要/指标接口，不同 AKShare 版本字段可能不同，所以做宽松识别
        df = None
        try:
            df = ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
        except Exception as e:
            profile["errors"].append("financial_abstract_ths:" + str(e)[:80])
        if df is None or getattr(df, "empty", True):
            try:
                df = ak.stock_financial_analysis_indicator(symbol=symbol)
            except Exception as e:
                profile["errors"].append("financial_analysis_indicator:" + str(e)[:80])

        if df is not None and not df.empty:
            picked = _pick_latest_finance_row(df)
            row = picked.get("row")
            date_col = picked.get("date_col")
            latest_dt = picked.get("latest_date")
            profile["report_sort_status"] = "已按报告期倒序取最新" if picked.get("sorted") else "未识别报告期，保守使用原始第一行"

            if row is not None:
                profit_col = _first_existing_col(df, ["净利润", "归母净利润", "扣非净利润", "净利润-同比增长", "归属净利润"])
                profit_yoy_col = _first_existing_col(df, ["净利润同比增长率", "归母净利润同比增长率", "净利润同比", "净利润增长率", "净利润-同比增长"])
                revenue_yoy_col = _first_existing_col(df, ["营业收入同比增长率", "营收同比", "营业收入增长率", "营业总收入同比增长率"])
                roe_col = _first_existing_col(df, ["净资产收益率", "ROE", "加权净资产收益率"])
                gm_col = _first_existing_col(df, ["销售毛利率", "毛利率"])

                profile["report_date"] = str(latest_dt.date()) if latest_dt is not None and not pd.isna(latest_dt) else (safe_str(row.get(date_col)) if date_col else "")
                profile["report_is_stale"] = _report_is_stale(latest_dt, max_days=500)
                profile["quarter_profit"] = _clean_numeric(row.get(profit_col)) if profit_col else None
                profile["profit_yoy"] = _clean_numeric(row.get(profit_yoy_col)) if profit_yoy_col else None
                profile["revenue_yoy"] = _clean_numeric(row.get(revenue_yoy_col)) if revenue_yoy_col else None
                profile["roe"] = _clean_numeric(row.get(roe_col)) if roe_col else None
                profile["gross_margin"] = _clean_numeric(row.get(gm_col)) if gm_col else None
                profile["ok"] = True

                if profile["report_is_stale"]:
                    profile["errors"].append("财报报告期过旧或无法确认最新报告期，不参与加分")

        # 业绩预告接口：字段可能变化，尽量只取标题/摘要
        try:
            yjyg = ak.stock_yjyg_em(date=datetime.now().strftime("%Y0331"))
            if yjyg is not None and not yjyg.empty:
                code_col = _first_existing_col(yjyg, ["股票代码", "代码", "证券代码"])
                if code_col:
                    hit = yjyg[yjyg[code_col].astype(str).str.zfill(6) == symbol]
                    if not hit.empty:
                        r = hit.iloc[0]
                        txt = " ".join([safe_str(x) for x in r.values[:8]])
                        profile["performance_text"] = txt[:200]
                        profile["ok"] = True
        except Exception as e:
            profile["errors"].append("yjyg:" + str(e)[:80])

    except Exception as e:
        profile["errors"].append("real_finance:" + str(e)[:120])
    return profile


def fetch_real_announcements(symbol: str, limit: int = 8) -> Dict[str, Any]:
    """
    尝试读取近期公告标题。
    优先 AKShare；失败时返回空公告并说明原因。
    """
    trading_status = get_trading_session_status()
    result = {
        "ok": False,
        "trading_status": trading_status,
        "source": "akshare",
        "announcements": [],
        "positive_hits": [],
        "negative_hits": [],
        "errors": [],
    }
    try:
        import akshare as ak
        df = None
        funcs = [
            ("stock_notice_report", lambda: ak.stock_notice_report(symbol="全部")),
        ]
        for name, fn in funcs:
            try:
                df = fn()
                if df is not None and not df.empty:
                    break
            except Exception as e:
                result["errors"].append(name + ":" + str(e)[:80])

        if df is not None and not df.empty:
            code_col = _first_existing_col(df, ["代码", "股票代码", "证券代码"])
            title_col = _first_existing_col(df, ["公告标题", "标题", "公告名称"])
            date_col = _first_existing_col(df, ["公告日期", "日期"])
            if code_col and title_col:
                hit = df[df[code_col].astype(str).str.zfill(6) == symbol]
                for _, row in hit.head(limit).iterrows():
                    title = safe_str(row.get(title_col))
                    dt = safe_str(row.get(date_col)) if date_col else ""
                    result["announcements"].append({"date": dt, "title": title})
                result["ok"] = True

        joined = " ".join([x["title"] for x in result["announcements"]])
        result["positive_hits"] = [k for k in POSITIVE_KEYWORDS if k in joined]
        result["negative_hits"] = [k for k in NEGATIVE_KEYWORDS if k in joined]
    except Exception as e:
        result["errors"].append("announcement:" + str(e)[:120])
    return mark_validity(result)



MAJOR_BAD_NEWS = ["立案", "退市", "风险警示", "处罚", "问询函", "财报亏损", "净利润大幅下滑", "营收大幅下滑"]
SOFT_BAD_NEWS = ["减持", "解禁", "亏损", "业绩下滑", "股东质押"]

def apply_technical_first_adjustment(category, level, rank, smart_score, risk_flags, fundamental_profile, pct_chg, amount, turnover, distance_ma5_pct, above_ma5):
    real = (fundamental_profile or {}).get("real_data") or {}
    finance_risk = real.get("finance_risk") or []
    negative_hits = (fundamental_profile or {}).get("negative_keywords") or []
    positive_hits = (fundamental_profile or {}).get("positive_keywords") or []
    risk_text = " ".join([str(x) for x in (risk_flags + finance_risk + negative_hits)])
    major_bad = any(k in risk_text for k in MAJOR_BAD_NEWS)
    soft_bad = any(k in risk_text for k in SOFT_BAD_NEWS)
    technical_strong = (pct_chg is not None and pct_chg >= 5 and above_ma5 and amount is not None and amount >= 200_000_000 and turnover is not None and turnover >= 3)
    near_good = distance_ma5_pct is not None and 0 <= distance_ma5_pct <= 3
    far_high = distance_ma5_pct is not None and distance_ma5_pct >= 6
    tag = "normal"; new_level = level; note = "技术优先：最终仍按5日线纪律执行。"; position_adjust = "按原计划执行。"
    if major_bad:
        if technical_strong:
            tag="strong_but_major_risk"; new_level="技术强但公告/财报风险高"; note="技术形态较强，但存在重大公告/财报风险，只适合观察或极短线，不适合重仓隔夜。"; position_adjust="仓位降低到计划仓位的1/3以内；严格看2:50和5日线。"; smart_score=max(0, smart_score-12)
            if category in ["quality_safe_near","safe_near"]: category="near"; rank=max(rank,2)
        else:
            tag="major_risk_filter"; new_level="重大风险过滤"; note="技术不够强且存在重大风险，暂不纳入买点池。"; position_adjust="不买。"; category="riskfilter"; rank=7; smart_score=max(0,smart_score-25)
    elif soft_bad and technical_strong:
        tag="emotion_short_term"; new_level="技术强但基本面弱"; note="技术面强，基本面/公告有瑕疵，定位为短线情绪票，不按价值票处理。"; position_adjust="只做短线；仓位降低；不适合长期拿。"; smart_score=max(0,smart_score-6)
        if category=="quality_safe_near": category="safe_near"; rank=max(rank,1)
    elif technical_strong and (positive_hits or ("净利润高增长" in (real.get("finance_tags") or [])) or ("营收增长" in (real.get("finance_tags") or []))):
        if near_good and category in ["safe_near","quality_safe_near"]:
            tag="tech_strong_with_support"; new_level="技术强且有基本面/利好支撑"; note="技术位置较好，并有公告/财务正面信号，可优先观察。"; position_adjust="仍然分批，不能一次满仓。"; category="quality_safe_near"; rank=0; smart_score=min(100,smart_score+5)
        elif far_high:
            tag="strong_wait_pullback"; new_level="技术强有支撑但远离5日线"; note="有支撑但已经远离5日线，仍然等回踩，不追高。"; position_adjust="等待回踩5日线附近再说。"
    elif technical_strong and not real.get("data_ok"):
        tag="tech_first_data_unknown"; new_level="技术强，基本面待核对"; note="技术面符合短线条件，但真实财报/公告数据不足，实盘前人工核对。"; position_adjust="按技术计划，小仓/分批执行。"
    fundamental_profile["technical_first"]={"tag":tag,"level":new_level,"note":note,"position_adjust":position_adjust,"major_bad":bool(major_bad),"soft_bad":bool(soft_bad),"technical_strong":bool(technical_strong),"principle":"技术面决定是否进入观察，基本面决定仓位和隔夜信心。"}
    return {"category":category,"level":new_level,"rank":rank,"smart_score":int(max(0,min(100,smart_score))),"fundamental_profile":fundamental_profile,"note":note,"position_adjust":position_adjust}


def build_real_data_profile(
    symbol: str,
    name: str,
    risk_flags: List[str],
) -> Dict[str, Any]:
    finance = fetch_real_financial_profile(symbol)
    announce = fetch_real_announcements(symbol, limit=8)

    data_ok = finance.get("ok") or announce.get("ok")
    positive = list(dict.fromkeys((announce.get("positive_hits") or [])))
    negative = list(dict.fromkeys((announce.get("negative_hits") or [])))

    finance_tags = []
    report_stale = bool(finance.get("report_is_stale", True))
    q_profit = finance.get("quarter_profit")
    profit_yoy = finance.get("profit_yoy")
    revenue_yoy = finance.get("revenue_yoy")
    roe = finance.get("roe")

    if q_profit is not None:
        finance_tags.append("盈利" if q_profit > 0 else "亏损")
    if profit_yoy is not None:
        if profit_yoy >= 30:
            finance_tags.append("净利润高增长")
        elif profit_yoy < -20:
            finance_tags.append("净利润下滑")
    if revenue_yoy is not None:
        if revenue_yoy >= 20:
            finance_tags.append("营收增长")
        elif revenue_yoy < -10:
            finance_tags.append("营收下滑")
    if roe is not None and roe >= 8:
        finance_tags.append("ROE较好")

    if report_stale:
        finance_tags = ["财报数据过旧"] if finance.get("report_date") else ["财报日期缺失"]
        # 过旧财报不参与盈利/增长加分，只作为参考显示
        q_profit = None
        profit_yoy = None
        revenue_yoy = None
        roe = None

    if finance.get("performance_text"):
        txt = finance.get("performance_text", "")
        positive += [k for k in POSITIVE_KEYWORDS if k in txt]
        negative += [k for k in NEGATIVE_KEYWORDS if k in txt]

    finance_risk = []
    if q_profit is not None and q_profit < 0:
        finance_risk.append("财报亏损")
    if profit_yoy is not None and profit_yoy < -30:
        finance_risk.append("净利润大幅下滑")
    if revenue_yoy is not None and revenue_yoy < -20:
        finance_risk.append("营收大幅下滑")
    if negative:
        finance_risk.append("公告利空")

    conclusion = "真实数据不足，按轻量规则辅助判断"
    if data_ok:
        if report_stale:
            conclusion = "真实财报报告期过旧或无法确认最新，仅作参考，不参与加分"
        elif finance_risk:
            conclusion = "真实财报/公告存在风险，需要谨慎"
        elif positive or ("净利润高增长" in finance_tags) or ("营收增长" in finance_tags):
            conclusion = "真实财报/公告存在积极信号，可提高关注级别"
        elif finance_tags:
            conclusion = "真实财务数据为中性或待进一步确认"
        else:
            conclusion = "已尝试读取真实数据，但可用字段有限"

    return {
        "enabled": True,
        "data_ok": bool(data_ok),
        "finance": finance,
        "announcements": announce.get("announcements") or [],
        "finance_tags": list(dict.fromkeys(finance_tags)),
        "positive_hits": list(dict.fromkeys(positive)),
        "negative_hits": list(dict.fromkeys(negative)),
        "finance_risk": finance_risk,
        "conclusion": conclusion,
        "errors": (finance.get("errors") or []) + (announce.get("errors") or []),
        "data_note": "真实数据来自 AKShare 财务/公告接口；如接口限流、字段变化或数据缺失，会自动降级到轻量验证。",
    }



def infer_themes(name: str, symbol: str = "") -> List[str]:
    """轻量级题材推断。没有外部公告源时，用名称关键词做保守判断。"""
    n = (name or "").lower()
    themes = []
    mapping = [
        ("牧", "农业/养殖"), ("农", "农业/养殖"), ("乳", "消费/食品"), ("食", "消费/食品"),
        ("芯", "芯片/半导体"), ("半导体", "芯片/半导体"), ("电子", "电子科技"),
        ("机器人", "机器人"), ("智能", "人工智能"), ("软件", "人工智能"), ("数据", "数据要素"),
        ("光伏", "新能源"), ("电池", "新能源"), ("锂", "新能源"), ("储能", "新能源"),
        ("军", "军工"), ("航", "军工/航空"), ("船", "军工/船舶"),
        ("医", "医药"), ("药", "医药"), ("生物", "医药"),
        ("汽车", "汽车产业链"), ("车", "汽车产业链"),
        ("传媒", "传媒娱乐"), ("文化", "传媒娱乐"), ("游戏", "传媒娱乐"),
        ("金", "金融/有色"), ("铜", "有色金属"), ("铝", "有色金属"), ("矿", "有色金属"),
    ]
    for k, v in mapping:
        if k in n and v not in themes:
            themes.append(v)
    if symbol.startswith("30"):
        themes.append("创业板")
    elif symbol.startswith("60"):
        themes.append("主板")
    return themes[:4] if themes else ["题材待确认"]


def build_fundamental_news_profile(
    symbol: str,
    name: str,
    pct_chg: Optional[float],
    amount: Optional[float],
    turnover: Optional[float],
    risk_flags: List[str],
    category: str,
    real_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Railway 免费环境里尽量避免重接口。
    这里先做轻量版：基于名称、涨幅、成交额、换手率、风险标签生成业绩/利好验证框架。
    后续可接入公告接口后替换这里。
    """
    themes = infer_themes(name, symbol)
    positive = []
    negative = []

    text = f"{name} {' '.join(themes)}"
    for k in POSITIVE_KEYWORDS:
        if k in text:
            positive.append(k)
    for k in NEGATIVE_KEYWORDS:
        if k in text:
            negative.append(k)

    # 技术分由涨幅、成交额、换手构成
    technical_score = 0
    if pct_chg is not None:
        if pct_chg >= 5:
            technical_score += 35
        if pct_chg >= 8:
            technical_score += 10
    if amount is not None:
        if amount >= 2_000_000_000:
            technical_score += 25
        elif amount >= 500_000_000:
            technical_score += 18
        elif amount >= 100_000_000:
            technical_score += 10
    if turnover is not None:
        if 3 <= turnover <= 20:
            technical_score += 25
        elif 20 < turnover <= 30:
            technical_score += 12
        elif turnover > 30:
            technical_score += 5
    technical_score = min(100, technical_score)

    # 题材分：只做保守加分，不把它当买点
    theme_score = 45
    if themes and themes != ["题材待确认"]:
        theme_score += 25
    if any(x in themes for x in ["人工智能", "机器人", "芯片/半导体", "新能源", "军工", "数据要素"]):
        theme_score += 20
    theme_score = min(100, theme_score)

    # 业绩分：轻量版默认中性；风险票扣分；名称/题材不代表业绩
    performance_score = 60
    if any("ST" in x or "退" in x or "亏损" in x for x in risk_flags):
        performance_score -= 30
    if any(x in positive for x in ["业绩预增", "扭亏", "订单增长"]):
        performance_score += 25
    if any(x in negative for x in ["业绩下滑", "亏损", "退市"]):
        performance_score -= 35
    performance_score = max(0, min(100, performance_score))

    # 真实财报与公告数据加权
    real_profile = real_profile or {"enabled": False}
    real_positive = real_profile.get("positive_hits") or []
    real_negative = real_profile.get("negative_hits") or []
    finance_tags = real_profile.get("finance_tags") or []
    finance_risk = real_profile.get("finance_risk") or []
    real_finance = real_profile.get("finance") or {}
    real_report_stale = bool(real_finance.get("report_is_stale", True))

    positive = list(dict.fromkeys(positive + real_positive))
    negative = list(dict.fromkeys(negative + real_negative))

    if (not real_report_stale) and ("净利润高增长" in finance_tags or "营收增长" in finance_tags or real_positive):
        performance_score += 18
        good_news_score = min(100, locals().get("good_news_score", 0) + 15)
    if finance_risk or real_negative:
        performance_score -= 28
        good_news_score = max(0, locals().get("good_news_score", 0) - 22)
    if real_report_stale:
        # 过旧财报不扣技术分，只取消基本面加分，并提示人工核对
        performance_score = min(performance_score, 60)
    if "财报亏损" in finance_risk or "净利润大幅下滑" in finance_risk:
        risk_flags = list(dict.fromkeys(risk_flags + finance_risk))

    performance_score = max(0, min(100, performance_score))


    # 风险分越高越危险
    risk_score = 0
    for flag in risk_flags:
        if "ST" in flag or "退" in flag or "688" in flag or "北交所" in flag:
            risk_score += 35
        elif "不足" in flag or "过高" in flag or "高波动" in flag:
            risk_score += 20
        else:
            risk_score += 10
    risk_score = min(100, risk_score)

    good_news_score = min(100, 50 + len(positive) * 12 - len(negative) * 18)
    composite = int(technical_score * 0.38 + theme_score * 0.22 + performance_score * 0.25 + locals().get("good_news_score", 0) * 0.15 - risk_score * 0.25)
    composite = max(0, min(100, composite))

    if category == "safe_near" and composite >= 75 and risk_score <= 20:
        final_level = "优质安全接近买点"
        final_tag = "quality_safe_near"
        conclusion = "技术位置较好，风险标签较少，题材/业绩验证为中性偏好。"
    elif category in ["safe_near", "near"] and risk_score <= 35:
        final_level = "技术买点待验证"
        final_tag = "validated_near"
        conclusion = "技术位置接近买点，但仍需人工核对公告、业绩和题材持续性。"
    elif risk_score >= 50:
        final_level = "技术强但风险高"
        final_tag = "fundamental_risk"
        conclusion = "涨幅或热度较高，但风险标签较重，不适合放进安全买点池。"
    else:
        final_level = "普通观察"
        final_tag = "normal_watch"
        conclusion = "可以观察，但基本面和利好支撑不够明确。"

    return {
        "enabled": True,
        "themes": themes,
        "positive_keywords": positive,
        "negative_keywords": negative,
        "technical_score": int(technical_score),
        "theme_score": int(theme_score),
        "performance_score": int(performance_score),
        "good_news_score": int(locals().get("good_news_score", 0)),
        "risk_score": int(risk_score),
        "composite_score": int(composite),
        "final_level": final_level,
        "final_tag": final_tag,
        "conclusion": conclusion,
        "real_data": real_profile,
        "real_data_ok": bool(real_profile.get("data_ok")) if real_profile else False,
        "data_note": "V3.6真实数据版：优先读取真实财务/公告数据；接口失败时自动降级到关键词/规则识别。实盘前仍需人工核对公告和财报。",
    }



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
    good_news_score = 0
    bad_news_score = 0
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
    real_data_profile = build_real_data_profile(symbol=symbol, name=name or symbol, risk_flags=risk_flags)
    fundamental_profile = build_fundamental_news_profile(
        symbol=symbol,
        name=name or symbol,
        pct_chg=pct_chg,
        amount=amount,
        turnover=turnover,
        risk_flags=risk_flags,
        category=category,
        real_profile=real_data_profile,
    )

    if category == "safe_near" and fundamental_profile.get("final_tag") == "quality_safe_near":
        category = "quality_safe_near"
        level = "优质安全接近买点"
        rank = 0
        tags.append("业绩/题材验证通过")
        action = "技术位置较好，风险标签较少，题材/业绩验证中性偏好。仍需人工核对公告后执行。"
        position = "可加入优先观察池；按交易计划分批，不能一次满仓。"
        risk = "实盘前必须核对公告、财报和分时走势。"
        fundamental_profile["final_level"] = "优质安全接近买点"
        fundamental_profile["final_tag"] = "quality_safe_near"

    tech_adjust = apply_technical_first_adjustment(
        category=category,
        level=level,
        rank=rank,
        smart_score=smart_score,
        risk_flags=risk_flags,
        fundamental_profile=fundamental_profile,
        pct_chg=pct_chg,
        amount=amount,
        turnover=turnover,
        distance_ma5_pct=distance_ma5_pct,
        above_ma5=above_ma5,
    )
    category = tech_adjust["category"]
    level = tech_adjust["level"]
    rank = tech_adjust["rank"]
    smart_score = tech_adjust["smart_score"]
    fundamental_profile = tech_adjust["fundamental_profile"]
    if tech_adjust.get("note"):
        tags.append(tech_adjust["level"])
        risk = (risk + "；" if risk else "") + tech_adjust["note"]
    if tech_adjust.get("position_adjust") and tech_adjust["position_adjust"] != "按原计划执行。":
        position = position + "；" + tech_adjust["position_adjust"]


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
        return [{"theme": safe_str(x.get("f14")), "board_code": safe_str(x.get("f12")), "pct_chg": safe_float(x.get("f3"))} for x in rows[:limit]]
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
    return mark_validity(result)


def build_summary(results: List[Dict[str, Any]], total_input: int, failed: int) -> Dict[str, Any]:
    return {
        "total_input": total_input, "success": len(results), "failed": failed,
        "quality_safe_near": len([x for x in results if x.get("category") == "quality_safe_near"]),
        "emotion_short_term": len([x for x in results if ((x.get("fundamental_profile") or {}).get("technical_first") or {}).get("tag") == "emotion_short_term"]),
        "major_risk": len([x for x in results if ((x.get("fundamental_profile") or {}).get("technical_first") or {}).get("major_bad")]),
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
    return jsonify({"ok": True, "service": "stock-5day-system-v2", "version": "3.7.1.1-railway-startup-fix", "time": datetime.now().isoformat(timespec="seconds"), "message": "后端正常，支持财报结果中文解释、交易日状态识别、大盘情绪联动与实盘交易计划"})



@app.route("/api/real_profile")
def api_real_profile():
    try:
        symbol = normalize_symbol(request.args.get("symbol", ""))
        return jsonify({
            "ok": True,
            "version": "3.7.1.1-railway-startup-fix",
            "symbol": symbol,
            "real_data": build_real_data_profile(symbol=symbol, name=symbol, risk_flags=[]),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__}), 400


@app.route("/api/analyze")
def api_analyze():
    good_news_score = 0
    bad_news_score = 0
    finance_score = 0
    news_score = 0
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
    return jsonify({"ok": True, "version": "3.7.1.1-railway-startup-fix", "summary": build_summary(results, len(symbols), len(errors)), "results": sort_valid_candidates([mark_validity(x) for x in results]), "errors": errors})








def safe_int_value(v, default=0):
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


# ===== V3.6.7.1 compatibility fixes =====
def fetch_real_data(symbol: str) -> Dict[str, Any]:
    """
    兼容旧版本函数名：部分版本使用 fetch_real_data_for_symbol / get_real_data / fetch_finance_and_news。
    如果不存在，就用财报与公告的轻量组合，保证 /api/finance_explain 和测试财报解释不报错。
    """
    symbol = safe_str(symbol)
    # 优先调用已有旧函数
    for fn_name in ["fetch_real_data_for_symbol", "get_real_data", "fetch_finance_and_news", "fetch_company_real_data"]:
        fn = globals().get(fn_name)
        if callable(fn) and fn_name != "fetch_real_data":
            try:
                return fn(symbol)
            except Exception:
                pass

    data = {
        "enabled": True,
        "data_OK": False,
        "data_note": "V3.6.7.1 兼容读取：若财报/公告接口失败，会返回轻量解释。",
        "finance": {
            "ok": False,
            "source": "compat",
            "errors": [],
            "report_date": None,
            "profit_yoy": None,
            "revenue_yoy": None,
            "gross_margin": None,
            "roe": None,
            "report_is_stale": True,
            "report_sort_status": "未取得财报数据"
        },
        "announcements": [],
        "positive_hits": [],
        "negative_hits": [],
        "finance_tags": [],
        "finance_risk": [],
        "conclusion": "暂未取得真实财报/公告数据，仅可做技术面参考。",
    }

    try:
        # 直接使用 AKShare 读取最新主要财务指标。接口有变化时不会中断主程序。
        import akshare as ak
        import pandas as pd
        indicators = None
        for api_name in ["stock_financial_analysis_indicator", "stock_financial_abstract"]:
            try:
                api = getattr(ak, api_name, None)
                if callable(api):
                    if api_name == "stock_financial_analysis_indicator":
                        indicators = api(symbol=symbol)
                    else:
                        indicators = api(symbol=symbol)
                    if indicators is not None and len(indicators) > 0:
                        break
            except Exception as e:
                data["finance"]["errors"].append(f"{api_name}: {str(e)[:80]}")

        if indicators is not None and len(indicators) > 0:
            df = indicators.copy()
            # 尝试找到日期列并倒序
            date_col = None
            for c in df.columns:
                if any(k in str(c) for k in ["日期", "报告期", "截止"]):
                    date_col = c
                    break
            if date_col:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df = df.sort_values(date_col, ascending=False)
            row = df.iloc[0].to_dict()

            def pick(keys):
                for k in keys:
                    for col, val in row.items():
                        if k in str(col):
                            try:
                                if val in ["", "-", "--", None]:
                                    continue
                                return float(val)
                            except Exception:
                                continue
                return None

            report_date = None
            if date_col and not pd.isna(row.get(date_col)):
                report_date = str(row.get(date_col))[:10]
            else:
                for col, val in row.items():
                    if any(k in str(col) for k in ["日期", "报告期", "截止"]):
                        report_date = str(val)[:10]
                        break

            profit_yoy = pick(["净利润同比", "净利润增长率", "净利润增长"])
            revenue_yoy = pick(["营业收入同比", "营收同比", "主营业务收入增长率", "营业收入增长率"])
            gross_margin = pick(["销售毛利率", "毛利率"])
            roe = pick(["净资产收益率", "ROE", "加权净资产收益率"])

            data["finance"].update({
                "ok": True,
                "source": "akshare-compat",
                "report_date": report_date,
                "profit_yoy": profit_yoy,
                "revenue_yoy": revenue_yoy,
                "gross_margin": gross_margin,
                "roe": roe,
                "report_is_stale": False if report_date else True,
                "report_sort_status": "兼容模式按报告期倒序取最新" if report_date else "兼容模式未识别报告期",
            })
            data["data_OK"] = True

            if profit_yoy is not None and profit_yoy > 30:
                data["finance_tags"].append("净利润增长")
            if revenue_yoy is not None and revenue_yoy < 0:
                data["finance_risk"].append("营收下滑")
            if gross_margin is not None and gross_margin > 30:
                data["finance_tags"].append("毛利率较好")

            if data["finance_tags"]:
                data["conclusion"] = "真实财报存在一定积极信号，可作为辅助加分项。"
            if data["finance_risk"]:
                data["conclusion"] += " 但存在风险项，需核对公告原文。"

    except Exception as e:
        data["finance"]["errors"].append(str(e)[:120])
        data["conclusion"] = "财报/公告接口暂时不可用，系统已降级为技术面参考。"

    return data
# ===== end compatibility fixes =====




# ===== V3.6.8 finance stable helpers =====
def normalize_finance_value(v):
    try:
        if v is None:
            return None
        s = str(v).replace("%", "").replace(",", "").strip()
        if s in ["", "-", "--", "nan", "None", "暂无"]:
            return None
        return float(s)
    except Exception:
        return None


def finance_pick(row: Dict[str, Any], keyword_groups):
    """
    多字段兼容：AKShare 不同接口字段名称经常变动，这里用关键词模糊匹配。
    """
    if not row:
        return None
    for group in keyword_groups:
        for col, val in row.items():
            c = str(col).lower()
            ok = True
            for k in group:
                if str(k).lower() not in c:
                    ok = False
                    break
            if ok:
                vv = normalize_finance_value(val)
                if vv is not None:
                    return vv
    return None


def stable_fetch_finance_from_akshare(symbol: str) -> Dict[str, Any]:
    """
    V3.6.8 稳定财报读取：
    1. 多接口尝试
    2. 多字段名兼容
    3. 取不到时返回 unavailable，不误判为差
    """
    result = {
        "ok": False,
        "source": "akshare-stable",
        "errors": [],
        "report_date": None,
        "profit_yoy": None,
        "revenue_yoy": None,
        "gross_margin": None,
        "roe": None,
        "report_is_stale": None,
        "report_sort_status": "财报暂未取得",
        "data_available": False,
        "confidence": "低",
        "missing_reason": "暂未取得有效财报字段",
    }

    try:
        import akshare as ak
        import pandas as pd
    except Exception as e:
        result["errors"].append("akshare_import:" + str(e)[:100])
        result["missing_reason"] = "AKShare 未安装或导入失败"
        return mark_validity(result)

    api_candidates = [
        ("stock_financial_analysis_indicator", {"symbol": symbol}),
        ("stock_financial_abstract", {"symbol": symbol}),
        ("stock_financial_report_sina", {"stock": symbol, "symbol": "利润表"}),
        ("stock_financial_report_sina", {"stock": symbol, "symbol": "资产负债表"}),
    ]

    best_row = None
    best_source = None
    best_date_col = None

    for api_name, kwargs in api_candidates:
        try:
            fn = getattr(ak, api_name, None)
            if not callable(fn):
                continue
            df = fn(**kwargs)
            if df is None or len(df) == 0:
                result["errors"].append(api_name + ": empty")
                continue

            # 只取 DataFrame
            try:
                df = df.copy()
            except Exception:
                result["errors"].append(api_name + ": not_dataframe")
                continue

            date_col = None
            for c in df.columns:
                cs = str(c)
                if any(k in cs for k in ["日期", "报告期", "截止", "公告日期", "报表日期"]):
                    date_col = c
                    break

            if date_col:
                try:
                    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                    df = df.sort_values(date_col, ascending=False)
                except Exception:
                    pass

            row = df.iloc[0].to_dict()
            # 简单评分：能取到越多关键字段越好
            p = finance_pick(row, [["净利润", "同比"], ["净利润", "增长"], ["归母", "同比"], ["归属", "同比"]])
            r = finance_pick(row, [["营业收入", "同比"], ["营收", "同比"], ["主营", "增长"], ["营业收入", "增长"]])
            gm = finance_pick(row, [["毛利率"], ["销售", "毛利"]])
            roe = finance_pick(row, [["roe"], ["净资产收益率"], ["净资产", "收益"]])

            score = sum(x is not None for x in [p, r, gm, roe])
            if score > 0:
                best_row = row
                best_source = api_name
                best_date_col = date_col
                break

        except Exception as e:
            result["errors"].append(api_name + ":" + str(e)[:120])

    if not best_row:
        return mark_validity(result)

    profit_yoy = finance_pick(best_row, [["净利润", "同比"], ["净利润", "增长"], ["归母", "同比"], ["归属", "同比"]])
    revenue_yoy = finance_pick(best_row, [["营业收入", "同比"], ["营收", "同比"], ["主营", "增长"], ["营业收入", "增长"]])
    gross_margin = finance_pick(best_row, [["毛利率"], ["销售", "毛利"]])
    roe = finance_pick(best_row, [["roe"], ["净资产收益率"], ["净资产", "收益"]])

    report_date = None
    if best_date_col:
        try:
            val = best_row.get(best_date_col)
            report_date = str(val)[:10]
        except Exception:
            pass
    if not report_date:
        for col, val in best_row.items():
            if any(k in str(col) for k in ["日期", "报告期", "截止", "公告日期", "报表日期"]):
                report_date = str(val)[:10]
                break

    fields_count = sum(x is not None for x in [profit_yoy, revenue_yoy, gross_margin, roe])
    result.update({
        "ok": fields_count > 0,
        "source": best_source or "akshare-stable",
        "report_date": report_date,
        "profit_yoy": profit_yoy,
        "revenue_yoy": revenue_yoy,
        "gross_margin": gross_margin,
        "roe": roe,
        "report_is_stale": False if report_date else None,
        "report_sort_status": "已按报告期倒序取最新" if report_date else "已取得字段，但未识别报告期",
        "data_available": fields_count > 0,
        "confidence": "高" if fields_count >= 3 else ("中" if fields_count >= 2 else "低"),
        "missing_reason": "" if fields_count > 0 else "未匹配到有效财务字段",
    })
    return mark_validity(result)


def stable_finance_unavailable(symbol: str, reason: str = "财报接口暂未返回有效字段") -> Dict[str, Any]:
    return {
        "enabled": True,
        "data_OK": False,
        "data_available": False,
        "finance_mode": "technical_only",
        "data_note": "财报暂未取得，本次不参与评分；请以技术面、5日线、大盘情绪和题材持续性为主。",
        "finance": {
            "ok": False,
            "source": "stable-unavailable",
            "errors": [],
            "report_date": None,
            "profit_yoy": None,
            "revenue_yoy": None,
            "gross_margin": None,
            "roe": None,
            "report_is_stale": None,
            "report_sort_status": "财报暂未取得",
            "data_available": False,
            "confidence": "无",
            "missing_reason": reason,
        },
        "announcements": [],
        "positive_hits": [],
        "negative_hits": [],
        "finance_tags": [],
        "finance_risk": [],
        "conclusion": "财报暂未取得，本次不参与评分；该结果按仅技术面模式处理。",
        "symbol": symbol,
    }


def fetch_real_data_stable(symbol: str) -> Dict[str, Any]:
    symbol = safe_str(symbol)
    # 先尝试旧函数，但旧函数可能返回空字段，因此需要二次校验
    old = None
    for fn_name in ["fetch_real_data_for_symbol", "get_real_data", "fetch_finance_and_news", "fetch_company_real_data"]:
        fn = globals().get(fn_name)
        if callable(fn):
            try:
                old = fn(symbol)
                f = (old or {}).get("finance") or {}
                if f.get("report_date") or f.get("profit_yoy") is not None or f.get("revenue_yoy") is not None or f.get("gross_margin") is not None or f.get("roe") is not None:
                    old["data_available"] = True
                    old["finance_mode"] = "finance_available"
                    old.setdefault("data_note", "已取得部分真实财报/公告数据。")
                    old.setdefault("symbol", symbol)
                    return old
            except Exception:
                pass

    fin = stable_fetch_finance_from_akshare(symbol)
    if not fin.get("data_available"):
        return stable_finance_unavailable(symbol, fin.get("missing_reason") or "财报接口暂未返回有效字段")

    rd = {
        "enabled": True,
        "data_OK": True,
        "data_available": True,
        "finance_mode": "finance_available",
        "data_note": "已通过稳定兼容模式取得财报字段；如字段不全，会降低置信度。",
        "finance": fin,
        "announcements": [],
        "positive_hits": [],
        "negative_hits": [],
        "finance_tags": [],
        "finance_risk": [],
        "conclusion": "已取得部分财报字段，可作为辅助参考。",
        "symbol": symbol,
    }

    if fin.get("profit_yoy") is not None and fin["profit_yoy"] > 30:
        rd["finance_tags"].append("净利润增长")
    if fin.get("revenue_yoy") is not None and fin["revenue_yoy"] < 0:
        rd["finance_risk"].append("营收下滑")
    if fin.get("gross_margin") is not None and fin["gross_margin"] >= 30:
        rd["finance_tags"].append("毛利率较好")
    if fin.get("roe") is not None and fin["roe"] < 5:
        rd["finance_risk"].append("ROE偏弱")

    if rd["finance_tags"] and not rd["finance_risk"]:
        rd["conclusion"] = "财报存在积极信号，可作为辅助加分项。"
    elif rd["finance_tags"] and rd["finance_risk"]:
        rd["conclusion"] = "财报有亮点但也有风险，不能简单理解为全面利好。"
    elif rd["finance_risk"]:
        rd["conclusion"] = "财报存在风险点，建议降低基本面权重。"

    return rd


# 覆盖兼容函数名，保证所有旧调用都走稳定逻辑
def fetch_real_data(symbol: str) -> Dict[str, Any]:
    return fetch_real_data_stable(symbol)
# ===== end V3.6.8 finance stable helpers =====



def explain_finance_result(real_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    V3.6.8：财报中文解释稳定版。
    关键原则：财报取不到不等于财报差，不参与评分，不误判为谨慎低分。
    """
    rd = real_data or {}
    finance = rd.get("finance") or {}
    positive_hits = rd.get("positive_hits") or []
    negative_hits = rd.get("negative_hits") or []
    finance_tags = rd.get("finance_tags") or []
    finance_risk = rd.get("finance_risk") or []

    data_available = bool(rd.get("data_available") or finance.get("data_available") or finance.get("ok"))
    report_date = finance.get("report_date")
    profit_yoy = finance.get("profit_yoy")
    revenue_yoy = finance.get("revenue_yoy")
    gross_margin = finance.get("gross_margin")
    roe = finance.get("roe")
    source = finance.get("source", "akshare")
    report_sort_status = finance.get("report_sort_status", "")
    confidence = finance.get("confidence", "未知")
    missing_reason = finance.get("missing_reason", "")

    def nfmt(v, suffix="%"):
        vv = normalize_finance_value(v)
        if vv is None:
            return "暂无"
        return f"{vv:.2f}{suffix}"

    # 没有财报数据：不扣分，不给谨慎，只显示“未取得”
    if not data_available or all(normalize_finance_value(x) is None for x in [profit_yoy, revenue_yoy, gross_margin, roe]):
        return {
            "score": None,
            "level": "未取得",
            "conclusion": "财报数据暂未取得，本次不参与评分；请以技术面、5日线、大盘情绪和题材持续性为主。",
            "report_date": report_date,
            "report_is_stale": None,
            "source": source,
            "report_sort_status": report_sort_status or "财报暂未取得",
            "confidence": "无",
            "data_available": False,
            "missing_reason": missing_reason or "接口未返回有效财报字段",
            "metrics": {
                "profit_yoy": "暂无",
                "revenue_yoy": "暂无",
                "gross_margin": "暂无",
                "roe": "暂无",
            },
            "positives": [],
            "risks": [],
            "neutral": ["财报缺失不代表公司基本面差，只表示本系统本次未成功取得有效字段。"],
            "human_summary": "财报评级：未取得。本次不参与评分，按仅技术面模式处理。"
        }

    positives, risks, neutral = [], [], []

    if report_date:
        positives.append(f"已读取报告期：{report_date}。")

    p = normalize_finance_value(profit_yoy)
    r = normalize_finance_value(revenue_yoy)
    gm = normalize_finance_value(gross_margin)
    rv = normalize_finance_value(roe)

    if p is None:
        neutral.append("净利润同比暂无数据。")
    elif p >= 100:
        positives.append(f"净利润同比大幅增长 {nfmt(p)}，属于明显积极信号。")
    elif p >= 30:
        positives.append(f"净利润同比增长 {nfmt(p)}，盈利改善较明显。")
    elif p >= 0:
        neutral.append(f"净利润同比小幅增长 {nfmt(p)}，盈利有改善但不算特别强。")
    elif p <= -30:
        risks.append(f"净利润同比下降 {nfmt(p)}，盈利压力较大。")
    else:
        risks.append(f"净利润同比下降 {nfmt(p)}，需要关注盈利稳定性。")

    if r is None:
        neutral.append("营收同比暂无数据。")
    elif r >= 30:
        positives.append(f"营收同比增长 {nfmt(r)}，主营扩张较明显。")
    elif r >= 0:
        neutral.append(f"营收同比增长 {nfmt(r)}，主营相对稳定。")
    elif r <= -15:
        risks.append(f"营收同比下降 {nfmt(r)}，收入端承压，需要重点核对。")
    else:
        risks.append(f"营收同比小幅下降 {nfmt(r)}，需观察后续恢复情况。")

    if gm is None:
        neutral.append("毛利率暂无数据。")
    elif gm >= 40:
        positives.append(f"毛利率 {nfmt(gm)}，盈利质量较好。")
    elif gm >= 20:
        neutral.append(f"毛利率 {nfmt(gm)}，处于可接受区间。")
    else:
        risks.append(f"毛利率 {nfmt(gm)}，偏低，需要注意利润质量。")

    if rv is None:
        neutral.append("ROE 暂无数据。")
    elif rv >= 15:
        positives.append(f"ROE {nfmt(rv)}，股东回报能力较强。")
    elif rv >= 8:
        neutral.append(f"ROE {nfmt(rv)}，回报能力中等。")
    elif rv >= 0:
        risks.append(f"ROE {nfmt(rv)}，回报能力偏弱。")
    else:
        risks.append(f"ROE {nfmt(rv)}，为负值，需谨慎。")

    if positive_hits:
        positives.append("公告中发现积极关键词：" + "、".join([str(x) for x in positive_hits[:5]]) + "。")
    if negative_hits:
        risks.append("公告中发现风险关键词：" + "、".join([str(x) for x in negative_hits[:5]]) + "。")
    if finance_tags:
        positives.append("财报标签：" + "、".join([str(x) for x in finance_tags[:5]]) + "。")
    if finance_risk:
        risks.append("财报风险：" + "、".join([str(x) for x in finance_risk[:5]]) + "。")

    score = 55
    score += min(25, len(positives) * 6)
    score -= min(30, len(risks) * 8)
    if p is not None:
        if p >= 100: score += 10
        elif p >= 30: score += 6
        elif p < -30: score -= 10
    if r is not None:
        if r >= 20: score += 6
        elif r < -15: score -= 8
    if confidence == "低":
        neutral.append("财报字段置信度较低，建议只作为轻量参考。")
        score = min(score, 65)

    # 利润增但营收降：降低过度乐观
    if p is not None and r is not None and p > 50 and r < 0:
        risks.append("净利润增长但营收下降，可能存在低基数、费用变化或非经常性因素，需要重点核对利润来源。")
        score = min(score, 68)

    score = max(0, min(100, int(score)))

    if score >= 75:
        level = "积极"
        conclusion = "财报/公告整体偏积极，可提高关注级别，但仍需配合5日线和大盘环境。"
    elif score >= 60:
        level = "偏积极"
        conclusion = "财报有一定亮点，可作为辅助加分项，但不能单独作为买入理由。"
    elif score >= 45:
        level = "中性"
        conclusion = "财报信号中性，重点仍应看技术面、题材持续性和5日线纪律。"
    else:
        level = "谨慎"
        conclusion = "财报或公告存在压力，建议降低关注等级，实盘前必须核对原始公告。"

    return {
        "score": score,
        "level": level,
        "conclusion": conclusion,
        "report_date": report_date,
        "report_is_stale": finance.get("report_is_stale"),
        "source": source,
        "report_sort_status": report_sort_status,
        "confidence": confidence,
        "data_available": True,
        "missing_reason": "",
        "metrics": {
            "profit_yoy": nfmt(p),
            "revenue_yoy": nfmt(r),
            "gross_margin": nfmt(gm),
            "roe": nfmt(rv),
        },
        "positives": positives[:8],
        "risks": risks[:8],
        "neutral": neutral[:8],
        "human_summary": f"财报评级：{level}（{score}分）。{conclusion}"
    }


@app.route("/api/finance_explain", methods=["POST", "GET"])
def api_finance_explain():
    payload = request.get_json(silent=True) if request.method == "POST" else request.args
    payload = payload or {}
    symbol = safe_str(payload.get("symbol", "300592"))
    try:
        rd = fetch_real_data(symbol)
        return jsonify({
            "ok": True,
            "version": "3.7.1.1-railway-startup-fix",
            "symbol": symbol,
            "real_data": rd,
            "finance_explain": explain_finance_result(rd),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "version": "3.7.1.1-railway-startup-fix",
            "symbol": symbol,
            "error": str(e),
            "type": e.__class__.__name__,
        }), 200




# ===== V3.7 valid MA5 and abnormal stock filters =====
def to_float_safe(v, default=None):
    try:
        if v is None:
            return default
        s = str(v).replace("%", "").replace(",", "").replace("亿", "").replace("万", "").strip()
        if s in ["", "-", "--", "None", "nan", "暂无"]:
            return default
        return float(s)
    except Exception:
        return default

def is_new_stock_like(symbol: str, name: str = "") -> bool:
    """
    过滤 N/C/U/W 等新股或特殊上市初期标识。
    说明：C 字头名称在创业板/科创板上市前5个交易日常见，N 为上市首日。
    """
    symbol = safe_str(symbol)
    name = safe_str(name).upper()
    raw_name = safe_str(name)
    if not symbol:
        return True
    if raw_name.startswith(("N", "C", "U", "W")):
        return True
    if "新股" in raw_name or "次新" in raw_name:
        return True
    # 北交所/新股异常一般不适合本5日线模型，先不过滤全部8字头，只过滤明显缺MA5或异常涨幅
    return False

def has_valid_ma5_item(item: Dict[str, Any]) -> bool:
    ma5 = to_float_safe(item.get("ma5") or item.get("MA5") or item.get("ma_5"), None)
    close = to_float_safe(item.get("close") or item.get("price") or item.get("收盘"), None)
    if ma5 is None or close is None:
        return False
    if ma5 <= 0 or close <= 0:
        return False
    return True

def abnormal_pct_item(item: Dict[str, Any], max_pct: float = 30.0) -> bool:
    pct = to_float_safe(item.get("pct_chg") or item.get("change_pct") or item.get("涨幅") or item.get("pct"), None)
    if pct is None:
        return False
    return abs(pct) > max_pct



def build_t_discipline_text(item: Dict[str, Any]) -> Dict[str, Any]:
    """底仓做T纪律辅助：只给纪律提醒，不给买卖指令。"""
    x = item or {}
    valid = bool(x.get("valid_for_5day_system", True))
    dist = to_float_safe(x.get("distance_ma5_pct") or x.get("distance_to_ma5"), None)
    pct = to_float_safe(x.get("pct_chg") or x.get("change_pct"), None)
    status = "仅纪律提醒"
    suggestion = "需要盘中分时黄线、成交量和MACD柱子共同确认，不能只靠日K做T。"
    risk = "做T必须围绕底仓，不能因为做T变成额外加仓。"
    if not valid:
        status = "不适合做T"
        suggestion = "该票触发风险过滤或缺少有效5日线，不适合作为做T样本。"
        risk = "无有效5日线或异常波动时，做T容易越做成本越高。"
    elif dist is not None and dist > 8:
        status = "谨慎反T观察"
        suggestion = "股价远离5日线，若盘中冲高缩量且MACD红柱缩短，只能观察减仓T，不适合追高正T。"
        risk = "远离5日线容易冲高回落，也可能继续强势，反T有卖飞风险。"
    elif dist is not None and -3 <= dist <= 5:
        status = "可观察正T/反T"
        suggestion = "股价接近5日线，可结合分时黄线、量价背离、MACD柱子判断日内T机会。"
        risk = "必须先小仓确认，不能一次满仓，不能让仓位越T越大。"
    elif pct is not None and pct > 7:
        status = "冲高谨慎"
        suggestion = "涨幅较大时更适合观察反T或减仓纪律，不适合追涨加仓。"
        risk = "高位做T容易买回更高或卖飞强势股。"
    return {
        "t_status": status,
        "t_suggestion": suggestion,
        "t_risk": risk,
        "t_rules": [
            "正T：低买高卖，适合急跌后修复。",
            "反T：高卖低接，适合冲高乏力。",
            "买入组合：新低 + 放量 + MACD绿柱缩短。",
            "卖出组合：新高 + 缩量 + MACD红柱缩短。",
            "没有底仓不做T；跌破5日线不硬T。"
        ],
        "disclaimer": "做T纪律辅助，不构成投资建议。"
    }

def mark_validity(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    给股票打上数据有效性标签。不会破坏原字段。
    """
    x = dict(item or {})
    symbol = safe_str(x.get("symbol") or x.get("code"))
    name = safe_str(x.get("name"))
    invalid_reasons = []

    if is_new_stock_like(symbol, name):
        invalid_reasons.append("新股/特殊上市标识")
    if abnormal_pct_item(x, 30):
        invalid_reasons.append("涨幅异常超过30%")
    if not has_valid_ma5_item(x):
        invalid_reasons.append("MA5缺失或无效")

    # 低价和成交额过滤继续保留，但不是绝对无效原因，可作为风险
    close = to_float_safe(x.get("close") or x.get("price"), None)
    if close is not None and close < 2:
        invalid_reasons.append("低价股风险")
    amount_raw = x.get("amount") or x.get("成交额") or x.get("turnover_amount")
    amount = to_float_safe(amount_raw, None)
    # 如果字段单位是元，1亿=100000000；如果已经是亿，低于1也算不足
    if amount is not None:
        if amount < 1:
            invalid_reasons.append("成交额不足1亿")
        elif amount > 10000 and amount < 100000000:
            invalid_reasons.append("成交额不足1亿")

    x["valid_ma5"] = has_valid_ma5_item(x)
    x["valid_for_5day_system"] = len(invalid_reasons) == 0
    x["invalid_reasons"] = invalid_reasons
    x["data_quality"] = "有效5日线" if len(invalid_reasons) == 0 else "数据不足/风险过滤"

    if invalid_reasons:
        # 没有有效5日线或异常票，不能给高分
        old_score = to_float_safe(x.get("score") or x.get("smart_score"), 0) or 0
        capped = min(int(old_score), 59)
        x["score"] = capped
        x["smart_score"] = capped
        x["tag"] = "风险过滤"
        x["advice"] = "触发风险过滤，先排除，不作为5日线候选。"
        x["conclusion"] = "数据不足或异常波动，不参与5日线交易计划。"
        x["trading_plan"] = "无有效5日线或触发异常过滤，不能生成实盘交易计划。"
        x["trading_plan_text"] = x["trading_plan"]
        x.setdefault("risk_text", "、".join(invalid_reasons))
    else:
        tags = x.get("tags") or []
        if isinstance(tags, list) and "有效5日线" not in tags:
            tags.append("有效5日线")
            x["tags"] = tags
        x["data_quality"] = "有效5日线"
    try:
        x["t_discipline"] = build_t_discipline_text(x)
    except Exception:
        pass
    return x

def filter_valid_candidates(items, keep_invalid: bool = False):
    """
    默认剔除无效候选；如果 keep_invalid=True，则保留但降分标红。
    """
    out = []
    removed = []
    for item in items or []:
        x = mark_validity(item)
        if x.get("valid_for_5day_system") or keep_invalid:
            out.append(x)
        else:
            removed.append(x)
    return out, removed

def sort_valid_candidates(items):
    def key(x):
        return (
            1 if x.get("valid_for_5day_system") else 0,
            to_float_safe(x.get("score") or x.get("smart_score"), 0) or 0,
            to_float_safe(x.get("pct_chg") or x.get("change_pct"), 0) or 0,
        )
    return sorted(items or [], key=key, reverse=True)
# ===== end V3.7 filters =====


def get_trading_session_status() -> Dict[str, Any]:
    """
    A股交易时间状态识别。节假日无法100%识别，因此：
    - 周六周日：非交易日
    - 工作日：按交易时段识别盘前/盘中/午休/尾盘/收盘后
    - 法定节假日需要实盘前人工核对
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        now = datetime.utcnow() + timedelta(hours=8)

    weekday = now.weekday()  # 0 Mon, 6 Sun
    hm = now.hour * 60 + now.minute
    is_weekend = weekday >= 5
    is_trading_day = not is_weekend

    # minutes
    t_0925 = 9 * 60 + 25
    t_0930 = 9 * 60 + 30
    t_1130 = 11 * 60 + 30
    t_1300 = 13 * 60
    t_1440 = 14 * 60 + 40
    t_1450 = 14 * 60 + 50
    t_1500 = 15 * 60

    if is_weekend:
        session = "周末/非交易日"
        data_mode = "复盘参考"
        is_live = False
        hint = "当前为周末，A股不开盘。题材、涨幅、成交额多为上一交易日收盘数据，只适合复盘和准备观察池。"
        action = "不要按实时盘中买卖判断；可整理下周观察名单。"
    elif hm < t_0925:
        session = "盘前"
        data_mode = "盘前准备"
        is_live = False
        hint = "当前为盘前，实时成交和题材持续性尚未确认。"
        action = "只做预选，不急于买入；等9:40后看题材是否延续。"
    elif t_0925 <= hm < t_0930:
        session = "集合竞价"
        data_mode = "竞价参考"
        is_live = True
        hint = "当前为集合竞价，价格波动较大，不能只看竞价冲高。"
        action = "观察竞价强弱，等开盘后确认承接。"
    elif t_0930 <= hm < t_1130:
        session = "早盘交易中"
        data_mode = "盘中快照"
        is_live = True
        hint = "当前为早盘交易时间，行情快照有参考价值，但容易冲高回落。"
        action = "9:40后筛选更稳定；不追远离5日线的票。"
    elif t_1130 <= hm < t_1300:
        session = "午间休市"
        data_mode = "上午复盘"
        is_live = False
        hint = "当前为午间休市，数据停留在上午收盘附近。"
        action = "适合复盘上午题材，下午开盘后再确认。"
    elif t_1300 <= hm < t_1440:
        session = "午后交易中"
        data_mode = "盘中快照"
        is_live = True
        hint = "当前为午后交易时间，可观察题材是否继续强。"
        action = "重点看承接和量能，不追明显远离5日线的票。"
    elif t_1440 <= hm < t_1450:
        session = "尾盘观察"
        data_mode = "尾盘检查"
        is_live = True
        hint = "当前接近尾盘，适合检查是否跌破5日线、是否站回5日线。"
        action = "按纪律执行：跌破5日线减仓，站不回不硬扛。"
    elif t_1450 <= hm < t_1500:
        session = "2:50纪律执行"
        data_mode = "实盘纪律"
        is_live = True
        hint = "当前为2:50纪律执行窗口。"
        action = "重点执行减半仓、止损、是否留仓，不再临时冲动追高。"
    else:
        session = "收盘后"
        data_mode = "收盘复盘"
        is_live = False
        hint = "当前已收盘，行情数据是收盘复盘数据。"
        action = "适合总结观察池和制定明日计划，不是实时买卖判断。"

    return {
        "ok": True,
        "timezone": "Asia/Shanghai",
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": weekday,
        "weekday_name": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday],
        "is_weekend": is_weekend,
        "is_trading_day_likely": is_trading_day,
        "session": session,
        "data_mode": data_mode,
        "is_live_window": is_live,
        "hint": hint,
        "action": action,
        "holiday_note": "系统可识别周末和交易时段，但法定节假日仍需人工核对交易所日历。"
    }


@app.route("/api/trading_status")
def api_trading_status():
    try:
        return jsonify({
            "ok": True,
            "version": "3.7.1.1-railway-startup-fix",
            "trading_status": get_trading_session_status(),
        })
    except Exception as e:
        return jsonify({"ok": False, "version": "3.7.1.1-railway-startup-fix", "error": str(e), "type": e.__class__.__name__}), 200



def fetch_market_sentiment() -> Dict[str, Any]:
    """
    大盘情绪快照：
    - 指数：上证、深成指、创业板
    - 全市场上涨/下跌家数
    - 涨停/跌停家数（按10%近似，含创业板20%会有偏差，作为情绪参考）
    """
    result = {
        "ok": False,
        "source": "eastmoney_push2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": [],
        "up_count": 0,
        "down_count": 0,
        "flat_count": 0,
        "limit_up_count": 0,
        "limit_down_count": 0,
        "total": 0,
        "up_ratio": None,
        "sentiment_score": 50,
        "sentiment_level": "中性",
        "position_factor": 0.5,
        "suggestion": "大盘中性，按个股5日线纪律执行。",
        "errors": [],
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

    try:
        # 指数行情
        idx_url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        idx_params = {
            "fltt": "2",
            "secids": "1.000001,0.399001,0.399006",
            "fields": "f12,f14,f2,f3,f4,f6",
            "_": str(int(datetime.now().timestamp() * 1000)),
        }
        r = requests.get(idx_url, params=idx_params, headers=headers, timeout=10)
        r.raise_for_status()
        idx_rows = (((r.json() or {}).get("data") or {}).get("diff") or [])
        for row in idx_rows:
            result["indices"].append({
                "code": safe_str(row.get("f12")),
                "name": safe_str(row.get("f14")),
                "price": safe_float(row.get("f2")),
                "pct_chg": safe_float(row.get("f3")),
                "amount": safe_float(row.get("f6")),
            })
    except Exception as e:
        result["errors"].append("index:" + str(e)[:120])

    try:
        # 全A快照，尽量取较多股票用于情绪统计
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": "5000",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f6,f8",
            "_": str(int(datetime.now().timestamp() * 1000)),
        }
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        rows = (((r.json() or {}).get("data") or {}).get("diff") or [])
        up = down = flat = lu = ld = total = 0
        for row in rows:
            code = safe_str(row.get("f12"))
            if not code or code.startswith(("688", "8", "4")):
                continue
            pct = safe_float(row.get("f3"))
            if pct is None:
                continue
            total += 1
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            else:
                flat += 1
            if pct >= 9.7:
                lu += 1
            if pct <= -9.7:
                ld += 1

        result.update({
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "limit_up_count": lu,
            "limit_down_count": ld,
            "total": total,
            "up_ratio": round(up / total * 100, 2) if total else None,
        })

        score = 50
        up_ratio = result["up_ratio"] or 0
        if up_ratio >= 65:
            score += 18
        elif up_ratio >= 55:
            score += 10
        elif up_ratio <= 35:
            score -= 18
        elif up_ratio <= 45:
            score -= 10

        score += min(16, lu * 0.35)
        score -= min(16, ld * 1.5)

        idx_pcts = [x.get("pct_chg") for x in result["indices"] if x.get("pct_chg") is not None]
        if idx_pcts:
            avg_idx = sum(idx_pcts) / len(idx_pcts)
            if avg_idx >= 1.0:
                score += 12
            elif avg_idx >= 0.3:
                score += 6
            elif avg_idx <= -1.0:
                score -= 14
            elif avg_idx <= -0.3:
                score -= 7

        score = int(max(0, min(100, round(score))))
        result["sentiment_score"] = score

        if score >= 75:
            level = "强势"
            factor = 0.8
            suggestion = "大盘情绪强，可优先观察技术合格且接近5日线的票，但仍不能追高满仓。"
        elif score >= 60:
            level = "偏强"
            factor = 0.65
            suggestion = "大盘偏强，按计划分批，优先安全买点。"
        elif score >= 45:
            level = "中性"
            factor = 0.5
            suggestion = "大盘中性，严格按5日线纪律，不放大仓位。"
        elif score >= 30:
            level = "偏弱"
            factor = 0.33
            suggestion = "大盘偏弱，只做小仓观察，远离5日线不追。"
        else:
            level = "弱势"
            factor = 0.2
            suggestion = "大盘弱势，原则上少操作或不操作，只保留最强且接近5日线的观察。"

        result["sentiment_level"] = level
        result["position_factor"] = factor
        result["suggestion"] = suggestion
        result["ok"] = True
    except Exception as e:
        result["errors"].append("breadth:" + str(e)[:120])

    return mark_validity(result)


def apply_market_sentiment_to_stock(item: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    """
    把大盘情绪加入个股卡片，不改变原始5日线规则，只调整仓位和提醒。
    """
    if not item or not market:
        return item
    score = market.get("sentiment_score", 50)
    level = market.get("sentiment_level", "中性")
    factor = market.get("position_factor", 0.5)

    market_note = f"大盘情绪：{level}（{score}分）。{market.get('suggestion','')}"
    item["market_sentiment"] = {
        "score": score,
        "level": level,
        "position_factor": factor,
        "suggestion": market.get("suggestion", ""),
    }

    cat = item.get("category", "")
    base_position = item.get("position", "") or item.get("position_advice", "")

    if score < 45:
        if cat in ["quality_safe_near", "safe_near", "near"]:
            item["position"] = (base_position + "；" if base_position else "") + "大盘偏弱，仓位自动降级，只能小仓确认。"
            item.setdefault("tags", []).append("大盘降仓")
            item["smart_score"] = max(0, int(item.get("smart_score", 0)) - 5)
        elif cat in ["far", "focus", "watch"]:
            item["position"] = (base_position + "；" if base_position else "") + "大盘偏弱，远离5日线不追。"
            item.setdefault("tags", []).append("大盘偏弱")
            item["smart_score"] = max(0, int(item.get("smart_score", 0)) - 8)
    elif score >= 75:
        if cat in ["quality_safe_near", "safe_near"]:
            item["position"] = (base_position + "；" if base_position else "") + "大盘强势，允许按计划分批，但仍不能一次满仓。"
            item.setdefault("tags", []).append("大盘配合")
            item["smart_score"] = min(100, int(item.get("smart_score", 0)) + 3)

    item["market_note"] = market_note
    return item


@app.route("/api/market_sentiment")
def api_market_sentiment():
    try:
        return jsonify({
            "ok": True,
            "version": "3.7.1.1-railway-startup-fix",
            "market": fetch_market_sentiment(),
        })
    except Exception as e:
        return jsonify({"ok": False, "version": "3.7.1.1-railway-startup-fix", "error": str(e), "type": e.__class__.__name__}), 200



def fetch_theme_stocks_by_board(board_code: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    根据东方财富概念/题材板块代码读取成分股。
    常见 board_code 格式：BKxxxx。
    """
    code = safe_str(board_code).strip().upper()
    if not code:
        raise ValueError("缺少题材板块代码")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": str(max(10, min(limit, 80))),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": f"b:{code}",
        "fields": "f12,f14,f2,f3,f6,f8",
        "_": str(int(datetime.now().timestamp() * 1000)),
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    r = requests.get(url, params=params, headers=headers, timeout=12)
    r.raise_for_status()
    rows = (((r.json() or {}).get("data") or {}).get("diff") or [])
    out = []
    for row in rows:
        code2 = safe_str(row.get("f12"))
        if len(code2) != 6:
            continue
        # 继续保持你的规则：去除688、北交所
        if code2.startswith("688") or code2.startswith(("8", "4")):
            continue
        if not code2.startswith(("00", "30", "60")):
            continue
        out.append({
            "symbol": code2,
            "name": safe_str(row.get("f14")),
            "price": safe_float(row.get("f2")),
            "pct_chg": safe_float(row.get("f3")),
            "amount": safe_float(row.get("f6")),
            "turnover": safe_float(row.get("f8")),
            "secid": market_prefix(code2) + code2,
        })
    return out

@app.route("/api/theme_stocks", methods=["POST", "GET"])
def api_theme_stocks():
    good_news_score = 0
    bad_news_score = 0
    finance_score = 0
    news_score = 0
    payload = request.get_json(silent=True) if request.method == "POST" else request.args
    payload = payload or {}
    board_code = safe_str(payload.get("board_code", ""))
    theme_name = safe_str(payload.get("theme", ""))
    limit = max(10, min(int(payload.get("limit", 50)), 80))
    try:
        stocks = fetch_theme_stocks_by_board(board_code, limit=limit)
        results = [quick_score_from_spot(x, enable_risk_filter=True) for x in stocks]
        for r in results:
            r["theme_source"] = {"theme": theme_name, "board_code": board_code}
            if "题材候选" not in r.get("tags", []):
                r.setdefault("tags", []).append("题材候选")
        results.sort(key=lambda x: (
            x.get("rank", 9),
            -int(x.get("smart_score", 0)),
            -((x.get("quote") or {}).get("pct_chg") or 0),
            -((x.get("quote") or {}).get("amount") or 0),
        ))
        return jsonify({
            "ok": True,
            "version": "3.7.1.1-railway-startup-fix",
            "theme": theme_name,
            "board_code": board_code,
            "summary": build_summary(results, len(stocks), 0),
            "results": sort_valid_candidates([mark_validity(x) for x in results]),
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "version": "3.7.1.1-railway-startup-fix",
            "theme": theme_name,
            "board_code": board_code,
            "error": str(e),
            "type": e.__class__.__name__,
            "hint": "题材成分股接口可能暂时不可用，稍后重试，或返回全部热门候选。",
            "results": [],
        }), 200


@app.route("/api/smart_hot", methods=["POST", "GET"])
def api_smart_hot():
    good_news_score = 0
    bad_news_score = 0
    finance_score = 0
    news_score = 0
    payload = request.get_json(silent=True) if request.method == "POST" else request.args
    payload = payload or {}
    quick_limit = max(20, min(int(payload.get("quick_limit", 35)), 60))
    enable_risk_filter = str(payload.get("enable_risk_filter", "true")).lower() != "false"
    include_risk = str(payload.get("include_risk", "true")).lower() != "false"

    try:
        spot_data = get_spot_candidates(limit=quick_limit, enable_risk_filter=enable_risk_filter, include_risk=include_risk)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "type": e.__class__.__name__, "where": "eastmoney_spot", "hint": "东方财富实时行情接口暂时不可用。稍后重试，或先用单股分析。", "version": "3.7.1.1-railway-startup-fix", "summary": build_summary([], 0, 1), "themes": [], "results": [], "errors": [{"error": str(e)}]}), 200
    candidates = spot_data["candidates"]
    quick_results = [quick_score_from_spot(x, enable_risk_filter=enable_risk_filter) for x in candidates]
    quick_results.sort(key=lambda x: (x.get("rank", 9), -(x.get("quote", {}).get("pct_chg") or 0), -int(x.get("smart_score", 0)), -(x.get("quote", {}).get("amount") or 0)))
    summary = build_summary(quick_results[:quick_limit], len(candidates), 0)
    summary["source_count"] = spot_data.get("source_count", 0)
    summary["candidate_count"] = len(candidates)
    summary["deep_analyzed"] = 0
    return jsonify({"ok": True, "version": "3.7.1.1-railway-startup-fix", "mode": "risk_filter_quick_first", "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "summary": summary, "themes": try_fetch_theme_board(limit=20), "results": quick_results[:quick_limit], "errors": [{"info": x} for x in spot_data.get("errors", [])]})


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
    return jsonify({"ok": True, "version": "3.7.1.1-railway-startup-fix", "offset": offset, "size": size, "next_offset": next_offset, "done": done, "total": len(symbols), "summary": build_summary(results, len(batch), len(errors)), "results": sort_valid_candidates([mark_validity(x) for x in results]), "errors": errors})




@app.route("/api/startup_check", methods=["GET"])
def api_startup_check():
    info = {
        "ok": True,
        "version": "3.7.1.1-railway-startup-fix",
        "flask_app": True,
        "pandas_loaded": pd is not None,
    }
    try:
        import akshare as ak
        info["akshare_loaded"] = True
        info["akshare_version"] = getattr(ak, "__version__", "unknown")
    except Exception as e:
        info["akshare_loaded"] = False
        info["akshare_error"] = str(e)[:200]
    return jsonify(info)

@app.route("/api/filter_status", methods=["GET"])
def api_filter_status():
    return jsonify({
        "ok": True,
        "version": "3.7.1.1-railway-startup-fix",
        "filters": [
            "排除 N/C/U/W 新股或特殊上市标识",
            "排除涨幅超过30%的异常波动票",
            "排除 MA5 缺失或无效股票",
            "排除低价股风险",
            "排除成交额不足1亿风险",
            "无有效MA5不生成交易计划",
            "异常候选最高分限制为59分"
        ],
        "principle": "没有有效5日线，就不参与5日线交易纪律评分。"
    })



@app.route("/api/t_discipline", methods=["GET"])
def api_t_discipline():
    return jsonify({
        "ok": True,
        "version": "3.7.1.1-railway-startup-fix",
        "position_principle": "做T是围绕已有底仓赚日内波动差价，不是额外加仓；目标是降低持仓成本，而不是频繁追涨杀跌。",
        "types": {
            "positive_t": "正T：低位买入，高位卖出，适合盘中急跌后修复，但必须有底仓和纪律。",
            "reverse_t": "反T：高位先卖，低位接回，适合盘中冲高乏力，但容易卖飞强势股。"
        },
        "signals": [
            {"name": "分时黄线", "buy": "白线明显低于黄线且跌幅过大，可观察低吸。", "sell": "白线明显高于黄线且冲高过远，可观察减仓。", "risk": "必须结合成交量和MACD。"},
            {"name": "量价背离", "buy": "股价新低但放量，恐慌释放后观察。", "sell": "股价新高但缩量，说明追涨力量不足。", "risk": "弱势放量可能继续出货。"},
            {"name": "MACD柱子", "buy": "新低绿柱缩短，杀跌动能减弱。", "sell": "新高红柱缩短，上涨动能减弱。", "risk": "MACD滞后，不能单独使用。"}
        ],
        "combo": {
            "buy_watch": "新低 + 放量 + MACD绿柱缩短：观察正T低吸。",
            "sell_watch": "新高 + 缩量 + MACD红柱缩短：观察反T/减仓。",
            "avoid": "没有底仓、趋势破位、跌破5日线未修复、远离5日线时，不适合强行做T。"
        },
        "risks": ["越T成本越高", "卖飞强势股", "没有底仓乱加仓", "弱势股越补越套", "频繁操作影响判断"],
        "disclaimer": "本模块是持仓纪律辅助，不是买卖指令，不构成投资建议。"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
