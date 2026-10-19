**家庭画像会话矩阵**

| 会话 | 身份 / 视口 | 对话与聊天轮次 | 主要任务 | 保存与恢复证据 |
|---|---|---|---|---|
| S0 准备（不计使用会话） | A访客，1440×960 | 0 | 读公开brief/snapshot，首页、中文与控件检查；等待父agent明确开始 | s0-desktop-initial.png、s0-zh-controls.json |
| S1 第一次使用 | A访客，1440×960 | C1，T01–T06，6次聊天；另UI-F1表单1次 | 五口、3卧、轮椅、可能带猫、入住日期；纠正错误、具体核实、收藏Villiers | s1-c1-t01至t06.json；s1-favorite-villiers.png |
| S2 关闭重开 | 同A存储，1440×960 | C1，T07–T12，6次聊天 | 语境恢复、预算和地点变化、猫变必须、通勤/学校、比较、交接；重命名 | s2-reopen-state.json、s2-pre-restart-state.json |
| 重启检查点 | 浏览器关闭、无待处理聊天 | 0 | 请求父agent实际停止/启动服务并等待明确完成 | restart-confirmation.txt |
| S3 重启后返回 | 同A存储，390×844 | C2，T13–T20，8次聊天；回看C1无新增聊天 | 验证重启恢复；独立High Barnet备用方案；周月预算、轮椅假设、六人四卧、收藏Woodfall；T15键盘发送 | s3-restart-ui-state.json、s3-restart-favorites.json、s3-c2-t15-keyboard.json、s3-c1-after-c2-change.json |
| S4 新身份 | 独立profile，先新访客后虚构注册B，1440×960 | C3，T21–T26，6次聊天 | 资料隔离、新家庭Manchester搜索、学生标签、预算降低、旧UI、长期提醒；注销/登录核对 | s4-fresh-identity.json、s4-b-favorite-isolation.json、s4-after-logout.json、s4-after-login.json |
| S5 只读回看 | 恢复A profile，1440×960 | 0 | A两对话、两收藏，与B内容隔离；关闭全部浏览器 | s5-original-identity-restored.json、s5-primary-two-favorites.png |

| 对话 | conversation_id | 聊天数 | 模拟使用会话 | 最终标题 |
|---|---|---:|---|---|
| C1 | 30b8a2ee3ca5446ba8ea22efe41e7a1e | 12 | S1、S2；S3/S5回看 | Family A — Watford verification |
| C2 | f5c8dc3380da4ea4a1724880954dd715 | 8 | S3 | Family A — High Barnet backup |
| C3 | 205170a28d5245538f14f9f421cb7e89 | 6 | S4 | Family B — Manchester |

总计26次真实聊天提交，3个有聊天对话，4次含聊天模拟会话，另一次只读回看；C1另外有1次表单提交。这些是同日加速模拟，不能当作真实数周使用。每轮精确时间、目的、request ID及turn_id见 [turn-matrix.csv](../../../../.runtime-persona-audit-2026-10-19/family/turn-matrix.csv)，逐字输入输出见 [TRANSCRIPT.md](../../../../.runtime-persona-audit-2026-10-19/family/TRANSCRIPT.md)。

T22原本与辅助API读取同批提交，整批被自动审批拒绝而未执行；后续T22独立提交一次。被拒绝的API未再次尝试、不计聊天，也没有取得跨身份消息。T15是26轮内的真实键盘发送，单独记录；UI-F1没有独立请求ID，不能将helper残留T04 ID重复计数。
