# 飞书备份 · 给 AI 助手的安装与使用说明

> 这是飞书备份唯一的操作说明。任何 AI 编程助手（Claude Code、Codex CLI、Cursor、Gemini CLI 等）读完这一份，就能带用户完成：安装 Python → 安装程序 → 7 步配置 → 第一次备份，以及之后的日常操作。给人看的介绍在同目录的 [README.md](README.md)。

**你在帮谁**：一个没有开发背景的人，他把本仓库的 GitHub 链接发给你，想把公司飞书「备份账号」能看到的文件定期备份到这台电脑（macOS 或 Windows）。装好以后由系统定时任务每周跑一次，不需要开着你。

---

## 一、写给 AI 的注意事项（先读完再动手）

1. **一次只问用户一个问题**，等回答再往下走。用大白话，不说 token、scope、LaunchAgent、schtasks 这类词；必须提到时顺手解释一句。面向用户的话一律用中文。
2. **任何一步出现 ✗：把那一行和下面「怎么办：」那一行原样告诉用户，然后停下**，等用户处理完再重跑这一步。不要自己跳过、不要猜别的办法。✓ 表示通过；⚠ 表示通过但要提醒用户（转告后问他要不要处理）。
3. **绝不经手 App Secret**：不要向用户索要 App ID / App Secret，不要让用户贴进对话，不要在回复里复述或打印它们，也不要自己读或改 `config.json`。凭证只能在程序弹出的系统对话框里填。如果用户还是把 App Secret 贴进了对话，告诉他：「这个密钥已经暴露了，请到飞书开发者后台『凭证与基础信息』里重置 App Secret，再用弹窗重新填一次。」
4. **沙箱和联网**：这个程序要连 `open.feishu.cn`、在本机 `127.0.0.1:8765` 起一个临时网页接收登录结果、写工作区以外的目录（见文末「本机位置」）、弹系统对话框、装系统定时任务。有的 AI 工具默认不允许这些（例如 Codex CLI 的沙箱默认不联网、只能写工作区；Gemini CLI 开了沙箱时也一样；Cursor 每条命令要用户点允许）。**遇到「连不上飞书」「Permission denied」「Operation not permitted」这类错误，先想到是你所在工具的权限限制**，请用户放开（例如批准这条命令在沙箱外运行、给本次会话开网络权限），不要当成飞书故障。
5. **耗时长的命令**：`setup --login`（最多等 10 分钟）、`scan`（几分钟到十几分钟）、`run --now`（第一次可能几个小时）。能后台运行就后台运行，结束后再读结果；不能后台运行、或单条命令有时间上限的工具，告诉用户「这一步要等一会儿，窗口别关」，或者请用户自己打开「终端」（Windows：PowerShell）粘贴运行，跑完把最后几行发给你。
6. **弹窗会等最多 5 分钟**：运行 `setup --credentials` 前先提醒用户留意弹出的小窗口（可能藏在别的窗口后面）。命令会一直等到用户填完或 5 分钟超时，期间不要催、不要重复运行。
7. **每条命令都可以加 `--json`**，输出机器可读的结果（`ok`、`checks[].ok/name/detail/fix` 等），方便你判断；转述给用户时用中文、去掉技术细节。
8. **不要替用户删任何备份文件或备份文件夹**，不要新建备份目录（只有用户同意后用 `setup --root <路径> --create` 新建），不要修改程序文件。
9. **只用本程序的命令备份，不要自己另写一套**：备份、扫描、检查有没有变动，一律通过下面列出的 `<fbk>` 命令完成。不要自己写脚本调用飞书接口，不要改用 lark-cli、飞书客户端导出或其他工具，也不要自己去比对本地文件。用户要的功能这里没有（例如「每小时检查一次」「只看哪个文件变了但不下载」），就直接告诉他「目前的飞书备份做不到这一点」，然后停下，不要临时拼一个替代方案：自己拼出来的东西不会记进备份清单，以后定时备份也接不上。
10. **转述结果时说明截至时间，不要把话说满**：`run` 结束后会打印「本轮 … 开始、… 结束」。告诉用户有没有新增或修改时，必须带上这个结束时间，例如「截至 11 月 17 日 22:41，新增 1 个，其余没有变化」。不能笼统地说「原有文件都没变」：备份是定时检查，结束时间之后在飞书上发生的修改，要等下一轮才知道。另外，同一个文件在飞书里改名或挪了位置，会出现在备份日志的「移动」里，旧版本挪进同目录的 `历史版本/`，并不是一个文件被删掉、另一个是新增的。
11. **Windows 版未经真机验证**：在 Windows 上开始前告诉用户这一点；遇到和本说明不一致的情况，原样记下来（命令、输出），请用户反馈给维护者，对照 [WINDOWS_CHECKLIST.md](WINDOWS_CHECKLIST.md)。

---

## 二、命令怎么写

程序只用 Python 标准库，不需要装任何包。第一次 `install` 之前，运行仓库里的 `fbk.py`；`install` 之后，**一律运行安装位置里的启动器 `launch.py`**（定时任务用的也是它，仓库以后更新或挪走都不影响）。

| | macOS（终端） | Windows（PowerShell） |
|---|---|---|
| 仓库位置 | `~/feishu-backup` | `$env:USERPROFILE\feishu-backup` |
| install 之前 | `python3 ~/feishu-backup/plugins/feishu-backup/skills/feishu-backup/scripts/fbk.py <命令>` | `py -3 "$env:USERPROFILE\feishu-backup\plugins\feishu-backup\skills\feishu-backup\scripts\fbk.py" <命令>` |
| install 之后 | `python3 ~/"Library/Application Support/feishu-backup/app/launch.py" <命令>` | `py -3 "$env:LOCALAPPDATA\feishu-backup\app\launch.py" <命令>` |

- 下文写成 **`<fbk> 命令`**，指上表「install 之后」那一行。
- Windows 上没有 `py` 时把 `py -3` 换成 `python`。用的是「命令提示符」（cmd）而不是 PowerShell 时，把 `$env:USERPROFILE` 写成 `%USERPROFILE%`、`$env:LOCALAPPDATA` 写成 `%LOCALAPPDATA%`。
- **Claude Code 用户**如果是用 `/plugin install feishu-backup@ellen` 装的插件，可以不 clone：install 之前用 `python3 <SKILL.md 所在目录>/scripts/fbk.py install`，之后同样用启动器。
- 全部子命令：`install`、`setup`（`--env` / `--credentials` / `--login` / `--root PATH [--create]` / `--schedule [--weekday N --time HH:MM]` / `--status`）、`login`、`doctor`、`scan`、`run [--now]`、`status`、`perms`、`schedule install|uninstall`；每个都可加 `--json`。拿不准时运行 `<fbk> <命令> --help`。

---

## 三、第 0 步：准备

先判断系统：能运行 `uname` 且输出 `Darwin` 是 macOS；PowerShell 里 `$env:OS` 是 `Windows_NT` 是 Windows。

### macOS

1. **检查 Python**：运行 `python3 --version`。
   - 输出 `Python 3.9` 或更高 → 通过。
   - 提示找不到命令、弹出「需要安装命令行开发者工具」、或版本低于 3.9 → 请用户在「终端」里运行 `xcode-select --install`，在弹出的系统窗口里点「安装」，等它装完（可能十几分钟到半小时），再运行一次 `python3 --version`。
2. **取得程序**：
   - 你自己的工作区里**已经有本仓库**（例如用户让你 clone 过、或正打开着它）→ 可以直接用那一份的 `plugins/feishu-backup/skills/feishu-backup/scripts/fbk.py` 做下面第 3 步的 `install`，不用再下载一份；`install` 之后一律用启动器，和仓库放在哪无关。
   - 否则看 `~/feishu-backup/.git` 在不在（`test -d ~/feishu-backup/.git && echo git`）：
     - 在（以前用 git 装过）→ `git -C ~/feishu-backup pull`。
     - `~/feishu-backup` 在、但没有 `.git`（以前下载压缩包装的）→ 先把旧文件夹**改名留着**（不要删）：`mv ~/feishu-backup ~/feishu-backup.old-$(date +%Y%m%d%H%M%S)`，再按下面两种办法之一重新取。
     - `~/feishu-backup` 不在 → `git clone https://github.com/liujunyuan0409-bot/ellen.git ~/feishu-backup`（上一步装的开发者工具里带了 git）。
   - 没有 git 时下载压缩包（每次解压到一个新建的临时文件夹；`unzip -o` 不会停下来问要不要覆盖；最后一步要求 `~/feishu-backup` 已经不存在，否则新文件夹会被塞进旧文件夹里）：
     ```
     T=$(mktemp -d) && \
     curl -fL -o "$T/feishu-backup.zip" https://github.com/liujunyuan0409-bot/ellen/archive/refs/heads/main.zip && \
     unzip -o -q "$T/feishu-backup.zip" -d "$T" && \
     mv "$T/ellen-main" ~/feishu-backup
     ```
     `curl -f`：下载地址出错（如 404）时直接报错退出，不会把错误页当成压缩包；`&&` 让前一步失败时后面几步不再执行。
3. **安装程序副本**：`python3 ~/feishu-backup/plugins/feishu-backup/skills/feishu-backup/scripts/fbk.py install --json`
   - 成功：退出码 0，JSON 里 `"ok": true`，`launcher` 是 `~/Library/Application Support/feishu-backup/app/launch.py`。从现在起用 `<fbk>`。
   - 失败：输出里有 ✗ 和「怎么办」，原样转述。

### Windows（Windows 版未经真机验证）

先告诉用户：「Windows 版还没有在真机上验证过，过程中如果和说明不一样，我会记下来。」

1. **检查 Python**：运行 `py -3 --version`；没有 `py` 再试 `python --version`。
   - 输出 `Python 3.9` 或更高 → 通过。
   - 提示找不到命令，或出现「Python was not found; run without arguments to install from the Microsoft Store」（这是系统自带的占位程序，不是真的 Python）→ 运行 `winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements`（你代为运行，或请用户在 PowerShell 里运行；中途可能弹出「是否允许更改」，请用户点「是」）。**装完要关掉并重新打开 PowerShell 窗口**（你自己的工具也可能要重开会话）才认得新装的 Python，然后再检查一次。
   - 没有 `winget`（较老的 Windows 10）→ 请用户到 https://www.python.org/downloads/ 下载安装包，安装时**勾选「Add python.exe to PATH」**，其他选项保持默认（默认会装 tcl/tk，输入凭证的小窗口要用它）。
2. **取得程序**：
   - 你自己的工作区里**已经有本仓库** → 可以直接用那一份的 `plugins\feishu-backup\skills\feishu-backup\scripts\fbk.py` 做第 3 步的 `install`，不用再下载。
   - 否则看 `Test-Path "$env:USERPROFILE\feishu-backup\.git"`：
     - `True`（以前用 git 装过）→ `git -C "$env:USERPROFILE\feishu-backup" pull`。
     - `False` 但 `$env:USERPROFILE\feishu-backup` 存在（以前下载压缩包装的）→ 先把旧文件夹**改名留着**（不要删）：`Rename-Item "$env:USERPROFILE\feishu-backup" ("feishu-backup.old-" + (Get-Date -Format yyyyMMddHHmmss))`，再按下面两种办法之一重新取。
     - 文件夹不存在且有 git（`git --version` 能输出版本）→ `git clone https://github.com/liujunyuan0409-bot/ellen.git "$env:USERPROFILE\feishu-backup"`。
   - 没有 git（每次解压到一个新建的临时文件夹；最后一步要求 `$env:USERPROFILE\feishu-backup` 已经不存在）：
     ```
     $T = Join-Path $env:TEMP ("feishu-backup-" + (Get-Date -Format yyyyMMddHHmmss))
     New-Item -ItemType Directory -Path $T | Out-Null
     Invoke-WebRequest -Uri https://github.com/liujunyuan0409-bot/ellen/archive/refs/heads/main.zip -OutFile "$T\feishu-backup.zip"
     Expand-Archive -Path "$T\feishu-backup.zip" -DestinationPath $T -Force
     Move-Item "$T\ellen-main" "$env:USERPROFILE\feishu-backup"
     ```
     `Invoke-WebRequest` 遇到 404 等 HTTP 错误会直接报红色错误；**任何一行报错就停下**，把报错原样告诉用户，不要接着运行后面几行。
     如果下载那一行报证书或连接错误（例如 `SEC_E_NO_CREDENTIALS`、「基础连接已经关闭」），不要在 PowerShell 里反复换写法硬试：请用户用浏览器打开 `https://github.com/liujunyuan0409-bot/ellen/archive/refs/heads/main.zip` 下载，告诉你 zip 存在哪里，再从 `Expand-Archive` 那一行接着做（`-Path` 换成用户给的位置）。
3. **安装程序副本**：`py -3 "$env:USERPROFILE\feishu-backup\plugins\feishu-backup\skills\feishu-backup\scripts\fbk.py" install --json`
   - 成功：退出码 0，`"ok": true`，`launcher` 在 `%LOCALAPPDATA%\feishu-backup\app\launch.py`。从现在起用 `<fbk>`。
   - 输出里中文是乱码：程序在 Windows 上默认按 UTF-8 输出；你的工具若按 GBK 读，就在同一个窗口先运行 `$env:PYTHONIOENCODING = "gbk:replace"` 再重跑（✓ ✗ 会变成 `?`，看 `--json` 里的 `ok` 判断即可）。

---

## 四、开始向导之前：飞书后台的应用

向导第 2 步要用到一个「飞书企业自建应用」的 App ID 和 App Secret。先问用户：**「公司飞书的开发者后台里，给备份用的应用建好了吗？」**

- 建好了（管理员已经私下把 App ID / App Secret 给了用户）→ 继续。
- 没建 → 带用户按 [README.md](README.md)「飞书后台准备」一步一步做（创建应用、批量导入权限、重定向 URL、机器人能力、发布），一次说一步。这部分要用备份账号或企业管理员登录飞书开发者后台。

然后运行 `<fbk> setup --status`（不联网，几秒钟），看已经做到第几步，从第一个不是 ✓ 的步骤开始。

---

## 五、7 步向导

### [1/7] 环境检查

- 命令：`<fbk> setup --env`
- 成功：三行 ✓——`Python 版本`、`网络：能连上 open.feishu.cn`、`证书`。
- 失败怎么转述：
  - `✗ 网络：连不上飞书` → 先排除你自己工具的沙箱/联网限制（见注意事项 4），再请用户检查网络。
  - `✗ 证书` → 原样转述「怎么办」（程序会按 Python 的来源给出修法：python.org 装的运行 Install Certificates，macOS 自带的更新系统，Homebrew 装的重装证书；Windows：更新 Python 或检查公司代理证书）。
  - `✗ Python 版本` → 回到第 0 步装新版 Python。

### [2/7] 应用凭证

- **先确认用户手里有这两样东西**，用大白话解释，然后问「你现在能打开这个页面吗？」：
  > 「这一步要两样东西：**App ID** 和 **App Secret**。可以把它们理解成飞书给这个备份程序发的『账号』和『密码』，程序靠它们才能连上你们公司的飞书。它们在飞书开发者后台能看到，查看方法：
  > 1. 用电脑浏览器打开 https://open.feishu.cn/app ，用**备份账号**扫码或登录（不是备份账号的话，请找你们的飞书管理员要）。
  > 2. 在「企业自建应用」列表里，点开你们**为备份建的那个应用**（不知道是哪个的话，问把这个备份工具交给你的人）。
  > 3. 左边点「凭证与基础信息」，页面最上面就是 **App ID**（以 `cli_` 开头）和 **App Secret**（一串星号，点旁边的眼睛图标显示，或点复制图标）。
  >
  > 如果列表里找不到这个应用，说明还没建，请管理员先按 README 的「飞书后台准备」建好再继续。
  > 待会儿会弹出小窗口让你粘贴进去。**App Secret 只粘贴到小窗口里，不要发给我，也不要发到聊天里。**」
- 用户说准备好了，再对用户说：「马上会弹出一个小窗口，请填 App ID；接着再弹一次，填 App Secret（输入时不显示）。窗口可能藏在别的窗口后面，最多等 5 分钟。」
- 用户说找不到、看不懂：一次只帮一个点（例如「登录后看到什么了？」），按上面 1→3 一步步带；**不要**让用户把 App Secret 截图或发过来，需要核对时只核对 App ID（`cli_` 开头那串）。
- 命令：`<fbk> setup --credentials`（在前台等它结束，最多 5 分钟多一点；如果你的工具对单条命令有更短的时间上限，请用户自己在终端运行）。
- 成功：`✓ 应用凭证：凭证有效，能换到访问令牌，已保存（App ID：…）`。
- 失败怎么转述：
  - `已取消…` → 用户点了取消、没填或超时。问用户「准备好了吗？」，再运行一次。
  - `App ID 或 App Secret 换不到访问令牌` → 原样转述「怎么办」（核对凭证、确认应用已启用）。
  - Windows：`这里弹不出输入窗口…` → 按输出里给的那一行，请用户自己打开 PowerShell 运行并按提示填写，填完告诉你。

### [3/7] 登录授权

- 先对用户说：「浏览器会打开飞书授权页，请用**要备份的那个飞书账号**（备份账号）登录并点『授权』。如果要扫码，需要用备份账号所在的手机扫。」
- 命令：`<fbk> setup --login`（**后台运行**，最多等 10 分钟）。
- 浏览器没自动打开：输出第一行「请在浏览器完成授权：」后面就是链接，把它发给用户，请他复制到浏览器打开。（加了 `--json` 时这行在 stderr。）
- 成功：`✓ 登录：已登录：<姓名>`。把姓名告诉用户，确认是备份账号；不是的话请用户退出飞书网页登录、换备份账号后重跑这一步。
- 可能还有一行 `⚠ 权限范围：登录时有 N 项权限没拿到` → 原样转述「怎么办」（后台批量导入权限 → 创建版本并发布 → 重新运行这一步）。
- 失败怎么转述：
  - `本机端口 8765 被占用` → 请用户关掉占用的程序或重启电脑后重试。
  - `等待授权超时` → 10 分钟内没点授权，问用户准备好后重跑。
  - 提到「重定向 URL」→ 后台「安全设置 → 重定向 URL」要有 `http://127.0.0.1:8765/callback`，见 README。

### [4/7] 权限体检

- 命令：`<fbk> doctor`（要实际调用飞书，可能要一两分钟）。
- 成功：8 项全部 ✓——列我的空间、列知识库、搜索、导出一份文档、下载文件 1 字节、权限判断、列群、读一条群消息。（装过定时任务后还会多一行「定时任务」。）
- 失败怎么转述：
  - 提到「缺少权限」→ 原样转述：去后台补权限，**创建新版本并发布**，再回到 [3/7] 重新登录。只加权限不发版、不重新登录都不会生效。
  - `读一条群消息` ✗ 且提到机器人 → 应用没开机器人能力，见 README「飞书后台准备」第 4 条。
  - `没有可测的样本，跳过` 显示为 ✓ 是正常的（例如账号里没有某类文件）。

### [5/7] 备份目录

- 问用户：**「备份放在哪个文件夹？第一次大约要几十 GB，建议放外接硬盘，或者空间充足的盘，留至少 100 GB。」** 可以给例子：mac `/Volumes/<外接盘名>/飞书备份` 或 `~/飞书备份`；Windows `D:\飞书备份`（放在浅层目录，路径越短越好）。
- 命令：`<fbk> setup --root "<路径>"`
- 输出 `✗ 目录存在：目录不存在` → 问用户「这个文件夹还不存在，要新建吗？」，同意后运行 `<fbk> setup --root "<路径>" --create`。**不要自己用 mkdir 新建。**
- 成功：`✓ 目录存在`、`✓ 可以写入`、`✓ 剩余空间`，最后 `✓ 已保存：备份目录设为 …`。
- 出现 ⚠（`iCloud 同步`、`OneDrive 同步`、`路径长度`）→ 转告用户原因，问要不要换一个目录；用户坚持也可以继续。
- `✗ 可以写入` / `✗ 剩余空间` → 原样转述「怎么办」，换目录后重跑。

### [6/7] 首次扫描（只统计，不下载）

- 命令：`<fbk> scan`（文件多时要几分钟到十几分钟，能后台就后台）。
- 成功：几行统计。用几句话讲给用户：一共多少项、上传的文件多少个多少 GB、在线文档估算多少、合计多少、预计耗时、哪些类型会跳过；提醒群聊附件不在估算里。如果最后有「备份目录核对」且出现 `✗ 剩余空间`，回到 [5/7] 换更大的盘。
- **然后明确问用户：「确认开始首次备份吗？」等用户说可以再进行下一步。** 用户没确认就停在这里。
- 失败：`一项都没读到` → 先 `<fbk> doctor` 看是网络还是权限问题。

### [7/7] 定时任务

- 先问用户：**「默认每周一中午 12 点自动备份（按这台电脑的时间），可以吗？」**
  - 可以 → `<fbk> setup --schedule`
  - 想换时间 → `<fbk> setup --schedule --weekday <1–7> --time <HH:MM>`（1=周一 … 7=周日，24 小时制；例如每周五晚上 8 点：`--weekday 5 --time 20:00`）。
- 成功：`✓ 定时任务：已安装（…）。计划：每周一 12:00（错过会在开机后补跑），本机时区。下次计划：…`，下面一行是卸载方法。把这些用大白话讲给用户：电脑开着时每小时检查一次，到了计划时间才真正备份；那个时间电脑关着或睡着，下次开机/唤醒后自动补；备份进行中电脑不会因为闲置而睡眠。
- 第一次备份（不用开着你，也不用开着终端）：
  - **mac**：`setup --schedule` 一装好，定时任务就**已经在后台开始**第一次备份了，**不要再问「要不要现在开始」**。直接告诉用户「第一次备份已经在后台开始了」。想确认就运行 `<fbk> status`：「最近一次运行」显示「还在跑或被中断」、开始时间就是刚才，说明正在备份。用户想看着它跑，也可以后台运行 `<fbk> run --now`——它会提示「已有备份在跑」并很快结束，这是正常的，进度看 `status`。
  - **Windows**：要等下一个整点或下次登录才会开始。问用户「要不要现在就开始第一次备份？」：要 → 运行 `schtasks /Run /TN feishu-backup`（让定时任务马上跑一次，关掉你和终端也不影响），再用 `<fbk> status` 确认「最近一次运行」的开始时间是刚才。这条命令报错时，改为**后台运行** `<fbk> run --now`；它很快结束并显示「已有备份在跑」，说明定时任务已经开始了。
  - 第一次备份可能要几个小时，甚至跨好几次开机；中途关机、合盖、关掉你都没关系，下次会从断点继续。
- 失败：原样转述「怎么办」。装之前程序会用定时任务将要用的那个 Python 试连一次飞书：提示证书校验失败或连不上时**没有安装**，按「怎么办」修好后重新运行这一步。

**做完对用户说**：「以后不用管它。出问题（需要重新登录、备份盘没插、空间不够、很久没备份成功）会弹系统通知。想看情况就跟我说『飞书备份状态』。」

---

## 六、日常操作

| 用户说 | 运行 | 怎么讲给用户 |
|---|---|---|
| 飞书备份状态 | `<fbk> status` | 上次成功时间、下次计划（「下次计划：每周一 12:00（日期 时间）」）、定时任务是否正常、各类文件已备份/失败/跳过多少、最近备份日志和权限缺口报告的位置。 |
| 现在备份一次 | `<fbk> run --now`（**后台**） | 不受每周时间限制。结束时读「本次结果」和下面的「本轮 … 结束」，按第一节第 10 条带上截至时间，再打开最新的备份日志总结。 |
| 先只备份某个文件夹试试 | 先 `<fbk> scan --only "<目录>"` 看有多少文件，再 `<fbk> run --only "<目录>"`（**后台**） | 路径从「我的空间」或「知识库/空间名」往下写，用 `/` 分隔，开头的「我的空间」可以省略，例如 `产品部/质量文件`。这是一次性试跑：只备份这个目录，不扫描群聊、不生成权限缺口报告、不算一次完整备份，每周的定时备份照常备份全部。输出「✗ 没有找到」时把「怎么办」原样转告。同一个目录可以重复跑，没变的文件不会重新下载。 |
| 看权限缺口 | `<fbk> perms` | 共发现多少链接、无权限几个、已删除几个；要细节就打开输出里的 CSV（`_报告/日期_权限缺口.csv`）读给用户。 |
| 把备份时间改成每周五晚上 8 点 | `<fbk> setup --schedule --weekday 5 --time 20:00` | 讲清新的计划和下次时间。如果上一个计划时间点之后还没备份过，改完不久就会补一次。 |
| 飞书备份重新登录 | `<fbk> login`（**后台**，同 [3/7]） | 提醒用户去浏览器用备份账号点授权。 |
| 飞书备份体检 / 备份有问题 | `<fbk> doctor` | 按 ✓/✗ 转述，✗ 原样给「怎么办」。 |
| 更新飞书备份 | 按第 0 步「取得程序」做：有 `.git` 就 `git -C … pull`；是下载压缩包装的，就把旧的 `feishu-backup` 文件夹改名留着、再按那里的命令重新下载解压（不要直接解压到旧文件夹上）。然后运行 install 之前那条 `fbk.py install`，再 `<fbk> setup --schedule` | 新版程序装好，定时任务也换成新版。 |
| 卸载飞书备份 | `<fbk> schedule uninstall` | 只停了定时任务，备份文件一个没删。还想清掉本机配置的话，按 README「卸载」请用户自己删；不要替用户删备份文件夹。 |

报告在哪：`<备份目录>/_报告/YYYY-MM-DD_HHMM_备份日志.md`（每次运行一份）、`<备份目录>/_报告/YYYY-MM-DD_权限缺口.csv`（Excel 可直接打开）。

---

## 七、退出码与排障

`run` 的退出码（`--json` 时在 `exit_code`）：

| 退出码 | 意思 | 怎么办 |
|---|---|---|
| 0 | 正常结束；或还没到计划时间、已有一次备份在跑、网络不通，安静地跳过了 | 不用处理。用户要求立刻备份就用 `run --now`；想知道是哪种就看 `status`。 |
| 1 | 运行出错 | 看最新备份日志和日志目录（见下）里当天的日志，找出错原因；必要时 `doctor`。 |
| 2 | 还没配置好，或备份目录不存在 | 先 `setup --status` 看缺哪步。目录不存在多半是外接盘没插：请用户插上，定时任务会自动继续。 |
| 3 | 登录过期，需要重新登录 | 后台运行 `login`，请用户去浏览器点授权。电脑超过 7 天没开机（或备份盘超过 7 天没插）登录就会过期，属正常。 |
| 4 | 备份盘空间不足 | 已备份的都保留。请用户清理空间或换更大的盘（换盘后把旧备份目录整个拷过去，再 `setup --root` 指过去），然后 `run --now`。 |

其他命令：退出码 0 = 全部通过；1 = 有 ✗（看「怎么办」）。退出码 2 且输出以 `usage:` 开头 = 命令写错了，对照第二节或 `--help` 改正。

常见情况：

- **通知或日志说「证书校验失败」**（`run` 退出码 1）→ 运行备份的那个 Python 不信任飞书的证书，不是断网。按通知里的修法做，然后重新运行 `<fbk> setup --schedule`（它会先试连一次，通过了才装）。
- **从没成功过、通知说「设置好 N 天了，还没有成功完成过一次」** → `<fbk> status` 看最近一次运行和原因，必要时 `<fbk> doctor`。
- **「连不上飞书」但用户说网络正常** → 多半是你所在 AI 工具的沙箱不让联网，请用户批准联网/在沙箱外运行。
- **登录时「端口 8765 被占用」** → 关掉占用的程序或重启电脑后重试。
- **体检某项 ✗ 提到权限** → 后台补权限 → 创建新版本并发布 → `login`。
- **`status` 或 `doctor` 里「定时任务」显示 ✗**（例如更新程序后、换了 Python 后）→ 运行 `<fbk> setup --schedule`。
- **提示「已有备份进程在运行」** → 定时任务或另一次手动运行正在备份，等它结束即可，不要强行结束。
- **备份日志里有「需手动处理（飞书限制）」一节** → 这些文件是飞书自己不让通过接口拿（群里太大的文件、太大的在线文档），程序没法备份，每轮都会列出来，不是程序漏了。告诉用户：需要的话在飞书客户端里手动下载，或把文档另存为 PDF，放进日志里写的那个目录。
- **用户问「同名文件怎么分得清」/ 看到「名字 (一串字母).pdf」** → 程序按飞书文件的固定编号认文件，不看名字，同名不会混；括号里是编号前 6 位，用来区分同一目录里的同名文件，哪个是哪个看那一轮备份日志里的说明。删掉又上传的同名文件，新的用原名、旧的在 `历史版本/`。
- **失败原因写「飞书限流」** → 飞书一时请求太多，程序已经等待重试过；下轮会自动再试，不用处理。
- **Windows 上输出乱码** → 见第 0 步 Windows 第 3 条。
- **Windows：任务计划程序里「飞书备份」一直显示「正在运行」很久，日志好几个小时没有新内容** → 这个任务同一时间只允许一个在跑，卡住的那个会挡住之后的每小时检查。请用户在「任务计划程序」里右键 `feishu-backup` →「结束」，或运行 `schtasks /End /TN feishu-backup`；之后的整点会正常重新开始，从断点继续。

---

## 八、本机位置

| 用途 | macOS | Windows |
|---|---|---|
| 配置、登录信息（只有本人可读，**不要发给别人**） | `~/.config/feishu-backup/` | `%APPDATA%\feishu-backup\` |
| 运行日志（保留 30 天） | `~/Library/Logs/feishu-backup/` | `%LOCALAPPDATA%\feishu-backup\logs\` |
| 程序副本 + 启动器 `launch.py` | `~/Library/Application Support/feishu-backup/app/` | `%LOCALAPPDATA%\feishu-backup\app\` |
| 定时任务 | `~/Library/LaunchAgents/local.feishu-backup.plist` | 任务计划程序里的 `feishu-backup` |
| 下载的仓库 | `~/feishu-backup` | `%USERPROFILE%\feishu-backup` |

备份目录本身由用户在 [5/7] 选定；里面的 `.feishu-backup/` 是清单数据库，换电脑时随备份目录一起拷走即可续上。
