# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LOF套利雷达 is a **cloud-native** real-time arbitrage monitoring system for Chinese QDII Listed Open-ended Funds (LOFs). It detects premium/discount arbitrage opportunities by comparing real-time market prices against intraday NAVs estimated from live benchmark indices.

The system runs entirely on Cloudflare infrastructure (Worker + Pages), backed by a GitHub Actions data pipeline. **There is no traditional server.** The Cloudflare Worker is the backend logic engine; index.html is a thin display client that consumes the Worker's output.

---

## Three-Tier Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 1 — Data Layer (GitHub Actions)                               │
│  触发：每日 00:05 北京时间 (UTC 16:05)，交易日                        │
│                                                                     │
│  sync_fund_data.py                                                  │
│    • 抓取 47 只基金 T-1 净值（fundgz + lsjz 双路，pingzhong 兜底）    │
│    • 抓取前十大持仓（东方财富 jjcc API）                               │
│    • 抓取 FX 结算汇率（Sina）                                         │
│    • 计算 drift 历史（30日滚动）及 drift_5d 修正因子                   │
│    • 写入 data/fund_daily.json → git commit → git push               │
│    • 连续失败 ≥3 次 → RuntimeError，Actions 红叉强制报警               │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  data/fund_daily.json
                            │  (Cloudflare Pages 静态托管，版本控制)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 2 — Logic Layer (Cloudflare Worker: worker-full.js)           │
│  触发：每分钟 cron + 前端 HTTP 请求                                   │
│                                                                     │
│  核心职责（逻辑中枢）：                                               │
│    • 实时抓取：腾讯行情 / 东方财富指数 / 新浪汇率 / Yahoo CME期货       │
│    • 读取 fund_daily.json（30分钟内存缓存）                           │
│    • 执行全量估值计算：bench加权 → FX修正 → 持仓动态 → T-2链式 → Drift │
│    • 强制时间戳校验（staleness_policy）                               │
│    • 溢价报警状态机（_alertState，防重推）                             │
│    • 输出 /api/snapshot（聚合快照 JSON）                              │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  /api/snapshot（干净数据，已完成所有计算）
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 3 — Presentation Layer (index.html)                           │
│  纯展示客户端，Vanilla JS，无框架，无构建                              │
│                                                                     │
│  主路径（正常）：                                                     │
│    Worker /api/snapshot → 直接渲染，无本地计算                        │
│                                                                     │
│  降级路径（Worker 不可达时）：                                         │
│    本地直连腾讯/东方财富/新浪 + 读 fund_daily.json → 本地重算           │
│    注意：降级路径是应急措施，不是正常流程，逻辑与 Worker 保持一致        │
└─────────────────────────────────────────────────────────────────────┘
```

**主路径下，index.html 不做任何估值计算**。它只消费 Worker 喂回的干净数据（premium%、nav、driftStatus 等已计算完毕的字段）并渲染。

---

## Layer Responsibilities & Development Constraints

### 核心职责分层

| 层级 | 角色 | 职责边界 |
|------|------|----------|
| **Action（数据层）** | 法定数据水源 | 负责净值、持仓等**重量级/静态数据**的定时抓取与审计。`fund_daily.json` 是唯一净值权威来源。 |
| **Worker（逻辑层）** | 实时合成引擎 | 负责指数/汇率等**热数据**的实时抓取，结合 Action 提供的静态基准进行估值计算。**不抓取基金净值**。 |
| **Page（展示层）** | 只读终端 + 应急补位 | 负责 UI 展现。拥有**【紧急避险抓取权】**：仅在 Action 数据残缺时，允许用 fundgz JSONP 临时补位，不可常态化。 |

### 开发红线

**单向优先原则**
> 严禁将「临时补位逻辑」常态化。`runFgzFallback()` 是应急措施，不是正常流程。
> 若净值大面积缺失，**必须在 Action 层（`sync_fund_data.py`）排查并修复根因**，而不是扩大前端补偿范围。

**禁止越位**
> 严禁在 Worker 中集成重量级 HTML 解析逻辑或大规模基金净值抓取。
> Worker 只做：读 JSON + 抓指数 + 做数学。

**单一水源**
> `fund_daily.json` 是净值的唯一主权威。Worker 从它读，前端从它读。
> 任何净值数据质量问题，溯源至 `sync_fund_data.py`，而不是在下游打补丁。

### 数据流（严格单向）

```
Action 写入 → fund_daily.json → Worker 读取 → /api/snapshot → Page 渲染
                     ↓
              Page 直读（fallback，仅 Worker 不可达时）
                     ↓
              fundgz JSONP（应急，仅 Action 数据残缺时）
```

---

## Key Files

| File | Role |
|------|------|
| `worker-full.js` | **逻辑中枢**：实时行情抓取 + 全量估值计算 + 报警，是系统的后端 |
| `index.html` | 展示层：消费 Worker snapshot，降级时本地重算（逻辑与 Worker 一致） |
| `data/fund_daily.json` | **唯一活数据来源**：NAV / 持仓 / 汇率 / drift 历史，Action 每日写入 |
| `data/fund_manifest.json` | **数据契约**：定义 fund_daily.json 的 schema、字段规范和 staleness_policy |
| `.github/scripts/sync_fund_data.py` | 数据层同步脚本，唯一有权写入 fund_daily.json 的程序 |
| `.github/workflows/daily-data-sync.yml` | Action 工作流（UTC 16:05 定时触发） |
| `.github/workflows/deploy.yml` | 推送时自动部署 Pages + Worker |
| `wrangler.toml` | Worker 配置：name=lof-arb-radar, crons, observability |

---

## Data Contract

`data/fund_manifest.json` 定义了 `fund_daily.json` 必须遵守的 schema 和时效策略：

```json
"staleness_policy": {
  "nav_max_lag_days": 3,
  "chain_estimation_allowed_lag_days": 2,
  "chain_estimation_requires_fresh_fetch_within_hours": 36,
  "drift_max_lag_days": 2,
  "drift_min_n": 3
}
```

Worker 在执行任何计算前，**必须**先对照此策略校验数据时效。

---

## Storage Architecture & KV Policy

### Source of Truth（唯一活数据来源）

**`data/fund_daily.json`**，与代码共版本管理，由 GitHub Actions 独占写入权。

`fund_manifest.json` 定义 schema 契约（静态，人工维护），`fund_daily.json` 是运行时数据（动态，Action 写入）。

### Cloudflare KV 使用规范（强制）

**定位**：KV 仅作为 Transient Cache（瞬时缓存），**不是** Source of Truth。

**禁止写入 KV 的内容：**
- `drift_history` 或任何历史序列
- 每日估值快照
- 任何可通过 Action + `git push` 持久化的数据

**允许写入 KV 的内容（极少数场景）：**
- 跨 isolate 的毫秒级实时状态（如报警跃迁去重）
- 但应首选模块级内存变量（`_alertState`），KV 仅在跨 isolate 去重确实必要时启用

**Action 规范：**
- 同步数据 → 写 `data/*.json` → `git add` → `git push`
- 严禁在 Action 中调用任何 KV 接口

> 背景：2026-03 KV 写入配额达 90% 预警，根因是误将 drift 历史序列写入 KV。
> 修复方案：将 30 日 drift 历史嵌入 `fund_daily.json` 每基金对象的 `history` 字段。
> 此后 Worker 对 KV 的依赖已**完全移除**。

---

## 数据质量与时间戳规范（技术宪法）

本节是项目的最高技术原则。所有新功能开发、Bug 修复、数据管道改动，必须以此为准。违反任一条款视为引入数据质量缺陷。

---

### 一、数据原子结构（强制三元组）

所有 fetch 接口返回值与 JSON 持久化字段，**必须**采用以下三元组结构，严禁只存数值：

```
{ value, date, sync_at }
```

| 字段 | 含义 | 示例 |
|------|------|------|
| `value` | 数值本体 | `3.7564` |
| `date` | 数值所属的业务日期（交易日） | `"2026-03-20"` |
| `sync_at` | 本次成功抓取/计算的 UTC 时间戳 | `"2026-03-23T01:15:00Z"` |

各核心对象对应的三元组字段名：

| 对象 | value 字段 | date 字段 | sync_at 字段 |
|------|-----------|-----------|-------------|
| NAV | `nav` | `nav_date` | `nav_fetch_time` |
| 估算净值 | `est_nav` / `est_nav_yesterday` | `est_nav_date` | `drift_computed_at`（Action 计算时刻）|
| 持仓 | `holdings[]` | `holdings_date` | `holdings_fetch_time` |
| Drift 修正因子 | `drift_5d` | _(由 history 窗口隐含)_ | `drift_computed_at` |
| 汇率 T-1 结算 | `usd_cnh_t1` / `hkd_cnh_t1` | `_fx.date` | _(sync_time 隐含)_ |

---

### 二、全量计算准入规则（计算前必须校验）

#### 2.1 实时估值（`raw_est = base_nav × (1 + bench_chg%)`）

| 前置条件 | 校验方式 | 失败处理 |
|---------|---------|---------|
| `nav_fetch_time` 距今 ≤ 36 小时 | `fetchAgeH ≤ 36` | 禁止计算，`nav = null` |
| `navLag`（nav_date 距今自然日）≤ 3 | `navLag ≤ 3` OR 开启链式补偿 | `navLag ≥ 2` 时开启 T-2 链式估值 |
| T-2 链式估值额外前提 | `estNavYesterday != null` AND `fetchAgeH ≤ 36` | 不满足则回退至 `officialNav` |

#### 2.2 Drift 修正（`adjusted_est = raw_est × (1 + drift_5d)`）

**三重前置条件，缺一不可：**

```
drift_active = (drift5d ≠ 0) AND (drift_n ≥ 3) AND (driftLagDays ≤ 2)
```

| 前置条件 | 含义 |
|---------|------|
| `drift5d ≠ 0` | 存在有效修正值 |
| `drift_n ≥ 3` | 样本量充足（最少 3 个交易日） |
| `driftLagDays ≤ 2` | `drift_computed_at` 距今 ≤ 2 天（数据未过期）|

**不满足任一条件 → `alpha = 0`（禁用补偿，回归原始估值）**

原则：**宁可无补偿，不可乱补偿。**

#### 2.3 溢价报警（UI 警报触发）

若底层任一数据戳超过 `staleness_policy` 阈值，**必须**强制锁定为 Unavailable：

```
staleness_policy:
  nav_max_lag_days: 3                               # nav_date 距今超过3天 → 估值不可用
  chain_estimation_required_fetch_within_hours: 36  # nav_fetch_time 超过36h → 禁止链式补偿
  drift_max_lag_days: 2                             # drift_computed_at 超过2天 → Drift Offline
  drift_min_n: 3                                    # 有效样本 < 3 → Drift Offline
```

UI 状态映射：

| 数据状态 | 溢价显示 | Drift 标注 |
|---------|---------|-----------|
| 全部新鲜，drift 有效 | 正常溢折价率 | `[Drift Calibrated]`（绿色） |
| 全部新鲜，drift 过期/不足 | 正常溢折价率 | `[Drift Offline]`（灰色） |
| nav_fetch_time > 36h | `Unavailable` | — |
| navLag > 3 且无链式补偿 | `Unavailable` | — |

---

### 三、开发禁令

> ❌ **严禁**在代码中使用任何不带日期校验的原始数值进行数学运算。

具体禁止行为：

```js
// ❌ 禁止：直接取值计算，不校验日期
const nav = fund.nav;
const est = nav * (1 + benchChg / 100);

// ✅ 正确：先校验 fetch_time 和 nav_date，再计算
const fetchAgeH = navFetchTime
  ? (Date.now() - new Date(navFetchTime).getTime()) / 3600000 : 999;
if (fetchAgeH > 36) { nav = null; /* 不计算 */ }
```

```python
# ❌ 禁止：不保存日期，只更新数值
fund['nav'] = new_value

# ✅ 正确：原子写入三元组
fund['nav']            = new_value
fund['nav_date']       = nav_date
fund['nav_fetch_time'] = now_utc
```

**附加禁令：**
- 严禁在 Action 中写入任何 KV 接口
- 严禁将历史序列（drift_history 等）写入 KV
- 严禁在 sync_fund_data.py 中用旧数据"假装成功"（fetch 失败必须递增 `nav_consecutive_fails`，≥3 次必须抛出 RuntimeError）

---

### 四、staleness_policy 是唯一阈值来源

所有超时判断的**数字常量**必须从 `staleness_policy`（`fund_manifest.json`）或等效的集中常量读取，禁止在计算代码中硬编码魔法数字（如直接写 `36`、`3`、`2`）。

Worker 和 index.html 当前直接使用字面量（历史遗留），后续重构时应统一提取为：

```js
const STALENESS = {
  NAV_FETCH_MAX_AGE_H:  36,
  NAV_MAX_LAG_DAYS:      3,
  DRIFT_MAX_LAG_DAYS:    2,
  DRIFT_MIN_N:           3,
};
```

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
              | 0                               （Drift Offline 时）

Step 4  当日估算净值
        nav = base × (1 + dynNavReturn / 100) × (1 + alpha)

Step 5  溢折价率
        premium% = (price − nav) / nav × 100
```

### 基准涨跌幅计算

单一基准：`benchChg = idxChg[benchCode]`

复合基准（缺失分量不计入分母，避免低估）：
```
benchChg = Σ(idxChg[i] × w[i]) / Σ(w[i] for available i)
```

典型复合基准：

| 基金 | 复合基准配置 | 说明 |
|------|------------|------|
| 501312 海外科技 | QQQ×0.8 + HSTECH×0.1 + A股×0.1 | 混合美港A |
| 160644 港美互联网 | QQQ×0.5 + HSTECH×0.5 | 港美各半 |
| 163208 全球油气 | XLE×0.5 + HSCEI×0.5 | 美油气+港能源 |
| 164906 中概互联网 | HSTECH | 持仓为港股中概，非 KWEB |

### 汇率修正

```
fxChgUsd = (usd_cnh_live / usd_cnh_t1 − 1) × 100
fxChgHkd = (hkd_cnh_live / hkd_cnh_t1 − 1) × 100

adj_return = (1 + bench_chg/100) × (1 + fx_chg/100) − 1   ← 乘法叠加，非加法
```

- `us*` → 叠加 USD/CNH；`hk*` → 叠加 HKD/CNH；`sh/sz/csi/sina*` → 无 FX

### 动态持仓加权估值

```
for holding in holdings:
    if tq.startswith('us'): skip  # 美股 A 股时段无盘中价，归入残差
    if chg == null: skip
    fx = fxChgHkd if tq.startswith('hk') else 0
    coveredReturn += ((1 + chg/100) × (1 + fx/100) − 1) × w
    coveredW      += w

dynNavReturn = (coveredReturn + adjBenchChg/100 × (1 − coveredW)) × 100
```

`holdingCoverage = coveredW / totalW`（港股基金通常 >80%，美股基金 = 0）

### T-2 链式净值补偿

```
# Action 每日凌晨写入
est_nav_yesterday = officialNav(T-2) × (1 + T-1_bench_chg%)

# Worker/前端用链式基准
nav = est_nav_yesterday × (1 + today_bench%)
    = officialNav(T-2) × (1 + T-1_bench%) × (1 + today_bench%)   ← 正确两步链
```

触发条件（三重，缺一不可）：
```
useChained = estNavYesterday != null && navLag >= 2 && fetchAgeH <= 36
```

UI：正常白色显示 officialNav；链式补偿时黄色显示 estNavYesterday + `est` 标注。

### Drift 偏差校准

离线计算（Action 每日）：
```
est_nav(t) = nav(t-1) × (1 + bench_chg(t) / 100)
drift(t)   = (nav(t) − est_nav(t)) / est_nav(t)
drift_5d   = mean(drift 最近5个有效值)
```

Hard Enforcement：
```
driftActive = (drift5d ≠ 0) && (drift_n ≥ 3) && (driftLagDays ≤ 2)
alpha = driftActive ? clamp(drift_5d, −2%, +2%) : 0
```

### 溢折价报警

默认阈值 1.5%（前端可调）：

| 溢折价绝对值 | 状态 | 显示 |
|------------|------|------|
| ≥ 阈值 | 报警 | 闪烁 + 背景高亮 + 微信推送 |
| ≥ 0.65 × 阈值 | 观察 | `⊙ 观察` |
| < 0.65 × 阈值 | 正常 | — |

Worker 内置状态机（`_alertState{}`）防重复推送。

### 回测逻辑

> 回测文件：`Archive/lof_backtest_v33.html`（归档，不在主部署中）

```
est_nav(t) = officialNav(t-1) × (1 + bench_chg(t))
premium(t) = (close_price(t) − est_nav(t)) / est_nav(t) × 100
signal = "sell" | "buy" | "hold"
```

回测指标：累计收益率、最大溢/折幅、信号频率、平均持仓天数（含 T+2 赎回延迟）。

---

## Data Sources

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

## Environment Variables / Secrets

| 名称 | 位置 | 用途 |
|------|------|------|
| `CF_API_TOKEN` | GitHub Secrets | Cloudflare 部署 |
| `CF_ACCOUNT_ID` | GitHub Secrets | Cloudflare 账号 |
| `WX_KEY` | GitHub Secrets | 企业微信 Webhook（持仓漂移预警） |
| `FUND_DAILY_URL` | wrangler.toml `[vars]` | 覆盖 Worker 读取的 JSON 地址 |

---

## Important Conventions

- Fund codes, benchmark mappings, fee structures, and subscription quotas are hardcoded in `worker-full.js` (`FUNDS` / `BENCH` constants) and mirrored in `index.html` for the fallback path. Updates require editing both files.
- The降级 fallback path in `index.html` must stay logically identical to `worker-full.js`. Any calculation change must be applied to both.
- BENCH composite entries use `[{tq, w}, ...]` format; missing components are excluded from the denominator (not treated as zero).
- CME futures (NQ=F → usQQQ/usIXIC/usXLK/usSMH, ES=F → usINX) override Tencent T-1 close prices during A-share trading hours. This is Worker-side only (server-to-server, no CORS issue).
- HK index fallback chain: `hkHSMI → hkHSSI → hkHSI`, `hkHSSI → hkHSI`, `hkHSCI → hkHSI`.

---

## Known Limitations

| 局限 | 原因 | 备注 |
|------|------|------|
| 美股持仓 A 股时段无价格 | 美股完全休市 | CME 期货已覆盖指数级别，个股无解 |
| Drift 历史需积累 3+ 日才生效 | 首次部署历史为空 | 等 Action 自然积累 |
| 持仓为季报，最多滞后 3 个月 | 信息披露规定 | 已标注 `holdings_date` |
| navLag > 3 时无估值 | 数据源故障 | 3-strike 机制触发红叉报警 |
| HK 孪生股覆盖率极低 | 仅 TME→01698.HK | 当前基金池不可行 |
