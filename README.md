# xhs-content-research

小红书内容调研 skill，作为 Claude Code 插件分发。

调研小红书内容的**采集、结构化分析与洞察输出**，是 `xhs-visual-director`（出图/排版）的上游——产出选题简报后可直接喂给视觉导演 skill。

## 三种调研模式

| 模式 | 用途 |
|------|------|
| **选题调研** | 给定主题/品类，找爆款角度与内容机会 |
| **竞品账号** | 分析指定账号的内容策略、发布节奏、爆款规律 |
| **单篇拆解** | 给定笔记链接，逐段/逐镜精拆套路 |

## 流水线

```
GOAL → GATHER → NORMALIZE → ANALYZE → OUTPUT
```

- **GATHER** — 混合采集：Playwright 自动化（首选）→ 用户手动喂（降级）→ 第三方导出（可选）
- **ANALYZE** — 固定 rubric 打标 + 跨篇洞察六维（含视频专属维度）
- **OUTPUT** — 原始数据进 Bitable 素材库，洞察出 markdown 报告 + visual-director 简报

## 目录结构

```
.claude-plugin/plugin.json          插件清单
skills/xhs-content-research/
  ├─ SKILL.md                       router + 五步流水线总控
  └─ references/
     ├─ analysis-rubric.md          两层分析 rubric
     ├─ gathering.md                三种采集方式 playbook
     ├─ bitable-schema.md           素材库表字段定义
     └─ output-templates.md         报告 + 简报模板
```

## 安装

作为 Claude Code 插件安装本 repo，或手动复制 `skills/xhs-content-research/` 到 `~/.claude/skills/`。
