# feishu-backup

把一个企业飞书账号（下称「备份账号」）能看到的文件，**每周一次**备份到这台电脑。支持 **macOS** 和 **Windows**（**Windows 版未经真机验证**，验收清单见 [WINDOWS_CHECKLIST.md](WINDOWS_CHECKLIST.md)）。

- 备份范围：**我的空间**、**知识库**、**共享给我**（群聊里分享过、或搜索得到的、备份账号能打开的文档）、**群聊附件**（文件和音视频，不含图片）
- 在线文档存 PDF，表格 / 多维表格存 xlsx，上传的文件保持原格式
- 增量备份：只下载变过的文件，中断后下次接着跑
- 文件在飞书上被改过，旧版移到同目录 `历史版本/`，按日期命名；**从不删除或覆盖本地文件**，飞书上删掉的文件本地也保留
- 每次运行生成备份日志，并列出「群里出现过、但备份账号打不开」的文件（权限缺口报告）
- 只读：程序只调用读取类接口，不会改动飞书上的任何东西

只需要 Python 3.9 或更高版本，只用标准库，不装任何依赖。日常由系统定时任务运行，不需要开着 AI 工具。

## 每周一次是怎么跑的

- 默认**每周一中午 12:00**（按这台电脑的时间）备份一次，可以改成别的星期几、别的时间。
- 电脑开着时，定时任务每小时检查一次，登录/开机时也检查一次：上一个计划时间点之后还没成功备份过，才真正开始备份；否则马上退出。所以周一中午电脑关着或睡着，下次开机/唤醒后的一小时内会自动补跑，一周内不会重复跑。
- 某次有文件夹没读全（不算成功）时，6 小时后再试。连续两周没备份成功，每天弹一次通知提醒。
- 随时可以让 AI「现在备份一次」，不受每周时间限制。
- **历史版本是每周的快照**：一周内同一个文件改了好几次，只留备份那一刻的版本。想要更密，把计划改成每天（让 AI 帮你改时间，或者在一周里多手动备份几次）。

## 怎么安装

把这个仓库的链接发给你的 AI 助手（Claude Code、Codex CLI、Cursor、Gemini CLI 等都可以），说：

> 帮我装一下这个，备份我们公司的飞书文件：https://github.com/liujunyuan0409-bot/ellen

AI 会读仓库里的 [AGENTS.md](AGENTS.md)，带你检查/安装 Python、安装程序，然后走 7 步配置：

1. 环境检查：Python 版本、能不能连上飞书、证书是否正常
2. 应用凭证：弹出系统小窗口填 App ID 和 App Secret（**只填在小窗口里，不要发给 AI**）。这两样相当于飞书发给备份程序的「账号」和「密码」，查看方法：浏览器打开 [飞书开发者后台](https://open.feishu.cn/app)，用备份账号登录 → 点开为备份建的那个应用 → 左边「凭证与基础信息」，最上面就是 App ID（`cli_` 开头）和 App Secret（点眼睛图标显示）。找不到就问飞书管理员；AI 也会一步步带你找
3. 登录授权：浏览器打开飞书授权页，用备份账号点「授权」
4. 权限体检：8 项实测（列我的空间、列知识库、搜索、导出、下载、权限判断、列群、读群消息）
5. 备份目录：选一个文件夹（建议外接盘或空间充足的盘，留至少 100 GB）；会检查可写、剩余空间、是否在 iCloud / OneDrive 同步目录
6. 首次扫描：只统计不下载，报告文件数、预估体积和耗时，你确认后才开始备份
7. 定时任务：装好每周的定时任务，第一次备份随即在后台开始

Claude Code 用户也可以用插件方式安装，然后说「配置飞书备份」：

```
/plugin marketplace add liujunyuan0409-bot/ellen
/plugin install feishu-backup@ellen
```

日常可以对 AI 说：「飞书备份状态」「现在备份一次」「看权限缺口」「把备份时间改成每周五晚上 8 点」「飞书备份重新登录」「更新飞书备份」。

## 飞书后台准备（只做一次）

用备份账号（或企业管理员）登录 [飞书开发者后台](https://open.feishu.cn/app)：

1. **创建应用**：「创建企业自建应用」，名字随意（例如「飞书备份」）。进入应用后，「凭证与基础信息」页有 App ID 和 App Secret，配置时要用。App Secret 只在弹窗里填，不要发到聊天里；万一泄露，在同一页重置即可。
2. **开通权限**：「权限管理」→「批量导入」，粘贴下面整段 JSON，确认开通：

   ```json
   {
     "scopes": {
       "tenant": [],
       "user": [
         "drive:drive:readonly",
         "drive:drive.metadata:readonly",
         "drive:file:readonly",
         "drive:file:download",
         "drive:export:readonly",
         "docs:document:export",
         "docs:doc:readonly",
         "docx:document:readonly",
         "sheets:spreadsheet:readonly",
         "bitable:app:readonly",
         "slides:presentation:read",
         "wiki:wiki:readonly",
         "wiki:node:read",
         "wiki:space:read",
         "space:document:retrieve",
         "search:docs:read",
         "docs:permission.member:auth",
         "im:chat:readonly",
         "im:message:readonly",
         "im:message.group_msg:get_as_user",
         "im:resource",
         "im:chat.members:read",
         "contact:user.base:readonly",
         "offline_access"
       ]
     }
   }
   ```

   这些都是**用户身份**的只读权限，共 24 项，是程序实际用到的最小集合；登录时也只申请这些。程序内置只读白名单，只会发读取类请求。
3. **重定向 URL**：「安全设置」→「重定向 URL」，添加 `http://127.0.0.1:8765/callback`。
4. **机器人能力**：「添加应用能力」→「机器人」。读群消息必须开启（不需要把机器人拉进群）。开启后在机器人设置里**关掉「允许被其他机器人邀请进群」**。
5. **发布**：「版本管理与发布」→「创建版本」→ 填写后「发布」，等企业管理员审批通过。想免审批，可由管理员在「管理后台 → 应用管理 → 该应用」里设置免审；可用范围建议只放备份账号一人。

以后在后台改了权限，都要**再创建一个版本并发布**，然后在每台电脑上重新登录一次才生效。

### 授权是累计的：怎样拿到真正只读的登录

程序登录时只申请上面 24 项只读权限，但飞书会把**这个账号以前对这个应用授过的所有权限**都一起放进登录凭据。如果这个应用以前开过写权限、备份账号也授权过（例如之前拿它做过别的事），凭据里就仍然带着写权限——程序自己不会用（只读白名单会拦下），但凭据本身不是只读的。想要真正只读：

1. 在开发者后台「权限管理」里，把应用的权限**只保留上面这 24 项**，删掉其他的，然后创建版本并发布。
2. 用备份账号在飞书里**撤销对这个应用的授权**（在飞书的个人设置里找「授权管理」或「已授权的应用」，找到这个应用，取消授权；不同版本的飞书入口位置可能略有不同）。
3. 在每台装了飞书备份的电脑上让 AI「飞书备份重新登录」，再「飞书备份体检」确认 8 项仍然全部通过。

## 多人、多台电脑

- 可以多个人、多台电脑都装，**都用同一个备份账号**，每台电脑各自存一份完整备份，彼此不需要协调。
- **每台电脑各自登录一次**（7 步里的第 3 步），各自保存登录信息，**不要拷贝或共用 `token.json`**。
- 如果某台电脑弹通知说「需要重新登录」（例如别的电脑登录后，飞书让这台的旧登录失效了；是否会这样还没有实测），在那台电脑上对 AI 说「飞书备份重新登录」再登一次即可。

## 登录有效期

登录信息只要电脑隔几天开一次机就会自动续期（定时任务每小时检查时顺手续上）。电脑**连续 7 天以上没开机**（或备份盘一直没插、备份目录不在），登录会过期，系统通知会提醒你，对 AI 说「飞书备份重新登录」即可。

## 备份目录长这样

```
<备份目录>/
├── 我的空间/…/历史版本/原名_2026-10-02_1530.xlsx
├── 知识库/<空间名>/…
├── 共享给我/<群名>/ 或 _搜索发现/
├── 群聊附件/<群名>/2026-11-13_报价单.xlsx
├── _报告/YYYY-MM-DD_HHMM_备份日志.md、YYYY-MM-DD_权限缺口.csv
└── .feishu-backup/        清单数据库（换电脑时一起拷走）
```

两个系统的文件名规则相同：`/ \ : * ? " < > |` 等字符换成 `_`，Windows 不允许的名字（如 `CON`、`AUX`、`COM1`）前面加 `_`；同一文件夹里重名（大小写不同也算重名）的，后一个名字后面加一小段标识区分，不会互相覆盖。飞书里只差大小写的两个**文件夹**（如 `Report` 和 `report`）会落到同一个本地文件夹里。

本机还会用到：

| 用途 | macOS | Windows |
|---|---|---|
| 应用凭证和登录信息（仅自己可读） | `~/.config/feishu-backup/` | `%APPDATA%\feishu-backup\` |
| 运行日志（保留 30 天） | `~/Library/Logs/feishu-backup/` | `%LOCALAPPDATA%\feishu-backup\logs\` |
| 定时任务运行的程序副本 | `~/Library/Application Support/feishu-backup/app/` | `%LOCALAPPDATA%\feishu-backup\app\` |
| 下载的程序（AI 安装时放的位置） | `~/feishu-backup` | `%USERPROFILE%\feishu-backup` |

## 换电脑

1. 把旧电脑的整个备份目录（含隐藏的 `.feishu-backup/`）拷到新电脑，或接上同一块外接盘。
2. 在新电脑上按「怎么安装」装一遍。第 5 步选拷过来的那个目录，已备份的文件不会重新下载。
3. 想省掉第 2 步，可以把旧电脑的配置文件 `config.json`（位置见上表）拷到新电脑同一位置（这个文件含 App Secret，用 U 盘或隔空投送，不要走聊天或邮件）。登录（第 3 步）仍要在新电脑上重新做一次。
4. 旧电脑不再备份的话，在旧电脑上卸载（见下）。

## 卸载

备份目录不会被动，里面的文件都还在，要不要删由你决定。

**macOS**（可以让 AI 做第 1 步，第 2 步自己在「访达」里删）：

1. 停掉定时任务：`python3 ~/"Library/Application Support/feishu-backup/app/launch.py" schedule uninstall`
2. 删除这几个文件夹：`~/.config/feishu-backup`（凭证和登录信息）、`~/Library/Application Support/feishu-backup`（程序副本）、`~/Library/Logs/feishu-backup`（日志）、`~/feishu-backup`（下载的程序）。
3. Claude Code 插件用户：`/plugin uninstall feishu-backup@ellen`。

**Windows**（Windows 版未经真机验证）：

1. 停掉定时任务：在 PowerShell 里运行 `py -3 "$env:LOCALAPPDATA\feishu-backup\app\launch.py" schedule uninstall`；之后「任务计划程序」里不再有 `feishu-backup`。
2. 删除这几个文件夹（在资源管理器地址栏粘贴路径即可打开）：`%APPDATA%\feishu-backup`（凭证和登录信息）、`%LOCALAPPDATA%\feishu-backup`（程序副本和日志）、`%USERPROFILE%\feishu-backup`（下载的程序）。
3. Claude Code 插件用户：`/plugin uninstall feishu-backup@ellen`。

想彻底收回授权，再按上面「授权是累计的」第 2 条在飞书里撤销这个应用的授权。

## 局限

- **看不到别人私人空间里的文件**：飞书里每个人的「我的空间」是私有的，管理员也读不到别人没分享的文件。公司文件要放进备份账号能看到的知识库或共享文件夹才会被备份。
- **免费版没有审计日志**，做不了「全企业文件盘点」。权限缺口报告只能发现两类文件：出现在备份账号所在群里的链接，以及搜索接口能搜到的文档。
- 思维笔记等没有导出接口的类型会跳过，在备份日志里列出。
- 群聊附件只备份文件和音视频，不备份图片；首次回溯最近 180 天的群消息。
- 每周一次，历史版本是每周的快照（见上）。
- 电脑关机期间不会备份，开机后补跑；首次备份几十 GB，可能要跨好几次开机才完成（中断了会接着跑）。
- 多台电脑之间不协调、不合并，各存各的。
- 不支持 Linux。**Windows 版未经真机验证**：等有 Windows 电脑时按 [WINDOWS_CHECKLIST.md](WINDOWS_CHECKLIST.md) 逐条验证后再去掉这个标注。

## 开发

```bash
python3 -m unittest discover -s plugins/feishu-backup/tests -t plugins/feishu-backup -v
```

测试全部用虚构数据，不访问真实飞书，也不碰本机的配置、定时任务和系统弹窗。各系统差异集中在 `skills/feishu-backup/scripts/fbk/platform/`。版本记录见 [CHANGELOG.md](CHANGELOG.md)。
