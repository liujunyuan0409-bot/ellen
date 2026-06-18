---
name: xhs-content-research
description: 小红书内容调研。覆盖选题调研、竞品账号分析、单篇笔记拆解三种模式,混合采集(Playwright/手动/第三方导出),产出 Bitable 素材库 + markdown 洞察报告与给 xhs-visual-director 的选题简报。当用户要调研小红书选题/对标爆款/分析竞品账号/拆解某篇笔记/找内容机会时使用。Also use when researching Xiaohongshu (XHS / RedNote) topics, benchmarking viral notes, analyzing competitor accounts, or deconstructing a specific note.
---

# xhs-content-research — 小红书内容调研总控

## 定位

本 skill 负责小红书内容的**采集、结构化分析与洞察输出**，不产出图像或排版方案。产出物（调研报告 + 选题简报）作为上游，可直接输入 `xhs-visual-director` skill 驱动封面设计与图文排版。

---

## 模式分诊

收到请求后，先判断调研模式，再进入对应配置（见下方差异表）：

| 模式 | 触发线索 | 典型请求示例 |
|------|---------|------------|
| **选题调研** | 用户给出主题关键词，想找爆款角度/内容机会，无特定账号或笔记 | "调研亲子游的爆款选题" / "这品类有什么内容机会" |
| **竞品账号** | 用户指定一个或多个账号名，想分析其内容策略、发布规律、爆款规律 | "分析这几个竞品账号的内容策略" / "看看对标账号发什么爆" |
| **单篇拆解** | 用户提供具体笔记链接或粘贴笔记内容，想逐段/逐镜精拆 | "拆解这篇小红书的套路" / "帮我分析这篇笔记为什么火" |

若请求模式不明确，先问用户确认。

---

## 五步流水线

```
① GOAL → ② GATHER → ③ NORMALIZE → ④ ANALYZE → ⑤ OUTPUT
```

**① GOAL — 明确调研目标**
确认模式（选题/竞品/单篇）、关键词或目标账号/笔记、样本量目标、时间窗，以及产出物要求（仅报告 / 含 Bitable 存档）。

**② GATHER — 采集笔记数据**
按优先级执行采集：Playwright 自动化（首选）→ 用户手动喂（降级）→ 第三方导出（可选）。每篇记录 `采集方式`，字段缺失留空不编造。完整操作步骤见 [references/gathering.md](references/gathering.md)。

**③ NORMALIZE — 结构化录入**
将采集的原始数据映射到素材库字段，写入 Bitable（无表时先建表）。字段定义与建表/写入约定见 [references/bitable-schema.md](references/bitable-schema.md)。

**④ ANALYZE — 打标与洞察提炼**
对每篇笔记填写 §A 共用打标字段、§B 类型分叉字段；汇总后提炼 §C 跨篇洞察六维；各模式附加 §D 特异性分析（热力分布/账号指标/逐镜精拆）。Rubric 全文见 [references/analysis-rubric.md](references/analysis-rubric.md)。

**⑤ OUTPUT — 输出调研报告与简报**
按模板生成调研报告（`analysis/xhs_<主题>_<日期>/report.md`）及 xhs-visual-director 选题简报。模板见 [references/output-templates.md](references/output-templates.md)。

---

## 三模式配置差异

| 维度 | 选题调研 | 竞品账号 | 单篇拆解 |
|------|---------|---------|---------|
| **入口参数** | 主题关键词、目标样本量（建议 ≥20 篇）、时间窗 | 目标账号名列表（1–5 个）、每账号采集篇数 | 笔记链接 或 粘贴正文/截图 |
| **GATHER 重点** | 搜索结果列表批量采集 | 逐账号主页翻页采集，补充粉丝量级 | 单篇深度采集（手动为主） |
| **④ ANALYZE 侧重** | 计算「角度×互动」热力矩阵，突出蓝海区域（rubric §D 选题调研） | 计算发布节奏、账号爆款率、选题矩阵三项账号级指标（rubric §D 竞品账号） | 对单篇做逐段/逐镜精拆，附爆款归因（rubric §D 单篇拆解） |
| **⑤ OUTPUT 侧重** | 报告以「选题机会」洞察为首位，给出可做/竞争过热/蓝海待验证三档判断 | 报告以「账号规律总结」为首位，其次为可借鉴套路 | 可仅出 markdown 精拆表，用户有存档需求时选择性写入 Bitable |
| **Bitable 写入** | 全量写入 | 全量写入 | 可选 |

> 三种模式均需在报告中标注样本图文 vs 视频占比及各自爆款率（rubric §D 三模式通用）。

---

## 收口提示

调研完成后，告知用户：

> 调研报告与选题简报已生成。可直接调用 **`xhs-visual-director`** skill，将简报作为输入，驱动封面设计与图文排版。
