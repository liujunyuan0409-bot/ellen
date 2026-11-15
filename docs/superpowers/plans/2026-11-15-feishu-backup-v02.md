# 飞书备份 v0.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已发布的 v0.1.0 基础上，把备份改成每周一次（默认周一 12:00，错过补跑），加上 Windows 支持，并让任意 AI 助手都能带不懂技术的人完成安装和第一次备份；顺带修掉 v0.1.0 遗留的两个小问题。

**Architecture:** 核心模块不动。与系统相关的部分抽到新包 `fbk/platform/`：位置、文件锁、通知、密码弹窗、防睡眠、文件名与长路径、定时任务。运行时按 `sys.platform` 选择实现。所有 Windows 实现都写成纯函数或可注入接口，在 macOS 上用单元测试覆盖。说明以 `plugins/feishu-backup/AGENTS.md` 为唯一正本，根目录 `AGENTS.md` 和 `SKILL.md` 只做指向。

**Tech Stack:** Python ≥ 3.9，只用标准库（Windows 额外用 `msvcrt`、`ctypes`、`tkinter`、`winreg`，都是标准库），unittest；macOS launchd / osascript；Windows 任务计划程序（`schtasks /XML`）/ PowerShell。

**Spec:** `docs/superpowers/specs/2026-11-13-feishu-backup-design.md`（v0.2）

## Global Constraints

- 只用 Python 标准库，兼容 3.9（不用 `X | None`、`match`）；测试框架用 unittest。测试命令（仓库根目录执行）：`python3 -m unittest discover -s plugins/feishu-backup/tests -t plugins/feishu-backup`，现在是 263 个 OK。
- 仓库是 **public**：不出现真实 App ID、企业域名、账号名或任何真实数据。测试全部离线，绝不碰真实的 `~/.config/feishu-backup`、`~/Library`、`%APPDATA%`、osascript、launchctl、schtasks、PowerShell 或飞书。
- **旧版永不删除、永不覆盖**：任何改动都不能削弱这条。
- 各系统的位置：

  | | macOS | Windows |
  |---|---|---|
  | 配置 | `~/.config/feishu-backup/` | `%APPDATA%\feishu-backup\` |
  | 日志 | `~/Library/Logs/feishu-backup/` | `%LOCALAPPDATA%\feishu-backup\logs\` |
  | 程序副本 | `~/Library/Application Support/feishu-backup/app/` | `%LOCALAPPDATA%\feishu-backup\app\` |

- 计划时间默认 `weekday=1`（周一，ISO 1–7）、`time="12:00"`，按**本机时区**。定时任务每小时检查一次，登录时也运行一次。
- 上次成功早于「最近一个已过去的计划时间点」→ 该跑。
- 没读全的那次（不算成功）之后，**6 小时**内不再重试。
- 「过期提醒」的条件：上次成功早于「往前数第二个计划时间点」，提醒每天最多一次。
- 备份根目录不存在时，绝不新建（只有 `setup --root --create` 可以新建）。
- 文件名两系统统一：
  - `/ \ : * ? " < > |` 和控制字符替换成 `_`；
  - 去掉结尾的空格和点；
  - Windows 保留名加前缀 `_`：`CON PRN AUX NUL COM1–COM9 LPT1–LPT9`，不区分大小写，带扩展名也算；
  - 单个名字 ≤ 255 字节；
  - 同目录同名的判断不区分大小写。
- Windows 的标注文案固定为「**Windows 版未经真机验证**」。
- 版本号改为 `0.2.0`，涉及 `fbk/__init__.py`、`plugin.json`、`marketplace.json`、CHANGELOG。
- 面向用户的输出一律用中文、大白话。commit 消息末尾空一行，加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。只 commit，不 push。

## Review Focus

1. **计划时间点的边界**：正好周一 12:00:00、12:00 前一秒，以及跨年、跨月的周一，期望 `last_slot` 结果正确，并且周一 12:00 之后最多只跑一次。由 Task 2 的 `test_last_slot_boundaries` 固定。
2. **带扩展名的 Windows 保留名**（如 `con.txt`、`Aux.PDF`、`nul`）和**结尾是点或空格的名字**：期望在两个系统上都清洗成同样合法的名字。由 Task 3 的 `test_windows_reserved_and_trailing` 固定。
3. **只有大小写不同的同目录同名文件**（`Report.pdf` 和 `report.pdf`）：期望第二个加上 ` (token前6位)`，不会在 macOS 或 Windows 上互相覆盖。由 Task 3 的 `test_case_insensitive_collision` 固定。
4. **Windows 上超过 260 字符的路径**：期望所有文件操作都经过长路径转换，不报 `FileNotFoundError`。由 Task 3 的 `test_long_path_prefix_applied_on_windows`（模拟 Windows）固定。
5. **Windows 上 tkinter 不可用**：期望退化为 PowerShell 读取密码，密码不出现在命令行参数里；用户取消时报「已取消」。由 Task 5 的 `test_prompt_falls_back_to_powershell` 固定。

---

## File Structure

```
plugins/feishu-backup/
├── AGENTS.md                         # 新：说明唯一正本（安装→向导→首次备份→日常→排障→各 AI 工具注意事项）
├── WINDOWS_CHECKLIST.md              # 新：Windows 真机验收清单
├── README.md / CHANGELOG.md          # 改
├── .claude-plugin/plugin.json        # 改 version
└── skills/feishu-backup/
    ├── SKILL.md                      # 改：只放触发词 + 指向 AGENTS.md
    └── scripts/
        ├── fbk.py                    # 改：新增 install 子命令、setup --schedule 参数、Windows 标注
        └── fbk/
            ├── __init__.py           # __version__ = "0.2.0"
            ├── config.py             # 改：位置改由 platform.paths 提供；新增 schedule 字段
            ├── names.py              # 改：调用 platform.fsnames
            ├── schedule.py           # 改：保留计划时间计算 + RunLock 外壳；系统相关部分转给 platform
            ├── store.py              # 改：记录根目录 + 修迁移截断（小问题 1）
            ├── sync.py / chats.py / fetcher.py   # 改：文件操作走 platform.fsnames.lp；同名判断不区分大小写
            ├── runner.py / wizard.py / notify.py # 改：调用 platform 实现
            └── platform/
                ├── __init__.py       # IS_WINDOWS / IS_MAC；current()
                ├── paths.py          # 位置
                ├── fsnames.py        # 文件名规则、lp() 长路径、casefold 比较键
                ├── lock.py           # posix flock / windows msvcrt.locking
                ├── notify.py         # osascript / PowerShell 通知
                ├── prompt.py         # osascript / tkinter → PowerShell 回退
                ├── awake.py          # caffeinate 参数 / SetThreadExecutionState
                ├── appcopy.py        # 按版本分目录的程序副本 + current 指针（小问题 2）
                ├── launchd.py        # macOS plist 生成/安装/校验
                └── winsched.py       # Windows 任务 XML 生成/安装/校验
```

---

### Task 1: platform 包骨架 + 位置

**Files:**
- Create: `scripts/fbk/platform/__init__.py`、`scripts/fbk/platform/paths.py`
- Modify: `scripts/fbk/config.py`（`CONFIG_DIR`、`LOG_DIR` 改为取自 paths；保持现有测试里 patch `config.CONFIG_DIR` 的方式仍然有效）、`scripts/fbk/__init__.py`（`__version__ = "0.2.0"`）
- Test: `tests/test_platform_paths.py`

**Interfaces:**
- Produces:
  - `platform.IS_WINDOWS: bool`、`platform.IS_MAC: bool`
  - `paths.config_dir(sysplat: str = sys.platform, env: Mapping = os.environ, home: Path = Path.home()) -> Path`
  - `paths.log_dir(...) -> Path`、`paths.app_root(...) -> Path`（参数同上，按 Global Constraints 的位置表返回；Windows 下 `APPDATA` / `LOCALAPPDATA` 缺失时回退到 `home/"AppData"/"Roaming"` 和 `home/"AppData"/"Local"`）

- [ ] **Step 1: 写失败测试**：mac 和 win 两种 `sysplat` 下三个函数的返回值与表一致；Windows 环境变量缺失时走回退路径。
- [ ] **Step 2: 运行，确认失败** → **Step 3: 实现** → **Step 4: 全量测试通过，仍是 263 个 + 新增的**
- [ ] **Step 5: Commit** `feat(feishu-backup): platform 包与各系统位置`

### Task 2: 每周计划时间、补跑、6 小时重试、过期提醒

**Files:**
- Modify: `scripts/fbk/config.py`（`Config` 新增 `schedule_weekday: int = 1`、`schedule_time: str = "12:00"`；读取旧配置时，缺少这两个字段就用默认值；`interval_hours` 继续读写但不再使用）、`scripts/fbk/schedule.py`、`scripts/fbk/runner.py`、`scripts/fbk/store.py`（新增 `last_run() -> Optional[Row]`，返回最近一次已结束的运行）、`scripts/fbk.py`（`setup --schedule [--weekday N] [--time HH:MM]`；`status` 显示下次计划时间）
- Test: `tests/test_schedule.py`、`tests/test_runner.py`、`tests/test_wizard.py`

**Interfaces:**
- Produces:
  - `schedule.parse_time(s: str) -> Tuple[int, int]`：不合法时抛 `ValueError`，错误信息为中文
  - `schedule.last_slot(now: datetime, weekday: int, hh: int, mm: int) -> datetime`：返回 ≤ now 的最近一个计划时间点，带本机时区（aware）
  - `schedule.next_slot(now, weekday, hh, mm) -> datetime`：返回严格大于 now 的下一个计划时间点
  - `schedule.is_due(last_success: Optional[int], now: int, weekday: int, hh: int, mm: int) -> bool`：`last_success is None` 时返回 True，否则判断 `last_success < last_slot(now).timestamp()`。旧签名 `is_due(last, interval_hours, now)` 删除，所有调用方同步修改
  - `schedule.is_stale(last_success, now, weekday, hh, mm) -> bool`：判断 `last_success < (last_slot(now) - 7 天)`
  - `schedule.RETRY_AFTER_INCOMPLETE = 6 * 3600`
  - runner 的判断顺序：计划性运行（非 `--now`）时，先看 `store.last_run()`：上次运行 ok=0 且没满 6 小时 → 跳过。然后看 `is_due`；过期提醒改用 `is_stale`

- [ ] **Step 1: 写失败测试**

```python
def test_last_slot_boundaries(self):
    tz = datetime.now().astimezone().tzinfo
    mon_noon = datetime(2026, 11, 16, 12, 0, tzinfo=tz)          # 周一
    self.assertEqual(last_slot(mon_noon, 1, 12, 0), mon_noon)
    self.assertEqual(last_slot(mon_noon - timedelta(seconds=1), 1, 12, 0), mon_noon - timedelta(days=7))
    self.assertEqual(last_slot(datetime(2027, 1, 1, 9, 0, tzinfo=tz), 1, 12, 0),
                     datetime(2026, 12, 28, 12, 0, tzinfo=tz))  # 跨年
def test_is_due_once_per_week(self): ...      # 周一 12:05 成功 → 当周再查一律 False；下周一 12:00 起为 True
def test_missed_monday_catches_up(self): ...  # 上次成功在上周一，本周三才开机 → True
def test_incomplete_run_retries_after_6h(self): ...   # 上次运行 ok=0 只过了 5 小时 → 跳过；过了 6 小时 → 运行
def test_stale_after_two_missed_slots(self): ...
def test_parse_time_rejects_bad(self): ...    # "25:00"、"12"、"ab" 都报错
def test_setup_schedule_saves_weekday_time(self): ...  # setup --schedule --weekday 5 --time 20:00 → 配置保存成功，输出里有「每周五 20:00」
def test_old_config_without_schedule_defaults(self): ...
```

- [ ] **Step 2: 运行，确认失败** → **Step 3: 实现**。时区用 `datetime.now().astimezone()` 的本地时区；计算用 aware datetime，避免夏令时出错 → **Step 4: 全量测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 改为每周计划（默认周一 12:00），错过补跑、没读全 6 小时后重试`

### Task 3: 文件名规则、大小写不敏感的同名判断、Windows 长路径

**Files:**
- Create: `scripts/fbk/platform/fsnames.py`
- Modify: `scripts/fbk/names.py`（`sanitize` 调用 `fsnames.clean_component`）、`scripts/fbk/sync.py`（`_choose_path` 和占用判断改用 casefold 键）、`scripts/fbk/fetcher.py`、`scripts/fbk/chats.py`、`scripts/fbk/sync.py`，以及 `store.py` 里打开、改名、建目录、删除文件的地方，统一经过 `lp()`
- Test: `tests/test_fsnames.py`、`tests/test_sync.py`

**Interfaces:**
- Produces:
  - `fsnames.RESERVED = {"CON","PRN","AUX","NUL", *("COM%d"%i for i in range(1,10)), *("LPT%d"%i for i in range(1,10))}`
  - `fsnames.clean_component(name: str) -> str`：按 Global Constraints 的文件名规则处理；结果为空或只剩点号时返回 `"_"`
  - `fsnames.fold(name: str) -> str`：返回 `unicodedata.normalize("NFC", name).casefold()`，作为比较键
  - `fsnames.lp(p: Union[str, Path], is_windows: bool = IS_WINDOWS) -> str`：Windows 上把绝对路径转成 `\\?\C:\...`（UNC 路径转成 `\\?\UNC\server\share\...`），已带前缀的保持不变；非 Windows 原样返回 `str(p)`
- `unique_path` 和 `sync._choose_path` 判断「已存在 / 被占用」时，比较同目录下所有条目的 `fold()`。如果被占用的正好是同一个 token 自己，仍视为可用（保持现有行为）。

- [ ] **Step 1: 写失败测试**

```python
def test_windows_reserved_and_trailing(self):
    self.assertEqual(clean_component("con.txt"), "_con.txt")
    self.assertEqual(clean_component("Aux.PDF"), "_Aux.PDF")
    self.assertEqual(clean_component("nul"), "_nul")
    self.assertEqual(clean_component("报告. "), "报告")
    self.assertEqual(clean_component("a:b*c?.pdf"), "a_b_c_.pdf")
def test_case_insensitive_collision(self):   # 同目录先放 Report.pdf（token A），再同步 report.pdf（token B）
    # → B 落到 "report (tokBBB).pdf"，A 的文件内容不变
def test_long_path_prefix_applied_on_windows(self):
    self.assertEqual(lp(r"C:\a\b", is_windows=True), "\\\\?\\C:\\a\\b")
    self.assertEqual(lp(r"\\srv\share\x", is_windows=True), "\\\\?\\UNC\\srv\\share\\x")
    self.assertEqual(lp("/tmp/x", is_windows=False), "/tmp/x")
```

- [ ] **Step 2: 运行，确认失败** → **Step 3: 实现**。改造完成后，在 sync / chats / fetcher / store 里 grep `os.replace|open(|mkdir|unlink|rename|shutil`，确认每一处都经过了 `lp()` → **Step 4: 全量测试通过**（v0.1.0 的同步、去重测试必须全部保持通过）
- [ ] **Step 5: Commit** `feat(feishu-backup): 两系统统一文件名规则、同名不区分大小写、Windows 长路径`

### Task 4: 文件锁、防睡眠、SIGTERM（跨平台）

**Files:**
- Create: `scripts/fbk/platform/lock.py`、`scripts/fbk/platform/awake.py`
- Modify: `scripts/fbk/schedule.py`（`RunLock` 改用 `platform.lock`）、`scripts/fbk/runner.py`（运行期间用 `awake.keep_awake()`；`install_term_handler` 在 Windows 上同时处理 `SIGBREAK`，如果该信号存在）
- Test: `tests/test_platform_lock.py`

**Interfaces:**
- Produces:
  - `lock.try_lock(fd: int) -> bool`、`lock.unlock(fd: int) -> None`：posix 用 `fcntl.flock(LOCK_EX|LOCK_NB)`，Windows 用 `msvcrt.locking(fd, LK_NBLCK, 1)`。按平台选择实现，同时提供 `_posix` / `_windows` 两个可单独测试的函数，Windows 版通过注入假的 msvcrt 来测
  - `awake.keep_awake() -> ContextManager`：Windows 上进入时调用 `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`，退出时恢复为 `ES_CONTINUOUS`；mac 上什么都不做（mac 由 caffeinate 包住整个进程）。`ctypes` 可注入

- [ ] **Step 1: 写失败测试**：现有 RunLock 测试全部保持通过；Windows 实现用假 msvcrt，验证调用了 `LK_NBLCK`、被占用时抛 `AlreadyRunning`；`keep_awake` 在 Windows 分支上用假 kernel32，验证进入和退出时传的标志。
- [ ] **Step 2–4: 先跑失败 → 实现 → 全量测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 跨平台运行锁与防睡眠`

### Task 5: 通知与密码弹窗（跨平台）

**Files:**
- Create: `scripts/fbk/platform/notify.py`、`scripts/fbk/platform/prompt.py`
- Modify: `scripts/fbk/notify.py`（节流逻辑保留，真正发通知改为调用 `platform.notify.send`）、`scripts/fbk/wizard.py`（`_dialog` 和 `prompt_secret` 改为调用 `platform.prompt.ask`）
- Test: `tests/test_platform_notify.py`、`tests/test_platform_prompt.py`

**Interfaces:**
- Produces:
  - `platform.notify.send(title: str, message: str, runner=subprocess.run, is_windows=IS_WINDOWS) -> bool`
    - mac：沿用现有 osascript 写法
    - Windows：`powershell -NoProfile -NonInteractive -Command <脚本>`，用 `[Windows.UI.Notifications.ToastNotificationManager]` 弹 ToastText02，AppId 用 `"Microsoft.Windows.Explorer"` 一类系统已注册的 ID。标题和内容通过环境变量 `FBK_TITLE` / `FBK_MSG` 传入，不拼进脚本字符串，避免注入
    - 任何失败都返回 False，不抛异常
  - `platform.prompt.ask(prompt: str, hidden: bool, runner=subprocess.run, is_windows=IS_WINDOWS, tk_factory=None) -> str`
    - mac：沿用现有 osascript 写法（`giving up after 300`）
    - Windows：先用 tkinter 的 `simpledialog.askstring(show="*" if hidden else None)`，窗口置顶，5 分钟超时用 `after` 实现；`import tkinter` 失败或没有 GUI 时，回退到 PowerShell `Read-Host -AsSecureString`，转成明文后从 stdout 读回
    - 取消、超时、空输入都抛 `wizard.Cancelled`（或 platform 里的同名异常，wizard 重新导出），中文信息不变
    - 密码永远不出现在命令行参数里

- [ ] **Step 1: 写失败测试**：Windows 通知的命令行里不含标题和内容文本（它们只在 env 里）；`test_prompt_falls_back_to_powershell`（`tk_factory` 抛 ImportError → 调用 PowerShell，argv 里没有密码，返回 stdout 去掉首尾空白后的内容）；取消（非 0 退出）抛 Cancelled；mac 原有测试全部保持通过。
- [ ] **Step 2–4: 先跑失败 → 实现 → 全量测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 跨平台通知与密码弹窗`

### Task 6: 程序副本按版本存放（修小问题 2）+ 两系统定时任务 + install 子命令

**Files:**
- Create: `scripts/fbk/platform/appcopy.py`、`scripts/fbk/platform/launchd.py`、`scripts/fbk/platform/winsched.py`
- Modify: `scripts/fbk/schedule.py`（`install` / `uninstall` / `verify` 改为按平台分派；原有 mac 实现迁到 `launchd.py`）、`scripts/fbk.py`（新增 `install` 子命令：复制程序副本并打印位置，不装定时任务；`setup --schedule` 先复制副本再装定时任务）
- Test: `tests/test_appcopy.py`、`tests/test_schedule.py`、`tests/test_winsched.py`

**Interfaces:**
- Produces:
  - `appcopy.install(src_dir: Path, app_root: Path, version: str) -> Path`
    - 复制到 `app_root/"versions"/version/`：先写临时目录，再改名；同版本已存在时先放到旁边，替换成功后再删掉
    - 然后原子写 `app_root/"current"`（文本文件，内容为版本号；写法是临时文件 + `os.replace`）
    - 返回启动器路径 `app_root/"launch.py"`。这个启动器自己也是用临时文件 + 替换写入的。它读取 `current`，把 `versions/<v>/` 插到 `sys.path` 最前面，再执行 `fbk.py` 的 `main()`
    - 旧版本目录要等「current 已不指向它，且它的 `in_use` 锁文件没被占用」才清理：启动器运行期间对 `versions/<v>/in_use` 持有 `platform.lock`；清理时能拿到锁才删
  - `appcopy.verify(app_root: Path, expected_version: str) -> Optional[str]`：一切正常返回 None，否则返回中文的问题描述
  - `launchd.plist_xml(python: str, launcher: str) -> bytes`：参数列表为 `[CAFFEINATE, "-i", python, launcher, "run"]`，`RunAtLoad` 为 True，`StartInterval` 为 3600
  - `winsched.task_xml(pythonw: str, launcher: str, user: str) -> str`：Task Scheduler 1.2 格式，UTF-16 声明，内容包括：
    - 触发器：`LogonTrigger`（指定 user）+ `TimeTrigger`，带 `Repetition` 的 `Interval`=PT1H、`Duration` 留空表示无限
    - `Settings`：`StartWhenAvailable`=true、`DisallowStartIfOnBatteries`=false、`StopIfGoingOnBatteries`=false、`MultipleInstancesPolicy`=IgnoreNew、`ExecutionTimeLimit`=PT0S
    - `Principal`：`LogonType`=InteractiveToken、`RunLevel`=LeastPrivilege
    - `Actions`：`Exec`，Command 为 pythonw，Arguments 为 `"<launcher>" run`
  - `winsched.install(xml, name="feishu-backup", runner=subprocess.run, tmpdir=None)`：把 xml 以 UTF-16 写到临时文件，执行 `schtasks /Create /TN feishu-backup /XML <file> /F`。另有 `uninstall`（`schtasks /Delete /TN feishu-backup /F`）和 `verify`（`schtasks /Query /TN feishu-backup /XML`，解析出 Command 和 Arguments，检查路径是否存在、appcopy.verify 是否通过）
  - `schedule.pick_python()`：mac 保持现有逻辑；Windows 取 `sys.executable` 同目录下的 `pythonw.exe`，没有就用 `sys.executable`
- 安装的时间提示改为「每周 X HH:MM（错过会在开机后补跑）」

- [ ] **Step 1: 写失败测试**：
  - 副本按版本存放，`current` 正确；装新版本时旧版本的 `in_use` 被占用 → 不删，释放后下次清理掉
  - 启动器能把 `current` 指向的版本导入进来（在临时目录里放一个假的 `fbk.py` 验证）
  - `plist_xml` 能解析回来，参数正确
  - `task_xml` 用 `xml.etree` 解析后，各字段与上面一致
  - `winsched.install` 调用的 schtasks 参数正确，xml 文件是 UTF-16（runner 注入）
  - `verify` 能报出路径缺失和版本不一致
  - `fbk.py install --json` 输出副本路径
- [ ] **Step 2–4: 先跑失败 → 实现 → 全量测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 程序副本按版本存放，新增 Windows 定时任务与 install 命令`

### Task 7: 修小问题 1（迁移截断）+ 向导的跨平台检查 + Windows 标注

**Files:**
- Modify: `scripts/fbk/store.py`、`scripts/fbk/wizard.py`、`scripts/fbk.py`
- Test: `tests/test_store.py`、`tests/test_wizard.py`

**Interfaces / 决定:**
- store：新增 `meta(key TEXT PK, value TEXT)` 表，每次打开时写入 `root=<当前根目录的绝对路径>`。迁移绝对路径时，先用 meta 里记录的旧根目录做精确前缀截取；没有记录时，依次在每个「我的空间 / 知识库 / 共享给我 / 群聊附件」出现的位置截断，选第一个能在当前根目录下找到文件的结果；都找不到时，用最后一次出现的位置截断。`user_version` 改为 3。
- wizard：
  - `check_env` 增加证书检查：对 open.feishu.cn 做一次 TLS 握手。出现 `ssl.SSLCertVerificationError` 时，fix 文案按平台给出：mac 是「运行 /Applications/Python 3.x/Install Certificates.command」；Windows 是「更新 Python 或检查公司代理证书」
  - `check_root` 增加云同步检测：Windows 上路径位于 `%OneDrive%`（环境变量 `OneDrive` / `OneDriveCommercial`）下时给警告；mac 保留现有 iCloud 检测
  - Windows 上路径超过 100 个字符时提示「建议放在浅层目录，例如 D:\飞书备份」（只警告，不判失败）
- Windows 上运行 `doctor` 和 `status` 时，第一行输出「⚠ Windows 版未经真机验证，遇到问题请对照 WINDOWS_CHECKLIST.md」；`--json` 输出加字段 `"windows_unverified": true`

- [ ] **Step 1: 写失败测试**：旧根目录路径里含「知识库」时，迁移结果正确（meta 有记录、没记录两种情况）；证书错误时给出对应 fix；OneDrive 警告；Windows 下 doctor 首行标注（模拟平台）。
- [ ] **Step 2–4: 先跑失败 → 实现 → 全量测试通过**
- [ ] **Step 5: Commit** `fix(feishu-backup): 迁移按记录的旧根目录截取；向导增加证书、OneDrive 检查与 Windows 标注`

### Task 8: 说明文档（任意 AI 可用）+ 版本登记 + 真机冒烟（Mac）

**Files:**
- Create: 仓库根目录 `AGENTS.md`、`plugins/feishu-backup/AGENTS.md`、`plugins/feishu-backup/WINDOWS_CHECKLIST.md`
- Modify: `plugins/feishu-backup/skills/feishu-backup/SKILL.md`、`plugins/feishu-backup/README.md`、`CHANGELOG.md`、`.claude-plugin/plugin.json`、仓库根目录 `.claude-plugin/marketplace.json`、根目录 `README.md`

**内容要求：**
- 根目录 `AGENTS.md`：只写本仓库有哪些插件、各自的说明在哪；用户要装飞书备份时，读 `plugins/feishu-backup/AGENTS.md`。
- `plugins/feishu-backup/AGENTS.md`（唯一正本），严格对应设计文档 §5.1：
  - 第 0 步准备，分 mac 和 Windows：检查 Python、安装 Python（`xcode-select --install` / `winget install Python.Python.3.12`）、clone 到固定位置（没有 git 时下载 zip）、`install`
  - 7 步向导，每步写清命令、怎么判断成功、出错时怎么转述
  - 「写给 AI 的注意事项」：沙箱和联网（Codex 等）、后台运行、弹窗等待、绝不经手 App Secret、一次只问一个问题、用大白话
  - 日常操作：状态、立即备份、看缺口、改计划时间（`setup --schedule --weekday N --time HH:MM`）
  - 退出码排障表
  - Windows 段落开头写明「Windows 版未经真机验证」
  - 所有命令用 `python3`（mac）/ `py -3` 或 `python`（Windows）调用安装位置里的 `launch.py`；首次 install 之前调用仓库里的 `fbk.py`
- `SKILL.md`：保留 frontmatter 和触发词，正文只写「按 plugins/feishu-backup/AGENTS.md 执行」和它的相对路径，不重复步骤。
- README：
  - 支持 mac 和 Windows（Windows 标注未经真机验证）
  - 每周一次的说明
  - 飞书后台准备，与现有内容一致，权限 JSON 由 `auth.SCOPES` 生成
  - 「授权是累计的」：如何撤销授权后重新登录，得到真正只读的令牌
  - 换电脑、卸载（两个系统分别写）、局限
  - 多人、多台电脑：每台电脑各自登录一次，不共用 `token.json`；某台提示需要重新登录时，再登录一次即可
- `WINDOWS_CHECKLIST.md`：真机逐条验证，每条有操作和预期结果：
  - 安装 Python
  - 向导 7 步
  - 定时任务出现在任务计划程序里
  - 改系统时间（或用 `--now`）后能运行
  - 通知能弹出
  - 长路径文件能落盘
  - `CON.txt` 等文件名会被正确改名
  - 合盖、关机后补跑
  - 卸载
- 版本 0.2.0 登记到 plugin.json、marketplace.json、CHANGELOG（列出 v0.2 的全部变化）。

- [ ] **Step 1: 全量测试 PASS**
- [ ] **Step 2: 安全检查**：`git grep -nE 'cli_[0-9a-f]{16}|rsillk|福娃|萨福|SAPPHO'` 输出为空
- [ ] **Step 3: 说明可执行性自检**：逐条对照 AGENTS.md 里出现的每个命令和参数，与 `fbk.py <cmd> --help` 一致
- [ ] **Step 4: Mac 真账号冒烟（不进仓）**：
  - `install`，确认副本位于 `~/Library/Application Support/feishu-backup/app/versions/0.2.0/`
  - 用启动器 `launch.py` 跑 `doctor`，8 项全部 ✓
  - 备份根目录设为 scratchpad 下的临时目录，跑 `run --now --limit 10`：10 个 NEW；再跑一次：全部 UNCHANGED
  - `status` 显示「下次：每周一 12:00 …」
  - **不安装定时任务**
  - 完成后删除临时目录和程序副本目录，把 config 的 backup_root 恢复为 null
- [ ] **Step 5: Commit**（不 push）`feat(feishu-backup): v0.2.0 每周备份、Windows 支持（未经真机验证）、通用 AI 安装说明`
