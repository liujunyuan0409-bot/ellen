# wugan-sales

五感咖啡月度销售表 skill。

每月从戴维盈动商家后台（销售汇总）取上月商品级数据，清洗成 **分类 → 商品 → 规格** 三层表，写进当年的 `YYYY年五感销售数据统计.xlsx`（一年一个文件）。

## 安装

```
/plugin install wugan-sales --marketplace liujunyuan0409-bot/ellen
```

## 口径

| 表列 | 后台字段 | 说明 |
|------|----------|------|
| 实际销量 | `real_num` | |
| 实际销售额(不含退款、优惠) | `real_total` | 销售总额 − 优惠 − 退款 |
| 单价 | `one_price` 均值 | 后台标价的算术平均 |
| 规格 | `key_list` 过滤 | 意式只留咖啡豆，周边只留颜色 |

取数条件：支付时间、整月、其余为销售汇总页默认值。业务规则（规格保留、改名、改分类、1 元计价商品）全部在 `rules.yaml`。

## 每月产出

- `年度汇总`（第一页）：分类 × 月份的销售额 / 销量矩阵，含 1元计价、后台合计、环比
- `YYYY年M月`：主表
- `YYYY年M月-1元计价`：补差价等单价 1 元的商品（销量 = 金额，不进主表）
- `YYYY年M月-异常与调整`：改名/改分类、0 元成交、新商品、对数结果

## 目录结构

```
.claude-plugin/plugin.json
skills/wugan-sales-monthly/
  ├─ SKILL.md                 流程与常见坑
  ├─ rules.yaml               清洗口径（唯一来源）
  └─ scripts/
     ├─ fetch_month.js        在已登录后台页面里取数（Playwright evaluate）
     └─ build_report.py       清洗 + 对数 + 写 Excel
```

依赖：Playwright MCP；Python `openpyxl`、`pyyaml`。登录由使用者在浏览器里手动完成，skill 不保存账号密码或 token。
