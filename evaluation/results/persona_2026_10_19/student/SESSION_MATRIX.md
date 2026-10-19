# 会话矩阵

5组目的明确的模拟使用会话，其中4组包含真实聊天；7次浏览器打开包括S2内驱动恢复重开。全部是同一天加速模拟，不能代表实际数周。

| 会话 | 设备/身份 | 对话/轮次 | 重点 | 结果 |
|---|---|---|---|---|
|S1|desktop 1440×960；original guest|C1；T01–T12（12轮）|首次中文发现、低预算、条件更正、同房追问、未知、收藏、连续12轮|找到1套历史备选并收藏；条件/日期/来源解释多处不一致|
|S2|mobile 390×844; desktop fallback；same guest, retained storage|C1 reopened；none (mechanical UI only)（0轮）|关闭重开、中文偏好、历史/收藏恢复、手机账号入口；测试脚本选择器恢复|历史与收藏恢复；语言重置；手机账号入口越界；错误选择器属于驱动而非产品|
|S3|desktop 1440×960；same guest then fictional registered account|C1 restore; C2 T13–20; C3 T21–25；T13–T25（13轮）|真实服务重启恢复、注册、换城市、pw/pcm、两房比较、房源身份、偏好跨对话/更改|访客12轮和收藏恢复；账号注册后另起空数据；找到2套合租及1套studio；记忆失败重试后恢复|
|S4|mobile 390×844；same registered account, retained storage|C3 restore; C4 new；T26（1轮）|再次关闭重开、最新偏好、账号会话与2套收藏恢复|正确回忆£1000整套studio；不能直接识别收藏身份但没有声称都匹配|
|S5|desktop 1440×960；fresh guest then login to own test account|C5; account C2/C3/C4 restored；T27（1轮）|新访客隔离、账号登录恢复、手动会话重命名/打开|新访客空历史/收藏且不知道偏好；登录恢复3条账号会话2套收藏；重命名成功|

精确对话/turn_id/原始JSONL行号见 [session_matrix.json](session_matrix.json)，逐轮文本见 [TRANSCRIPTS.md](../../../../.runtime-persona-audit-2026-10-19/student/TRANSCRIPTS.md)。