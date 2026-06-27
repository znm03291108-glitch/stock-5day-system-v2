# 5日线强势股交易纪律系统 V3.7.2：undefined显示修复 + 风险提示去重版

本版在 V3.7.1 基础上修复前端显示问题。

修复内容：
- 修复“操作建议：undefined”
- 修复“仓位：undefined”
- 修复“风控：undefined”
- 风险过滤提示去重，不再重复显示两遍
- 后端统一补齐 operation_advice / position_advice / risk_advice
- 无效 MA5 股票仍然会显示风险过滤，但不生成5日线交易计划
- 保留底仓做T纪律模块
- 保留有效5日线过滤、新股异常过滤、财报稳定、大盘情绪

检查：
/api/health 应显示 3.7.2-ui-undefined-fix
/api/ui_fix_status 可查看显示修复状态
/api/t_discipline 可查看做T规则
/api/filter_status 可查看过滤规则

刷新：
网站地址/?v=372

风险提示：
本系统为复盘和交易纪律辅助工具，不构成投资建议。
