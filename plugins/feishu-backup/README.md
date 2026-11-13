# feishu-backup

把一个企业飞书账号（下称「备份账号」）能看到的文件，定期备份到这台 Mac：

- **我的空间**、**知识库**、**共享给我**（群聊里分享过、或搜索得到的、备份账号能打开的文档）、**群聊附件**（文件和音视频，不含图片）
- 在线文档存 PDF，表格 / 多维表格存 xlsx，上传的文件保持原格式
- 增量备份：只下载变过的文件，中断后下次接着跑
- 文件在飞书上被改过，旧版移到同目录 `历史版本/`，按日期命名；**从不删除或覆盖本地文件**
- 每次运行生成备份日志，并列出「群里出现过、但备份账号打不开」的文件（权限缺口报告）
- 只读：程序只调用读取类接口，不会改动飞书上的任何东西

只用 macOS 自带的 `python3`（3.9 或更高）和标准库，不装任何依赖。日常由系统定时任务运行，不需要开着 Claude Code。

## 飞书后台准备（只做一次）

用备份账号（或企业管理员）登录 [飞书开发者后台](https://open.feishu.cn/app)：

1. **创建应用**：「创建企业自建应用」，名字随意（例如「飞书备份」）。进入应用后，「凭证与基础信息」页有 App ID 和 App Secret，配置时要用。App Secret 只在弹窗里填，不要发到聊天里；万一泄露，在同一页重置即可。
2. **开通权限**：「权限管理」→「批量导入」，粘贴下面整段 JSON，确认开通：

   ```json
   {
     "scopes": {
       "tenant": [],
       "user": [
         "base:app:copy",
         "base:app:create",
         "base:app:read",
         "base:app:update",
         "base:appmode_block:create",
         "base:appmode_block:delete",
         "base:appmode_block:read",
         "base:appmode_block:update",
         "base:appmode_page:create",
         "base:appmode_page:delete",
         "base:appmode_page:read",
         "base:dashboard:copy",
         "base:dashboard:create",
         "base:dashboard:delete",
         "base:dashboard:read",
         "base:dashboard:update",
         "base:workspace:create",
         "base:workspace:list",
         "base:workspace:read",
         "base:workspace:update",
         "bitable:app",
         "bitable:app:readonly",
         "board:whiteboard:node:create",
         "board:whiteboard:node:delete",
         "board:whiteboard:node:read",
         "board:whiteboard:node:update",
         "docs:doc",
         "docs:doc:readonly",
         "docs:document.comment:create",
         "docs:document.comment:delete",
         "docs:document.comment:read",
         "docs:document.comment:update",
         "docs:document.comment:write_only",
         "docs:document:copy",
         "docs:document:export",
         "docs:document:import",
         "docs:document.media:download",
         "docs:document.media:upload",
         "docs:permission.member",
         "docs:permission.member:auth",
         "docs:permission.member:create",
         "docs:permission.member:delete",
         "docs:permission.member:readonly",
         "docs:permission.member:retrieve",
         "docs:permission.member:transfer",
         "docs:permission.member:update",
         "docx:document",
         "docx:document.block:convert",
         "docx:document:create",
         "docx:document:readonly",
         "docx:document:write_only",
         "drive:document.content:read",
         "drive:document.mention:read",
         "drive:drive",
         "drive:drive.metadata:readonly",
         "drive:drive.search:readonly",
         "drive:drive:readonly",
         "drive:drive:version",
         "drive:drive:version:readonly",
         "drive:export:readonly",
         "drive:file",
         "drive:file.like:readonly",
         "drive:file:download",
         "drive:file:readonly",
         "drive:file:upload",
         "drive:file:view_record:readonly",
         "im:chat",
         "im:chat:read",
         "im:chat:readonly",
         "im:chat:update",
         "im:message",
         "im:message:readonly",
         "im:resource",
         "mindnote:node:create",
         "mindnote:node:read",
         "minutes:minutes",
         "minutes:minutes.artifacts:read",
         "minutes:minutes.basic:read",
         "minutes:minutes.media:export",
         "minutes:minutes.search:read",
         "minutes:minutes:readonly",
         "search:docs:read",
         "sheets:spreadsheet",
         "sheets:spreadsheet.meta:read",
         "sheets:spreadsheet.meta:write_only",
         "sheets:spreadsheet:create",
         "sheets:spreadsheet:read",
         "sheets:spreadsheet:readonly",
         "sheets:spreadsheet:write_only",
         "slides:presentation:create",
         "slides:presentation:read",
         "slides:presentation:update",
         "slides:presentation:write_only",
         "space:document.event:read",
         "space:document:delete",
         "space:document:move",
         "space:document:retrieve",
         "space:document:shortcut",
         "space:folder:create",
         "wiki:member:create",
         "wiki:member:retrieve",
         "wiki:member:update",
         "wiki:node:copy",
         "wiki:node:create",
         "wiki:node:move",
         "wiki:node:read",
         "wiki:node:retrieve",
         "wiki:node:update",
         "wiki:setting:read",
         "wiki:space:read",
         "wiki:space:retrieve",
         "wiki:space:write_only",
         "wiki:wiki",
         "wiki:wiki:readonly",
         "contact:user.base:readonly",
         "offline_access",
         "im:message.group_msg:get_as_user",
         "im:message.p2p_msg:get_as_user",
         "im:chat.members:read"
       ]
     }
   }
   ```

   这些都是**用户身份**权限，共 119 项。其中有写权限，但程序内置只读白名单，只会发读取类请求。
3. **重定向 URL**：「安全设置」→「重定向 URL」，添加 `http://127.0.0.1:8765/callback`。
4. **机器人能力**：「添加应用能力」→「机器人」。读群消息必须开启（不需要把机器人拉进群）。开启后在机器人设置里**关掉「允许被其他机器人邀请进群」**。
5. **发布**：「版本管理与发布」→「创建版本」→ 填写后「发布」，等企业管理员审批通过。想免审批，可由管理员在「管理后台 → 应用管理 → 该应用」里设置免审；可用范围建议只放备份账号一人。

以后在后台改了权限，都要**再创建一个版本并发布**，然后在本机重新登录一次才生效。

## 安装与配置

```
/plugin marketplace add liujunyuan0409-bot/ellen
/plugin install feishu-backup@ellen
```

然后对 Claude 说 **「配置飞书备份」**。Claude 会带你走 7 步，每步都实际调用飞书检查一遍，失败就停在那一步并告诉你怎么修：

1. 环境检查：Python 版本、能不能连上飞书
2. 应用凭证：弹出系统对话框填 App ID 和 App Secret（最多等 5 分钟）
3. 登录授权：浏览器打开飞书授权页，用备份账号点「授权」
4. 权限体检：8 项实测（列我的空间、列知识库、搜索、导出、下载、权限判断、列群、读群消息）
5. 备份目录：选一个文件夹，建议外接盘或 `~/飞书备份`；会检查可写、剩余空间、是否在 iCloud 同步目录
6. 首次扫描：只统计不下载，报告文件数、预估体积和耗时，你确认后才开始备份
7. 定时任务：每小时检查一次，距上次成功满 24 小时才真正备份；Mac 关机期间不跑，开机后自动补

日常可以说：「飞书备份状态」「现在备份一次」「看权限缺口」「飞书备份重新登录」。

**登录有效期**：登录信息每次备份时自动续期。Mac 连续 7 天以上没有成功备份（例如一直没开机），登录会过期，系统通知会提醒你，对 Claude 说「飞书备份重新登录」即可。

## 备份目录长这样

```
<备份目录>/
├── 我的空间/…/历史版本/原名_2026-10-02_1530.xlsx
├── 知识库/<空间名>/…
├── 共享给我/<群名>/ 或 _搜索发现/
├── 群聊附件/<群名>/2026-11-13_报价单.xlsx
├── _报告/YYYY-MM-DD_备份日志.md、YYYY-MM-DD_权限缺口.csv
└── .feishu-backup/        清单数据库（换电脑时一起拷走）
```

本机还会用到：`~/.config/feishu-backup/`（应用凭证和登录信息，权限仅自己可读）、`~/Library/Logs/feishu-backup/`（运行日志，保留 30 天）。

## 换电脑

1. 把旧电脑的整个备份目录（含隐藏的 `.feishu-backup/`）拷到新电脑或接上同一块外接盘。
2. 新电脑上按「安装与配置」装插件，说「配置飞书备份」。第 5 步选拷过来的那个目录，已备份的文件不会重新下载。
3. 想省掉第 2 步，可以把旧电脑的 `~/.config/feishu-backup/config.json` 拷到新电脑同一位置（这个文件含 App Secret，用 U 盘或隔空投送，不要走聊天或邮件）。登录（第 3 步）仍要在新电脑上重新做一次。
4. 旧电脑上卸载定时任务（见下），免得两台同时备份。

## 卸载

1. 对 Claude 说「卸载飞书备份」，或运行 `python3 <插件目录>/skills/feishu-backup/scripts/fbk.py schedule uninstall`，停掉定时任务。
2. 删除 `~/.config/feishu-backup` 文件夹（凭证和登录信息）。
3. `/plugin uninstall feishu-backup@ellen`。

备份目录不会被动，里面的文件都还在，要不要删由你决定。

## 局限

- **看不到别人私人空间里的文件**：飞书里每个人的「我的空间」是私有的，管理员也读不到别人没分享的文件。公司文件要放进备份账号能看到的知识库或共享文件夹才会被备份。
- **免费版没有审计日志**，做不了「全企业文件盘点」。权限缺口报告只能发现两类文件：出现在备份账号所在群里的链接，以及搜索接口能搜到的文档。
- 思维笔记等没有导出接口的类型会跳过，在备份日志里列出。
- 群聊附件只备份文件和音视频，不备份图片；首次回溯最近 180 天的群消息。
- 历史版本是「每次运行时的快照」：两次备份之间改了多次，只留运行那一刻的版本。
- 只支持 macOS。

## 开发

```bash
python3 -m unittest discover -s plugins/feishu-backup/tests -t plugins/feishu-backup -v
```

测试全部用虚构数据，不访问真实飞书。版本记录见 [CHANGELOG.md](CHANGELOG.md)。
