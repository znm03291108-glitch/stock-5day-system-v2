# 5日线强势股交易纪律系统 V3.6.7.1：财报解释修正版

修复内容：
- 修复 name 'fetch_real_data' is not defined
- 修复 good_news_score 未赋值导致单股分析失败
- /api/finance_explain 增加兼容读取逻辑
- 如果 AKShare 财报接口失败，会降级返回中文提示，不影响系统运行
- 单股分析继续支持财报中文解释

升级：
覆盖 app.py 和 index.html，建议同时覆盖 README.md、requirements.txt、Procfile。

检查：
/api/health 应显示 3.6.7.1-finance-explain-fix

测试：
/api/finance_explain?symbol=300592
