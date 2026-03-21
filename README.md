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
| `fundgz.1234567.com.cn` | 净值应急补位 JSONP | Page（应急） |

---

## 环境变量 / Secrets

| 名称 | 位置 | 用途 |
|------|------|------|
| `CF_API_TOKEN` | GitHub Secrets | Cloudflare 部署 |
| `CF_ACCOUNT_ID` | GitHub Secrets | Cloudflare 账号 |
| `WX_KEY` | GitHub Secrets | 企业微信 Webhook（持仓漂移预警） |
| `FUND_DAILY_URL` | wrangler.toml `[vars]` | 覆盖 Worker 读取的 JSON 地址 |
