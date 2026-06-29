export default {
  async fetch(request) {
    const url = new URL(request.url);

    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type"
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    if (url.pathname !== "/quote") {
      return json({ ok: false, error: "Use /quote?codes=300592,600000" }, corsHeaders);
    }

    const codesText = url.searchParams.get("codes") || "";
    const codes = codesText
      .split(/[,\s，]+/)
      .map(x => x.trim())
      .filter(Boolean)
      .slice(0, 30);

    if (codes.length === 0) {
      return json({ ok: false, error: "No codes" }, corsHeaders);
    }

    try {
      const items = [];

      for (const code of codes) {
        const secid = toSecid(code);
        const quote = await fetchQuote(secid);
        const ma5 = await fetchMa5(secid);

        if (!quote) continue;

        const price = toNum(quote.f2);
        const changePct = toNum(quote.f3);
        const volumeRatio = toNum(quote.f10);

        let volumeText = "正常";
        if (Number.isFinite(volumeRatio)) {
          if (volumeRatio >= 1.5) volumeText = "放量";
          else if (volumeRatio > 0 && volumeRatio < 0.8) volumeText = "缩量";
        }

        let trendText = "震荡";
        if (Number.isFinite(price) && Number.isFinite(ma5)) {
          if (price > ma5 && changePct > 0) trendText = "向上";
          else if (price < ma5 && changePct < 0) trendText = "走弱";
        } else {
          if (changePct > 0) trendText = "向上";
          if (changePct < 0) trendText = "走弱";
        }

        items.push({
          code,
          name: quote.f14 || code,
          price,
          changePct,
          ma5,
          volumeText,
          trendText,
          raw: quote
        });
      }

      return json({
        ok: true,
        source: "eastmoney_proxy",
        time: new Date().toISOString(),
        count: items.length,
        items
      }, corsHeaders);

    } catch (err) {
      return json({
        ok: false,
        error: String(err && err.message ? err.message : err)
      }, corsHeaders);
    }
  }
};

function toSecid(code) {
  const c = String(code).trim();

  if (/^6/.test(c)) return "1." + c;
  if (/^(0|3)/.test(c)) return "0." + c;
  if (/^(8|4|9)/.test(c)) return "0." + c;

  return "0." + c;
}

async function fetchQuote(secid) {
  const fields = [
    "f12", "f13", "f14",
    "f2", "f3", "f4", "f5", "f6",
    "f7", "f8", "f9", "f10",
    "f15", "f16", "f17", "f18"
  ].join(",");

  const api =
    "https://push2.eastmoney.com/api/qt/ulist.np/get" +
    "?fltt=2&invt=2&fields=" + fields +
    "&secids=" + encodeURIComponent(secid);

  const res = await fetch(api, {
    headers: {
      "User-Agent": "Mozilla/5.0",
      "Referer": "https://quote.eastmoney.com/"
    }
  });

  const data = await res.json();
  const diff = data && data.data && data.data.diff;

  if (!Array.isArray(diff) || diff.length === 0) return null;
  return diff[0];
}

async function fetchMa5(secid) {
  const api =
    "https://push2his.eastmoney.com/api/qt/stock/kline/get" +
    "?secid=" + encodeURIComponent(secid) +
    "&fields1=f1,f2,f3,f4,f5,f6" +
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61" +
    "&klt=101&fqt=1&end=20500101&lmt=5";

  const res = await fetch(api, {
    headers: {
      "User-Agent": "Mozilla/5.0",
      "Referer": "https://quote.eastmoney.com/"
    }
  });

  const data = await res.json();
  const klines = data && data.data && data.data.klines;

  if (!Array.isArray(klines) || klines.length === 0) return "";

  const closes = klines
    .map(x => {
      const arr = String(x).split(",");
      return Number(arr[2]);
    })
    .filter(x => Number.isFinite(x) && x > 0);

  if (closes.length === 0) return "";

  const avg = closes.reduce((a, b) => a + b, 0) / closes.length;
  return Math.round(avg * 100) / 100;
}

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : "";
}

function json(data, headers) {
  return new Response(JSON.stringify(data, null, 2), {
    headers: {
      ...headers,
      "Content-Type": "application/json; charset=utf-8"
    }
  });
}
