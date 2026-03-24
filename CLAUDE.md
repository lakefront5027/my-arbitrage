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
│  触发：每日 07:00 北京时间 (UTC 23:00)，交易日                        │
│  （美股收盘 3h+ 后运行，确保 USO/BNO/QQQ 等前一日收盘涨跌幅稳定可用）  │
│                                                                     │
│  sync_fund_data.py                                                  │
│    • 抓取 47 只基金 T-1 净值（fundgz + lsjz 双路，pingzhong 兜底）    │
│    • 抓取前十大持仓（东方财富 jjcc API）                               │
│    • 抓取 FX 结算汇率（Sina）                                         │
│    • 计算 drift 历史（30日滚动）及 drift_5d 修正因子                   │
│    • 写入 est_nav_yesterday 链式补偿锚点（update_chain_anchors）       │
│    • 写入 data/fund_daily.json → git commit → git push               │
│    • 连续失败 ≥3 次 → RuntimeError，Actions 红叉强制报警               │
│    • 接口失败自动重试 2 次（间隔 3s）                                  │
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
| `.github/scripts/sync_closing_idx.py` | 收盘快照同步脚本，写入 idx_closing.json |
| `.github/workflows/daily-data-sync.yml` | Action 工作流（UTC 23:00 = 北京 07:00 定时触发） |
| `.github/workflows/closing-data-sync.yml` | 收盘快照工作流（UTC 07:05 = 北京 15:05，交易日） |
| `.github/workflows/deploy.yml` | 推送时自动部署 Pages + Worker |
| `data/idx_closing.json` | 收盘指数快照：sz399961 / sz399979 / sinaAG0，Action 每日 15:05 写入 |
| `wrangler.toml` | Worker 配置：name=lof-arb-radar, crons, observability |

---

## Data Contract

`data/fund_manifest.json` 定义了 `fund_daily.json` 必须遵守的 schema 和时效策略：

```json
"staleness_policy": {
  "nav_max_lag_days": 3,
  "chain_estimation_allowed_lag_days": 2,
  "sync_max_lag_trading_days": 1,
  "drift_max_lag_days": 2,
  "drift_min_n": 3
}
```

所有阈值单位均为**交易日**（原 `chain_estimation_requires_fresh_fetch_within_hours: 36` 已废弃，替换为 `sync_max_lag_trading_days: 1`）。Worker 在执行任何计算前，**必须**先对照此策略校验数据时效。

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
| 链式补偿锚点 | `est_nav_yesterday` | `est_nav_date`（T-1 交易日） | `sync_time`（Action 运行时刻）|
| 持仓 | `holdings[]` | `holdings_date` | `holdings_fetch_time` |
| Drift 修正因子 | `drift_5d` | _(由 history 窗口隐含)_ | `drift_computed_at` |
| 汇率 T-1 结算 | `usd_cnh_t1` / `hkd_cnh_t1` | `_fx.date` | _(sync_time 隐含)_ |

**JS 层实现：`AssetEntity` 类型（2026-03 确立）**

三元组原则在 JS 层通过 `AssetEntity` 类型强制落地。`worker-full.js` 和 `index.html` 顶部均有定义：

```js
/**
 * AssetEntity — 系统内所有净值数据的统一载体
 * @typedef {{ value: number, date: string, sync_at?: string, src?: string }} AssetEntity
 */
```

规则：
- **所有计算函数的参数和返回值必须是 `AssetEntity`，严禁传递裸数字**
- `navMap`、`officialNav`、`estNavYesterday`（当返回时）均为 `AssetEntity | null`
- 计算输出的 `nav` 字段在 `/api/snapshot` 中也是 `AssetEntity`：`{ value, date: benchDate }`
- UI 渲染一律使用 `f.nav.value`、`f.officialNav.value`，严禁直接对 `f.nav` 调用 `.toFixed()`

---

### 二、全量计算准入规则（计算前必须校验）

#### 2.1 实时估值（`computeNav(base, benchDate, dynNavReturn, alpha)`）

所有前置条件均为**前置锁**：任一不满足则拒绝计算，对应输出置 null 并向下游传播。

| 前置条件 | 校验方式 | 失败处理 |
|---------|---------|---------|
| `base.date` 与 `benchDate` 相邻（严格 T-1） | `tradingDayLag(base.date, benchDate) === 1`（`computeNav` 内硬断言） | 不等于 1 → `computeNav` 熔断返回 null，**无任何退路** |
| `navLag ≥ 2` 时必须启用链式补偿 | `useChained = estNavEntity && navLag >= 2 && syncLagDays <= 1` | chain 失败 + navLag ≥ 2 → base.date 跨多日 → computeNav 熔断 → nav = null |
| 时序单向性：`benchDate >= navDate` | `resolveNavBasis()` 内部断言 | 违反则 `navBasis.type = 'INVALID'`，nav = null |
| `syncLagDays`（JSON 同步距 benchDate 交易日数）≤ 1 | `tradingDayLag(syncDateBj, benchDate) ≤ 1` | `syncLagDays ≥ 2` → 禁用链式补偿 → navLag ≥ 2 时 computeNav 熔断 |

#### 2.2 Drift 修正（`adjusted_est = raw_est × (1 + drift_5d)`）

**三重前置条件，缺一不可：**

```
drift_active = (drift5d ≠ 0) AND (drift_n ≥ 3) AND (driftLagDays ≤ 2)
```

| 前置条件 | 含义 |
|---------|------|
| `drift5d ≠ 0` | 存在有效修正值 |
| `drift_n ≥ 3` | 样本量充足（最少 3 个交易日） |
| `driftLagDays ≤ 2` | `drift_computed_at` 距 benchDate **≤ 2 个交易日**（不是日历天：周五计算的 drift，周一 driftLagDays = 1）|

**不满足任一条件 → `alpha = 0`（禁用补偿，回归原始估值）**

原则：**宁可无补偿，不可乱补偿。**

#### 2.3 溢价报警（UI 警报触发）

若底层任一数据戳超过 `staleness_policy` 阈值，**必须**强制锁定为 Unavailable：

```
staleness_policy（所有阈值单位：交易日）:
  nav_max_lag_days: 3           # navLag（交易日）超过3 → 估值不可用
  sync_max_lag_trading_days: 1  # syncLagDays > 1 → 禁止链式补偿（JSON 超过1个交易日未同步）
  drift_max_lag_days: 2         # driftLagDays（交易日）超过2 → Drift Offline
  drift_min_n: 3                # 有效样本 < 3 → Drift Offline
```

UI 状态映射：

| 数据状态 | 溢价显示 | Drift 标注 |
|---------|---------|-----------|
| 全部新鲜，drift 有效 | 正常溢折价率 | `[Drift Calibrated]`（绿色） |
| 全部新鲜，drift 过期/不足 | 正常溢折价率 | `[Drift Offline]`（灰色） |
| navLag = 1，正常 T-1 估算 | 正常溢折价率 | — |
| navLag ≥ 2，链式启用（syncLagDays ≤ 1） | 正常溢折价率，黄色 `est` 标注 | — |
| **navLag ≥ 2，链式失效**（syncLagDays > 1 或锚点缺失）| **`Unavailable`（computeNav 熔断）** | — |
| syncLagDays > 1（JSON 超过1交易日未同步）| `Unavailable`（链式禁用，computeNav 熔断） | — |
| navLag > 3（交易日）且无链式补偿 | `Unavailable` | — |
| benchDate < navDate（时序倒置） | `Unavailable`（INVALID） | — |

---

### 三、开发禁令

> ❌ **严禁**在代码中使用任何不带日期校验的原始数值进行数学运算。

具体禁止行为：

```js
// ❌ 禁止：直接取值计算，裸数字进入乘法，日期在赋值时丢失
const officialNav = fund.nav;
const est = officialNav * (1 + benchChg / 100);   // base 是裸数字，日期已消失

// ✅ 正确：base 是 AssetEntity，computeNav 在乘法发生处断言日期对齐
const base = useChained
  ? { value: fund.est_nav_yesterday, date: fund.est_nav_date, src: 'chain' }
  : { value: fund.nav, date: fund.nav_date, src: 'official' };

// computeNav 内部：tradingDayLag(base.date, benchDate) !== 1 → return null（无退路）
// base 跨多个交易日（Action 连续缺席）→ 熔断，不产生虚假溢价率
const nav = computeNav(base, benchDate, dynNavReturn, alpha);

// 1. benchDate 必须来自数据源时间戳，不得用 new Date()
// 2. navLag = tradingDayLag(navDate, benchDate)，不得用日历天
// 3. syncLagDays = tradingDayLag(syncDateBj, benchDate)，替代 fetchAgeH
// 4. benchDate >= navDate（时序单向性断言）
if (navBasis.type === 'INVALID') { /* resolveNavBasis 已处理，nav = null */ }
if (syncLagDays > 1) { useChained = false; /* JSON 超过1交易日未同步，chain 禁用 */ }
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
  SYNC_MAX_LAG_TRADING_DAYS: 1,  // syncLagDays 阈值（替代原 NAV_FETCH_MAX_AGE_H: 36）
  NAV_MAX_LAG_DAYS:          3,  // navLag 交易日阈值
  DRIFT_MAX_LAG_DAYS:        2,  // driftLagDays 交易日阈值
  DRIFT_MIN_N:               3,  // drift 最少有效样本数
};
```

---

### 五、实时行情中间值有效性规范

本节是对第二节的补充扩展。第二节管住"静态数据（NAV/Drift）是否够新"；本节管住"实时抓取的中间值（指数/汇率）是否真的取到了"。

#### 5.1 null ≠ 0（最高优先原则）

实时抓取的指数涨跌幅，**必须严格区分"未抓到（null）"和"真实零涨跌（0）"**：

```js
// ❌ 禁止：抓取失败静默变 0，污染下游计算
return idxChg[benchCode] ?? 0;
const chg = (d.data.f170 || 0) / 100;   // f170=null 时误置 0%

// ✅ 正确：抓取失败返回 null，让 null 向下传播
return idxChg[benchCode] ?? null;
if (d.data.f170 == null) return null;    // 不记录，下游感知缺失
```

#### 5.2 null 向下强制传播

中间值一旦为 null，必须阻断后续所有依赖它的计算：

```
idxChg[code] = null（未抓到）
  → calcBenchChg() = null
  → calcDynamicNavReturn() = null（残差无法填补时）
  → nav = null（不计算）
  → premium = null（不计算）
  → UI 显示 "指数缺失"，而非基于错误数据的溢折价率
```

`benchOk = benchChg != null` 字段必须随快照输出，供 UI 区分"估值不可信"与"价格缺失"。

#### 5.3 指数涨跌幅双层合理性校验

**第一层：写入 idxChg 之前（数据源层）**

所有来源（腾讯/东财/新浪/Yahoo）的指数值，写入 `idxChg` 前必须通过 `idxSanityOk()` 校验：

| 指数类别 | 阈值 | 说明 |
|---------|------|------|
| 普通股票/综合指数 | ±20% | 即使成分股打板，指数层面不会超过此幅度 |
| 商品期货/商品 ETF | ±30% | 原油等品种历史极端行情预留空间 |

超出阈值 → 丢弃并记录 warn 日志，**不写入 idxChg**（等同于未抓到，触发 null 传播）。

**商品类代码集合**（`COMMODITY_IDX`，在 worker-full.js 和 index.html 中各维护一份）：
`sinaAG0 / usUSO / usBNO / usXOP / usIXC / usGLD / usGLDM / usIAU / usSGOL / usAAAU / usSLV / usCPER / usBCI / usCOMT / usXLE`

**第二层：calcBenchChg 输出后（计算结果层）**

加权基准涨跌幅计算结果 `|benchChg| > 20%` → 返回 null。
防御持仓加权或复合基准在极端情况下产生异常结果。

注意：个股涨跌幅（`stockChg`）**不做合理性过滤**，让其正常参与持仓加权估值。

#### 5.4 数据源状态随快照透出

每次 `/api/snapshot` 必须携带 `fetchStatus` 对象：

```json
{
  "tencent": true,
  "eastmoney": false,
  "sina": true,
  "yahoo": true,
  "idxMissing": ["sz399961", "sinaAG0"]
}
```

`idxMissing` 列出所有 BENCH 用到但未能取到的指数代码，无需看 Worker 日志即可定位数据链断点。

#### 5.5 fallback 路径强制对齐

`index.html` 降级路径的所有数据校验逻辑**必须与 worker-full.js 完全一致**，包括：
- `EM_CODES` 保持同步（同样包含 `sinaAG0`、`sz399961`、`sz399979`）
- `idxSanityOk()` 使用相同的阈值和 `COMMODITY_IDX` 集合
- null 传播链路与 Worker 相同

**指数数据源分层（已验证，不可改变）：**

| 指数 | 主数据源 | 兜底来源 | 说明 |
|------|---------|---------|------|
| `sz399961` / `sz399979` | 腾讯（盘中实时） | EM fill-only | EM 对这两个指数盘中返回 f170=0，腾讯提供正确实时值；EM 仅作填空备份 |
| `sinaAG0` | 新浪 nf_AG0（Worker 代理） | 腾讯 nf_AG0（fill-only 兜底） → 收盘快照 | **EM 113.AG0 无效**（rc=100/data:null，已从 EM_CODES 移除）；腾讯支持 nf_AG0，映射为 sinaAG0 |
| `csi93xxxx` / `sh000985` | 东财 EM（唯一来源） | 收盘快照 | 腾讯不支持 |
| `hkHSSI` / `hkHSMI` / `hkHSCI` | 东财 EM | hkHSI 降级链 | 腾讯不支持 |

**合并优先级（严格执行）：**
```
腾讯（盘中实时）→ EM（fill-only，不覆盖已有值）→ 新浪（可覆盖，优先级最高）
```

> 历史：2026-03 发现 161217/161715/161226 基准持续显示 0%。
> 根因 1：`sz399961`/`sz399979` 遗漏加入东财；后移入 EM_CODES 但 EM 对计算型指数盘中返回 f170=0，EM 合并（overwrite）导致 0 覆盖正确值。修复：EM 改为 fill-only，`sz399961`/`sz399979` 由腾讯实时提供，EM 仅在腾讯返回 null 时填空。
> 根因 2：`sinaAG0: '113.AG0'` 在 EM_CODES 中是死项（EM 返回 rc=100/data:null），已确认并移除。161226 在交易时段唯一来源为新浪 nf_AG0（Worker 通过代理访问），直连模式无来源，显示"指数缺失"属正常。

#### 5.6 汇率数据规范（关键约束）

**Sina FX 代码（已验证，严禁改动）：**

| 变量 | Sina 代码 | 字段索引 | 说明 |
|------|----------|---------|------|
| `_fxUsdCnh` | `fx_susdcnh` | `[1]` | USD/CNH 离岸人民币现价 |
| `_fxHkdCnh` | `fx_shkdcnh` | `[1]` | HKD/CNH 离岸港元现价（**注意：`fx_shkcnh` 是无效代码，返回空串**）|

> Bug 历史：`fx_shkcnh` 被使用了相当长时间，导致 `_fxHkdCnh` 始终 null，所有港股 LOF
> 的汇率修正从未生效，fxOk 始终 false，用户看到的⚠️全是误报。2026-03 修复为 `fx_shkdcnh`。

**FX 来源约束（架构固定）：**

- FX 数据**只能来自 Sina**（`hq.sinajs.cn`）
- Sina 强制校验 `Referer: https://finance.sina.com.cn/`，浏览器 JSONP/fetch 均无法直连
- `qt.gtimg.cn`（腾讯）经过测试**完全不支持任何 FX 代码**，返回 `v_pv_none_match`
- 因此 FX 数据**必须由 Worker 代理抓取**；直连模式下 FX = null 是系统固有限制，非 Bug

**当汇率不可达时（直连模式）：**
- `fxChgUsd = null` / `fxChgHkd = null`
- `calcAdjustedBenchChg()` 退化为 `fxChg = 0`（假设汇率无变动，精度下降）
- 每只基金输出 `fxOk: boolean`；UI 在溢折价旁显示 `⚠`
- **设计原则：带误差的估值比完全不可用更有价值；但必须明确标注精度下降。**

#### 5.7 集合竞价噪音过滤（原则，暂不实现代码）

集合竞价阶段（A 股 09:15–09:25）腾讯接口会返回尚未成交的委托价，与实际开盘价可能差异较大。

**原则：**
- 集合竞价期间抓取的行情（包含指数涨跌幅）**不应用于估值计算和报警触发**
- 建议在此时间窗口内暂停计算循环或将结果标记为 `pending`，待 09:25 正式开盘后再启用
- 具体实现方式待定（服务端时间窗口过滤 vs. 前端 UI 冻结）

---

### 六、时间驱动原则全局规范（2026-03 确立）

本节是前五节的扩展与完整化，确立系统级时间逻辑的统一准则。与前五节共同构成不可分割的**技术宪法**。

#### 6.1 核心原则

> **所有数据必须携带绝对时间戳；所有涉及市场逻辑的时间判断，必须以交易日为单位；时间戳是每一次计算的前置锁——不对齐则拒绝计算，向下游传播 null。**

三条原则构成完整链路：
1. **时间戳**：每个数值都能回答"我属于哪个市场时刻"
2. **交易日历**：判断"过了多久"用市场时间，而非钟表时间
3. **前置锁**：每个公式执行前，所有输入的时间戳必须满足该公式的时序约束；否则返回 null，绝不用错误对齐的数据计算

时间戳不只是元数据，它们是**计算的准入条件**：

```
est_nav(T) = nav(T-1) × bench_chg(T)
  前置锁：date_map[bench] == T  AND  nav_date == T-1  AND  tradingDayLag(T-1, T) == 1

drift(T) = (nav(T) - est_nav(T)) / est_nav(T)
  前置锁：date_map[bench] == nav_date == T  （bench 与净值属于同一交易日）

useChained:
  前置锁：navLag >= 2  AND  syncLagDays <= 1  AND  estNavYesterday != null
```

#### 6.2 时间单向性断言

**严格要求：`benchDate >= navDate` 必须始终成立。**

`benchDate` 是当前市场行情日期，`navDate` 是最新官方净值日期。净值永远滞后于行情。若出现 `benchDate < navDate`，说明官方净值走在了行情前面（接口异常）或 benchDate 提取回退到了旧值（时间戳解析失败）。

**处理规则**：检测到 `benchDate < navDate` 时，`navBasis.type` 强制设为 `'INVALID'`，nav 输出 null，禁止计算溢价率。防止数据错乱产生方向性错误的溢折价。

#### 6.3 禁令（新增，与三节禁令合并执行）

| 禁止行为 | 错误原因 | 强制替代 |
|---------|---------|---------|
| `/ 86400000` 计算"距今几天" | 日历天不感知节假日/周末 | `tradingDayLag(date, benchDate)` |
| `fetchAgeH ≤ 36`（小时门槛） | 正常周末跨越 ~72h，周一必然误触发 | `syncLagDays ≤ 1`（交易日）|
| `new Date().toISOString()` 作为 benchDate | 服务器时钟 ≠ 市场数据时间 | 从数据源时间戳提取（marketContext）|
| `benchDate < navDate` 时继续计算 | 时序倒置，结果方向性错误 | 强制 nav = null，navBasis.type = 'INVALID' |
| 前置锁不满足时静默降级 | 隐藏数据质量问题，污染下游 | 置 null 向下传播，UI 显示缺失原因 |
| 向 nav 计算传递裸数字（`base = fund.nav`） | 日期在赋值时丢失，乘法无从校验 | `base = AssetEntity`，`computeNav` 断言日期 |
| `base = officialNav \|\| prevClose`（prevClose 无日期） | prevClose 无净值日期，无法通过日期锁 | prevClose 退出估值路径；无合法 base → nav = null |

#### 6.4 各时间计算的正确单位

| 计算 | 单位 | 实现 | 阈值 |
|------|------|------|------|
| `navLag`（净值滞后） | 交易日 | `tradingDayLag(navDate, benchDate)` | ≥2 → 开链式；>3 → 不可用 |
| `driftLagDays`（Drift 鲜度） | 交易日 | `tradingDayLag(driftComputedDate, benchDate)` | ≤ 2 |
| `syncLagDays`（JSON 同步鲜度） | 交易日 | `tradingDayLag(syncDateBj, benchDate)` | ≤ 1（替代 `fetchAgeH`）|
| `drift_5d` 窗口 | 交易日（天然） | `history` 数组只在交易日写入，`[-5:]` = 5 交易日 | n ≥ 3 |
| `history` 滚动窗口 | 交易日（天然） | 同上，`[-30:]` = 30 交易日 | — |

**系统中不存在任何以日历天/时钟小时做市场逻辑判断的代码。**

#### 6.5 benchDate：时间驱动的核心锚点

严禁用服务器时钟推断，必须从数据源时间戳提取：

| 基金类型 | 来源 | 提取方式 | 精度 |
|---------|------|---------|------|
| A股 / 港股 | 腾讯 `field[30]` | `parseTencentDate(fields)` → `YYYYMMDD` | 精确 |
| 美股 | Yahoo `regularMarketTime` | Unix 秒，UTC-5 偏移 → 美股交易日 | 精确 |
| 收盘快照路径 | `idx_closing.json[benchCode].date` | 快照自身字段，直接使用 | 精确 |
| **降级路径（直连，无 Yahoo）** | `tqData.aShareDate` | **美股基金退化为 A 股时间戳近似** | **近似** |

**降级路径强制要求**：当美股基金（`cat === 'us'`）的 benchDate 来自 `aShareDate` 时，`navBasis.aligned` 必须为 `false`，UI 溢折价旁显示降级标注（黄色 `[估(降级)]`），告知操作者时序对齐为近似值。

#### 6.6 syncDateBj：Action 同步鲜度锚点

替代 `fetchAgeH ≤ 36`，进入交易日框架：

```js
// fund_daily.json 最近同步的北京日期（UTC → UTC+8）
const syncDateBj = daily?._meta?.sync_time
  ? new Date(new Date(daily._meta.sync_time).getTime() + 8 * 3_600_000)
      .toISOString().slice(0, 10)
  : null;

const syncLagDays = (syncDateBj && benchDate) ? tradingDayLag(syncDateBj, benchDate) : 99;
const useChained  = estNavYesterday && navLag >= 2 && syncLagDays <= 1;
```

| `syncLagDays` | 含义 |
|--------------|------|
| `0` | Action 当日已同步（07:00 跑完，09:30 Worker 消费） |
| `1` | 上一交易日同步（正常，**含周末/长假跨越**） |
| `≥ 2` | Action 连续缺席，链式锚点不可信，禁用 |

**长假免疫**：`tradingDayLag` 使用 `trading_dates` 集合，国庆 7 天长假期间，9 月 30 日同步的 JSON 在 10 月 8 日早盘 `syncLagDays = 1`。无需额外补丁。

#### 6.8 AssetEntity 与 computeNav 断路器（2026-03 实现）

本节记录"数据原子结构"在 JS 计算层的具体落地方式，是第一节三元组原则的执行机制。

**设计原则：诚实优先于可用性**

系统宁可显示 null，也不计算一个日期对不上的数字。在有限降级（US → aShareDate）时，必须明确标注精度损失，而不是静默妥协。

**物理隔离：`computeNav` 断路器**

```js
function computeNav(base, benchDate, dynNavReturn, alpha) {
  if (!base || !base.date || !benchDate) return null;
  if (dynNavReturn == null) return null;
  // 硬断言：base 必须恰好是 benchDate 的前一个交易日
  if (tradingDayLag(base.date, benchDate) !== 1) return null;
  return { value: base.value * (1 + dynNavReturn / 100) * (1 + alpha),
           date: benchDate, src: 'estimated' };
}
```

含义：如果 Action 连续两天没跑（数据断裂），即便存在旧净值，`base.date` 与 `benchDate` 之间的 `tradingDayLag > 1`，断路器熔断，绝不产生"跨时空乘法"的虚假溢价率。

**身份透明：全链路 AssetEntity**

从 `fund_daily.json` 读取到 UI 渲染，净值数据始终以 `{ value, date }` 形式存在，从不解体为裸数字：

```
fund_daily.json
  └─ { nav, nav_date, nav_fetch_time }
       → navMap[code] = { value, date, sync_at, src }   ← 读入即构造，不解体

base = useChained ? estNavEntity : officialNav            ← AssetEntity，携带日期

computeNav(base, benchDate, ...)
  → { value, date: benchDate }                           ← 输出也是对象

f.nav.value  /  f.officialNav.value                      ← UI 一律通过 .value 取数
```

UI 收益：彻底告别"T-1"这种模糊的相对描述。每行数据标明自己的"生产日期"——`nav.date` = `benchDate`，即该估值所代表的交易日，而非官方净值的发布日期。

**降级而不盲目：US 基金 benchDate 自愈**

当 Yahoo 接口失败时（`usDate = null`），US 基金 benchDate 降级为 `aShareDate`（A 股时间戳近似），而非直接返回 null 导致 `navLag = 99`。降级路径必须标注 `navBasis.aligned = false`，UI 显示精度下降提示。

#### 6.7 Action 层（Phase 5）：bench 数据的日期锁

`fetch_bench_chg_batch(data, t1_date)` 返回 `(chg_map, date_map)`：

| 数据源 | `date_map[code]` 来源 |
|-------|----------------------|
| EM / Tencent | `t1_date`（响应不含日期，以净值日期上下文推断） |
| Yahoo | `regularMarketTime`（US: UTC-5；HK ^HSI/^HSCE: UTC+8） |
| `idx_closing.json` | `entry['date']`（快照自身字段，最权威） |

- `update_drift`：写入前校验 `date_map[bench] == nav_date`，不符则该条置 null（拒绝写入）
- `update_chain_anchors`：`est_nav_date` 取自 `date_map` 实际 bench 交易日，而非 `t1_date`（净值日期）

---

## 收盘后数据持续性机制

### 背景

部分指数（`sz399961`、`sz399979`、`sinaAG0`）在收盘后东财/新浪均停止提供实时数据，返回 null。
这导致依赖这些指数的基金（161217/161715/161226）在非交易时段显示"指数缺失"排在列表末尾，
即使溢折价高达 30%，对普通用户极不友好。

### 解决方案：收盘快照 Action

**新增 `.github/workflows/closing-data-sync.yml`**，每个交易日北京时间 **15:05**（收盘后5分钟）运行：
- 调用 `.github/scripts/sync_closing_idx.py` 抓取上述指数的当日收盘涨跌幅
- 写入 `data/idx_closing.json`，格式与 `fund_daily.json` 的三元组规范一致（`chg / date / sync_at`）
- 单个指数失败时保留旧值；全部失败时抛出异常，Actions 红叉报警
- 支持扩展：在 `sync_closing_idx.py` 的 `CLOSING_IDX` 字典加一行即可纳入新指数

**Worker / index.html（降级路径）** 在实时抓取返回 null 时，读取 `idx_closing.json` 作为兜底：

```
实时抓取成功 → 使用实时数据（快照完全跳过）
实时抓取返回 null + 非交易时段 → 使用收盘快照（syncLagDays ≤ 1，即上一交易日内写入）
实时抓取返回 null + 交易时段 → 显示"指数缺失"（快照禁用，防止误用昨日数据）
```

**UI 标注**：使用了快照数据的基金，溢折价旁显示灰色 `(收)` 标签，三处（表格/卡片/弹窗）均有。

### 快照守卫：交易时段禁用

```js
// isBjTradingHours() = 当天是交易日 AND 北京时间 09:15–15:00
if (closingData && !isBjTradingHours()) {
  // 才允许用收盘快照回填
}
```

交易时段内如果实时接口偶发失败，必须显示"指数缺失"而非以昨日收盘数据误导用户。

---

## 交易日历机制

### 背景

两个问题共享同一底层需求（知道哪些天是交易日）：
1. `isBjTradingHours()` 需要判断今天是否交易日（区分法定节假日 vs. 普通周末）
2. `navLag` 原先使用日历天数，导致周一 navLag=3（周五净值）误触发链式估值

### 实现：方案一（`chinesecalendar` 包）+ 方案二（历史积累）组合

**`sync_fund_data.py`** 每次成功同步后：
- 用 `chinesecalendar.is_workday()` 验证 `data_date` 确实是交易日
- 将其追加进 `fund_daily.json._meta.trading_dates`，滚动保留最近 **90个交易日**

**Worker / index.html** 读取 `trading_dates` 后注入本地工具函数：

| 函数 | 用途 |
|------|------|
| `setTradingDates(arr)` | 注入历史集合（fetchAllData 时调用） |
| `isTradingDay(dateStr)` | 窗口内精确判断，窗口外降级为周末判断 |
| `tradingDayLag(from, to)` | 计算两日期间的交易日数，替代原日历天数 |
| `isBjTradingHours()` | 判断当前是否在交易时段，用于快照守卫 |

**`navLag` 语义变更**：

```
之前：navLag = 日历天数（周一周五净值 → navLag=3，误触发链式估值）
之后：navLag = 交易日数（周一周五净值 → navLag=1，正确）
```

`staleness_policy` 的阈值（`nav_max_lag_days: 3`、`chain_estimation_allowed_lag_days: 2`）单位
同步改为交易日，语义更准确，数值不变。

### 精度说明

- **窗口内（近90个交易日）**：`isTradingDay()` 精确到节假日级别
- **窗口外**：降级为周末判断，不影响 `navLag`（只看近期历史）
- `chinesecalendar` 包每年需更新版本以覆盖新年度节假日（dependabot 可自动处理）

---

## 容错与自愈机制

### 🛰️ 数据流架构 (v2.0 强力解耦版)

| 组件 | 核心职责 | 容错策略 |
| :--- | :--- | :--- |
| **Worker Agent** | 并行抓取 (AllSettled)，Header 伪装穿透 | 失败时返回 null + missing 标记，不中断链路 |
| **Frontend Core** | 优先渲染 Worker 完整包 | 监控数据空位，触发 `asyncPatchFromEM` |
| **Direct Mode** | 绕过 Worker，腾讯 JSONP + 东财 fetch 直连 | FX 降级为 null（新浪无法直连），⚠️ 标注精度下降 |

**维护注记**：修改数据源时，优先检查 `worker-full.js` 的并行抓取列表，确保 `Promise.allSettled` 的解构顺序与请求顺序严格对应。

### 并发隔离：Promise.allSettled

Worker 内部四路数据抓取（腾讯 / 东财 / 新浪 / Yahoo）采用 `Promise.allSettled` 并行执行：

```js
const [tqRes, emRes, sinaRes, futRes] = await Promise.allSettled([
  fetchTencent(daily), fetchEastmoney(), fetchSina(), fetchYahooFutures(),
]);
// 各路独立解包，rejected 时用空对象兜底，不阻塞整体
const tqData = tqRes.status === 'fulfilled' ? tqRes.value : { funds:{}, indices:{}, stockChg:{} };
```

任何单一源超时/失败（包括被封 IP）**严禁阻塞**其他源数据。`fetchStatus.idxMissing` 在每次 snapshot 中声明缺失的 bench 指数列表，供前端定位断点并触发异步补丁。

### 双路径自愈：Worker Agent + 前端 Async Patch

典型故障场景：**VPN 用户 → Cloudflare 路由至境外 PoP → 东财 push2 被封 → EM 专属指数缺失**

```
路径 A（Worker）：Chrome 124 UA + Referer 伪装，硬磕数据源屏蔽
                  失败时在 snapshot.fetchStatus.idxMissing 中声明空位

路径 B（前端补丁）：asyncPatchFromEM() 检测到 idxMissing 后，
                   利用浏览器本地 IP（国内用户可直连 EM），
                   异步 fetch EM CORS 接口，单点补齐缺失指数，重算受影响基金
```

**`asyncPatchFromEM` 约束：**
- **非阻塞**：主链路 render 完成后后台异步触发，不延迟首屏
- **精确补丁**：只重算 `benchOk=false` 且 bench 被补丁覆盖的基金
- **标记来源**：补丁基金 `_src = 'W+P'`，状态栏注记"全量实时 · 本地补丁"
- **安全降级**：浏览器也取不到 EM 数据时静默放弃，不覆盖现有结果

### Worker 请求头伪装规范

所有向第三方数据源发出的 fetch，**必须**携带完整浏览器特征头（已在各 fetch 函数中实现）：

| 数据源 | Referer | UA | 备注 |
|-------|---------|-----|------|
| 东财 push2 | `https://quote.eastmoney.com/` | Chrome 124 | Accept: application/json |
| 新浪 hq | `https://finance.sina.com.cn/` | Chrome 124 | Accept-Language: zh-CN |
| 腾讯 qtimg | `https://gu.qq.com` | Chrome 124 | Accept: \*/\* |

### 直连模式能力边界（已测试，固化为规范）

| 能力 | 直连模式 | 原因 |
|------|---------|------|
| 腾讯行情（价格/A股/HK指数） | ✅ 可用 | JSONP，无 CORS 限制 |
| 东财 EM 指数 | ✅ 可用 | fetch，push2 有 CORS 头 |
| 新浪 FX（USD/CNH, HKD/CNH） | ❌ 不可用 | Referer 校验；腾讯经测试不提供任何 FX 代码 |
| 白银 AG0（sinaAG0） | ✅ 腾讯 nf_AG0 兜底 | 腾讯 `nf_AG0` 可直连（JSONP），解析后映射为 `sinaAG0`；EM `113.AG0` **无效** |
| FX ⚠️ 警告 | 必然出现 | 直连 FX=null 是固有限制，正确行为，非 Bug |

---

## 估值计算逻辑（完整）

### 完整计算链

```
Step 1  基准净值选取（AssetEntity，携带日期）
        base = estNavEntity             （navLag ≥ 2 且链式条件满足：useChained = true）
             | officialNav              （navLag = 1，正常情况）
             | null                     （无合法 base → 后续全部熔断）

        注：prevClose 已从估值路径移除——其无净值日期，无法通过日期锁。

Step 2  盘中动态估值收益率
        dynNavReturn = calcDynamicNavReturn()   （持仓加权 + bench 残差，含 FX 修正）

Step 3  Drift 偏差修正因子
        alpha = clamp(drift_5d, −2%, +2%)       （Drift Active 时）
              | 0                               （Drift Offline 时）

Step 4  computeNav 断路器（物理隔离点）
        前置锁：tradingDayLag(base.date, benchDate) === 1
          满足 → nav = AssetEntity { value: base.value × (1 + dynNavReturn/100) × (1 + alpha),
                                     date: benchDate }
          不满足（base 跨多日、base=null、benchDate=null）→ nav = null，无退路

        特例：navBasis.type === 'T0_OFFICIAL'（今日官方净值已发布）
          → nav = { value: base.value, date: base.date }，不叠加涨跌幅

Step 5  溢折价率
        premium% = (price − nav.value) / nav.value × 100
        nav = null 时 premium = null（UI 显示"—"或"指数缺失"）
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

### 跨市场 LOF 官方净值语义

**T 日官方净值的构成**：

```
T 日净值 = A 股部分（T 日 15:00 收盘）
         + 美股部分（T 日美股收盘，北京时间 T+1 凌晨 4–5 点）
         合并后的净资产 ÷ 总份额
```

关键含义：
- 基金公司在美股收盘后（北京 T+1 凌晨 4–5 点）才能完成 T 日净值计算
- 基金公司通常在北京时间 T+1 06:00–07:00 前后将 T 日净值推送至数据源
- 因此 Action 定时 **北京 07:00（UTC 23:00）** 运行，正好在净值稳定可用之后抓取

这也解释了链式补偿的必要性：A 股盘中（T 日 09:30–15:00），基金公司尚未公布 T 日净值（因为 T 日美股还未收盘），所以 Worker 能拿到的最新官方净值是 T-1 日的，`navLag = 1`；如果遇到非交易日或数据延迟，`navLag ≥ 2` 时链式补偿介入。

---

### T-2 链式净值补偿

```
# Action 每日北京 07:00 写入（美股收盘后 3h+，T-1 bench chg 稳定可用）
# 由 update_chain_anchors() 无条件写入，不依赖"是否有新净值"
est_nav_yesterday = officialNav(T-2) × (1 + T-1_bench_chg%)
est_nav_date      = T-1 交易日日期

# Worker/前端用链式基准
nav = est_nav_yesterday × (1 + today_bench%)
    = officialNav(T-2) × (1 + T-1_bench%) × (1 + today_bench%)   ← 正确两步链
```

触发条件（三重前置锁，缺一不可）：
```
useChained = estNavEntity != null        # estNavEntity = { value, date: est_nav_date }（AssetEntity）
          && navLag >= 2                 # navLag 单位：交易日
          && syncLagDays <= 1            # syncLagDays 替代 fetchAgeH（交易日，长假免疫）
```

`syncLagDays = tradingDayLag(syncDateBj, benchDate)`，其中 `syncDateBj` 为 `_meta.sync_time` 转换的北京日期。

**重要实现约束**：
- `est_nav_yesterday` 由 `update_chain_anchors()` 专职写入，与 drift 历史追踪完全解耦
- `est_nav_date` 取自 `date_map`（bench 数据的实际交易日），而非 `t1_date`（nav 日期）；QDII 净值延迟时两者可能不同
- `update_drift()` 内部计算的 `est_nav` 仅用于 drift 对账（`prev_nav × bench_chg`），**不写入 est_nav_yesterday**
- bench 数据不可用时：清除旧 `est_nav_yesterday` 防止 Worker 误用过期锚点
- `sinaAG0` 在 Action 的 bench 抓取中通过 `_TQ_ALIASES: sinaAG0 → nf_AG0` 映射至腾讯接口

UI：正常白色显示 officialNav；链式补偿时黄色显示 estNavYesterday + `est` 标注。

### Drift 偏差校准

离线计算（Action 每日）：
```
est_nav(t) = nav(t-1) × (1 + bench_chg(t) / 100)
drift(t)   = (nav(t) − est_nav(t)) / est_nav(t)
drift_5d   = mean(drift 最近5个有效值)
```

Hard Enforcement（前置锁，单位均为交易日）：
```
driftLagDays = tradingDayLag(driftComputedDate, benchDate)  # 交易日数，非日历天
driftActive  = (drift5d ≠ 0) && (drift_n ≥ 3) && (driftLagDays ≤ 2)
alpha        = driftActive ? clamp(drift_5d, −2%, +2%) : 0
```

Action 层前置锁（`update_drift`）：写入 drift 条目前校验 `date_map[bench] == nav_date`；不符则拒绝写入，避免错日 bench 数据污染历史序列。

### 补偿模块状态监控（Stats Bar）

`index.html` Stats Bar 下方有一行「补偿状态栏」，实时聚合三个补偿模块的运行状况：

| 徽章 | 判断依据 | 颜色含义 |
|------|---------|---------|
| **汇率** | `calcFxOk(code, fxChgUsd, fxChgHkd)` | 绿=✓正常 / 橙=✗缺失（跨市场基金未得到FX数据） |
| **Drift 校准** | `driftStatus === 'ACTIVE'` 计数 | 绿=多数激活 / 黄=少于半数 / 橙=全部挂起 |
| **链式补偿** | `navLag` + `useChained` 三态 | 见下表 |

**链式补偿三态**（是本监控的核心设计，区分「不需要」与「静默失效」）：

| 显示 | 条件 | 颜色 | 含义 |
|------|------|------|------|
| `不需要` | 所有基金 `navLag < 2` | 灰 | 净值都是最新的，链式无需介入（正常） |
| `N 只启用` | `navLag ≥ 2` 且 `useChained = true` | 黄 | 链式补偿正常运行，estNavYesterday 参与了溢价率计算 |
| `N 只失效` | `navLag ≥ 2` 但 `useChained = false` | 橙 | **静默失效**：净值滞后但锚点缺失或 `syncLagDays > 1`（Action 超过1交易日未运行），需检查 Action 日志 |

**关键参数释义：**

- `navLag`：benchDate 距最新官方净值日期的**交易日数**（非自然日）。`navLag=1` = 正常（昨天净值）；`navLag≥2` = 需要链式补偿
- `useChained`：三重前置锁同时满足时为 `true`：`estNavYesterday != null`（锚点存在）AND `navLag >= 2`（确实滞后）AND `syncLagDays <= 1`（JSON 在上一交易日内同步）
- `failedChain`：`navLag >= 2 && !useChained` 的基金数——应跑链式但未跑，是需要介入排查的信号

**`fxOk` 的语义**：仅对需要汇率的基金（bench 含 `us*` 或 `hk*`）才会返回 `false`；纯 A 股基金始终返回 `true`（不需要 FX，不算失败）。

---

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
