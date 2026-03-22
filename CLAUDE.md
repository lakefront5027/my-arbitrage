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

## Important Conventions

- The version suffix (e.g., `v33`) appears in all filenames; increment consistently when releasing a new version.
- Fund codes, benchmark mappings, fee structures, and subscription quotas are all hardcoded in the HTML. There is no external config file — updates require editing the HTML directly.
- JSONP callbacks use dynamically generated function names to avoid collisions across parallel requests.
- The app is designed for Safari/Chrome on macOS; no mobile layout.
