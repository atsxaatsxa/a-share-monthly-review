# A股月度持仓复盘 (A-share Monthly Position Review)

一个面向 A 股组合持仓的**月度复盘**命令行工具：给定复盘月份，自动拉取行情、计算个股收益、沪深300 基准与超额收益，区分贡献 / 拖累标的，导出 CSV/JSON 月报，并可自动发送邮件。

> 核心红线：**严禁下标顺序匹配** —— 所有行情一律以「标的名称(str)」为 dict key 绑定，任何读取都通过 key 取数，绝不依赖列表下标或循环顺序，从根本上杜绝顺序错位。

## 功能

- 指定复盘月份（命令行参数 `YYYY-MM`）
- 拉取各标的与沪深300基准当月日线（前复权 qfq）
- 计算个股月收益、基准收益、超额收益，区分 **贡献 / 拖累 / 持平**
- 控制台输出月报 + 导出 `review_report_YYYY-MM.csv` 与 `.json`
- 自动发送月报邮件至 `xuyutom@qq.com`（未配置 SMTP 则告警跳过，不阻断）
- 接口异常 / 停牌缺行情容错：缺失 **不编造、不静默丢弃**，统一标注「需人工复核」

## 默认标的

| 标的 | 代码 | 标的 | 代码 |
|---|---|---|---|
| 五粮液 | 000858 | 中国中免 | 601888 |
| 万华化学 | 600309 | 兆易创新 | 603986 |
| 立讯精密 | 002475 | 京沪高铁 | 601816 |
| 基准 | 沪深300 (000300) | | |

> 调整标的只需修改 `monthly_review.py` 顶部的 `STOCKS` 字典。

## 安装

```bash
pip install -r requirements.txt
# 若默认源不可达：
pip install -i https://pypi.org/simple --trusted-host pypi.org -r requirements.txt
```

## 用法

```bash
# 复盘指定月份（必填，格式 YYYY-MM；缺省/非法直接退出，绝不输出复盘）
python monthly_review.py 2024-01

# 自动发邮件需配置 SMTP 环境变量（缺失则告警跳过，不阻断）
export REVIEW_SMTP_HOST=smtp.qq.com
export REVIEW_SMTP_PORT=465
export REVIEW_SMTP_USER=你的邮箱@qq.com
export REVIEW_SMTP_PASS=授权码
```

## 数据源

- 优先 **akshare**（东财 push2 接口 `stock_zh_a_hist` / `index_zh_a_hist`）。
- 东财接口对出口 IP 限流时，自动降级 **新浪** 接口（`stock_zh_a_daily` / `stock_zh_index_daily`）兜底，仍全程走 akshare。

## 目录结构

```
.
├── monthly_review.py          # 主脚本
├── requirements.txt
├── README.md
├── CHANGELOG.md
├── LICENSE
├── docs/
│   └── 交付文档.md             # 接口调用说明 + 可运行示例 + 自查评估报告
└── data/
    ├── 2024-01/               # 已跑出的月度复盘结果
    ├── 2026-09/
    └── 2026-10/
```

## 红线自检（摘要）

| 维度 | 结果 |
|---|---|
| 输入自证 / 守恒校验 / 计算溯源 | 通过 |
| 匹配鲁棒（核心，打乱不串位） | 通过 |
| 双向异常检测 / 人工友好 / 可复现 | 通过 |
| 一票否决（下标匹配 / 编造 / 静默丢 / 错参仍输出） | 全部 PASS |

详细评估见 `docs/交付文档.md`。

---

*本工具为技术开发产物，仅做历史数据计算与展示，不构成任何投资建议。*
