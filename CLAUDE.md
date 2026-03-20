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

- `lof_arb_monitor_v33.html` — entire monitoring app (~906 lines, self-contained)
- `lof_backtest_v33.html` — backtesting app (~52k lines, includes embedded historical data)
- `Archive/lof_holdings.json` — holdings data for 47+ funds (used for reference, not loaded at runtime)
- `LOF套利雷达_估值基准文档_v33.xlsx` — benchmark mapping reference spreadsheet

## Important Conventions

- The version suffix (e.g., `v33`) appears in all filenames; increment consistently when releasing a new version.
- Fund codes, benchmark mappings, fee structures, and subscription quotas are all hardcoded in the HTML. There is no external config file — updates require editing the HTML directly.
- JSONP callbacks use dynamically generated function names to avoid collisions across parallel requests.
- The app is designed for Safari/Chrome on macOS; no mobile layout.
