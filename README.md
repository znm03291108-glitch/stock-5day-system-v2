# 5日线强势股交易纪律系统 V3.7.1.1：Railway启动修复版

修复重点：
- 针对 502 Bad Gateway / Application failed to respond 做启动稳定性修复
- 保留 V3.7.1 底仓做T纪律模块
- 增加 /api/startup_check 启动诊断接口
- 移除 __pycache__
- 增加 .python-version，建议 Railway 使用 Python 3.11.9
- pandas 导入失败时不直接拖垮首页，改为接口提示

检查：
/api/health
/api/startup_check
/api/t_discipline

刷新：
网站地址/?v=3711

风险提示：本系统为复盘和交易纪律辅助工具，不构成投资建议。
