---
name: qq-channel
description: 管理 QQ 官方频道（非普通 QQ 群）：查询频道、子频道、成员、论坛、公告和日程。
triggers: QQ频道, 子频道, guild, channel, 论坛, 帖子, 频道公告, 频道日程
---
本 Skill 只处理 QQ 官方频道 Guild/Channel，不处理 NapCat 普通 QQ 群。先用 `query_qq_channel` 查询真实 ID 和权限，不猜测 ID。

常用只读路径：`/users/@me/guilds`、`/guilds/{guild_id}/api_permission`、`/guilds/{guild_id}/channels`、`/guilds/{guild_id}/members`、`/channels/{channel_id}/threads`。所有占位符必须换成纯数字 ID，query 值使用字符串。

只有部署者明确要求并且 `write_qq_channel` 通过本地 operator 校验时，才能发帖、评论、创建公告或日程。支持的写入仅限：`PUT /channels/{channel_id}/threads`、`POST /channels/{channel_id}/threads/{thread_id}/comment`、`POST /guilds/{guild_id}/announces`、`POST /channels/{channel_id}/schedules`、`PATCH /channels/{channel_id}/schedules/{schedule_id}`。删除与子频道结构变更没有开放，不得绕过。

论坛发帖 body 使用 `title`、`content`、`format`（Markdown 为 3）。日程时间戳是毫秒字符串；先核对时区和起止时间。收到权限错误时，调用频道权限接口说明缺少什么，不要重复尝试。
