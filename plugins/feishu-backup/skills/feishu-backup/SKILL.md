---
name: feishu-backup
description: 把企业飞书账号里的云文档、知识库、共享文件和群聊附件每周备份到这台电脑（macOS / Windows），旧版本按日期归档，并列出备份账号打不开的文件。用户说「配置飞书备份 / 飞书备份设置 / 飞书备份状态 / 现在备份一次 / 看权限缺口 / 改飞书备份时间 / 飞书备份重新登录 / 更新飞书备份 / 卸载飞书备份」，或问飞书文件怎么备份到本地时使用。
---

# 飞书备份

按 **`plugins/feishu-backup/AGENTS.md`** 执行——从这份 SKILL.md 看是 `../../AGENTS.md`（插件根目录下的 AGENTS.md）。那是唯一的操作说明：注意事项、第 0 步准备、7 步向导、日常操作、退出码与排障都在里面，这里不重复。

用 `/plugin install` 装的插件不必再 clone 仓库：第 0 步「取得程序」可以跳过，install 之前运行 `python3 <本 SKILL.md 所在目录>/scripts/fbk.py install`（Windows 用 `py -3`），之后一律用 AGENTS.md 第二节里的启动器 `launch.py`。
