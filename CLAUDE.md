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

## Important Conventions

- Fund codes, benchmark mappings, fee structures, and subscription quotas are hardcoded in `worker-full.js` (`FUNDS` / `BENCH` constants) and mirrored in `index.html` for the fallback path. Updates require editing both files.
- The降级 fallback path in `index.html` must stay logically identical to `worker-full.js`. Any calculation change must be applied to both.
- BENCH composite entries use `[{tq, w}, ...]` format; missing components are excluded from the denominator (not treated as zero).
- CME futures (NQ=F → usQQQ/usIXIC/usXLK/usSMH, ES=F → usINX) override Tencent T-1 close prices during A-share trading hours. This is Worker-side only (server-to-server, no CORS issue).
- HK index fallback chain: `hkHSMI → hkHSSI → hkHSI`, `hkHSSI → hkHSI`, `hkHSCI → hkHSI`.
