# ellen · Claude Code 插件集

个人 Claude Code 插件 marketplace。每个插件相互独立，可以单独安装。

## 插件目录

### 内容创作

| 插件 | 用途 |
|------|------|
| [xhs-content-research](plugins/xhs-content-research) | 小红书选题调研、竞品账号分析、单篇拆解 |

### 经营数据

| 插件 | 用途 |
|------|------|
| [wugan-sales](plugins/wugan-sales) | 五感咖啡月度销售表（商家后台取数 → 清洗 → Excel） |

### 办公效率

| 插件 | 用途 |
|------|------|
| [feishu-backup](plugins/feishu-backup) | 飞书文件每周备份到本机（macOS / Windows，Windows 版未经真机验证），旧版按日期归档，附权限缺口报告 |

## 安装

```
/plugin marketplace add liujunyuan0409-bot/ellen
/plugin install xhs-content-research@ellen
/plugin install wugan-sales@ellen
/plugin install feishu-backup@ellen
```

不用 Claude Code 也行：把本仓库链接发给你的 AI 助手（Codex CLI、Cursor、Gemini CLI 等），它会读根目录的 [AGENTS.md](AGENTS.md) 找到对应插件的说明。飞书备份的安装与使用说明在 [plugins/feishu-backup/AGENTS.md](plugins/feishu-backup/AGENTS.md)。

## 新增插件

1. 新建 `plugins/<插件名>/`，放 `.claude-plugin/plugin.json`、`README.md`、`skills/<skill名>/SKILL.md`
2. 在 `.claude-plugin/marketplace.json` 的 `plugins` 里登记，写上 `category`
3. 在本 README 对应分类下加一行；没有合适的分类就新建一个
4. 插件要给非 Claude 的 AI 助手用时，在根目录 [AGENTS.md](AGENTS.md) 的表里登记说明位置
