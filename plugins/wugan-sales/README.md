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

取数条件：支付时间、整月、其余为销售汇总页默认值。业务规则（规格保留、改名、改分类、1 元计价商品、异常剔除）全部在 `rules.yaml`。单笔 ≥ 1 万的消费会先停下来让人确认。

## 每月产出

- `年度汇总`（第一页）：经营指标（订单、客单、新老客、储值、营收与现金流入）+ 分类 × 月份的销售额 / 销量矩阵
- `月度经营数据`：每月一行的经营明细，已剔除 rules.yaml 里确认的异常
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

依赖：支持页面脚本执行的 Playwright MCP；Python `openpyxl`、`pyyaml`。登录由使用者在浏览器里手动完成，skill 不保存账号密码或 token。只读 DOM 浏览器工具不能代替接口取数。

当前版本 **0.2.0**，变更见 [CHANGELOG.md](CHANGELOG.md)。默认年度文件目录为 `~/Desktop/Ellen/五感专题分析`，可在技能的 `config.json` 配置或用 `--dir` / `--workbook` 覆盖。目录/文件缺失会停止，新建年度文件须确认后加 `--create`。

运行前先用 Playwright 执行 `scripts/browser_probe.js` 并保存结果，再运行 `scripts/preflight.py --month YYYY-MM --browser-check <probe.json>`（路径相对技能目录）。完整操作和异常确认流程见 `skills/wugan-sales-monthly/SKILL.md`。

报表会校验整月数据、分页完整性和余额对数，先写临时文件回读验证，再备份并原子替换。每次发布在 `_backup/` 保存版本与文件哈希的审计记录。开发验证：

```bash
python -I skills/wugan-sales-monthly/scripts/test_reliability.py
node skills/wugan-sales-monthly/scripts/test_fetch.mjs
```
