# 飞书文件自动备份 skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做一个 Claude Code 插件 `feishu-backup`。它在 Mac 上定期把备份账号能看到的飞书文件增量备份到本地，旧版本按日期归档，并输出权限缺口报告。

**Architecture:** 一个只用 Python 标准库的包 `fbk`，由 `fbk.py` 子命令驱动，由 macOS LaunchAgent 每小时唤醒一次，程序自己判断是否该跑。所有飞书调用都经过 `FeishuClient`，它内置只读白名单。本地状态存在备份目录内的 SQLite 清单里。SKILL.md 负责让 Claude 带用户走配置向导：先逐个问用户，再用命令行参数调用脚本，因为脚本不能在 Claude 的 Bash 里做交互式输入。

**Tech Stack:** Python ≥ 3.9（只用标准库：urllib、sqlite3、http.server、hashlib、plistlib、unittest），macOS launchd、osascript，飞书 OpenAPI（open.feishu.cn）。

**Spec:** `docs/superpowers/specs/2026-11-13-feishu-backup-design.md`

## Global Constraints

- 插件目录 `plugins/feishu-backup/`；代码在 `plugins/feishu-backup/skills/feishu-backup/scripts/`，测试在 `plugins/feishu-backup/tests/`。
- **只用 Python 标准库**，不引入任何第三方包。测试框架用 `unittest`。
- 仓库是 **public**：App ID、App Secret、token、企业域名、任何备份数据或清单都不能进仓。测试数据全部虚构。
- 配置目录 `~/.config/feishu-backup/`：`config.json`、`token.json` 权限 0600，目录 0700。
- 日志目录 `~/Library/Logs/feishu-backup/`，保留 30 天。
- 备份根目录下固定子目录：`我的空间/`、`知识库/`、`共享给我/`、`群聊附件/`、`_报告/`、`.feishu-backup/`（含 `manifest.sqlite`、`run.lock`）。
- 历史版本：同目录 `历史版本/原名_YYYY-MM-DD_HHMM.扩展名`。时间取旧版的飞书修改时间，按 Asia/Shanghai 时区。重名追加 `-2`、`-3`。
- 格式：docx/doc/slides → `.pdf`；sheet/bitable → `.xlsx`；file 和群附件 → 原格式；其他类型 → 跳过。
- 群附件：只收 `file` / `audio` / `media` 三类消息，**不收 image**，存到 `群聊附件/<群名>/YYYY-MM-DD_原文件名`。
- 去重优先级：我的空间 > 知识库 > 共享给我；附件和上传文件按 SHA-256 去重。
- 默认值：`interval_hours=24`、`lookback_days=180`、LaunchAgent `StartInterval=3600` + `RunAtLoad`、请求 5 次/秒、导出并发 ≤3、下载并发 ≤4、429/5xx 重试 5 次（指数退避）、磁盘余量 2 GiB、token 提前 300 秒续期。
- OAuth 回调 `http://127.0.0.1:8765/callback`。
- **任何情况下都不删除、不覆盖本地已有的版本文件。**
- 面向用户的输出（终端、报告、通知）一律用中文。

## Review Focus

1. **飞书侧文件夹本身就叫「历史版本」**：期望行为是它照常作为普通目录备份，里面的文件不会被当成本地归档。由 Task 6 的测试 `test_remote_folder_named_history_is_normal_dir` 固定。
2. **文件名含 `/`、换行或超长中文名（超过 255 字节）、同目录同名**：期望清洗后能落盘，扩展名保留，同名文件互不覆盖。由 Task 1 的测试固定。
3. **下载中途合盖或断网，留下 `.part` 文件**：期望下次运行先清理 `.part`，再重新下载这个文件，清单不会把它记成已完成。由 Task 5 的 `test_part_file_not_committed_on_error` 和 Task 8 的 `test_run_cleans_stale_parts` 固定。
4. **外接备份盘没插，备份根目录不存在**：期望程序不在系统盘上偷偷新建同名目录，而是退出并发通知（每天最多一次）。由 Task 8 的 `test_missing_root_does_not_create` 固定。
5. **长期没开机，refresh token 失效**：期望程序停止运行、发出「需要重新登录」的通知、退出码非 0，并且不把这次运行记为成功。由 Task 2 的 `test_refresh_failure_raises_login_required` 和 Task 8 的 `test_login_required_notifies` 固定。

---

## File Structure

```
plugins/feishu-backup/
├── .claude-plugin/plugin.json
├── README.md                        # 安装、飞书后台前置配置、换电脑
├── CHANGELOG.md
├── skills/feishu-backup/
│   ├── SKILL.md                     # 触发词、向导对话流程、日常命令、排障
│   └── scripts/
│       ├── fbk.py                   # argparse 入口
│       └── fbk/
│           ├── __init__.py
│           ├── config.py            # 路径、Config、token 读写
│           ├── names.py             # 文件名清洗、版本名、扩展名规则
│           ├── client.py            # HTTP、限流、重试、只读白名单、分页
│           ├── auth.py              # OAuth 登录、TokenManager、SCOPES
│           ├── store.py             # SQLite 清单
│           ├── walker.py            # 我的空间 / 知识库 / 快捷方式 遍历
│           ├── fetcher.py           # 导出、下载、.part、哈希、磁盘检查
│           ├── sync.py              # 新增/更新/归档/改名/去重/缺失标记
│           ├── chats.py             # 群消息扫描：链接 + 附件
│           ├── perms.py             # 链接可读性判断 + 权限缺口 CSV
│           ├── report.py            # 运行日志 md
│           ├── schedule.py          # 是否到期、运行锁、LaunchAgent
│           ├── notify.py            # 系统通知（带每日节流）
│           └── wizard.py            # setup 各步骤、doctor 体检、scan 统计
└── tests/
    ├── fakes.py                     # FakeTransport：按 (method, path) 返回预置响应
    └── test_*.py                    # 每个模块一个
```

运行测试的统一命令（在仓库根目录执行）：
`python3 -m unittest discover -s plugins/feishu-backup/tests -t plugins/feishu-backup -v`
测试文件开头把 `skills/feishu-backup/scripts` 加进 `sys.path`。

---

### Task 1: 插件骨架 + 配置 + 命名规则

**Files:**
- Create: `plugins/feishu-backup/.claude-plugin/plugin.json`、`CHANGELOG.md`、`skills/feishu-backup/scripts/fbk/__init__.py`、`config.py`、`names.py`
- Test: `plugins/feishu-backup/tests/test_names.py`、`test_config.py`

**Interfaces:**
- Produces:
  - `config.CONFIG_DIR: Path`、`config.LOG_DIR: Path`
  - `@dataclass Config(app_id: str, app_secret: str, backup_root: Optional[Path] = None, interval_hours: int = 24, lookback_days: int = 180)`
  - `load_config() -> Config`（没有配置文件时抛 `ConfigMissing`）、`save_config(cfg: Config) -> None`、`load_token() -> Optional[dict]`、`save_token(tok: dict) -> None`
  - `names.EXPORT_EXT = {"docx": "pdf", "doc": "pdf", "slides": "pdf", "sheet": "xlsx", "bitable": "xlsx"}`
  - `names.sanitize(name: str, max_bytes: int = 240) -> str`
  - `names.local_filename(ftype: str, name: str) -> Optional[str]`：上传文件保留原名；可导出类型用 `名字.pdf` 或 `名字.xlsx`；其他类型返回 None
  - `names.version_name(filename: str, mtime: int) -> str`
  - `names.unique_path(path: Path) -> Path`：如果 `path` 已存在，依次试 `stem-2.ext`、`stem-3.ext`……

- [ ] **Step 1: 写失败测试**

```python
def test_sanitize_replaces_illegal_and_newlines(self):
    self.assertEqual(sanitize("a/b:c\\d\ne"), "a_b_c_d_e")
def test_sanitize_truncates_keeping_ext(self):
    s = sanitize("长" * 200 + ".xlsx")
    self.assertTrue(s.endswith(".xlsx")); self.assertLessEqual(len(s.encode()), 240)
def test_local_filename_rules(self):
    self.assertEqual(local_filename("docx", "品牌手册"), "品牌手册.pdf")
    self.assertEqual(local_filename("sheet", "预算"), "预算.xlsx")
    self.assertEqual(local_filename("file", "合同.zip"), "合同.zip")
    self.assertIsNone(local_filename("mindnote", "脑图"))
def test_version_name_uses_shanghai_time(self):
    # 1759390200 = 2025-10-02 15:30 Asia/Shanghai
    self.assertEqual(version_name("2026 预算.xlsx", 1759390200), "2026 预算_2025-10-02_1530.xlsx")
def test_unique_path_appends_counter(self):  # 用 tempfile 建出 a.pdf 和 a-2.pdf
    self.assertEqual(unique_path(d / "a.pdf").name, "a-3.pdf")
def test_save_config_is_0600(self):  # 在测试里把 CONFIG_DIR patch 到临时目录
    self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
```

- [ ] **Step 2: 运行，确认失败**（`ModuleNotFoundError: fbk`）
- [ ] **Step 3: 实现上面列出的接口，并写好 `plugin.json`**（`name: feishu-backup`、`version: 0.1.0`、`description: 飞书文件定期备份到本地 Mac，旧版按日期归档，输出权限缺口报告`）。时区用 `timezone(timedelta(hours=8))`，避免依赖 zoneinfo 数据。
- [ ] **Step 4: 运行测试，全部通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 插件骨架、配置与命名规则`

### Task 2: HTTP 客户端 + 只读白名单 + 登录与续期

**Files:**
- Create: `scripts/fbk/client.py`、`scripts/fbk/auth.py`、`tests/fakes.py`
- Test: `tests/test_client.py`、`tests/test_auth.py`

**Interfaces:**
- Consumes: `Config`、`load_token`、`save_token`
- Produces:
  - `Transport = Callable[[str, str, dict, Optional[bytes]], tuple[int, dict, bytes]]`，默认实现基于 urllib
  - `client.ApiError(code: int, msg: str)`、`client.ReadOnlyViolation`
  - `client.ALLOWED_POST = {"/drive/v1/export_tasks", "/suite/docs-api/search/object", "/drive/v1/metas/batch_query", "/auth/v3/tenant_access_token/internal", "/authen/v2/oauth/token"}`
  - `FeishuClient(token_provider: Callable[[], str], transport: Transport = default, rps: float = 5.0, base: str = "https://open.feishu.cn/open-apis")`
    - `.get(path, params=None) -> dict`：返回 `data`，`code != 0` 时抛 `ApiError`
    - `.post(path, body) -> dict`
    - `.download(path, dest: Path, params=None, headers=None) -> int`：返回写入的字节数，写到调用方给的路径
    - `.head_size(path) -> Optional[int]`：带 `Range: bytes=0-0` 请求，读 `Content-Range` 里的总大小
    - `.paginate(path, params, items_key: str, token_param="page_token", token_field="page_token") -> Iterator[dict]`
  - `auth.SCOPES: list[str]`：设计文档 §3 的 120 项，从本次实测授权结果原样抄入
  - `auth.LoginRequired(Exception)`
  - `auth.build_authorize_url(app_id, redirect_uri, scopes, state) -> str`
  - `auth.run_login(cfg, open_browser=True, timeout=600) -> dict`：起 `127.0.0.1:8765` 回调服务 → 换 token → `save_token`（加 `obtained_at` 字段）
  - `auth.TokenManager(cfg, transport=default).access_token() -> str`：离过期不到 300 秒就用 refresh token 续期；续期失败抛 `LoginRequired`
  - `auth.tenant_token(cfg, transport=default) -> str`

- [ ] **Step 1: 写失败测试**

```python
def test_post_outside_allowlist_raises_without_sending(self):
    t = FakeTransport(); c = FeishuClient(lambda: "x", transport=t)
    with self.assertRaises(ReadOnlyViolation): c.post("/drive/v1/files/abc/delete", {})
    self.assertEqual(t.calls, [])
def test_non_get_methods_blocked(self):  # PUT/PATCH/DELETE 一律禁止
def test_retries_429_then_succeeds(self):  # 前两次返回 429，第三次返回 200，sleep 被打桩
def test_api_error_on_nonzero_code(self):
def test_paginate_follows_tokens(self):  # 两页，has_more 为 True，再为 False
def test_head_size_parses_content_range(self):  # "bytes 0-0/7704352" → 7704352
def test_authorize_url_contains_scope_and_state(self):
def test_token_refresh_when_near_expiry(self):  # obtained_at 设为 now-7000，expires_in=7200 → 触发续期
def test_refresh_failure_raises_login_required(self):  # 续期接口返回 code=20037
```

- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现。** 限流用简单令牌桶，加 `threading.Lock`，因为下载是多线程的。重试只针对 429、5xx 和网络异常，退避时间 `2**attempt` 秒。`sleep` 作为可注入参数，方便测试打桩。
- [ ] **Step 4: 测试全部通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 只读客户端与 OAuth 登录`

### Task 3: SQLite 清单

**Files:**
- Create: `scripts/fbk/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `Store(db_path: Path)`，首次打开时建表，`PRAGMA user_version = 1`
  - 表 `items(token PK, type, source, remote_dir TEXT(JSON 数组), name, remote_mtime INT, local_path TEXT, sha256 TEXT, status TEXT, also_seen TEXT(JSON), last_backup_at INT)`。`status` 取值：`ok` | `failed` | `skipped` | `remote_deleted` | `access_lost` | `duplicate`
  - `blobs(sha256 PK, local_path)`、`chat_cursor(chat_id PK, last_ts INT)`、`attachments(file_key PK, chat_id, message_id, sha256, local_path, status)`
  - `links(token PK, kind, status, hint, sender, chat_name, sent_at INT, first_seen INT)`、`runs(id INTEGER PK, started INT, finished INT, ok INT, summary TEXT)`
  - 方法：`get_item(token) -> Optional[sqlite3.Row]`、`upsert_item(**cols)`、`all_tokens(statuses=("ok",)) -> set[str]`、`set_status(token, status)`、`find_blob(sha) -> Optional[Path]`、`add_blob(sha, path)`、`get_cursor(chat_id) -> Optional[int]`、`set_cursor(chat_id, ts)`、`upsert_attachment(...)`、`get_attachment(file_key)`、`upsert_link(token, **cols)`（保留最早的 `first_seen`）、`links() -> list[Row]`、`start_run() -> int`、`finish_run(run_id, ok: bool, summary: dict)`、`last_success() -> Optional[int]`

- [ ] **Step 1: 写失败测试**：建表；`upsert_item` 两次后字段被覆盖、`first_seen` 不变；`last_success` 只认 `ok=1` 的运行；`all_tokens` 按状态过滤。
- [ ] **Step 2: 运行，确认失败** → **Step 3: 实现** → **Step 4: 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): SQLite 清单`

### Task 4: 遍历我的空间、知识库、快捷方式

**Files:**
- Create: `scripts/fbk/walker.py`
- Test: `tests/test_walker.py`

**Interfaces:**
- Consumes: `FeishuClient.get`、`FeishuClient.paginate`、`names.sanitize`
- Produces:
  - `@dataclass RemoteItem(token: str, type: str, name: str, rel_dir: tuple, source: str, mtime: int, has_children: bool = False)`
    - `source` 取值：`"my"` | `"wiki"` | `"shared"`
    - `rel_dir[0]` 取值：`"我的空间"` | `"知识库"` | `"共享给我"`
  - `walk_my_space(client) -> Iterator[RemoteItem]`：从 `GET /drive/explorer/v2/root_folder/meta` 拿到根目录，再用 `GET /drive/v1/files?folder_token=` 逐层递归（`page_size=200`，翻页参数 `next_page_token`）。文件夹不产出，只拼进 `rel_dir`。`shortcut` 类型解析 `shortcut_info.target_token` 和 `target_type`，按目标产出，`name` 用快捷方式自己的名字，`mtime` 用 `metas/batch_query` 查目标的 `latest_modify_time`；目标查不到时跳过并记一条 warning。
  - `walk_wiki(client) -> Iterator[RemoteItem]`：`GET /wiki/v2/spaces`，再递归 `GET /wiki/v2/spaces/{id}/nodes`（`parent_node_token`）。`rel_dir = ("知识库", 空间名, *祖先节点标题)`。`mtime` 取节点的 `obj_edit_time`。有子节点的文档节点产出时 `has_children=True`，它的子节点放在 `rel_dir + (节点标题,)` 下面。
  - 所有路径片段都经过 `sanitize`。

- [ ] **Step 1: 写失败测试**（用 FakeTransport 预置 JSON 响应）

```python
def test_my_space_nested_paths(self):  # 根目录 → 「市场部」→ docx「品牌手册」
    self.assertEqual(items[0].rel_dir, ("我的空间", "市场部"))
def test_shortcut_resolves_to_target(self):
    self.assertEqual((it.token, it.type), ("TGT1", "sheet"))
def test_wiki_children_under_parent_title(self):
    self.assertEqual(child.rel_dir, ("知识库", "产品库", "上新规范"))
def test_folder_name_with_slash_sanitized(self):  # 文件夹名 "A/B" → rel_dir 里是 "A_B"
```

- [ ] **Step 2–4: 先跑失败 → 实现 → 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 云空间与知识库遍历`

### Task 5: 导出、下载、.part 临时文件、哈希、磁盘检查

**Files:**
- Create: `scripts/fbk/fetcher.py`
- Test: `tests/test_fetcher.py`

**Interfaces:**
- Consumes: `FeishuClient`、`names.EXPORT_EXT`
- Produces:
  - `DiskFull(Exception)`、`ExportFailed(Exception)`
  - `ensure_space(root: Path, need_bytes: int, margin: int = 2 * 1024**3) -> None`：用 `shutil.disk_usage`
  - `export_to(client, token: str, ftype: str, dest: Path, poll: float = 2.0, timeout: float = 300) -> Path`：`POST /drive/v1/export_tasks {file_extension, token, type}` → 轮询 `GET /drive/v1/export_tasks/{ticket}?token=`，`job_status` 为 0 表示成功、1 或 2 表示处理中，其他值抛 `ExportFailed` → 下载 `GET /drive/v1/export_tasks/file/{file_token}/download`
  - `download_file(client, token: str, dest: Path) -> Path`：`GET /drive/v1/files/{token}/download`
  - `download_attachment(client, message_id: str, file_key: str, dest: Path) -> Path`：`GET /im/v1/messages/{message_id}/resources/{file_key}?type=file`
  - 以上三个函数都先写到 `dest.name + ".part"`，成功后 `os.replace` 到 `dest`；出错时删除 `.part` 再把异常抛出去
  - `sha256_file(path: Path) -> str`
  - `clean_parts(root: Path) -> int`：递归删掉 `*.part`，返回删除的个数

- [ ] **Step 1: 写失败测试**：导出先返回「处理中」再返回「完成」，最终文件内容正确；`test_part_file_not_committed_on_error`（传输中途抛错 → `dest` 和 `.part` 都不存在）；`ensure_space` 在剩余空间不足时抛 `DiskFull`（打桩 `disk_usage`）；`clean_parts` 返回正确个数。
- [ ] **Step 2–4: 先跑失败 → 实现 → 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 导出下载与原子落盘`

### Task 6: 同步引擎（新增 / 更新归档 / 改名移动 / 去重 / 缺失标记）

**Files:**
- Create: `scripts/fbk/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `Store`、`RemoteItem`、`fetcher.*`、`names.*`
- Produces:
  - `class Outcome(str, Enum)`：`NEW`、`UPDATED`、`UNCHANGED`、`MOVED`、`DUPLICATE`、`SKIPPED`、`FAILED`
  - `@dataclass SyncContext(client, store: Store, root: Path, fetch=fetcher)`：`fetch` 可注入，方便测试
  - `archive_current(local: Path, old_mtime: int) -> Path`：移到 `local.parent / "历史版本" / version_name(local.name, old_mtime)`，用 `unique_path` 避免重名，**只移动，不删除**
  - `sync_item(ctx, item: RemoteItem) -> Outcome`，规则：
    1. `local_filename` 返回 None → `SKIPPED`，状态记 `skipped`
    2. 清单里已有这个 token，但 `source` 优先级更低、或者 `rel_dir` 指向别处，而现有记录来源优先级更高 → `DUPLICATE`，把这个位置追加到 `also_seen`
    3. 清单里没有 → `ensure_space` → 下载或导出 → 对 `file` 类型算 sha256，如果 `blobs` 里已有相同内容，删掉刚下载的副本、指向已有路径 → 结果为 `DUPLICATE`；否则 `NEW`
    4. 目标路径变了，但 `mtime` 没变 → 移动当前文件，同时把它的 `历史版本/` 里以 `原名_` 开头的文件搬到新位置的 `历史版本/` → `MOVED`
    5. `mtime` 变大：
       - 对 `file` 类型：先下载到临时位置，sha256 和本地相同 → 丢弃临时文件，只更新 `remote_mtime` → `UNCHANGED`
       - 内容不同（以及所有导出类型）：先把新版下载到 `.part`，成功后再 `archive_current(旧文件, 旧 mtime)`，然后用 `os.replace` 把新版放到原位置 → `UPDATED`
    6. 任何异常 → 状态记 `failed`，返回 `FAILED`，不再往外抛（`DiskFull` 和 `LoginRequired` 例外，这两个要往外抛）
  - `mark_missing(store, seen: set[str]) -> int`：把清单里状态为 `ok` 但本次没遍历到的 token 改成 `remote_deleted`，**不碰本地文件**

- [ ] **Step 1: 写失败测试**

```python
def test_new_doc_saved_as_pdf(self)
def test_update_archives_old_with_old_mtime(self):
    self.assertTrue((d / "历史版本" / "品牌手册_2025-10-02_1530.pdf").exists())
    self.assertEqual((d / "品牌手册.pdf").read_bytes(), b"v2")
def test_update_download_failure_keeps_old_file_in_place(self)
def test_identical_file_content_not_archived(self)
def test_same_token_second_location_is_duplicate(self)
def test_same_content_different_token_deduped(self)
def test_move_carries_history(self)
def test_remote_folder_named_history_is_normal_dir(self):  # 云端路径 ("我的空间","历史版本") 下的文件照常同步
def test_mark_missing_never_deletes(self)
def test_unknown_type_skipped(self)
```

- [ ] **Step 2–4: 先跑失败 → 实现 → 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 同步引擎与历史版本归档`

### Task 7: 群消息扫描（链接 + 附件）与权限判断

**Files:**
- Create: `scripts/fbk/chats.py`、`scripts/fbk/perms.py`
- Test: `tests/test_chats.py`、`tests/test_perms.py`

**Interfaces:**
- Consumes: `FeishuClient`、`Store`、`fetcher.download_attachment`、`sync.SyncContext`、`walker.RemoteItem`、`sync.sync_item`
- Produces:
  - `chats.LINK_RE`：匹配 `https?://[\w.-]*feishu\.cn/(docx|docs|doc|sheets|base|wiki|file|mindnotes|slides)/([A-Za-z0-9]{10,})`
  - `chats.KIND_TO_TYPE = {"docx": "docx", "docs": "doc", "doc": "doc", "sheets": "sheet", "base": "bitable", "file": "file", "mindnotes": "mindnote", "slides": "slides"}`（`wiki` 单独处理）
  - `chats.ATTACHMENT_TYPES = {"file", "audio", "media"}`
  - `@dataclass LinkHit(kind, token, chat_name, sender, sent_at: int, hint: str)`：`hint` = 消息正文去掉链接后的前 80 个字符，或卡片标题
  - `@dataclass AttachmentHit(chat_id, chat_name, message_id, file_key, file_name, sent_at: int)`
  - `chats.extract_links(body_json: str) -> list[tuple[str, str]]`
  - `chats.attachment_filename(sent_at: int, name: str) -> str`：返回 `YYYY-MM-DD_原文件名`，日期按上海时区
  - `chats.scan(client, store, lookback_days: int, now: int) -> Iterator[LinkHit | AttachmentHit]`：`GET /im/v1/chats` 翻页 → 对每个群调 `GET /im/v1/messages?container_id_type=chat&container_id=&start_time=&sort_type=ByCreateTimeAsc`，`start_time` 取 `max(cursor, now - lookback)`；处理完每个群再 `set_cursor`
  - `chats.save_attachment(ctx, hit) -> Outcome`：`attachments` 表里已有这个 `file_key` → `UNCHANGED`；下载到 `群聊附件/<群名>/` → 算 sha，`blobs` 里有相同内容 → 删掉副本，结果为 `DUPLICATE`
  - `perms.classify(client, kind, token) -> tuple[str, Optional[str]]`：返回 (`"ok"` | `"denied"` | `"gone"`, 解析出的类型)。`wiki` 先调 `GET /wiki/v2/spaces/get_node`。权限判断调 `GET /drive/v1/permissions/{token}/members/auth?type=&action=view`：`auth_result` 为 False → denied；错误码 1063005 / 1063001 → gone。
  - `perms.write_report(store, path: Path) -> dict`：写 UTF-8 BOM 的 CSV，列依次为 `状态,类型,链接,文件名线索,分享人,群名,分享时间,首次发现日期`，首行之前写汇总注释行和局限说明；返回 `{"total", "denied", "gone"}`
  - 能打开、但不在清单里的链接 → 构造 `RemoteItem(source="shared", rel_dir=("共享给我", 群名))` 交给 `sync_item`
  - `chats.search_shared(client, known: set[str]) -> Iterator[RemoteItem]`：`POST /suite/docs-api/search/object {search_key: "", count: 50, offset}` 翻页直到 `has_more` 为 False。对不在 `known` 里、类型可导出的结果，产出 `RemoteItem(source="shared", rel_dir=("共享给我", "_搜索发现"))`，`mtime` 用 `metas/batch_query` 补齐

- [ ] **Step 1: 写失败测试**：
  - 从 text、post、interactive 三种消息体里都能抽出链接；`image` 消息不产出附件
  - `attachment_filename(1763020800, "报价单.xlsx") == "2025-11-13_报价单.xlsx"`（1763020800 = 2025-11-13 16:00，上海时间）
  - 第二次扫描从游标处开始，并且不早于 `now - lookback`
  - 附件按 `file_key` 去重；按内容去重
  - `classify` 三种结果，wiki 走 `get_node`
  - CSV 带 BOM、表头完全一致、汇总数字正确
  - `search_shared` 跳过 `known` 里的 token，翻页能走完
- [ ] **Step 2–4: 先跑失败 → 实现 → 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 群消息链接与附件扫描、权限缺口报告`

### Task 8: 运行编排、报告、调度、通知

**Files:**
- Create: `scripts/fbk/report.py`、`scripts/fbk/schedule.py`、`scripts/fbk/notify.py`、`scripts/fbk/runner.py`
- Test: `tests/test_schedule.py`、`tests/test_runner.py`

**Interfaces:**
- Consumes: 以上全部模块
- Produces:
  - `report.RunReport`：用 `counts: Counter[Outcome]` 和 `entries: list[tuple[Outcome, str, str]]`（结果、路径、原因）记录；`write_markdown(path: Path, started, finished)`，按「新增 / 更新 / 移动 / 去重 / 跳过 / 失败 / 已归档的历史版本」分节
  - `schedule.LABEL = "local.feishu-backup"`、`schedule.is_due(last_success: Optional[int], interval_hours: int, now: int) -> bool`
  - `schedule.RunLock(root)`：上下文管理器，锁文件是 `.feishu-backup/run.lock`，里面写 PID；已有存活进程时抛 `AlreadyRunning`，PID 已经死了就接管
  - `schedule.plist_xml(python: str, script: str) -> bytes`：用 `plistlib` 生成，包含 `RunAtLoad=True`、`StartInterval=3600`，`ProgramArguments=[python, script, "run"]`，stdout 和 stderr 指向 `LOG_DIR`
  - `schedule.install() -> Path`（写到 `~/Library/LaunchAgents/local.feishu-backup.plist`，再 `launchctl bootstrap gui/$UID`）、`schedule.uninstall()`
  - `notify.notify(title, message, key: Optional[str] = None)`：用 `osascript display notification`；带 `key` 时同一个 key 每天最多发一次，节流状态存在 `CONFIG_DIR/notify_state.json`
  - `runner.run(now: Optional[int] = None, force: bool = False) -> int`，返回退出码，顺序如下：
    1. 读配置
    2. `backup_root` 不存在 → 通知（key=`root_missing`），返回 2。**不创建目录**
    3. 不到期且没有 `force` → 返回 0
    4. 加 `RunLock`
    5. `clean_parts`
    6. `start_run`
    7. 用线程池（下载 ≤4、导出 ≤3）对 `walk_my_space`、`walk_wiki` 产出的每一项调用 `sync_item`
    8. `mark_missing`
    9. `chats.scan`：链接 → `classify` → `upsert_link`，能打开的交给 `sync_item(shared)`；附件 → `save_attachment`。然后跑 `chats.search_shared(client, store.all_tokens())`，产出的每一项交给 `sync_item`
    10. 写两份报告
    11. `finish_run(ok=True)`

    异常处理：`LoginRequired` → 通知「需要重新登录：对 Claude 说『飞书备份重新登录』」，`finish_run(ok=False)`，返回 3。`DiskFull` → 通知「备份盘空间不足」，返回 4。网络不通（urllib 的 URLError 出现在第一个请求上）→ 安静地返回 0，不记为成功。
  - 日志：`logging` 输出到 `LOG_DIR/fbk-YYYY-MM-DD.log`，启动时删除 30 天前的日志

- [ ] **Step 1: 写失败测试**：
  - `is_due` 的边界：`None` → True；23 小时 → False；24 小时 → True
  - `RunLock` 遇到存活 PID 抛异常，遇到死 PID 则接管
  - `plist_xml` 能被解析回来，并且包含 `RunAtLoad` 和 `StartInterval` 3600
  - `test_missing_root_does_not_create`
  - `test_login_required_notifies`（`notify` 打桩，返回值为 3，`last_success` 不变）
  - `test_run_cleans_stale_parts`
  - 端到端：在 FakeTransport 上预置一个 docx 和一个上传文件，`run(force=True)` 之后两个文件都落盘，并生成了 `_报告/*_备份日志.md`
- [ ] **Step 2–4: 先跑失败 → 实现 → 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 运行编排、调度、通知与报告`

### Task 9: 配置向导、体检、只统计扫描、CLI 入口

**Files:**
- Create: `scripts/fbk/wizard.py`、`scripts/fbk.py`
- Test: `tests/test_wizard.py`

**Interfaces:**
- Consumes: 以上全部模块
- Produces:
  - `@dataclass Check(name: str, ok: bool, detail: str, fix: str = "")`
  - `wizard.check_env() -> list[Check]`：Python 版本 ≥ 3.9；能连上 `open.feishu.cn:443`
  - `wizard.check_credentials(cfg) -> Check`：能换到 tenant token。失败时 `fix` 写清楚：去开发者后台 →「凭证与基础信息」核对 App Secret，并确认应用状态是「已启用」
  - `wizard.prompt_secret() -> tuple[str, str]`：用 `osascript display dialog ... with hidden answer` 弹两次窗口，分别取 App ID 和 App Secret，**不经过终端和聊天**
  - `wizard.doctor(client) -> list[Check]`：8 项实测，依次是：列我的空间、列知识库、搜索、导出一份 docx（只发起任务，不下载）、取一个上传文件的 1 字节、权限判断、列群、读一条群消息。每项失败时给出对应的 `fix`，例如错误码 232025 → 「在开发者后台为应用开启机器人能力并发版」；230027 或 99991672 → 「缺少权限 X：开发者后台 → 权限管理 → 批量导入 → 发版 → 重新运行 setup --login」
  - `wizard.check_root(path: Path, need_bytes: int) -> list[Check]`：目录能否写入；剩余空间是否 ≥ 需要量 × 1.5；路径在 `~/Library/Mobile Documents` 或 `~/Desktop`、`~/Documents` 下且开启了 iCloud 时给警告（判断不了时只给提示，不判失败）
  - `wizard.scan_stats(client) -> dict`：只统计不下载，返回 `{"items_by_type", "upload_bytes", "export_est_bytes", "est_minutes"}`。上传文件大小用 `head_size` 精确统计；在线文档每类抽 8 份，导出后只读 `file_size` 字段，不下载
  - `fbk.py` 子命令：
    - `setup --credentials`（弹窗取凭证 → `check_credentials` → 保存）
    - `setup --login`（`run_login` → 显示「已登录：姓名」）
    - `setup --root PATH`（`check_root`，通过后保存）
    - `setup --schedule`（`schedule.install`）
    - `setup --status`（逐项显示哪些步骤已完成）
    - `doctor`、`scan`、`run [--now]`、`status`（上次成功时间、清单里各状态的数量、最近一次报告路径）
    - `perms`（打印最新缺口报告的汇总）
    - `schedule install|uninstall`

    每个命令都打印人能读的中文，`--json` 时改为输出机器可读的 JSON
- [ ] **Step 1: 写失败测试**：
  - `doctor` 遇到 232025 时，`fix` 里包含「机器人」
  - `check_root` 遇到不存在的路径时返回失败，并且不创建目录
  - `scan_stats` 用 FakeTransport 统计出预期的字节数
  - `fbk.py status --json` 在空清单上能正常输出
- [ ] **Step 2–4: 先跑失败 → 实现 → 测试通过**
- [ ] **Step 5: Commit** `feat(feishu-backup): 配置向导、体检与 CLI`

### Task 10: SKILL.md、README、仓库登记、真账号冒烟

**Files:**
- Create: `plugins/feishu-backup/skills/feishu-backup/SKILL.md`、`plugins/feishu-backup/README.md`
- Modify: `.claude-plugin/marketplace.json`（追加插件条目，`category: 办公效率`，`version: 0.1.0`）、根目录 `README.md`（新增「办公效率」分类，在下面加一行，并在安装命令里加一行）

**SKILL.md 必须写明：**
- 触发词：配置飞书备份、飞书备份状态、现在备份一次、看权限缺口、飞书备份重新登录、卸载飞书备份
- 向导对话流程，严格按设计文档 §5.1 的 7 步走，**一次只问一个问题**：
  1. `doctor` 里的环境部分
  2. `setup --credentials`（提醒用户留意弹窗）
  3. `setup --login`（在后台运行，提醒用户去浏览器点授权）
  4. `doctor`
  5. 问备份目录 → `setup --root`
  6. `scan`，把结果读给用户，**等用户确认后**才继续
  7. `setup --schedule`，再询问是否立即运行 `run --now`（放在后台运行）

  任何一步出现 ✗ 时，原样转述 `fix` 内容并停下来
- 说明：脚本不能做交互式输入，所以凭证只能通过弹窗获取；**绝不在对话里索要或回显 App Secret**
- 日常各命令与输出的解读方法；报告文件在哪里
- 排障表：退出码 2 / 3 / 4 各自的含义和处理办法

**README 必须写明：** 飞书后台的前置配置（新建企业自建应用、批量导入 `auth.SCOPES`、重定向 URL、开启机器人能力、发版、可选的免审）、安装命令、换电脑步骤、卸载步骤、局限说明（看不到别人私人空间里的文件；免费版没有审计日志）。

- [ ] **Step 1: 全量测试**：`python3 -m unittest discover -s plugins/feishu-backup/tests -t plugins/feishu-backup -v`，全部 PASS
- [ ] **Step 2: 安全检查**：`git grep -nE 'cli_[0-9a-f]{16}|feishu\.cn/(docx|sheets|wiki)/[A-Za-z0-9]{10,}|app_secret"\s*:\s*"[^"]'` 输出为空（测试里只能出现虚构的 token）
- [ ] **Step 3: 真账号冒烟（在本机做，不进仓）**：用已有的 `~/.config/feishu-backup/` 配置，备份目录设为临时目录。依次运行 `doctor`（8 项全部 ✓）、`scan`（文件数与设计文档 §3 一致，偏差不超过 ±2%）。然后临时加一个 `--limit 20` 调试参数跑 `run --now`：20 个文件落盘、报告生成。再跑一次 `run --now`，结果应全部为 UNCHANGED。冒烟完成后删掉临时目录。`--limit` 保留为隐藏参数。
- [ ] **Step 4: Commit 并推送**

```bash
git add plugins/feishu-backup .claude-plugin/marketplace.json README.md
git commit -m "feat(feishu-backup): v0.1.0 飞书文件自动备份插件"
git push origin main
```
