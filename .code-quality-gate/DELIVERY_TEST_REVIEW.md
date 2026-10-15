# 发送验收修正

009 首次验收把 ConversationStore.message_count 当成全部消息数，实际该方法只计用户消息（供提炼间隔使用），导致三项助手记账断言错误。不能为通过测试改变该存储契约。

原测试、门禁配置、适配器、任务和 red 证据已原样保存到 history/DELIVERY-009-R1-inputs.zip；原 evidence 与任务不改。修正为 actual_recent 的真实消息计数，未减少案例或断言。另补充重复回执记账不能重复注入 Prompt 或投影的业务断言。

009 未进行 green，无实现迭代预算消耗；R2 沿用全部门禁与 12 次预算。R2 的 red 针对目前确实存在的重复 Prompt/投影行为，不回退已实现代码制造失败。
