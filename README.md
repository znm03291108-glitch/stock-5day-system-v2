# 5日线强势股交易纪律系统 V2.0：自动行情分析版

## 功能

输入 A 股股票代码，系统自动获取日K数据，并计算：

- 今日涨幅
- 开盘价、收盘价、最高价、最低价
- 5日线、10日线、20日线
- 成交量、5日均量
- 是否放量
- 是否大阳线
- 是否站上5日线
- 距离5日线百分比
- 连续跌破5日线天数
- 10分制评分
- 买入、半仓、减仓、清仓建议

## 本地运行

```bash
pip install -r requirements.txt
python app.py
```

浏览器打开：

```text
http://127.0.0.1:5000
```

## Railway 部署

1. 新建 GitHub 仓库，例如：stock-5day-system-v2
2. 上传全部文件
3. Railway 选择 Deploy from GitHub Repo
4. Railway 会自动识别 Procfile
5. 部署成功后打开 Railway 生成的网址

## GitHub Pages + Railway 分离部署

如果你只把 index.html 放到 GitHub Pages：

1. Railway 只部署 app.py、requirements.txt、Procfile
2. GitHub Pages 只放 static/index.html 并改名为 index.html
3. 打开网页后，在“后端地址”填写 Railway 地址，例如：
   https://xxxx.up.railway.app

## 注意事项

- 本系统是交易纪律辅助工具，不构成投资建议。
- 数据源可能延迟、缺失、接口变动。
- 真实买卖前必须用券商软件再次核对行情。
- 不建议包装成“荐股”“稳赚”“自动盈利”类产品。
