# 第三方内容说明

这份说明区分“参考了一个项目”和“在仓库中包含它的内容”。引用仓库不等于取得其中所有代码、数据或图片的再分发权。

## 狗头军师 Skill

- 上游：[shengjidaguai-china/goutoujunshi](https://github.com/shengjidaguai-china/goutoujunshi)
- 固定版本：`6db7354a4002dc7c448a9c87ffdad8132570c9d3`
- 本地目录：`skills/goutoujunshi/`
- 上游许可证：[MIT](skills/goutoujunshi/LICENSE)，并保留其中文说明。

目录中保留上游文档、知识和许可。本项目新增 `bot-adapter.json`、`bot-core.md` 及 `skills/goutoujunshi-conversation/` 进行群聊适配。Bot 只按本地装配读取相关指令，不自动执行上游脚本。

## AstrBot 表情图包

- 上游：[anka-afk/astrbot-meme-pack-official-01](https://github.com/anka-afk/astrbot-meme-pack-official-01)
- 固定版本：`77f976826f058e566b7a84035774192871b6baac`
- 上游清单声明：[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
- 本地映射：`memes/catalog.json`
- 分类、精选旧路径及说明：[SOURCE.md](memes/astrbot-official-01/SOURCE.md)

此源码快照保留映射和来源说明，**不包含第三方图片本体**。已有部署里的图片继续留在本地，没有删除。上游许可证声明不能证明每张二创图片的原作者都授权了再分发；商业使用或公开再分发前需要进一步核实。

如果你已确认用途与授权，可以取得固定版本并恢复本地目录。下面命令仅作操作说明，本项目不会自动下载：

```bash
git clone https://github.com/anka-afk/astrbot-meme-pack-official-01.git /tmp/qunbot-meme-pack
git -C /tmp/qunbot-meme-pack checkout 77f976826f058e566b7a84035774192871b6baac
mkdir -p memes/astrbot-official-01/upstream
cp -R /tmp/qunbot-meme-pack/memes/. memes/astrbot-official-01/upstream/
```

使用一个不存在的临时目录，避免覆盖其他下载。恢复后的 `upstream/<分类>/` 由 catalog 的 `packs` 项扫描；根部十张精选副本是旧标签兼容项，不必为了全量分类读取而复制。保留本说明、上游署名和许可，重启或重新加载素材后再验证。

## Kinna 图片与未收录素材

`memes/ip/` 是本项目已有的 Kinna 形象和表情素材，README 使用其中一张。项目尚未为这些图片另行指定对外使用许可，不能把第三方 Skill 的 MIT 许可当成图片或整个项目的许可。

本地高校照片和校徽目录尚未完整接入，且没有随本次源码快照提交。补完功能时需要先记录来源、授权与必要署名，不能仅凭“网上能下载”就公开分发。

## 设计参考

[Pallas-Bot](https://github.com/PallasBot/Pallas-Bot) 的主页结构、[AstrBot](https://github.com/AstrBotDevs/AstrBot) 及相关插件的能力划分是设计参考，不表示这些项目的功能已经被完整移植。NapCat 是独立部署的 QQ 接入服务，本仓库不包含其登录票据或部署镜像。

项目整体许可证仍待维护者明确。在许可明确前，不应将本仓库描述为“全部 MIT”或保证商业再分发许可。
