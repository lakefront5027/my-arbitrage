// ══════════════════════════════════════════════════════
//  LOF 套利雷达 — Cloudflare Worker (Dual Mode)
//  Mode A: HTTP Fetch  (/api/quote, /api/sina)
//  Mode B: Scheduled Cron (every 1 minute)
// ══════════════════════════════════════════════════════
//
//  ┌─────────────────────────────────────────────────────┐
//  │              KV 使用规范（强制约束）                   │
//  ├─────────────────────────────────────────────────────┤
//  │  定位：KV 仅作为 Transient Cache（瞬时缓存）            │
//  │        严禁作为 Source of Truth 或历史序列存储           │
//  │                                                     │
//  │  Source of Truth = GitHub 仓库内的 JSON 文件           │
//  │    data/fund_daily.json  ← 净值 / 持仓 / drift 历史    │
//  │                                                     │
//  │  ✅ 允许写入 KV：                                     │
//  │    • 跨 isolate 的实时状态（如报警跃迁去重）             │
//  │      → 但当前已改用模块级 _alertState，KV 已完全移除      │
//  │                                                     │
//  │  ❌ 禁止写入 KV：                                     │
//  │    • drift_history / 误差历史序列                     │
//  │    • 每日估值快照                                     │
//  │    • 任何可由 Action + Git commit 持久化的数据           │
//  │                                                     │
//  │  Action 规范：                                       │
//  │    同步数据优先写文件系统（data/*.json + git push）       │
//  │    仅当需要跨环境毫秒级实时共享时才考虑 KV                │
//  └─────────────────────────────────────────────────────┘

const CONFIG = {
  ALERT_THRESHOLD: 1.5,        // 溢价预警阈值(%)
  WECHAT_WEBHOOK: '',           // 企业微信机器人Webhook URL（留空则不发送）
  SINA_PROXY_URL: 'https://patient-pond-824c.3031315027ghb.workers.dev', // 新浪代理（保留供前端用）
  // fund_daily.json 托管地址（CF Pages），由 GitHub Action 每日更新
  FUND_DAILY_URL: 'https://my-arbitrage.pages.dev/data/fund_daily.json',
};

// ── fund_daily.json 内存缓存（isolate 级别，约 30s 有效） ─
let _dailyCache = null;
let _dailyCacheTs = 0;
const DAILY_CACHE_TTL = 30 * 60 * 1000; // 30 分钟

let _closingCache = null;
let _closingCacheTs = 0;

async function loadIdxClosing(env) {
  const now = Date.now();
  if (_closingCache && now - _closingCacheTs < DAILY_CACHE_TTL) return _closingCache;
  const baseUrl = (env && env.FUND_DAILY_URL) || CONFIG.FUND_DAILY_URL;
  const url = baseUrl.replace('fund_daily.json', 'idx_closing.json');
  try {
    const resp = await fetch(url, { cf: { cacheTtl: 1800 } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _closingCache = await resp.json();
    _closingCacheTs = now;
  } catch (e) {
    console.warn('[closing] loadIdxClosing 失败:', e.message);
  }
  return _closingCache;
}

async function loadFundDaily(env) {
  const now = Date.now();
  if (_dailyCache && now - _dailyCacheTs < DAILY_CACHE_TTL) return _dailyCache;
  // 优先用 env 变量覆盖 URL（wrangler.toml [vars] FUND_DAILY_URL = "..."）
  const url = (env && env.FUND_DAILY_URL) || CONFIG.FUND_DAILY_URL;
  try {
    const resp = await fetch(url, { cf: { cacheTtl: 1800 } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _dailyCache = await resp.json();
    _dailyCacheTs = now;
  } catch (e) {
    console.warn('[daily] loadFundDaily 失败:', e.message);
    // 保持旧缓存（如果有）
  }
  return _dailyCache;
}

// ── 基金列表（47只，完整数据） ────────────────────────
const FUNDS = [
  // 欧美市场
  {code:'161127',name:'标普生物科技LOF',    tq:'sz161127',cat:'us',quota:'限10',   fee:'1.20%',rfee:'1.00%'},
  {code:'164906',name:'中概互联网LOF',      tq:'sz164906',cat:'us',quota:'开放',   fee:'1.20%',rfee:'1.50%'},
  {code:'501312',name:'海外科技LOF',        tq:'sh501312',cat:'us',quota:'限2千',  fee:'1.20%',rfee:'1.20%'},
  {code:'164824',name:'印度基金LOF',        tq:'sz164824',cat:'us',quota:'限1千',  fee:'1.20%',rfee:'1.50%'},
  {code:'160644',name:'港美互联网LOF',      tq:'sz160644',cat:'us',quota:'限10万', fee:'1.50%',rfee:'1.50%'},
  {code:'162415',name:'美国消费LOF',        tq:'sz162415',cat:'us',quota:'限500',  fee:'1.20%',rfee:'1.50%'},
  {code:'161126',name:'标普医疗保健LOF',    tq:'sz161126',cat:'us',quota:'限10',   fee:'1.20%',rfee:'1.00%'},
  {code:'161128',name:'标普信息科技LOF',    tq:'sz161128',cat:'us',quota:'限10',   fee:'1.20%',rfee:'1.00%'},
  {code:'161125',name:'标普500LOF',         tq:'sz161125',cat:'us',quota:'限10',   fee:'1.20%',rfee:'1.50%'},
  {code:'161130',name:'纳斯达克100LOF',     tq:'sz161130',cat:'us',quota:'限10',   fee:'1.20%',rfee:'0.60%'},
  {code:'501300',name:'美元债LOF',          tq:'sh501300',cat:'us',quota:'限1万',  fee:'0.80%',rfee:'1.50%'},
  {code:'160140',name:'美国REIT精选LOF',    tq:'sz160140',cat:'us',quota:'限100万',fee:'1.20%',rfee:'1.00%'},
  {code:'501225',name:'全球芯片LOF',        tq:'sh501225',cat:'us',quota:'暂停',   fee:'1.50%',rfee:'1.50%'},
  // 欧美·商品
  {code:'160216',name:'国泰商品LOF',        tq:'sz160216',cat:'cm',quota:'限1千',  fee:'1.50%',rfee:'1.50%'},
  {code:'161116',name:'黄金主题LOF',        tq:'sz161116',cat:'cm',quota:'暂停',   fee:'0%',   rfee:'1.50%'},
  {code:'164701',name:'黄金LOF',            tq:'sz164701',cat:'cm',quota:'限50',   fee:'0.80%',rfee:'1.50%'},
  {code:'165513',name:'中信保诚商品LOF',    tq:'sz165513',cat:'cm',quota:'开放',   fee:'1.60%',rfee:'1.50%'},
  {code:'160719',name:'嘉实黄金LOF',        tq:'sz160719',cat:'cm',quota:'暂停',   fee:'1.20%',rfee:'1.50%'},
  {code:'161815',name:'抗通胀LOF',          tq:'sz161815',cat:'cm',quota:'开放',   fee:'1.60%',rfee:'1.50%'},
  {code:'163208',name:'全球油气能源LOF',    tq:'sz163208',cat:'cm',quota:'暂停',   fee:'1.50%',rfee:'1.50%'},
  {code:'501018',name:'南方原油LOF',        tq:'sh501018',cat:'cm',quota:'暂停',   fee:'1.20%',rfee:'1.50%'},
  {code:'161129',name:'原油LOF易方达',      tq:'sz161129',cat:'cm',quota:'暂停',   fee:'1.20%',rfee:'1.50%'},
  {code:'160723',name:'嘉实原油LOF',        tq:'sz160723',cat:'cm',quota:'暂停',   fee:'1.20%',rfee:'1.50%'},
  {code:'162719',name:'石油LOF',            tq:'sz162719',cat:'cm',quota:'暂停',   fee:'1.20%',rfee:'1.50%'},
  {code:'162411',name:'华宝油气LOF',        tq:'sz162411',cat:'cm',quota:'开放',   fee:'1.50%',rfee:'1.50%'},
  {code:'160416',name:'石油基金LOF',        tq:'sz160416',cat:'cm',quota:'暂停',   fee:'1.20%',rfee:'1.50%'},
  // 亚洲市场·港股
  {code:'501303',name:'恒生中型股LOF',      tq:'sh501303',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.60%'},
  {code:'161124',name:'港股小盘LOF',        tq:'sz161124',cat:'hk',quota:'限1千',  fee:'1.20%',rfee:'1.00%'},
  {code:'160322',name:'港股精选LOF',        tq:'sz160322',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'1.50%'},
  {code:'501021',name:'香港中小LOF',        tq:'sh501021',cat:'hk',quota:'暂停',   fee:'1.20%',rfee:'1.20%'},
  {code:'501310',name:'价值基金LOF',        tq:'sh501310',cat:'cn',quota:'开放',   fee:'1.20%',rfee:'0.90%'},
  {code:'501302',name:'恒生指数基金LOF',    tq:'sh501302',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.60%'},
  {code:'501307',name:'银河高股息LOF',      tq:'sh501307',cat:'hk',quota:'开放',   fee:'1.00%',rfee:'0.68%'},
  {code:'501306',name:'港股高股息LOFC',     tq:'sh501306',cat:'hk',quota:'开放',   fee:'0.00%',rfee:'0.60%'},
  {code:'160717',name:'H股LOF',             tq:'sz160717',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.95%'},
  {code:'501311',name:'新经济港通LOF',      tq:'sh501311',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.90%'},
  {code:'501301',name:'香港大盘LOF',        tq:'sh501301',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.90%'},
  {code:'164705',name:'恒生LOF',            tq:'sz164705',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'1.00%'},
  {code:'161831',name:'恒生国企LOF',        tq:'sz161831',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'1.20%'},
  {code:'501305',name:'港股高股息LOF',      tq:'sh501305',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.60%'},
  {code:'160924',name:'恒生指数LOF',        tq:'sz160924',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'1.20%'},
  {code:'501025',name:'香港银行LOF',        tq:'sh501025',cat:'hk',quota:'开放',   fee:'1.20%',rfee:'0.90%'},
  // A股行业LOF
  {code:'161226',name:'国投白银LOF',         tq:'sz161226',cat:'cm',quota:'暂停',   fee:'1.50%',rfee:'0.50%'},
  {code:'161217',name:'国投上游资源LOF',    tq:'sz161217',cat:'cn',quota:'开放',   fee:'1.50%',rfee:'0.50%'},
  {code:'161715',name:'招商大宗商品LOF',    tq:'sz161715',cat:'cn',quota:'开放',   fee:'1.50%',rfee:'0.50%'},
  {code:'161725',name:'招商中证白酒LOF',    tq:'sz161725',cat:'cn',quota:'开放',   fee:'1.50%',rfee:'0.50%'},
  {code:'161032',name:'富国中证煤炭LOF',    tq:'sz161032',cat:'cn',quota:'开放',   fee:'1.50%',rfee:'0.50%'},
];

// ── 外盘基准映射（完整47只） ────────────────────────
const BENCH = {
  '161127': 'usXBI',
  '164906': 'hkHSTECH',                                              // 持仓主体为港股中概，usKWEB(T-1)→hkHSTECH(实时)
  '501312': [{tq:'usQQQ',w:0.8},{tq:'hkHSTECH',w:0.1},{tq:'sh000985',w:0.1}],
  '164824': 'usINDA',
  '160644': [{tq:'usQQQ',w:0.5},{tq:'hkHSTECH',w:0.5}],            // 港美各半：GOOGL/NVDA/TSM + 腾讯/阿里HK
  '162415': 'usXLY',
  '161126': 'usRSPH',
  '161128': 'usXLK',
  '161125': 'usINX',
  '161130': 'usQQQ',
  '501300': 'usAGG',
  '160140': 'usRWR',
  '501225': 'usSMH',
  '160216': [{tq:'usSGOL',w:0.234},{tq:'usGLD',w:0.193},{tq:'usGLDM',w:0.154},{tq:'usUSO',w:0.153},{tq:'usSLV',w:0.151},{tq:'usCPER',w:0.143},{tq:'usXOP',w:0.038}],
  '161116': 'sh518880',
  '164701': 'usGLD',
  '165513': 'usGLD',
  '160719': 'sh518880',
  '161815': [{tq:'usGLD',w:0.171},{tq:'usIAU',w:0.168},{tq:'usAAAU',w:0.144},{tq:'usSGOL',w:0.139},{tq:'usBCI',w:0.122},{tq:'usCOMT',w:0.095},{tq:'usUSO',w:0.051},{tq:'usBNO',w:0.044},{tq:'usSLV',w:0.024},{tq:'usCPER',w:0.053}],
  '163208': [{tq:'usXLE',w:0.5},{tq:'hkHSCEI',w:0.5}],             // 全球油气：US油气ETF + HK能源/公用
  '501018': [{tq:'usUSO',w:0.6},{tq:'usBNO',w:0.4}],
  '161129': 'usUSO',
  '160723': 'usUSO',
  '162719': 'usXOP',
  '162411': 'usXOP',
  '160416': 'usIXC',
  '501303': 'hkHSMI',
  '161124': 'hkHSSI',
  '160322': 'hkHSCI',
  '501021': [{tq:'hkHSMI',w:0.5},{tq:'hkHSSI',w:0.5}],
  '501310': [{tq:'sh000300',w:0.5},{tq:'hkHSCEI',w:0.5}],
  '501302': 'hkHSI',
  '501307': 'csi930917',
  '501306': 'csi930914',
  '160717': 'hkHSCEI',
  '501311': 'hkHSTECH',
  '501301': 'hkHSCEI',
  '164705': 'hkHSI',
  '161831': 'hkHSCEI',
  '501305': 'csi930914',
  '160924': 'hkHSI',
  '501025': 'csi930792',
  '161226': 'sinaAG0',
  '161217': 'sz399961',
  '161715': 'sz399979',
  '161725': 'sz399987',
  '161032': 'sz399998',
};

// NO_GSZ_FUNDS 已移除 — NAV 统一由 GitHub Action 写入 fund_daily.json

// ── 东方财富代码映射（腾讯不支持或盘中返回0的指数的兜底数据源）────────────
// 注意：sinaAG0 (113.AG0) EM 返回 rc=100/data:null，已确认 EM 不支持该代码；
//       sinaAG0 只能由新浪 nf_AG0 提供，收盘后由 idx_closing.json 兜底。
const EM_CODES = {
  'csi930917': '2.930917',
  'csi930914': '2.930914',
  'csi930792': '2.930792',
  'sh000985':  '1.000985',
  'hkHSSI':    '124.HSSI',
  'hkHSMI':    '124.HSMI',
  'hkHSCI':    '124.HSCI',
  // sz399961/sz399979：腾讯已纳入实时拉取；EM 仅在腾讯返回 null 时填空（fill-only 策略）
  'sz399961':  '0.399961',  // 中证资源与环境（161217）
  'sz399979':  '0.399979',  // 中证大宗商品股票（161715）
};

// ── 商品类指数/期货（单日允许更大波动，±30% 阈值）────────
// 其余指数使用 ±20% 阈值（指数层面即使成分股打板也不会超过此幅度）
const COMMODITY_IDX = new Set([
  'sinaAG0',                                           // 上期所白银主力
  'usUSO','usBNO','usXOP','usIXC',                     // 原油/油气 ETF
  'usGLD','usGLDM','usIAU','usSGOL','usAAAU',          // 黄金 ETF
  'usSLV','usCPER',                                    // 白银/铜 ETF
  'usBCI','usCOMT',                                    // 大宗商品综合
  'usXLE',                                             // 能源板块
]);

// ── 交易日工具函数 ────────────────────────────────────
// trading_dates 由 sync_fund_data.py 每日写入 fund_daily.json._meta，滚动保留90个交易日。
// 历史窗口内：精确判断（有记录=交易日，无记录=节假日/周末）
// 历史窗口外：降级为周末判断（不影响 navLag，navLag 只看近期历史）

let _tradingDates = null;  // Set<string>，由 fetchAllData 注入

function setTradingDates(arr) {
  _tradingDates = new Set(arr || []);
}

/** 判断某日是否为交易日（dateStr: 'YYYY-MM-DD'） */
function isTradingDay(dateStr) {
  if (_tradingDates && _tradingDates.size > 0) {
    // 在窗口内：有记录=交易日，无记录=非交易日
    const sorted = [..._tradingDates].sort();
    if (dateStr >= sorted[0]) return _tradingDates.has(dateStr);
  }
  // 窗口外或未加载：降级周末判断
  const d = new Date(dateStr + 'T00:00:00Z');
  const dow = d.getUTCDay();
  return dow !== 0 && dow !== 6;
}

/** 计算两日期间（左开右闭）的交易日数，用于 navLag */
function tradingDayLag(fromDateStr, toDateStr) {
  if (!fromDateStr || !toDateStr || fromDateStr >= toDateStr) return 0;
  let count = 0;
  const end = new Date(toDateStr + 'T00:00:00Z');
  const cur = new Date(fromDateStr + 'T00:00:00Z');
  cur.setUTCDate(cur.getUTCDate() + 1);
  while (cur <= end) {
    const ds = cur.toISOString().slice(0, 10);
    if (isTradingDay(ds)) count++;
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
  return count;
}

/** 当前是否处于北京时间交易时段（09:15–15:00，且当天是交易日） */
function isBjTradingHours() {
  const now = new Date();
  const bj = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
  const bjDate = `${bj.getFullYear()}-${String(bj.getMonth()+1).padStart(2,'0')}-${String(bj.getDate()).padStart(2,'0')}`;
  const mins = bj.getHours() * 60 + bj.getMinutes();
  return isTradingDay(bjDate) && mins >= 555 && mins < 900;  // 09:15–15:00
}

/** 合理性校验：指数单日涨跌幅是否在合理范围内（过滤解析脏数据） */
function idxSanityOk(tqCode, chg) {
  if (chg == null || !isFinite(chg)) return false;
  const limit = COMMODITY_IDX.has(tqCode) ? 30 : 20;
  return Math.abs(chg) <= limit;
}

// ── Header Ticker 指数 ────────────────────────────────
const TICKER_IDX = [
  {tq:'usIXIC', label:'纳斯达克'},
  {tq:'usINX',  label:'标普500'},
  {tq:'hkHSI',  label:'恒生'},
  {tq:'usGLD',  label:'黄金GLD'},
  {tq:'usXOP',  label:'油气XOP'},
  {tq:'usUSO',  label:'原油USO'},
];

// 需要向腾讯请求的所有代码（去重）
function getAllTqCodes() {
  const set = new Set();
  FUNDS.forEach(f => set.add(f.tq));
  Object.values(BENCH).forEach(b => {
    if (Array.isArray(b)) b.forEach(x => set.add(x.tq));
    else set.add(b);
  });
  TICKER_IDX.forEach(i => set.add(i.tq));
  // 新浪/东财专属，不走腾讯
  // sz399961/sz399979：腾讯实时可访问，不排除（EM fill-only 兜底）
  // sinaAG0 腾讯不识别，替换为 nf_AG0（白银主力期货，解析后映射回 sinaAG0）
  ['sinaAG0','csi930917','csi930914','csi930792','sh000985',
   'hkHSSI','hkHSMI','hkHSCI'].forEach(c => set.delete(c));
  set.add('nf_AG0');
  return [...set];
}

// ── 持仓代码工具 ──────────────────────────────────────

/**
 * 将 fund_daily.json holdings[].code 转为腾讯行情代码
 * HK 5位数字 → hkXXXXX
 * A股 6位数字 → sh/szXXXXXX（6/7/8/9开头→SH，0/3开头→SZ）
 * US 纯字母  → usXXX（昨收价，A股时段美市休市）
 * 含特殊字符 / 格式异常 → null（跳过）
 */
function holdingToTqCode(raw) {
  if (!raw || typeof raw !== 'string') return null;
  const code = raw.trim().replace(/\.(HK|US|SH|SZ)$/i, '');
  if (!code || code.includes('.') || code.includes(' ')) return null;
  if (/^\d{5}$/.test(code)) return 'hk' + code;
  if (/^\d{6}$/.test(code)) return ('6789'.includes(code[0]) ? 'sh' : 'sz') + code;
  if (/^[A-Z]{1,5}$/.test(code)) return 'us' + code;
  return null;
}

/** 从 fund_daily.json 收集所有持仓的腾讯代码（去重） */
function getHoldingTqCodes(daily) {
  if (!daily) return [];
  const set = new Set();
  for (const [code, fund] of Object.entries(daily)) {
    if (code.startsWith('_') || !fund.holdings) continue;
    for (const h of fund.holdings) {
      const tq = holdingToTqCode(h.code);
      if (tq) set.add(tq);
    }
  }
  return [...set];
}

// 状态跃迁报警：KV 持久化，key=alert:{code}，value="above"|"below"

// ── CME 期货映射（A股时段美市休市，期货仍 23h 交易） ──────────
// 用 Yahoo Finance regularMarketChangePercent，该字段在合约内计算
// 自动规避换月跳价：换月后新合约以当日开盘为基准，不出现隔日大跳
const FUTURES_MAP = {
  'NQ%3DF': ['usQQQ', 'usIXIC', 'usXLK', 'usSMH'],  // 纳指100期货 → QQQ/XLK/SMH
  'ES%3DF': ['usINX'],                                  // 标普500期货 → INX
};

/**
 * 从 Yahoo Finance 拉取 CME 期货实时涨跌幅
 * 返回 { usQQQ: chgPct, usINX: chgPct, ... }，失败时返回 {}
 * 注：仅 Worker 端可调用（浏览器有 CORS 限制）
 */
async function fetchYahooFutures() {
  const overrides = {};
  await Promise.all(Object.entries(FUTURES_MAP).map(async ([sym, tqCodes]) => {
    try {
      const url = `https://query1.finance.yahoo.com/v8/finance/chart/${sym}?interval=1m&range=1d`;
      const resp = await fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' },
        cf: { cacheTtl: 60 },
      });
      if (!resp.ok) return;
      const data = await resp.json();
      const meta = data?.chart?.result?.[0]?.meta;
      if (!meta) return;
      const chgPct = meta.regularMarketChangePercent;
      if (chgPct == null || !isFinite(chgPct)) return;
      for (const tq of tqCodes) overrides[tq] = chgPct;
      console.log(`[futures] ${sym} → ${chgPct.toFixed(3)}% → [${tqCodes.join(',')}]`);
    } catch (e) {
      console.warn(`[futures] ${sym} 拉取失败:`, e.message);
    }
  }));
  return overrides;
}

// ══════════════════════════════════════════════════════
//  数据获取函数（Worker 环境，使用 fetch()，非浏览器）
// ══════════════════════════════════════════════════════

/**
 * 腾讯行情：批量拉取所有代码（含持仓个股）
 * 返回 { funds, indices, stockChg:{tqCode:chgPct} }
 * daily 已知时将持仓 tq 码一并打入同一请求，零额外 HTTP
 */
async function fetchTencent(daily = null) {
  const holdingTqs = getHoldingTqCodes(daily);
  const codes = [...new Set([...getAllTqCodes(), ...holdingTqs])].join(',');
  const url = `https://qt.gtimg.cn/q=${codes}`;
  try {
    const resp = await fetch(url, {
      headers: {
        'Referer': 'https://gu.qq.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
      }
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const buf = await resp.arrayBuffer();
    const text = new TextDecoder('gbk').decode(buf);

    const result = { funds: {}, indices: {} };

    // 解析每行 var v_CODE="...";
    const lines = text.split('\n');
    const lineMap = {};
    for (const line of lines) {
      const m = line.match(/^(?:var\s+)?v_(\w+)="([^"]*)"/);
      if (m) lineMap[m[1]] = m[2];
    }

    // 基金行情
    for (const f of FUNDS) {
      const key = f.tq;
      const raw = lineMap[key];
      if (!raw || raw.length < 10) continue;
      const p = raw.split('~');
      const price = parseFloat(p[3]);
      const prev = parseFloat(p[4]);
      const vol = parseFloat(p[37]) || 0;
      if (price > 0) {
        result.funds[f.code] = {
          price,
          prevClose: prev,
          chg: prev > 0 ? (price - prev) / prev * 100 : 0,
          vol,
        };
      }
    }

    // 指数行情（用于估值+Ticker）
    const allIdxCodes = new Set();
    Object.values(BENCH).forEach(b => {
      if (Array.isArray(b)) b.forEach(x => allIdxCodes.add(x.tq));
      else allIdxCodes.add(b);
    });
    TICKER_IDX.forEach(i => allIdxCodes.add(i.tq));
    // 排除新浪/东财专属，不在腾讯请求中
    // sz399961/sz399979：腾讯可访问，实时数据由腾讯提供（EM fill-only 兜底）
    // sinaAG0：仅新浪 nf_AG0 有效，腾讯无此代码
    ['sinaAG0','csi930917','csi930914','csi930792','sh000985',
     'hkHSSI','hkHSMI','hkHSCI'].forEach(c => allIdxCodes.delete(c));

    for (const tqCode of allIdxCodes) {
      const raw = lineMap[tqCode];
      if (!raw || raw.length < 5) continue;
      const p = raw.split('~');
      const price = parseFloat(p[3]);
      const prev = parseFloat(p[4]);
      if (price > 0) {
        result.indices[tqCode] = {
          price,
          chg: prev > 0 ? (price - prev) / prev * 100 : 0,
        };
      }
    }

    // 白银期货：nf_AG0 → sinaAG0（腾讯兜底；Sina 可用时在后续 merge 中覆盖）
    const rawAG0 = lineMap['nf_AG0'];
    if (rawAG0 && rawAG0.length > 5) {
      const p = rawAG0.split('~');
      const price = parseFloat(p[3]), prev = parseFloat(p[4]);
      if (price > 0 && prev > 0) result.indices['sinaAG0'] = { price, chg: (price - prev) / prev * 100 };
    }

    // 持仓个股涨跌幅（Plan A：仅 HK + A股；US 跳过，归入 bench 残差）
    const stockChg = {};
    if (daily) {
      for (const [code, fund] of Object.entries(daily)) {
        if (code.startsWith('_') || !fund.holdings) continue;
        for (const h of fund.holdings) {
          const tq = holdingToTqCode(h.code);
          if (!tq || tq.startsWith('us')) continue; // US 股跳过
          if (stockChg[tq] !== undefined) continue;  // 已解析
          const raw = lineMap[tq];
          if (!raw || raw.length < 5) continue;
          const p = raw.split('~');
          const price = parseFloat(p[3]);
          const prev  = parseFloat(p[4]);
          if (price > 0 && prev > 0) stockChg[tq] = (price - prev) / prev * 100;
        }
      }
    }
    result.stockChg = stockChg;

    return result;
  } catch (e) {
    console.error('腾讯行情失败:', e.message);
    return { funds: {}, indices: {}, stockChg: {} };
  }
}

/**
 * 东方财富：拉取腾讯不支持的指数（CSI/HSSI/HSMI/HSCI等）
 * 返回 { key: chgPct }
 */
async function fetchEastmoney() {
  const results = await Promise.allSettled(
    Object.entries(EM_CODES).map(async ([key, secid]) => {
      try {
        const url = `https://push2.eastmoney.com/api/qt/stock/get?secid=${secid}&fields=f43,f169,f170`;
        const resp = await fetch(url, {
          headers: {
            'Referer':    'https://quote.eastmoney.com/',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept':     'application/json, text/plain, */*',
          },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const d = await resp.json();
        // 不依赖 f43：CSI 计算型指数 f43 可能为 null/0，但 f170 仍有效
        if (d.data && d.data.f170 != null) {
          return [key, d.data.f170 / 100];
        }
        return null;
      } catch (e) {
        console.error(`东财 ${key} 失败:`, e.message);
        return null;
      }
    })
  );
  const out = {};
  results.forEach(r => {
    if (r.status === 'fulfilled' && r.value) out[r.value[0]] = r.value[1];
  });
  return out;
}

/**
 * 新浪财经：拉取白银AG0（nf_AG0）+ A股指数 sz399987/sz399998 + 实时汇率
 * sinaAG0 唯一来源：新浪 nf_AG0（EM 113.AG0 不支持，返回 rc=100/data:null）
 * sz399961/sz399979：腾讯实时拉取，EM fill-only 兜底；新浪无对应 API
 * 返回 { sinaAG0: chg, sz399987: chg, sz399998: chg, _fxUsdCnh, _fxHkdCnh }
 */
async function fetchSina() {
  try {
    // fx_shkdcnh = 正确的港元兑离岸人民币代码（fx_shkcnh 为无效代码，返回空串）
    const list = 'nf_AG0,sz399987,sz399998,fx_susdcnh,fx_shkdcnh';
    const url = `https://hq.sinajs.cn/list=${list}`;
    const resp = await fetch(url, {
      headers: {
        'Referer':          'https://finance.sina.com.cn/',
        'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept':           '*/*',
        'Accept-Language':  'zh-CN,zh;q=0.9',
      }
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const buf = await resp.arrayBuffer();
    const text = new TextDecoder('gbk').decode(buf);
    const out = {};

    // 期货：nf_AG0（白银主连）p[5]=现价，p[10]=前结算价
    const mAG = text.match(/hq_str_nf_AG0="([^"]+)"/);
    if (mAG) {
      const p = mAG[1].split(',');
      const cur = parseFloat(p[5]), prev = parseFloat(p[10]);
      if (cur > 0 && prev > 0) out.sinaAG0 = (cur - prev) / prev * 100;
    }

    // A股指数：p[3]=现价，p[2]=昨收（sz399961/sz399979 由 EM_CODES 提供，不在此列）
    for (const code of ['sz399987', 'sz399998']) {
      const re = new RegExp(`hq_str_${code}="([^"]+)"`);
      const m = text.match(re);
      if (m) {
        const p = m[1].split(',');
        const cur = parseFloat(p[3]), prev = parseFloat(p[2]);
        if (cur > 0 && prev > 0) out[code] = (cur - prev) / prev * 100;
      }
    }

    // FX 现价（绝对值，非涨跌幅）供 calcAdjustedBenchChg 使用
    // fx_susdcnh = USD/CNH 离岸人民币；fx_shkdcnh = HKD/CNH（注意：fx_shkcnh 为无效代码）
    for (const [sinaCode, outKey] of [['fx_susdcnh', '_fxUsdCnh'], ['fx_shkdcnh', '_fxHkdCnh']]) {
      const re = new RegExp(`hq_str_${sinaCode}="([^"]+)"`);
      const m = text.match(re);
      if (m) {
        const rate = parseFloat(m[1].split(',')[1]);
        if (rate > 0) out[outKey] = rate;
      }
    }

    return out;
  } catch (e) {
    console.error('新浪行情失败:', e.message);
    return {};
  }
}

// ── NAV 由 GitHub Action 统一写入 fund_daily.json，Worker 不做任何抓取 ──

// ══════════════════════════════════════════════════════
//  核心计算：溢价率
// ══════════════════════════════════════════════════════

/**
 * 根据 idxChg 和 BENCH 计算基准涨跌幅
 */
function calcBenchChg(code, idxChg) {
  const benchDef = BENCH[code];
  if (!benchDef) return null;
  let result;
  if (Array.isArray(benchDef)) {
    let benchChg = 0, totalW = 0;
    benchDef.forEach(b => {
      const chg = idxChg[b.tq];
      if (chg != null) {           // 缺失分量不计入分母，避免拉低结果
        benchChg += chg * b.w;
        totalW += b.w;
      }
    });
    result = totalW > 0 ? benchChg / totalW : null;  // 全部缺失 → null，不是 0
  } else {
    result = idxChg[benchDef] ?? null;  // 未抓到 → null，与真实 0% 区分
  }
  // 加权结果合理性校验：复合 bench 超过 ±20% 视为脏数据
  if (result != null && Math.abs(result) > 20) {
    console.warn(`[sanity] ${code} benchChg=${result.toFixed(2)}% 超出合理范围，返回null`);
    return null;
  }
  return result;
}

/**
 * FX 修正版基准涨跌幅计算
 * 对每个 bench 分量独立叠加汇率变化：(1+bench_chg%/100)×(1+fx_chg%/100)−1
 * us* → USD/CNH；hk* → HKD/CNH；sh/sz/csi/sina* → 无 FX
 * fxChgUsd / fxChgHkd 为空时自动降级为纯指数估值
 */
function calcAdjustedBenchChg(code, idxChg, fxChgUsd, fxChgHkd) {
  function fxForCode(tqCode) {
    if (tqCode.startsWith('us')) return fxChgUsd || 0;
    if (tqCode.startsWith('hk')) return fxChgHkd || 0;
    return 0; // sh/sz/csi/sina — CNY 计价，无需 FX
  }
  const benchDef = BENCH[code];
  if (!benchDef) return null;
  if (Array.isArray(benchDef)) {
    let navReturn = 0, totalW = 0;
    benchDef.forEach(b => {
      const ic = idxChg[b.tq];
      if (ic == null) return;      // 与 calcBenchChg 对齐：缺失分量跳过，不补 0
      const fx = fxForCode(b.tq);
      navReturn += ((1 + ic / 100) * (1 + fx / 100) - 1) * b.w;
      totalW += b.w;
    });
    return totalW > 0 ? navReturn / totalW * 100 : null;  // 全部缺失 → null
  }
  const ic = idxChg[benchDef];
  if (ic == null) return null;
  const fx = fxForCode(benchDef);
  return ((1 + ic / 100) * (1 + fx / 100) - 1) * 100;
}

/**
 * 动态持仓加权估值
 * HK + A 股持仓个股：逐笔加权 × FX 修正
 * 无价格的分量（含全部 US 股）：用 bench 残差填补
 */
function calcDynamicNavReturn(code, idxChg, stockChg, fxChgUsd, fxChgHkd, daily) {
  const holdings = daily && daily[code] && daily[code].holdings;
  if (!holdings || !holdings.length) {
    return calcAdjustedBenchChg(code, idxChg, fxChgUsd, fxChgHkd);
  }
  let coveredReturn = 0, coveredW = 0;
  for (const h of holdings) {
    const tq = holdingToTqCode(h.code);
    if (!tq || tq.startsWith('us')) continue; // US 在 A 股时段无盘中价，归残差
    const chg = stockChg[tq];
    if (chg == null) continue;
    const w  = h.ratio / 100;
    const fx = tq.startsWith('hk') ? (fxChgHkd || 0) : 0;
    coveredReturn += ((1 + chg / 100) * (1 + fx / 100) - 1) * w;
    coveredW += w;
  }
  const adjBench = calcAdjustedBenchChg(code, idxChg, fxChgUsd, fxChgHkd);
  if (adjBench == null && coveredW < 1.0) return null;  // 残差无法填补 → 不计算
  const benchReturn = (adjBench ?? 0) / 100;
  return (coveredReturn + benchReturn * (1 - coveredW)) * 100;
}

/**
 * 该基金所需汇率是否全部取到
 * us* bench → 需要 fxChgUsd；hk* bench → 需要 fxChgHkd；cn/csi → 不需要 FX
 */
function calcFxOk(code, fxChgUsd, fxChgHkd) {
  const benchDef = BENCH[code];
  if (!benchDef) return true;
  const tqs = Array.isArray(benchDef) ? benchDef.map(b => b.tq) : [benchDef];
  if (tqs.some(c => c.startsWith('us')) && fxChgUsd == null) return false;
  if (tqs.some(c => c.startsWith('hk')) && fxChgHkd == null) return false;
  return true;
}

/**
 * 持仓覆盖率：有实时价格的持仓权重 / 全部持仓权重（US 权重不计入分子）
 */
function calcHoldingCoverage(code, stockChg, daily) {
  const holdings = daily && daily[code] && daily[code].holdings;
  if (!holdings || !holdings.length) return 0;
  let totalW = 0, coveredW = 0;
  for (const h of holdings) {
    const w = h.ratio / 100;
    totalW += w;
    const tq = holdingToTqCode(h.code);
    if (!tq || tq.startsWith('us')) continue;
    if (stockChg[tq] != null) coveredW += w;
  }
  return totalW > 0 ? coveredW / totalW : 0;
}

/**
 * 聚合全量数据，计算溢价率，返回统一 JSON
 */
async function fetchAllData(env = {}) {
  // 先加载 daily + closing（30 分钟内存缓存，通常即时返回）
  const [daily, closingData] = await Promise.all([
    loadFundDaily(env),
    loadIdxClosing(env),
  ]);

  // 注入交易日历（供 isTradingDay / tradingDayLag 使用）
  setTradingDates(daily && daily._meta && daily._meta.trading_dates);

  // 再并行拉取行情（fetchTencent 需要 daily 来批量加持仓代码）
  // allSettled 断点隔离：任何单路失败不阻塞其他数据源；各 fetch 函数内部也有 try/catch
  const [tqRes, emRes, sinaRes, futRes] = await Promise.allSettled([
    fetchTencent(daily),
    fetchEastmoney(),
    fetchSina(),
    fetchYahooFutures(),
  ]);
  const tqData           = tqRes.status   === 'fulfilled' ? tqRes.value   : { funds: {}, indices: {}, stockChg: {} };
  const emIdx            = emRes.status   === 'fulfilled' ? emRes.value   : {};
  const sinaIdx          = sinaRes.status === 'fulfilled' ? sinaRes.value : {};
  const futuresOverrides = futRes.status  === 'fulfilled' ? futRes.value  : {};
  // 记录哪些源整体失败（区别于"源正常但数据为空"）
  if (tqRes.status  === 'rejected') console.error('[fetch] 腾讯整体失败:', tqRes.reason);
  if (emRes.status  === 'rejected') console.error('[fetch] 东财整体失败:', emRes.reason);
  if (sinaRes.status === 'rejected') console.warn ('[fetch] 新浪整体失败:', sinaRes.reason);
  if (futRes.status  === 'rejected') console.warn ('[fetch] Yahoo整体失败:', futRes.reason);

  const stockChg = tqData.stockChg || {};

  // 从 fund_daily.json 提取 navMap
  const navMap = {};
  if (daily) {
    for (const [code, fund] of Object.entries(daily)) {
      if (!code.startsWith('_') && fund.nav > 0) {
        navMap[code] = { nav: fund.nav, date: fund.nav_date || '' };
      }
    }
  }

  // FX 实时现价 + T-1 结算价 → 日内涨跌幅
  const fxUsdCnh   = sinaIdx._fxUsdCnh;
  const fxHkdCnh   = sinaIdx._fxHkdCnh;
  const t1Fx       = daily && daily['_fx'];
  // null 表示"未取到"，calcAdjustedBenchChg 内部用 fxChgUsd||0 降级计算
  // 但 null 本身会被 calcFxOk() 检测，输出 fxOk=false 标记估值精度下降
  const fxChgUsd   = (fxUsdCnh && t1Fx && t1Fx.usd_cnh_t1)
    ? (fxUsdCnh / t1Fx.usd_cnh_t1 - 1) * 100 : null;
  const fxChgHkd   = (fxHkdCnh && t1Fx && t1Fx.hkd_cnh_t1)
    ? (fxHkdCnh / t1Fx.hkd_cnh_t1 - 1) * 100 : null;

  // 合并指数涨跌幅（_fx* 键不写入 idxChg；合理性校验过滤脏数据）
  const idxChg = {};
  function setIdx(code, chg) {
    if (idxSanityOk(code, chg)) {
      idxChg[code] = chg;
    } else {
      console.warn(`[sanity] ${code} chg=${chg} 超出合理范围，丢弃`);
    }
  }
  Object.entries(tqData.indices).forEach(([code, d]) => setIdx(code, d.chg));
  // EM 填空策略：仅填充腾讯未抓到的指数，不覆盖腾讯已有的实时值
  Object.entries(emIdx).forEach(([code, chg]) => { if (idxChg[code] == null) setIdx(code, chg); });
  // Sina 覆盖策略：Sina 优先级最高（nf_AG0 更实时），允许覆盖 EM 的值
  Object.entries(sinaIdx).forEach(([code, chg]) => {
    if (!code.startsWith('_')) setIdx(code, chg);
  });

  // CME 期货覆盖（A股时段实时，优先级高于腾讯T-1数据）
  Object.entries(futuresOverrides).forEach(([tq, chg]) => setIdx(tq, chg));

  // 降级兜底
  if (idxChg['hkHSMI'] == null || idxChg['hkHSMI'] === 0) {
    if (idxChg['hkHSSI'] != null) idxChg['hkHSMI'] = idxChg['hkHSSI'];
    else if (idxChg['hkHSI'] != null) idxChg['hkHSMI'] = idxChg['hkHSI'];
  }
  if (idxChg['hkHSSI'] == null || idxChg['hkHSSI'] === 0) {
    if (idxChg['hkHSI'] != null) idxChg['hkHSSI'] = idxChg['hkHSI'];
  }
  if (idxChg['hkHSCI'] == null || idxChg['hkHSCI'] === 0) {
    if (idxChg['hkHSI'] != null) idxChg['hkHSCI'] = idxChg['hkHSI'];
  }

  // 收盘快照 fallback：补充非交易时段 null 的指数代码
  // 数据来自 data/idx_closing.json（closing-data-sync.yml 每日 15:05 写入）
  // 遵守 staleness_policy：sync_at 超过 36 小时视为过期，不回填
  // 交易时段（09:15-15:00）内禁用：此时应显示"指数缺失"而非用昨日收盘数据误导用户
  const staleIdxCodes = new Set();
  if (closingData && !isBjTradingHours()) {
    const MAX_STALE_MS = 36 * 3600 * 1000;
    const nowMs = Date.now();
    for (const [code, entry] of Object.entries(closingData)) {
      if (code.startsWith('_')) continue;
      if (idxChg[code] != null) continue;          // 实时数据已有，不覆盖
      if (!entry || entry.chg == null || !entry.sync_at) continue;
      const age = nowMs - new Date(entry.sync_at).getTime();
      if (age > MAX_STALE_MS) continue;            // 数据过期，不回填
      if (!idxSanityOk(code, entry.chg)) continue; // 合理性校验
      idxChg[code] = entry.chg;
      staleIdxCodes.add(code);
    }
    if (staleIdxCodes.size > 0) {
      console.log('[closing] 收盘快照回填:', [...staleIdxCodes].join(','));
    }
  }

  // 计算每只基金
  const funds = FUNDS.map(f => {
    const tq = tqData.funds[f.code];
    const navInfo = navMap[f.code];
    const officialNav = navInfo ? navInfo.nav : null;
    const navDate = navInfo ? navInfo.date : '';

    let price = null, chg = null, vol = 0, prevClose = null;
    if (tq) {
      price = tq.price;
      chg = tq.chg;
      vol = tq.vol;
      prevClose = tq.prevClose;
    }

    const benchChg       = calcBenchChg(f.code, idxChg);       // 纯指数涨幅（用于显示），null = 未抓到
    const adjBenchChg    = calcAdjustedBenchChg(f.code, idxChg, fxChgUsd, fxChgHkd); // FX修正基准
    const fxAdj          = (adjBenchChg != null && benchChg != null) ? adjBenchChg - benchChg : null;
    const dynNavReturn   = calcDynamicNavReturn(f.code, idxChg, stockChg, fxChgUsd, fxChgHkd, daily);
    const benchOk        = benchChg != null;                   // false → 指数未抓到，估值不可信
    const benchDef_      = BENCH[f.code];
    const benchStale     = benchDef_                           // true → 指数来自收盘快照（非实时）
      ? (Array.isArray(benchDef_) ? benchDef_.some(b => staleIdxCodes.has(b.tq)) : staleIdxCodes.has(benchDef_))
      : false;
    const fxOk           = calcFxOk(f.code, fxChgUsd, fxChgHkd); // false → 汇率缺失，估值未计入即时汇率
    const holdingCoverage = calcHoldingCoverage(f.code, stockChg, daily);

    // 偏差校准：Hard Enforcement — 宁可无补偿，不可乱补偿
    // 前置条件：drift_computed_at ≤2天 AND drift_n ≥3；否则 alpha=0（禁用补偿）
    const fundDaily         = daily && daily[f.code];
    const drift5d           = fundDaily ? (fundDaily.drift_5d           || 0)   : 0;
    const driftN            = fundDaily ? (fundDaily.drift_n            || 0)   : 0;
    const driftComputedAt   = fundDaily ? (fundDaily.drift_computed_at  || null): null;
    const driftLagDays      = driftComputedAt
      ? Math.round((Date.now() - new Date(driftComputedAt).getTime()) / 86400000)
      : 99;
    const driftActive  = drift5d !== 0 && driftN >= 3 && driftLagDays <= 2;
    const alpha        = driftActive ? Math.max(-0.02, Math.min(0.02, drift5d)) : 0;
    const driftStatus  = driftActive ? 'ACTIVE' : 'SUSPENDED';

    // T-2 检测：计算 nav_date 距今交易日数，≥2 表示滞后超过1个交易日
    // 使用交易日而非日历日，避免周一 navLag=3 误触发链式补偿
    const todayStr = new Date().toISOString().slice(0, 10);
    const navLag   = navDate ? tradingDayLag(navDate, todayStr) : 99;

    // T-2 链式修正：前提是 nav_fetch_time 足够新（≤36h）才可信
    // 若 fetch_time 陈旧，说明数据源本身有问题，不应盲目信任链式推算
    const estNavYesterday = fundDaily ? (fundDaily.est_nav_yesterday || null) : null;
    const navFetchTime    = fundDaily ? (fundDaily.nav_fetch_time || null) : null;
    const fetchAgeH       = navFetchTime
      ? (Date.now() - new Date(navFetchTime).getTime()) / 3600000
      : 999;
    const useChained = estNavYesterday && navLag >= 2 && fetchAgeH <= 36;
    const base       = useChained ? estNavYesterday : (officialNav || prevClose);

    let nav = null, premium = null;
    if (base > 0 && price != null && dynNavReturn != null) {  // dynNavReturn null = 指数缺失
      nav = base * (1 + dynNavReturn / 100) * (1 + alpha);
      premium = (price - nav) / nav * 100;
    }

    return {
      code: f.code,
      name: f.name,
      cat: f.cat,
      price,
      prevClose,
      chg,
      nav,
      officialNav,
      navDate,
      navLag,
      premium,
      benchChg,
      benchOk,
      benchStale,
      fxOk,
      fxAdj,
      holdingCoverage,
      useChained,
      estNavYesterday: useChained ? estNavYesterday : null,
      holdingsDate: fundDaily ? (fundDaily.holdings_date || null) : null,
      drift5d,
      driftN,
      driftStatus,
      quota: f.quota,
      fee: f.fee,
      rfee: f.rfee,
      vol,
      _src: 'W',   // 数据来源标记：W = Worker 聚合
    };
  });

  // Ticker 指数数据
  const indices = {};
  TICKER_IDX.forEach(i => {
    const d = tqData.indices[i.tq];
    if (d) indices[i.tq] = d.chg;
  });

  // ── 数据源状态快照 ─────────────────────────────────
  // 记录本次各路 fetch 是否有效，以及哪些 bench 指数缺失
  const benchCodesNeeded = new Set();
  FUNDS.forEach(f => {
    const b = BENCH[f.code];
    if (!b) return;
    if (Array.isArray(b)) b.forEach(x => benchCodesNeeded.add(x.tq));
    else benchCodesNeeded.add(b);
  });
  const idxMissing = [...benchCodesNeeded].filter(c => idxChg[c] == null);

  const fetchStatus = {
    tencent:   Object.keys(tqData.funds).length > 0 || Object.keys(tqData.indices).length > 0,
    eastmoney: Object.keys(emIdx).length > 0,
    sina:      sinaIdx._fxUsdCnh != null,
    yahoo:     Object.keys(futuresOverrides).length > 0,
    fxOk:      fxChgUsd != null && fxChgHkd != null,  // 汇率数据是否完整
    idxMissing,  // bench 用到但未能取到的指数代码列表
    closingFallback: staleIdxCodes.size > 0,          // 是否有指数来自收盘快照
    staleIdxCodes: [...staleIdxCodes],                 // 具体哪些指数来自收盘快照
  };

  return {
    funds,
    indices,
    fx: {
      usd_cnh:    fxUsdCnh  || null,
      hkd_cnh:    fxHkdCnh  || null,
      usd_cnh_t1: t1Fx ? t1Fx.usd_cnh_t1 : null,
      hkd_cnh_t1: t1Fx ? t1Fx.hkd_cnh_t1 : null,
      chg_usd:    fxChgUsd  || null,
      chg_hkd:    fxChgHkd  || null,
    },
    fetchStatus,
    ts: Date.now(),
  };
}

// ══════════════════════════════════════════════════════
//  工具函数
// ══════════════════════════════════════════════════════

function toBeijingDate(ts) {
  const d = new Date(ts);
  const offset = 8 * 60 * 60 * 1000;
  return new Date(d.getTime() + offset).toISOString().slice(0, 10);
}

function corsHeaders(origin) {
  const allowed = ['https://lakefront5027.github.io'];
  const ao = (origin && allowed.includes(origin)) ? origin : '*';
  return {
    'Access-Control-Allow-Origin': ao,
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Cache-Control',
    'Access-Control-Max-Age': '86400',
    'Vary': 'Origin',
  };
}

function jsonResp(data, status = 200, origin) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json;charset=UTF-8',
      ...corsHeaders(origin),
    },
  });
}

// ══════════════════════════════════════════════════════
//  企业微信 Webhook 通知
// ══════════════════════════════════════════════════════

async function sendWechatAlert(fund) {
  if (!CONFIG.WECHAT_WEBHOOK) return;

  const isPremium = fund.premium > 0;
  const direction = isPremium ? '溢价卖出套利' : '折价买入申购套利';
  const premStr = (fund.premium >= 0 ? '+' : '') + fund.premium.toFixed(2) + '%';
  const navStr = fund.nav != null ? fund.nav.toFixed(4) : '—';
  const priceStr = fund.price != null ? fund.price.toFixed(4) : '—';
  const benchStr = (fund.benchChg >= 0 ? '+' : '') + fund.benchChg.toFixed(2) + '%';

  const content = [
    '**LOF套利预警** 🚨',
    `基金：${fund.name} (${fund.code})`,
    `溢价率：${premStr}`,
    `估值：${navStr} | 场内价：${priceStr}`,
    `指数涨幅：${benchStr}`,
    `建议：${direction}`,
  ].join('\n');

  try {
    await fetch(CONFIG.WECHAT_WEBHOOK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        msgtype: 'markdown',
        markdown: { content },
      }),
    });
  } catch (e) {
    console.error('企业微信通知失败:', e.message);
  }
}

// 模块级报警状态（代替 KV，同一 isolate 生命周期内去重）
// isolate 回收后状态重置，最多多发一次通知，成本可控。
const _alertState = {}; // { [code]: 'above' | 'below' }

async function checkAndAlert(funds) {
  if (!CONFIG.WECHAT_WEBHOOK) return;

  for (const fund of funds) {
    if (fund.premium == null) continue;

    const isAbove = Math.abs(fund.premium) >= CONFIG.ALERT_THRESHOLD;
    const prevState = _alertState[fund.code] || null;

    if (isAbove) {
      if (prevState !== 'above') await sendWechatAlert(fund); // 跃迁时推送
      _alertState[fund.code] = 'above';
    } else {
      if (prevState === 'above') _alertState[fund.code] = 'below';
    }
  }
}

// ══════════════════════════════════════════════════════
//  Mode A: HTTP Handler
// ══════════════════════════════════════════════════════

async function handleRequest(request, env = {}) {
  const url = new URL(request.url);
  const path = url.pathname;
  const origin = request.headers.get('Origin') || '';

  // CORS preflight
  if (request.method === 'OPTIONS') {
    return new Response(null, { headers: corsHeaders(origin) });
  }

  // GET /api/ping — 存活探针（100ms 超时判断，立即响应）
  if (path === '/api/ping') {
    return new Response(JSON.stringify({ ok: 1, ts: Date.now() }), {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...corsHeaders(origin) },
    });
  }

  // GET /api/nav — 所有基金 T-1 净值（只读 fund_daily.json，不做实时抓取）
  if (path === '/api/nav') {
    try {
      const daily = await loadFundDaily(env);
      const navMap = {};
      if (daily) {
        for (const [code, fund] of Object.entries(daily)) {
          if (!code.startsWith('_') && fund.nav > 0) {
            navMap[code] = { nav: fund.nav, date: fund.nav_date || '', src: fund.nav_src || 'daily' };
          }
        }
      }
      return jsonResp(navMap, 200, origin);
    } catch (e) {
      return jsonResp({}, 500, origin);
    }
  }

  // GET /api/daily — 透传 fund_daily.json（含 nav + holdings + bench 配置）
  if (path === '/api/daily') {
    try {
      const daily = await loadFundDaily(env);
      if (!daily) return jsonResp({ error: 'unavailable' }, 503, origin);
      return new Response(JSON.stringify(daily), {
        status: 200,
        headers: {
          'Content-Type': 'application/json;charset=UTF-8',
          'Cache-Control': 'public, max-age=1800, s-maxage=1800',
          ...corsHeaders(origin),
        },
      });
    } catch (e) {
      return jsonResp({ error: e.message }, 500, origin);
    }
  }

  // GET /api/snapshot — 聚合快照（NAV 由 fund_daily.json 提供，Worker 不抓取）
  if (path === '/api/snapshot') {
    try {
      const data = await fetchAllData(env);
      return new Response(JSON.stringify(data), {
        status: 200,
        headers: {
          'Content-Type': 'application/json;charset=UTF-8',
          'Cache-Control': 'public, max-age=12, s-maxage=12',
          'CF-Cache-Status': 'DYNAMIC',
          ...corsHeaders(origin),
        },
      });
    } catch (e) {
      console.error('/api/snapshot 失败:', e);
      return jsonResp({ error: e.message }, 500, origin);
    }
  }

  // GET /api/quote — 全量数据（旧路径，保持兼容）
  if (path === '/api/quote') {
    try {
      const data = await fetchAllData(env);
      return jsonResp(data, 200, origin);
    } catch (e) {
      console.error('/api/quote 失败:', e);
      return jsonResp({ error: e.message }, 500, origin);
    }
  }

  // GET /api/sina?callback=xxx — 新浪代理（JSONP模式，返回解析后的涨跌幅）
  // GET /api/sina        — 普通JSON模式
  if (path === '/api/sina') {
    const callback = url.searchParams.get('callback');
    try {
      const sinaData = await fetchSina();
      if (callback) {
        const body = `${callback}(${JSON.stringify(sinaData)});`;
        return new Response(body, {
          status: 200,
          headers: {
            'Content-Type': 'application/javascript;charset=UTF-8',
            'Cache-Control': 'no-cache',
            ...corsHeaders(origin),
          },
        });
      }
      return jsonResp(sinaData, 200, origin);
    } catch (e) {
      if (callback) {
        return new Response(`${callback}({});`, {
          status: 200,
          headers: { 'Content-Type': 'application/javascript;charset=UTF-8', ...corsHeaders(origin) },
        });
      }
      return new Response('', { status: 502, headers: corsHeaders(origin) });
    }
  }

  // 404
  return new Response('Not found', { status: 404, headers: corsHeaders(origin) });
}

// ══════════════════════════════════════════════════════
//  Mode B: Scheduled Cron
// ══════════════════════════════════════════════════════

async function sendDailySummary(funds) {
  if (!CONFIG.WECHAT_WEBHOOK) return;

  const alerts = funds
    .filter(f => f.premium != null && Math.abs(f.premium) >= CONFIG.ALERT_THRESHOLD)
    .sort((a, b) => Math.abs(b.premium) - Math.abs(a.premium));

  let content;
  if (alerts.length === 0) {
    content = `**LOF套利雷达 09:15 开盘播报** 📊\n当前无基金超过阈值 ${CONFIG.ALERT_THRESHOLD}%，市场平静。`;
  } else {
    const lines = alerts.map(f => {
      const sign = f.premium > 0 ? '🔴 溢价' : '🟢 折价';
      const prem = (f.premium >= 0 ? '+' : '') + f.premium.toFixed(2) + '%';
      return `${sign} **${f.name}**（${f.code}）：${prem}`;
    });
    content = [
      `**LOF套利雷达 09:15 开盘播报** 📊`,
      `共 ${alerts.length} 只基金超过阈值 ${CONFIG.ALERT_THRESHOLD}%：`,
      ...lines,
    ].join('\n');
  }

  try {
    await fetch(CONFIG.WECHAT_WEBHOOK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msgtype: 'markdown', markdown: { content } }),
    });
  } catch (e) {
    console.error('09:15 汇总推送失败:', e.message);
  }
}

async function handleScheduled(cron, env) {
  try {
    const data = await fetchAllData(env);
    if (cron === '15 1 * * 1-5') {
      console.log('[Cron 09:15] 发送开盘汇总...');
      await sendDailySummary(data.funds);
    } else {
      console.log('[Cron 每分钟] 检查跃迁报警...');
      await checkAndAlert(data.funds);
    }
    console.log(`[Cron] 完成，基金${data.funds.length}只`);
  } catch (e) {
    console.error('[Cron] 定时任务失败:', e);
  }
}

// ══════════════════════════════════════════════════════
//  Cloudflare Workers Entry Point
// ══════════════════════════════════════════════════════

export default {
  async fetch(request, env, ctx) {
    if (env.WX_KEY) CONFIG.WECHAT_WEBHOOK = `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=${env.WX_KEY}`;
    return handleRequest(request, env);
  },

  async scheduled(event, env, ctx) {
    if (env.WX_KEY) CONFIG.WECHAT_WEBHOOK = `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=${env.WX_KEY}`;
    ctx.waitUntil(handleScheduled(event.cron, env));
  },
};
