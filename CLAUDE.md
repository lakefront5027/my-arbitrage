# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LOF套利雷达 is a browser-based real-time arbitrage monitoring tool for Chinese QDII Listed Open-ended Funds (LOFs). It detects premium/discount arbitrage opportunities by comparing real-time market prices against estimated intraday NAVs derived from benchmark index movements.

## Running the Application

No build step required — open HTML files directly in a browser:

- **Real-time monitoring**: Open `lof_arb_monitor_v33.html`
- **Backtesting**: Open `lof_backtest_v33.html`

No package.json, no npm, no compilation.

## Architecture

### Core Valuation Formula

```
Intraday NAV = Official NAV (T-1) × (1 + Benchmark Daily Change%)
Premium/Discount % = (Market Price − Intraday NAV) / Intraday NAV × 100%
```

### Data Sources (all via JSONP or fetch)

| Source | Purpose |
|--------|---------|
| `qt.gtimg.cn` | Real-time fund prices + 40+ benchmark indices (primary) |
| `fundgz.1234567.com.cn/js/{code}.js` | Official T-1 NAV (primary) |
| `pingzhongdata.com` | T-1 NAV fallback |
| `push2.eastmoney.com/api/qt/stock/get` | Mainland index fallback |

NAV sources form a fallback chain: `fundgz → pingzhongdata → lsjz historical`. Similarly, some HK indices fall back: `hkHSMI → hkHSSI → hkHSI`.

### Fund-to-Benchmark Mapping

47 QDII LOF funds (4 categories: US Equities, Commodities, Hong Kong, A-Share Sector) are each mapped to one or more benchmark indices in `lof_arb_monitor_v33.html`. Some funds use **composite benchmarks** with weighted averages (e.g., 80% QQQ + 10% HSTECH + 10% Bond index). This mapping table is hardcoded and is the most critical domain-specific data in the codebase.

### State Management

- `rows[]` — array holding per-fund state (price, NAV, premium%, signals)
- `idxChg{}` — cache of index daily change values, keyed by index symbol
- UI updates are triggered after all parallel fetches resolve

### Async Pattern

All API calls use `Promise.all()` with JSONP dynamic script injection. Each request has a 6–8 second timeout. Failed sources are silently skipped (graceful degradation).

## Key Files

| File | Role |
|------|------|
| `index.html` | 主监控页面（前端，自包含） |
| `worker-full.js` | Cloudflare Worker（计算引擎 + HTTP 代理） |
| `data/fund_daily.json` | 每日数据文件（NAV / 持仓 / 汇率 / drift 历史），由 Action 维护 |
| `.github/scripts/sync_fund_data.py` | GitHub Action 同步脚本 |
| `.github/workflows/daily-data-sync.yml` | Action 工作流（00:05 北京时间触发） |

## Storage Architecture & KV Policy

### Three-Layer Architecture

```
GitHub Actions (后勤层)          Worker (计算层)           index.html (展示层)
─────────────────────           ──────────────           ──────────────────
sync_fund_data.py               worker-full.js            browser
  • 抓取 NAV / 持仓 / 汇率          • 纯计算引擎                • 读 Worker snapshot
  • 计算 drift & 嵌入 history       • 读 fund_daily.json        • 本地直连备用
  • git commit → data/*.json       • 不写任何持久化存储
```

### Source of Truth（唯一事实来源）

**GitHub 仓库内的 JSON 文件**，与代码共版本管理：

| 文件 | 内容 |
|------|------|
| `data/fund_daily.json` | 净值、持仓、汇率、drift 历史（30日列式数组）、drift_5d 修正因子 |

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

- The version suffix (e.g., `v33`) appears in all filenames; increment consistently when releasing a new version.
- Fund codes, benchmark mappings, fee structures, and subscription quotas are all hardcoded in the HTML. There is no external config file — updates require editing the HTML directly.
- JSONP callbacks use dynamically generated function names to avoid collisions across parallel requests.
- The app is designed for Safari/Chrome on macOS; no mobile layout.
