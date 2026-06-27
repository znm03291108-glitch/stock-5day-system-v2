
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

def nf(v):
    try:
        if pd.isna(v): return None
        return float(v)
    except Exception:
        return None

def ns(v):
    try:
        if pd.isna(v): return ''
        return str(v)
    except Exception:
        return ''

def norm(s:str)->str:
    s=(s or '').strip().lower().replace('sh','').replace('sz','').replace('bj','')
    s=''.join(c for c in s if c.isdigit())
    if len(s)!=6: raise ValueError('股票代码必须是6位数字，例如 300592')
    return s

def detect(df):
    mp={'date':['日期','date'],'open':['开盘','open'],'close':['收盘','close'],'high':['最高','high'],'low':['最低','low'],'volume':['成交量','volume'],'amount':['成交额','amount'],'pct':['涨跌幅','pct_chg'],'turnover':['换手率','turnover']}
    res={}; cols=list(df.columns)
    for k,arr in mp.items():
        for a in arr:
            if a in cols: res[k]=a; break
    miss=[k for k in ['date','open','close','high','low','volume'] if k not in res]
    if miss: raise ValueError('行情字段不完整：'+str(miss)+' 当前字段：'+str(cols))
    return res

def hist(symbol, adjust=''):
    import akshare as ak
    end=datetime.now().strftime('%Y%m%d'); start=(datetime.now()-timedelta(days=90)).strftime('%Y%m%d')
    df=ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date=start, end_date=end, adjust=adjust or '')
    if df is None or df.empty: raise ValueError('没有获取到行情数据')
    return df

def analyze_stock(symbol:str, adjust='', name='', meta:Optional[Dict[str,Any]]=None):
    symbol=norm(symbol); df=hist(symbol, adjust); c=detect(df); w=df.copy()
    for k in ['open','close','high','low','volume','amount','pct','turnover']:
        if k in c: w[c[k]]=pd.to_numeric(w[c[k]], errors='coerce')
    w[c['date']]=pd.to_datetime(w[c['date']]); w=w.sort_values(c['date']).reset_index(drop=True)
    if len(w)<20: raise ValueError('历史数据不足20个交易日')
    close=w[c['close']]; vol=w[c['volume']]
    w['MA5']=close.rolling(5).mean(); w['MA10']=close.rolling(10).mean(); w['MA20']=close.rolling(20).mean(); w['VOL_MA5']=vol.rolling(5).mean()
    last=w.iloc[-1]; prev=w.iloc[-2]
    closev=nf(last[c['close']]); openv=nf(last[c['open']]); volm=nf(last[c['volume']]); ma5=nf(last['MA5']); ma10=nf(last['MA10']); ma20=nf(last['MA20']); vol5=nf(last['VOL_MA5'])
    if closev is None or openv is None or ma5 is None: raise ValueError('关键行情数据为空')
    pct=nf(last[c['pct']]) if 'pct' in c else None
    if pct is None:
        pc=nf(prev[c['close']]); pct=((closev-pc)/pc*100) if pc else None
    amount=nf(last[c['amount']]) if 'amount' in c else None; turnover=nf(last[c['turnover']]) if 'turnover' in c else None
    if meta:
        pct=meta.get('pct_chg', pct); amount=meta.get('amount', amount); turnover=meta.get('turnover', turnover)
    dist=(closev-ma5)/ma5*100 if ma5 else None
    below=0
    for _,r in w.iloc[::-1].iterrows():
        rv=nf(r[c['close']]); rm=nf(r['MA5'])
        if rv is not None and rm is not None and rv<rm: below+=1
        else: break
    rise=pct is not None and pct>=5; vol_break=volm is not None and vol5 is not None and volm>=vol5*1.5; vol_above=volm is not None and vol5 is not None and volm>=vol5
    big=((closev-openv)/openv*100)>=3 and closev>openv if openv else False
    above5=closev>=ma5; above10=ma10 is not None and closev>=ma10; above20=ma20 is not None and closev>=ma20
    far=dist is not None and dist>=6; near=rise and above5 and dist is not None and 0<=dist<=3
    score=0
    if rise: score+=2
    if vol_break: score+=2
    elif vol_above: score+=1
    if big: score+=2
    elif closev>openv: score+=1
    if above5: score+=2
    if above10 or above20: score+=1
    if dist is not None and 0<=dist<=8: score+=1
    smart=min(100, int(round(min(max(pct or 0,0),10)*2 + (20 if vol_break else 12 if vol_above else 4) + (20 if above5 and above10 and above20 else 12 if above5 else 0) + (15 if big else 8 if closev>openv else 0) + (15 if near else 8 if above5 and not far else 2) + (10 if (amount or 0)>=5e9 else 7 if (amount or 0)>=1e9 else 4 if (amount or 0)>=3e8 else 0))))
    if not rise: level,cat,rank,act,pos,risk='不看','ignore',6,'涨幅没有超过5%，不符合强势股第一条件。','不买。','没有明显主力进攻信号。'
    elif below>=3: level,cat,rank,act,pos,risk='清仓信号','risk',5,'连续3天没有站回5日线。','已有仓位应清仓。','趋势失效。'
    elif not above5: level,cat,rank,act,pos,risk='风控信号','risk',4,'股价跌破5日线，尾盘站不回先减一半。','不加仓。','连续3天站不回清仓。'
    elif near and smart>=65: level,cat,rank,act,pos,risk='接近买点','near',1,'涨幅超过5%，站上5日线，且距离5日线不远。','可分批，先小仓确认。','尾盘跌破5日线减半。'
    elif far: level,cat,rank,act,pos,risk='强势但远离5日线','far',3,'股价强势，但已经远离5日线，不适合满仓追高。','最多半仓，等回踩5日线。','远离5日线容易回落。'
    elif smart>=80: level,cat,rank,act,pos,risk='重点关注','focus',2,'强势评分较高，等待5日线附近确认。','分批。','跌破5日线减半。'
    elif smart>=65: level,cat,rank,act,pos,risk='加入自选','watch',3,'有一定强度，等5日线附近确认。','轻仓或等待。','避免追高。'
    else: level,cat,rank,act,pos,risk='暂不操作','ignore',6,'条件不完整，先观察。','不买。','信号不足。'
    tags=['涨幅超过5%' if rise else '涨幅不足5%','明显放量' if vol_break else ('量能高于5日均量' if vol_above else '未明显放量'),'大阳线' if big else '非大阳线','站上5日线' if above5 else '跌破5日线']
    if far: tags.append('远离5日线')
    if near: tags.append('接近买点')
    if amount and amount>=1e9: tags.append('成交额活跃')
    if turnover and turnover>=8: tags.append('高换手')
    return {'symbol':symbol,'name':name or symbol,'category':cat,'rank':rank,'score':score,'smart_score':smart,'tags':tags,'quick_only':False,'quote':{'date':str(last[c['date']].date()),'open':openv,'close':closev,'volume':volm,'amount':amount,'turnover':turnover,'pct_chg':pct},'analysis':{'ma5':ma5,'ma10':ma10,'ma20':ma20,'vol_ma5':vol5,'distance_ma5_pct':dist,'below_ma5_days':below},'advice':{'level':level,'action':act,'position':pos,'risk':risk}}

def quick_score(item):
    pct=item.get('pct_chg') or 0; amount=item.get('amount') or 0; turnover=item.get('turnover') or 0
    score=int(min(100, min(max(pct,0),10)*3 + (25 if amount>=5e9 else 18 if amount>=1e9 else 10 if amount>=3e8 else 0) + (20 if turnover>=15 else 14 if turnover>=8 else 8 if turnover>=3 else 0)))
    cat='watch' if pct>=5 else 'ignore'; level='热门候选' if pct>=5 else '只看热度'
    return {'symbol':item['symbol'],'name':item.get('name') or item['symbol'],'category':cat,'rank':3 if cat=='watch' else 6,'score':0,'smart_score':score,'quick_only':True,'tags':['热门候选' if pct>=5 else '涨幅不足5%','成交额活跃' if amount>=1e9 else '成交额一般','高换手' if turnover>=8 else '换手一般'],'quote':{'date':datetime.now().strftime('%Y-%m-%d'),'open':None,'close':item.get('price'),'volume':None,'amount':amount,'turnover':turnover,'pct_chg':pct},'analysis':{'ma5':None,'ma10':None,'ma20':None,'vol_ma5':None,'distance_ma5_pct':None,'below_ma5_days':None},'advice':{'level':level,'action':'快速筛选只看热度，需要单股深度分析后再决定。','position':'先加入观察，不直接追。','risk':'买入前必须核对5日线。'}}

def spot_candidates(limit=80):
    import akshare as ak
    df=ak.stock_zh_a_spot_em()
    if df is None or df.empty: raise ValueError('没有获取到 A 股实时行情')
    code='代码'; name='名称'; pct='涨跌幅'; price='最新价'; amount='成交额'; turnover='换手率'
    if code not in df.columns or pct not in df.columns: raise ValueError('实时行情字段不完整：'+str(list(df.columns)))
    d=df.copy(); d[code]=d[code].astype(str); d=d[d[code].str.len()==6]; d=d[d[code].str.startswith(('00','30','60','68'))]
    for col in [pct,price,amount,turnover]:
        if col in d.columns: d[col]=pd.to_numeric(d[col],errors='coerce')
    pool=pd.concat([d.sort_values(pct,ascending=False).head(limit), d.sort_values(amount,ascending=False).head(limit) if amount in d.columns else d.head(0), d.sort_values(turnover,ascending=False).head(limit) if turnover in d.columns else d.head(0)], ignore_index=True).drop_duplicates(subset=[code])
    out=[]
    for _,r in pool.iterrows():
        out.append({'symbol':ns(r[code]),'name':ns(r[name]) if name in d.columns else ns(r[code]),'pct_chg':nf(r[pct]) if pct in d.columns else None,'price':nf(r[price]) if price in d.columns else None,'amount':nf(r[amount]) if amount in d.columns else None,'turnover':nf(r[turnover]) if turnover in d.columns else None})
    return {'source_count':int(len(d)),'candidates':out}

def themes(limit=20):
    try:
        import akshare as ak
        b=ak.stock_board_concept_name_em()
        if b is None or b.empty: return []
        name='板块名称' if '板块名称' in b.columns else ('名称' if '名称' in b.columns else None); pct='涨跌幅' if '涨跌幅' in b.columns else None
        if not name: return []
        if pct: b[pct]=pd.to_numeric(b[pct],errors='coerce'); b=b.sort_values(pct,ascending=False)
        return [{'theme':ns(r[name]),'pct_chg':nf(r[pct]) if pct else None} for _,r in b.head(limit).iterrows()]
    except Exception:
        return []

def summary(results,total,failed):
    return {'total_input':total,'success':len(results),'failed':failed,'near':sum(x.get('category')=='near' for x in results),'focus':sum(x.get('category')=='focus' for x in results),'watch':sum(x.get('category') in ['watch','far'] for x in results),'far':sum(x.get('category')=='far' for x in results),'risk':sum(x.get('category')=='risk' for x in results),'ignore':sum(x.get('category')=='ignore' for x in results),'quick_only':sum(bool(x.get('quick_only')) for x in results)}

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/api/health')
def health(): return jsonify({'ok':True,'service':'stock-5day-system-v2','version':'3.0.1-fast-smart','time':datetime.now().isoformat(timespec='seconds'),'message':'后端正常，可以开始极速智能筛选'})

@app.route('/api/analyze')
def api_analyze():
    try: return jsonify(analyze_stock(request.args.get('symbol',''), request.args.get('adjust','')))
    except Exception as e: return jsonify({'error':str(e)}),400

@app.route('/api/batch_analyze', methods=['POST'])
def batch():
    try:
        p=request.get_json(silent=True) or {}; raw=(p.get('symbols') or '').replace('\n',',').replace('，',',').replace('、',',').replace(' ',',')
        syms=[]
        for x in raw.split(','):
            if x.strip():
                try: syms.append(norm(x))
                except Exception: pass
        syms=list(dict.fromkeys(syms))[:20]
        res=[]; err=[]
        for s in syms:
            try: res.append(analyze_stock(s))
            except Exception as e: err.append({'symbol':s,'error':str(e)})
        res.sort(key=lambda x:(x.get('rank',9),-int(x.get('smart_score',0)),-(x.get('quote',{}).get('pct_chg') or 0)))
        return jsonify({'ok':True,'version':'3.0.1-fast-smart','summary':summary(res,len(syms),len(err)),'results':res,'errors':err})
    except Exception as e: return jsonify({'error':str(e)}),400

@app.route('/api/smart_hot', methods=['POST','GET'])
def smart_hot():
    try:
        p=request.get_json(silent=True) if request.method=='POST' else request.args; p=p or {}
        min_pct=float(p.get('min_pct',5)); quick_limit=max(20,min(int(p.get('quick_limit',35)),80)); deep_limit=max(5,min(int(p.get('deep_limit',12)),18))
        sp=spot_candidates(quick_limit); cand=sp['candidates']
        quick=[quick_score(x) for x in cand]
        quick.sort(key=lambda x:(-(x.get('quote',{}).get('pct_chg') or 0),-int(x.get('smart_score',0))))
        deep=sorted([x for x in cand if (x.get('pct_chg') or 0)>=min_pct], key=lambda x:(-(x.get('pct_chg') or 0),-(x.get('amount') or 0)))[:deep_limit]
        res=[]; err=[]
        for it in deep:
            try: res.append(analyze_stock(it['symbol'], name=it.get('name'), meta={'pct_chg':it.get('pct_chg'),'amount':it.get('amount'),'turnover':it.get('turnover')}))
            except Exception as e: err.append({'symbol':it.get('symbol'),'name':it.get('name'),'error':str(e)})
        done={x['symbol'] for x in res}; merged=res+[x for x in quick if x['symbol'] not in done]
        merged.sort(key=lambda x:(x.get('rank',9),-int(x.get('smart_score',0)),-(x.get('quote',{}).get('pct_chg') or 0)))
        merged=merged[:quick_limit]
        sm=summary(merged,len(cand),len(err)); sm.update({'source_count':sp['source_count'],'candidate_count':len(cand),'deep_analyzed':len(res)})
        return jsonify({'ok':True,'version':'3.0.1-fast-smart','mode':'quick_first_deep_limited','date':datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'summary':sm,'themes':themes(20),'results':merged,'errors':err})
    except Exception as e: return jsonify({'error':str(e),'where':'api_smart_hot'}),400

if __name__=='__main__': app.run(host='0.0.0.0', port=5000)
