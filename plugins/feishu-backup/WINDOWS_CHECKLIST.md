# Windows 真机验收清单（v0.2.0）

**Windows 版未经真机验证。** 有 Windows 电脑后按本清单逐条做一遍，每条记下「通过 / 不通过 + 实际看到的」。全部通过后，去掉 README、AGENTS.md、CHANGELOG 和程序里的「Windows 版未经真机验证」标注（程序里是 `fbk.py` 的 `WINDOWS_NOTICE`）。

约定：

- 在 **PowerShell** 里操作。`<fbk>` 指 `py -3 "$env:LOCALAPPDATA\feishu-backup\app\launch.py"`（install 之后）；`<repo-fbk>` 指 `py -3 "$env:USERPROFILE\feishu-backup\plugins\feishu-backup\skills\feishu-backup\scripts\fbk.py"`。
- 日志在 `%LOCALAPPDATA%\feishu-backup\logs\`，可以用 `Get-Content (Get-ChildItem "$env:LOCALAPPDATA\feishu-backup\logs" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName -Tail 30` 看最新一份的最后 30 行。
- 第 1–3 节最好由一个 AI 助手按 [AGENTS.md](AGENTS.md) 带着做（顺便验证说明本身）；其余各节手工做。
- 涉及在飞书里建测试文件的条目，用备份账号在飞书网页上自己建、用完删掉；程序本身对飞书只读。

---

## 1. 安装 Python 与程序

| # | 操作 | 预期结果 |
|---|---|---|
| 1.1 | 在没有 Python 的 Windows 10/11 上运行 `py -3 --version` 和 `python --version` | `py` 找不到；`python` 提示去 Microsoft Store（系统占位程序） |
| 1.2 | `winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements`，关掉并重开 PowerShell | `py -3 --version` 输出 `Python 3.12.x` |
| 1.3 | `py -3 -c "import tkinter; tkinter.Tk().destroy(); print('ok')"` | 输出 `ok`（winget 装的 Python 带 tkinter） |
| 1.4 | 有 git：`git clone https://github.com/liujunyuan0409-bot/ellen.git "$env:USERPROFILE\feishu-backup"`；没有 git：按 AGENTS.md 用 `Invoke-WebRequest` + `Expand-Archive` | `%USERPROFILE%\feishu-backup\plugins\feishu-backup\` 存在 |
| 1.5 | `<repo-fbk> install --json` | 退出码 0，`"ok": true`；`%LOCALAPPDATA%\feishu-backup\app\` 下有 `launch.py`、`current`（内容 `0.2.0`）、`versions\0.2.0\fbk.py` |
| 1.6 | 让 AI 工具（输出走管道）运行 `<fbk> setup --status`；再在 PowerShell 窗口里直接运行一次 | 两处都正常显示中文和 ✓ / – 符号，不报 `UnicodeEncodeError`，不乱码 |

## 2. 向导 7 步

| # | 操作 | 预期结果 |
|---|---|---|
| 2.1 | `<fbk> setup --env` | 三项 ✓：Python 版本、网络、证书 |
| 2.2 | 拔网线 / 断 Wi-Fi 后再跑 2.1 | `✗ 网络：连不上飞书`，「怎么办：检查网络后重试」；恢复网络 |
| 2.3 | `<fbk> setup --credentials`，先填 App ID，再填 App Secret | 两次都弹出 tkinter 小窗口，**出现在最前面**（不被 PowerShell / 浏览器挡住），光标已在输入框；App Secret 输入显示为 `*`；完成后 `✓ 应用凭证：凭证有效…已保存` |
| 2.4 | 再跑 2.3，弹窗出现后什么都不做，等 5 分钟 | 窗口自动关闭，输出「已取消…」，退出码 1，原有凭证不变 |
| 2.5 | PowerShell 回退：在 PowerShell 窗口里运行 `py -3 -c "import sys,runpy; sys.modules['tkinter']=None; sys.argv=['launch.py','setup','--credentials']; runpy.run_path(r'$env:LOCALAPPDATA\feishu-backup\app\launch.py', run_name='__main__')"` | 不弹小窗口，改在当前窗口里**看得见的提示语**下要求输入（先 App ID，再 App Secret，后者输入不显示）；填完 `✓ 应用凭证` 已保存。提示语没有混进保存的值（之后 2.7 体检能通过即证明） |
| 2.6 | `<fbk> setup --login` | 浏览器打开飞书授权页；用备份账号授权后，输出 `✓ 登录：已登录：<姓名>`；期间 Windows 防火墙若弹窗询问 Python 联网，记下来（只监听 127.0.0.1，应可点「取消」不影响） |
| 2.7 | `<fbk> doctor` | 第一行是「⚠ Windows 版未经真机验证…」，下面 8 项全部 ✓ |
| 2.8 | `<fbk> setup --root "D:\飞书备份测试"`（目录不存在） | `✗ 目录存在：目录不存在`，**没有新建任何目录** |
| 2.9 | `<fbk> setup --root "D:\飞书备份测试" --create` | 目录被新建；✓ 目录存在 / 可以写入 / 剩余空间 / 已保存 |
| 2.10 | `<fbk> setup --root "$env:OneDrive\飞书备份测试" --create`（有 OneDrive 时） | 出现 `⚠ OneDrive 同步` 提醒；之后改回 2.9 的目录 |
| 2.11 | `<fbk> scan` | 只统计不下载：文件数、体积、预计耗时；备份目录里没有新文件 |
| 2.12 | `<fbk> setup --schedule` | `✓ 定时任务：已安装（任务计划程序里的「feishu-backup」）。计划：每周一 12:00…下次计划：…`，下一行给出卸载命令 |
| 2.13 | `<fbk> status` | 第一行是 Windows 未验证标注；有「下次计划：每周一 12:00（日期 时间）」和「定时任务：已安装」 |

## 3. 定时任务

| # | 操作 | 预期结果 |
|---|---|---|
| 3.1 | 打开「任务计划程序」→ 任务计划程序库 | 有 `feishu-backup`；触发器两条：「登录时」和「安装当天 0:00 起，每隔 1 小时无限期重复」（所以在每个整点触发）；操作是 `pythonw.exe "...\app\launch.py" run`；「只在用户登录时运行」；条件里「只有在计算机使用交流电源时才启动」**未勾选** |
| 3.2 | `schtasks /Query /TN feishu-backup /XML` | 能导出 XML（说明 `schtasks /Create /XML` 接受了程序生成的任务 XML）；`MultipleInstancesPolicy` 为 `IgnoreNew`，`StartWhenAvailable` 为 `true` |
| 3.3 | `schtasks /Run /TN feishu-backup`（还没成功备份过） | 不弹黑色命令行窗口；日志里出现开始备份的记录；任务状态「正在运行」；备份目录开始出现文件 |
| 3.4 | 3.3 运行期间运行 `powercfg /requests` | `SYSTEM` 一栏里有 python / pythonw（防睡眠生效） |
| 3.5 | 3.3 跑完后再 `schtasks /Run /TN feishu-backup` | 很快结束；日志写「本周期已经成功备份过，跳过」 |
| 3.6 | 改计划时间后能运行：`<fbk> setup --schedule --weekday <今天是周几> --time <比现在早 5 分钟>`，然后 `schtasks /Run /TN feishu-backup`（或等到下一个整点） | 开始一次新的备份（上次成功早于新的计划时间点）；完成后再 `/Run` 一次则跳过。也可以直接 `<fbk> run --now`：不看计划时间立即备份 |
| 3.7 | 改系统时间：把系统时间调到下周一 12:05（需要管理员），等下一个整点或 `schtasks /Run` | 开始备份；之后把时间改回自动同步。若登录在这期间过期（时间跳过 7 天），按提示重新登录属正常 |
| 3.8 | 合盖 / 睡眠补跑：把计划设成 10 分钟后，立刻让电脑睡眠，等计划时间过了 20 分钟再唤醒 | 唤醒后一小时内（StartWhenAvailable 补跑错过的整点）日志出现开始备份 |
| 3.9 | 关机补跑：把计划设成 10 分钟后，关机，过了计划时间再开机登录 | 登录后几分钟内（「登录时」触发器）日志出现开始备份 |
| 3.10 | 卡住的运行会挡住之后的触发：`schtasks /Run` 开始一次长备份，运行中用任务管理器把 pythonw 的网络断掉或让它卡住（例如拔网线时它正在下载大文件）；观察下一个整点 | 因为 `IgnoreNew`，下一个整点**不会**再起新的运行（任务计划程序历史里记「已忽略」）。恢复办法：任务计划程序里右键 `feishu-backup` →「结束」，或 `schtasks /End /TN feishu-backup`；下一次触发能正常开始，从断点继续，不提示「已有备份进程在运行」 |
| 3.11 | 电池供电：拔掉电源后 `schtasks /Run /TN feishu-backup` | 照常运行（`DisallowStartIfOnBatteries=false`），拔电源时不会被停掉 |

## 4. 通知

| # | 操作 | 预期结果 |
|---|---|---|
| 4.1 | `py -3 -c "import sys; sys.path.insert(0, r'$env:LOCALAPPDATA\feishu-backup\app\versions\0.2.0'); from fbk.platform import notify; print(notify.send('飞书备份', '这是一条测试通知'))"` | 右下角弹出 Windows 通知，标题「飞书备份」、内容是中文；命令输出 `True`；不闪黑窗口 |
| 4.2 | 把备份目录设在 U 盘上，拔掉 U 盘，`schtasks /Run /TN feishu-backup` | 弹通知「备份目录不存在：…插上备份盘后会自动继续」；U 盘那个盘符下**没有**被新建任何目录；同一天再 `/Run` 不再重复弹 |
| 4.3 | 系统「专注助手 / 勿扰」打开时重复 4.1 | 不弹也不报错（通知中心里能看到即可） |

## 5. 文件名与路径

| # | 操作 | 预期结果 |
|---|---|---|
| 5.1 | 在飞书「我的空间」上传名为 `CON.txt`、`aux.docx`、`com1`、`LPT9.xlsx` 的文件，备份一次 | 本地分别是 `_CON.txt`、`_aux.docx`、`_com1`、`_LPT9.xlsx`，都能用资源管理器打开 |
| 5.2 | 上传名字里带 `a:b?c*.txt`、结尾有空格或点（`报告. `）的文件，备份一次 | 本地是 `a_b_c_.txt`、`报告`；备份日志里没有失败 |
| 5.3 | 长路径文件：在飞书里建一串很深的文件夹（每层名字 30 个字左右，建 8–10 层），最里面放一个文件，备份一次 | 文件成功落盘（完整路径超过 260 个字符）；`Test-Path -LiteralPath "\\?\<完整路径>"` 为 True；再跑一次是「未变化」；在飞书上改一下再备份，旧版进了 `历史版本\` |
| 5.4 | 很深的备份根目录：`<fbk> setup --root "D:\<一串嵌套文件夹，整个路径约 220 个字符>" --create`，然后 `<fbk> run --now` | 向导出现 `⚠ 路径长度` 提醒；如果仍然能备份，确认 `.feishu-backup\manifest.sqlite` 已建立、再跑一次是「未变化」。清单数据库的路径没加 `\\?\` 前缀，若这里报错（打不开备份清单），记下报错原文——这是已知风险，届时建议改成浅层目录 |
| 5.5 | 大小写只差的两个文件夹：在飞书同一层建 `Report` 和 `report` 两个文件夹，各放一个**同名**文件 `a.txt`（内容不同）和一个不同名文件，备份一次 | 本地只有一个文件夹（NTFS 不区分大小写，两者合并）；里面有两个 `a.txt` 的版本——一个叫 `a.txt`，另一个带 ` (xxxxxx)` 标识；**没有任何文件被覆盖**（两份内容都在），两个不同名文件也都在 |
| 5.6 | 同一文件夹里上传 `Note.txt` 和 `note.txt` | 本地一个是 `Note.txt`，另一个带 ` (xxxxxx)` 标识，内容各自正确 |

## 6. 程序更新与旧版本清理

| # | 操作 | 预期结果 |
|---|---|---|
| 6.1 | 复制一份仓库到别处，把其中 `skills\feishu-backup\scripts\fbk\__init__.py` 的版本改成 `0.2.1-test`；`schtasks /Run` 开始一次长备份，**运行期间**用这份复制的 `fbk.py` 跑 `install` | `versions\` 下同时有 `0.2.0` 和 `0.2.1-test`，`current` 是 `0.2.1-test`；正在运行的 `0.2.0` **没有被删**，备份不中断 |
| 6.2 | 6.1 的备份结束后，再用复制的那份跑一次 `install` | `versions\0.2.0` 被清理掉 |
| 6.3 | 半删的旧版本：如果 6.2 或任何一次清理后 `versions\` 里留下了一个不完整的旧版本目录（Windows 上清理时可能和新启动的进程撞上，删到一半失败） | 下一次 `install` 会把它删干净；`current` 指向的版本始终完整，`<fbk> doctor` 正常 |
| 6.4 | 收尾：用正式仓库（0.2.0）再跑一次 `install`，然后 `<fbk> setup --schedule` | `current` 回到 `0.2.0`，`0.2.1-test` 被清理；`<fbk> status` 里「定时任务：已安装」，没有 ✗ |

## 7. 卸载

| # | 操作 | 预期结果 |
|---|---|---|
| 7.1 | `<fbk> schedule uninstall` | 输出「已卸载定时任务。备份文件和清单都保留…」；任务计划程序里没有 `feishu-backup` 了 |
| 7.2 | 再运行一次 7.1 | 不报错 |
| 7.3 | 按 README「卸载 → Windows」删除 `%APPDATA%\feishu-backup`、`%LOCALAPPDATA%\feishu-backup`、`%USERPROFILE%\feishu-backup` | 都能删掉（没有进程占用）；备份目录原样保留 |

## 8. 其他

| # | 操作 | 预期结果 |
|---|---|---|
| 8.1 | 查看 `%APPDATA%\feishu-backup\token.json` 的「属性 → 安全」 | 只有本人、SYSTEM、Administrators 能访问（用户目录默认权限） |
| 8.2 | 换一个 AI 工具（例如 Codex CLI）从零按 AGENTS.md 走一遍 1–2 节 | AI 能独立完成；遇到沙箱/联网限制时会先请用户放开权限，而不是报告「飞书故障」；全程没有向用户索要 App Secret |
