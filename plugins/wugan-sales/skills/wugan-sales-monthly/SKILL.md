---
name: wugan-sales-monthly
description: Use when the user asks for 五感咖啡 / 五感 monthly sales statistics (五感销售数据统计, 月度销售表, 商品销量/销售额占比), or mentions the 戴维盈动商家管理后台 (zhyx.eingdong.com) 销售汇总 export for a given month.
---

# 五感月度销售表

## Overview
从戴维盈动商家后台「运营数据 → 销售汇总」按月取商品级明细，按 `rules.yaml` 清洗成 **分类 → 商品 → 规格** 三层表，写进**当年的年度文件** `~/Desktop/YYYY年五感销售数据统计.xlsx`（一年一个文件，不存在就新建）。
`~/Desktop/五感销售数据统计.xlsx` 是 2025 年手工版（8–12 月），只读不写，但新品识别会参考它。
**口径只认 `rules.yaml`**；脚本对不平、遇到没登记的规格选项会直接停下，不要绕过。

## 已验证的口径（2025-08 ~ 12 与手工表逐行对平）
| 表列 | 接口字段 | 说明 |
|---|---|---|
| 实际销量 | `real_num` | |
| 实际销售额(不含退款、优惠) | `real_total` | = 销售总额 − 优惠 − 退款 |
| 单价 | `one_price` 均值 | 后台标价在明细行上的算术平均，不是销售额/销量 |
| 规格 | `key_list` 过滤 | 只保留 `spec_rules.keep`（意式：咖啡豆；周边：颜色） |

取数条件 = 销售汇总页默认：**支付时间**（`time_type=2`）、整月、全时段、不拆套餐、不合并同名。

## 流程
1. **确定月份**：默认上一个完整自然月；sheet 名 `YYYY年M月`。
2. **登录**：Playwright `browser_navigate` 到 `https://zhyx.eingdong.com/console/#/login`。如果没有跳到 `#/index`，**停下请用户在浏览器里登录**（不要索要或保存账号密码）。
3. **取数**：读 `scripts/fetch_month.js`，把 `__MONTH__` 替换成 `YYYY-MM`，整段作为 `browser_evaluate` 的 function，`filename` 存到 `.playwright-mcp/wugan_raw_YYYY-MM.json`（Playwright 只能写到它允许的目录）。结果里有 `error` 就按提示处理（`NOT_LOGGED_IN` → 回第 2 步）。
4. **试跑**：
   ```bash
   python -I scripts/build_report.py --raw <raw.json> --dry-run   # 文件按 raw 的年份自动定位；--dir 改目录
   ```
   需要 `openpyxl` 和 `pyyaml`。
   - 退出码 2 `UNKNOWN_SPEC_OPTIONS`：把列出的选项给用户看，按用户意见补进 `rules.yaml` 的 keep/drop 后重跑。明显属于冰量/浓度/奶/尺码/克重的可以直接归 drop，但要在回复里列出来。
   - 退出码 3 `RECON_FAIL`：**不出表**，排查原因。
   - 摘要里的「新商品 / 新分类」：问用户要不要改名或改分类（写进 `product_rename` / `reclassify`）。新品 = 同目录所有年度文件里**此前月份**没出现过；补历史月份时按月份从早到晚依次写入，否则新品会被多报。
5. **写入**：去掉 `--dry-run` 再跑一次。文件已存在时会先备份到同目录 `_backup/`，再新增三个 sheet：当月主表、`-1元计价`、`-异常与调整`，并按月份自动排好顺序。sheet 已存在时退出码 4，只有用户确认后才加 `--replace`。
6. **回复用户**：主表总额、1 元计价金额、和上月比较、异常与调整的要点。

## rules.yaml 各项
- `spec_rules`：哪些分类展开规格，保留哪些选项
- `spec_rename` / `product_rename` / `reclassify`：人工统一名称和分类，每次命中都会记到「异常与调整」
- `one_yuan_price_max`：0 < 标价 ≤ 阈值的商品（补差价、1 元「手冲咖啡」）不进主表，**主表占比分母不含这部分**
- `category_order`：主表分类排序

## 常见坑
- **补差价按 1 元计价，销量 = 金额**：2025-10 一项就是 75,884 件。放进主表会把销量和占比全部扭曲，所以单独放一个 sheet。
- **标价 0 的是赠品**，留在主表；成交 0 元的行（100% 优惠）会列在「异常与调整」里。
- **后台页面顶部的「实际销售额」可能和明细加总差几十元**（退款归属不同），表格以明细为准，差额写在「异常与调整」。
- 不要用透视表的「求和」算单价（2025-11 手工表就是这样错的），也不要换成销售额/销量。
- 接口 `page_size=2000`，超过时脚本返回 `PAGINATED`，需要先给 fetch 加分页。
- 这个仓库是公开的：raw json 和工作簿**不要提交**，token 只在浏览器里用。
