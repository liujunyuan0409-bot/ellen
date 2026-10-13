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

## 安装

```
/plugin marketplace add liujunyuan0409-bot/ellen
/plugin install xhs-content-research@ellen
/plugin install wugan-sales@ellen
```

## 新增插件

1. 新建 `plugins/<插件名>/`，放 `.claude-plugin/plugin.json`、`README.md`、`skills/<skill名>/SKILL.md`
2. 在 `.claude-plugin/marketplace.json` 的 `plugins` 里登记，写上 `category`
3. 在本 README 对应分类下加一行；没有合适的分类就新建一个
