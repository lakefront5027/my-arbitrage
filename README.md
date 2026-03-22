# LOF 套利雷达

实时监控 47 只 QDII LOF 基金的溢折价套利机会。通过对比场内市价与基准指数推算的盘中估值，捕捉可交易的价差窗口。

**线上地址**：[my-arbitrage.pages.dev](https://my-arbitrage.pages.dev)

---

## 快速上手

无需构建，直接浏览器打开：

| 文件 | 用途 |
|------|------|
| `index.html` | 实时监控主界面 |
| `lof_backtest_v33.html` | 历史回测 |

---

## 核心估值公式

```
盘中估值 = T-1 官方净值 × (1 + 基准指数当日涨幅%)
溢折价率 = (场内价格 − 盘中估值) / 盘中估值 × 100%
```

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Action（后勤层）                  每日 00:05 北京时间    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ sync_fund_data.py                                        │   │
│  │  1. 抓取 47 只基金 T-1 净值（fundgz → lsjz → pingzhong）│   │
│  │  2. 抓取前十大持仓（东方财富 jjcc）                      │   │
│  │  3. 持仓审计：偏移 >5% → GitHub Issue + 企业微信预警     │   │
│  │  4. 写入 data/fund_daily.json 并 git commit              │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │ fund_daily.json（静态 JSON，CF Pages 托管）
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Cloudflare Worker（计算层）                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ worker-full.js                                           │   │
│  │  • 读取 fund_daily.json（30min 缓存）                    │   │
│  │  • 实时抓取：腾讯行情 + 东方财富指数 + 新浪汇率          │   │
│  │  • 合成：盘中估值 = officialNav × (1 + benchChg%)        │   │
│  │  • 输出：/api/snapshot（聚合快照）/api/daily（JSON透传） │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │ /api/snapshot（JSON）
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Cloudflare Pages（展示层）                                      │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ index.html                                               │   │
│  │  主路：Worker /api/snapshot → 直接渲染                   │   │
│  │  备路：本地直连腾讯/东方财富/新浪（Worker 不可用时）     │   │
│  │  NAV主路：fund_daily.json → _navCache                    │   │
│  │  NAV应急：fundgz JSONP 补位（Action 数据残缺时触发）     │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 开发者架构守则 v6.1

### 🛡️ 核心职责分层

| 层级 | 角色 | 职责边界 |
|------|------|----------|
| **Action（后勤层）** | 法定数据水源 | 负责净值、持仓等**重量级/静态数据**的定时抓取与审计。`fund_daily.json` 是唯一净值权威来源。 |
| **Worker（计算层）** | 实时合成引擎 | 负责指数/汇率等**热数据**的实时抓取，结合 Action 提供的静态基准进行估值计算。**不抓取基金净值**。 |
| **Page（展示层）** | 只读终端 + 应急补位 | 负责 UI 展现。拥有**【紧急避险抓取权】**：在 Action 数据残缺时，允许使用轻量方案（如 fundgz JSONP）临时补位，确保可用性。 |

### 🛡️ 开发红线

**单向优先原则**
> 严禁将「临时补位逻辑」常态化。`runFgzFallback()` 是应急措施，不是正常流程。
> 若净值大面积缺失，**必须在 Action 层（`sync_fund_data.py`）排查并修复根因**，而不是扩大前端补偿范围。

**禁止越位**
> 严禁在 Worker 中集成重量级 HTML 解析逻辑或大规模基金净值抓取。
> Worker 只做：读 JSON + 抓指数 + 做数学。

**单一水源**
> `fund_daily.json` 是净值的唯一主权威。Worker 从它读，前端从它读。
> 任何净值数据质量问题，溯源至 `sync_fund_data.py`，而不是在下游打补丁。

### 数据流单向图

```
Action 写入 → fund_daily.json → Worker 读取 → /api/snapshot → Page 渲染
                     ↓
              Page 直读（fallback）
                     ↓
              fundgz JSONP（应急，仅残缺时）
```

---

## 关键文件

| 文件 | 说明 |
|------|------|
| `data/fund_daily.json` | 47 只基金的净值 + 持仓（Action 每日写入） |
| `index.html` | 监控主界面（自包含，无构建） |
| `worker-full.js` | Cloudflare Worker（计算层） |
| `.github/scripts/sync_fund_data.py` | 数据同步脚本（后勤层） |
| `.github/workflows/daily-data-sync.yml` | 定时触发（UTC 16:05 = 北京 00:05） |
| `.github/workflows/deploy.yml` | 推送自动部署 Pages + Worker |

---

## 数据源

| 来源 | 用途 | 层级 |
|------|------|------|
| `fundgz.1234567.com.cn` | T-1 净值（主路） | Action |
| `api.fund.eastmoney.com/f10/lsjz` | T-1 净值（备路） | Action |
| `fund.eastmoney.com/pingzhongdata` | T-1 净值（第三路） | Action |
| `api.fund.eastmoney.com/f10/jjcc` | 前十大持仓 | Action |
| `qt.gtimg.cn` | 实时价格 + 40+ 指数 | Worker / Page |
| `push2.eastmoney.com` | 大陆指数（备） | Worker / Page |
| `query1.finance.yahoo.com` | CME 期货实时（NQ=F / ES=F）| Worker |
| `hq.sinajs.cn` | 实时汇率（USD/CNH、HKD/CNH）| Worker / Page |
| `fundgz.1234567.com.cn` | 净值应急补位 JSONP | Page（应急） |

---

## 环境变量 / Secrets

| 名称 | 位置 | 用途 |
|------|------|------|
| `CF_API_TOKEN` | GitHub Secrets | Cloudflare 部署 |
| `CF_ACCOUNT_ID` | GitHub Secrets | Cloudflare 账号 |
| `WX_KEY` | GitHub Secrets | 企业微信 Webhook（持仓漂移预警） |
| `FUND_DAILY_URL` | wrangler.toml `[vars]` | 覆盖 Worker 读取的 JSON 地址 |

---

## 估值计算逻辑（完整）

### 完整计算链

```
Step 1  基准净值选取
        base = est_nav_yesterday        （T-2 滞后且链式条件满足时）
             | officialNav(T-1)         （正常情况）
             | prevClose                （NAV 完全缺失时降级）

Step 2  盘中动态估值收益率
        dynNavReturn = calcDynamicNavReturn()   （持仓加权 + bench 残差，含 FX 修正）

Step 3  Drift 偏差修正因子
        alpha = clamp(drift_5d, −2%, +2%)       （Drift Active 时）
              | 0                               （Drift Offline 时，见下文）

Step 4  当日估算净值
        nav = base × (1 + dynNavReturn / 100) × (1 + alpha)

Step 5  溢折价率
        premium% = (price − nav) / nav × 100
```

---

### 基准涨跌幅计算

**单一基准**

```
benchChg = idxChg[benchCode]
```

**复合基准**（缺失分量不计入分母，避免低估）

```
benchChg = Σ(idxChg[i] × w[i]) / Σ(w[i] for available i)
```

**典型复合基准**

| 基金 | 复合基准配置 | 说明 |
|------|------------|------|
| 501312 海外科技 | QQQ×0.8 + HSTECH×0.1 + A股×0.1 | 混合美港A |
| 160644 港美互联网 | QQQ×0.5 + HSTECH×0.5 | 港美各半 |
| 163208 全球油气 | XLE×0.5 + HSCEI×0.5 | 美油气+港能源 |
| 164906 中概互联网 | HSTECH | 持仓为港股中概，非 KWEB |

---

### 汇率修正

```
fxChgUsd = (usd_cnh_live / usd_cnh_t1 − 1) × 100
fxChgHkd = (hkd_cnh_live / hkd_cnh_t1 − 1) × 100
```

对每个 bench 分量**乘法叠加**（而非加法）：

```
adj_return = (1 + bench_chg/100) × (1 + fx_chg/100) − 1
```

- `us*` 代码 → 叠加 USD/CNH 涨跌
- `hk*` 代码 → 叠加 HKD/CNH 涨跌
- `sh/sz/csi/sina*` → 无 FX（CNY 计价）

`fxAdj = adjBenchChg − benchChg`（弹窗展示汇率净贡献，不参与最终计算）

---

### 动态持仓加权估值

有持仓数据时，用个股实时价格替代纯指数估值：

```
for holding in holdings:
    if tq.startswith('us'): skip  # 美股 A 股时段无盘中价，归入残差
    if chg == null: skip           # 无价格，归入残差
    fx = fxChgHkd if tq.startswith('hk') else 0
    coveredReturn += ((1 + chg/100) × (1 + fx/100) − 1) × w
    coveredW      += w

# 残差（US持仓 + 无价格持仓 + 未披露持仓）用 bench 填补
dynNavReturn = (coveredReturn + adjBenchChg/100 × (1 − coveredW)) × 100
```

`holdingCoverage = coveredW / totalW`（覆盖率越高估值越精准）

---

### T-2 链式净值补偿

部分 QDII 基金官方净值固定滞后一日（T-2），需两步链式推算：

```
# Action 每日凌晨计算并写入 fund_daily.json
est_nav_yesterday = officialNav(T-2) × (1 + T-1_bench_chg%)

# 前端/Worker 用链式基准
nav = est_nav_yesterday × (1 + today_bench%)
    = officialNav(T-2) × (1 + T-1_bench%) × (1 + today_bench%)   ← 正确
```

**触发条件（三重，缺一不可）**

```
useChained = estNavYesterday != null      # Action 已积累历史数据
          && navLag >= 2                  # nav_date 滞后 ≥ 2 自然日
          && fetchAgeH <= 36              # nav_fetch_time 距今 ≤ 36h
```

UI：正常显示白色 officialNav；链式补偿时显示黄色 estNavYesterday + `est` 标注。

---

### Drift 偏差校准

**离线计算（GitHub Action 每日）**

```
est_nav(t) = nav(t-1) × (1 + bench_chg(t) / 100)
drift(t)   = (nav(t) − est_nav(t)) / est_nav(t)
drift_5d   = mean(drift 最近5个有效值)
```

**Hard Enforcement — 宁可无补偿，不可乱补偿**

```
driftActive = (drift5d ≠ 0)
           && (drift_n ≥ 3)          # 至少3个交易日样本
           && (driftLagDays ≤ 2)     # drift_computed_at 距今 ≤ 2 天

alpha = driftActive ? clamp(drift_5d, −2%, +2%) : 0
```

UI：`[Drift Calibrated]`（绿色）或 `[Drift Offline]`（灰色）标注于溢折价下方。

---

### 溢折价报警

默认阈值 1.5%（前端可调）：

| 溢折价绝对值 | 状态 | 显示 |
|------------|------|------|
| ≥ 阈值 | 报警 | 闪烁，行背景高亮，微信推送 |
| ≥ 0.65 × 阈值 | 观察 | `⊙ 观察` |
| < 0.65 × 阈值 | 正常 | — |

Worker 内置状态机防重复推送（模块级 `_alertState{}`）。

---

## 回测逻辑

> 回测文件：`Archive/lof_backtest_v33.html`（归档，不在主部署中）

**核心逻辑与实盘一致**，对历史区间每个交易日模拟：

```
est_nav(t) = officialNav(t-1) × (1 + bench_chg(t))
premium(t) = (close_price(t) − est_nav(t)) / est_nav(t) × 100

signal = "sell" if premium(t) > threshold   → 卖场内 + 申场外
       | "buy"  if premium(t) < -threshold  → 买场内 + 赎场外
       | "hold"
```

回测指标：累计收益率（价差）、最大溢/折幅、信号频率、平均持仓天数（含 T+2 赎回延迟）。

---

## 已知局限与改进方向

| 局限 | 原因 | 备注 |
|------|------|------|
| 美股持仓 A 股时段无价格 | 美股完全休市 | CME 期货已覆盖指数级别，个股无解 |
| Drift 历史需积累 3+ 日才生效 | 首次部署历史为空 | 等 Action 自然积累 |
| 持仓为季报，最多滞后 3 个月 | 信息披露规定 | 已标注 `holdings_date` |
| navLag > 3 时无估值 | 数据源故障 | 3-strike 机制触发红叉报警 |
| HK 孪生股覆盖率极低 | 仅 TME→01698.HK | 当前基金池不可行 |
