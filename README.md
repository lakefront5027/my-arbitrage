# LOF 套利雷达

A cloud-native real-time arbitrage monitoring system for Chinese QDII Listed Open-ended Funds (LOFs). It detects premium/discount arbitrage opportunities by comparing real-time market prices against intraday NAVs estimated from live benchmark indices.

实时监控 47 只 QDII LOF 基金的溢折价套利机会。

**线上地址**：[my-arbitrage.pages.dev](https://my-arbitrage.pages.dev)

---

## UI 说明

### 表格字段

| 列 | 含义 |
|----|------|
| T-1净值 | 白色：T-1 官方净值；黄色 + `est`：链式估算补偿（T-2 滞后基金） |
| 溢折价 | 场内价 vs 盘中估值的偏差率 |
| `[Drift Calibrated]`（绿色） | Drift 偏差修正已启用（样本 ≥3 且数据 ≤2 天） |
| `[Drift Offline]`（灰色） | 修正数据不足或已过期，alpha = 0 |

### 报警颜色

| 状态 | 触发条件 | 行为 |
|------|---------|------|
| 报警 | 溢折价绝对值 ≥ 阈值（默认 1.5%） | 行高亮 + 闪烁 + 企业微信推送 |
| 观察 | 溢折价 ≥ 0.65 × 阈值 | `⊙ 观察` 标注 |
| 正常 | 溢折价 < 0.65 × 阈值 | — |

---

## GitHub Actions 运行频率

| 工作流 | 触发时间 |
|--------|---------|
| `daily-data-sync.yml` | 每日 UTC 16:05（北京时间 00:05），仅交易日 |
| `deploy.yml` | 每次推送到 main 分支 |

> 完整架构说明、估值算法、开发规范见 `CLAUDE.md`。

<img width="1437" height="769" alt="Screenshot 2026-03-22 at 22 17 55" src="https://github.com/user-attachments/assets/45985ae8-92ef-4ef8-b12c-904ab812507a" />
