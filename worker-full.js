// ══════════════════════════════════════════════════════
//  LOF 套利雷达 — Cloudflare Worker (Dual Mode)
//  Mode A: HTTP Fetch  (/api/quote, /api/sina)
//  Mode B: Scheduled Cron (every 1 minute)
// ══════════════════════════════════════════════════════

const CONFIG = {
  ALERT_THRESHOLD: 1.5,        // 溢价预警阈值(%)
  WECHAT_WEBHOOK: '',           // 企业微信机器人Webhook URL（留空则不发送）
  SINA_PROXY_URL: 'https://patient-pond-824c.3031315027ghb.workers.dev', // 新浪代理（保留供前端用）
};

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
  '164906': 'usKWEB',
  '501312': [{tq:'usQQQ',w:0.8},{tq:'hkHSTECH',w:0.1},{tq:'sh000985',w:0.1}],
  '164824': 'usINDA',
  '160644': 'usKWEB',
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
  '163208': 'usXLE',
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

// ── 降级基金集合（fundgz无实时估值，改用pingzhongdata+lsjz） ──
const NO_GSZ_FUNDS = new Set([
  '164906','164824','501300','501225','160216','164701','160719','163208','501018','161129','160723','501021','501310','161226',
  '161125','161126','161127','161128','161130','162415','160140','160644',
  '160717','160924','164705','161831','160322','161124',
  '501303','501301','501302','501305','501306','501307','501311','501025',
  '161815','165513','161116',
  '501312','162719','162411','160416',
]);

// ── 东方财富代码映射（腾讯不支持的指数） ────────────
const EM_CODES = {
  'csi930917': '2.930917',
  'csi930914': '2.930914',
  'csi930792': '2.930792',
  'sh000985':  '1.000985',
  'hkHSSI':    '124.HSSI',
  'hkHSMI':    '124.HSMI',
  'hkHSCI':    '124.HSCI',
  'sinaAG0':   '113.AG0',   // 上期所白银主力合约（东财 secid）
};

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
  ['sinaAG0','csi930917','csi930914','csi930792','sh000985','hkHSSI','hkHSMI','hkHSCI'].forEach(c => set.delete(c));
  return [...set];
}

// 状态跃迁报警：KV 持久化，key=alert:{code}，value="above"|"below"

// ══════════════════════════════════════════════════════
//  数据获取函数（Worker 环境，使用 fetch()，非浏览器）
// ══════════════════════════════════════════════════════

/**
 * 腾讯行情：批量拉取所有代码
 * 返回 { funds: {code: {price,prevClose,chg,vol}}, indices: {tqCode: {price,chg}} }
 */
async function fetchTencent() {
  const codes = getAllTqCodes().join(',');
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
    // 排除新浪/东财/CSI专属（sz399961等A股指数腾讯可访问，保留）
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

    return result;
  } catch (e) {
    console.error('腾讯行情失败:', e.message);
    return { funds: {}, indices: {} };
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
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const d = await resp.json();
        if (d.data && d.data.f43 > 0) {
          const chg = (d.data.f170 || 0) / 100;
          return [key, chg];
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
 * 新浪财经：拉取白银AG0 + A股指数 sz399961/sz399979/sz399987/sz399998
 * 返回 { sinaAG0: chg, sz399961: chg, sz399979: chg, sz399987: chg, sz399998: chg }
 */
async function fetchSina() {
  try {
    const list = 'nf_AG0,sz399961,sz399979,sz399987,sz399998';
    const url = `https://hq.sinajs.cn/list=${list}`;
    const resp = await fetch(url, {
      headers: {
        'Referer': 'https://finance.sina.com.cn',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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

    // A股指数：p[3]=现价，p[2]=昨收
    for (const code of ['sz399961', 'sz399979', 'sz399987', 'sz399998']) {
      const re = new RegExp(`hq_str_${code}="([^"]+)"`);
      const m = text.match(re);
      if (m) {
        const p = m[1].split(',');
        const cur = parseFloat(p[3]), prev = parseFloat(p[2]);
        if (cur > 0 && prev > 0) out[code] = (cur - prev) / prev * 100;
      }
    }

    return out;
  } catch (e) {
    console.error('新浪行情失败:', e.message);
    return {};
  }
}

/**
 * 净值获取：pingzhongdata.js（东方财富JS文件）
 * 返回 { nav, date } 或 null
 */
async function fetchNavFromPingzhong(code) {
  try {
    const url = `https://fund.eastmoney.com/pingzhongdata/${code}.js`;
    const resp = await fetch(url, {
      headers: { 'Referer': 'https://fund.eastmoney.com' }
    });
    if (!resp.ok) return null;
    const text = await resp.text();

    // 解析 Data_netWorthTrend
    let nav = null, date = '';
    const m1 = text.match(/var\s+Data_netWorthTrend\s*=\s*(\[[\s\S]*?\]);/);
    if (m1) {
      try {
        const arr = JSON.parse(m1[1]);
        if (arr && arr.length > 0) {
          const last = arr[arr.length - 1];
          const v = parseFloat(last.y != null ? last.y : last[1]);
          if (v > 0 && v <= 50) {
            nav = v;
            const ts = last.x != null ? last.x : last[0];
            date = ts ? toBeijingDate(ts) : '';
          }
        }
      } catch (_) {}
    }

    // 降级读 Data_ACWorthTrend
    if (!nav) {
      const m2 = text.match(/var\s+Data_ACWorthTrend\s*=\s*(\[[\s\S]*?\]);/);
      if (m2) {
        try {
          const arr2 = JSON.parse(m2[1]);
          if (arr2 && arr2.length > 0) {
            const last2 = arr2[arr2.length - 1];
            const v2 = parseFloat(Array.isArray(last2) ? last2[1] : last2.y);
            const ts2 = Array.isArray(last2) ? last2[0] : last2.x;
            date = ts2 ? toBeijingDate(ts2) : '';
            if (v2 > 0) nav = v2;
          }
        } catch (_) {}
      }
    }

    return nav ? { nav, date } : null;
  } catch (e) {
    console.error(`pingzhongdata ${code} 失败:`, e.message);
    return null;
  }
}

/**
 * 净值获取：lsjz API（历史净值，JSON）
 * 返回 { nav, date } 或 null
 */
async function fetchNavFromLsjz(code) {
  try {
    const url = `https://api.fund.eastmoney.com/f10/lsjz?fundCode=${code}&pageIndex=1&pageSize=1&callback=cb`;
    const resp = await fetch(url, {
      headers: { 'Referer': 'https://fund.eastmoney.com' }
    });
    if (!resp.ok) return null;
    const text = await resp.text();
    // 剥离 JSONP wrapper
    const m = text.match(/cb\s*\((.+)\)\s*;?\s*$/s);
    if (!m) return null;
    const d = JSON.parse(m[1]);
    const item = d && d.Data && d.Data.LSJZList && d.Data.LSJZList[0];
    if (item && item.DWJZ) {
      return { nav: parseFloat(item.DWJZ), date: item.FSRQ || '' };
    }
    return null;
  } catch (e) {
    console.error(`lsjz ${code} 失败:`, e.message);
    return null;
  }
}

/**
 * 净值获取：fundgz（天天基金估值接口），用于非NO_GSZ_FUNDS
 * 返回 { nav, date } 或 null
 */
async function fetchNavFromFundgz(code) {
  try {
    const url = `https://fundgz.1234567.com.cn/js/${code}.js?rt=${Date.now()}`;
    const resp = await fetch(url, {
      headers: { 'Referer': 'https://fund.eastmoney.com' }
    });
    if (!resp.ok) return null;
    const text = await resp.text();
    // JSONP: jsonpgz({...});
    const m = text.match(/jsonpgz\s*\((.+)\)\s*;?\s*$/s);
    if (!m) return null;
    const d = JSON.parse(m[1]);
    const dwjz = parseFloat(d.dwjz);
    if (dwjz > 0) {
      return { nav: dwjz, date: d.jzrq || '' };
    }
    const gsz = parseFloat(d.gsz);
    if (gsz > 0) {
      return { nav: gsz, date: (d.gztime || '').slice(0, 10) };
    }
    return null;
  } catch (e) {
    console.error(`fundgz ${code} 失败:`, e.message);
    return null;
  }
}

/**
 * 并行拉取两个接口，取日期更新的净值
 */
async function fetchNavFromEM(code) {
  const [r1, r2] = await Promise.all([fetchNavFromPingzhong(code), fetchNavFromLsjz(code)]);
  if (!r1 && !r2) return null;
  if (!r1) return r2;
  if (!r2) return r1;
  const d1 = r1.date || '', d2 = r2.date || '';
  return d2 > d1 ? r2 : r1;
}

/**
 * 批量并行拉取所有基金净值
 * 返回 { code: { nav, date } }
 */
async function fetchAllNavs() {
  const results = await Promise.allSettled(
    FUNDS.map(async f => {
      let r = null;
      if (NO_GSZ_FUNDS.has(f.code)) {
        r = await fetchNavFromEM(f.code);
      } else {
        r = await fetchNavFromFundgz(f.code);
        if (!r) r = await fetchNavFromEM(f.code);
      }
      return { code: f.code, result: r };
    })
  );
  const navMap = {};
  results.forEach(r => {
    if (r.status === 'fulfilled' && r.value && r.value.result) {
      navMap[r.value.code] = r.value.result;
    }
  });
  return navMap;
}

// ══════════════════════════════════════════════════════
//  核心计算：溢价率
// ══════════════════════════════════════════════════════

/**
 * 根据 idxChg 和 BENCH 计算基准涨跌幅
 */
function calcBenchChg(code, idxChg) {
  const benchDef = BENCH[code];
  if (!benchDef) return 0;
  if (Array.isArray(benchDef)) {
    let benchChg = 0, totalW = 0;
    benchDef.forEach(b => {
      const chg = idxChg[b.tq];
      if (chg != null) {           // 缺失分量不计入分母，避免拉低结果
        benchChg += chg * b.w;
        totalW += b.w;
      }
    });
    return totalW > 0 ? benchChg / totalW : 0;
  }
  return idxChg[benchDef] ?? 0;
}

/**
 * 聚合全量数据，计算溢价率，返回统一 JSON
 */
async function fetchAllData(skipNav = false) {
  const [tqData, emIdx, sinaIdx, navMap] = await Promise.all([
    fetchTencent(),
    fetchEastmoney(),
    fetchSina(),
    skipNav ? Promise.resolve({}) : fetchAllNavs(),
  ]);

  // 合并指数涨跌幅
  const idxChg = {};
  Object.entries(tqData.indices).forEach(([code, d]) => { idxChg[code] = d.chg; });
  Object.entries(emIdx).forEach(([code, chg]) => { idxChg[code] = chg; });
  Object.entries(sinaIdx).forEach(([code, chg]) => { idxChg[code] = chg; });

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

    const benchChg = calcBenchChg(f.code, idxChg);
    const base = officialNav || prevClose;
    let nav = null, premium = null;
    if (base > 0 && price != null) {
      nav = base * (1 + benchChg / 100);
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
      premium,
      benchChg,
      quota: f.quota,
      fee: f.fee,
      rfee: f.rfee,
      vol,
    };
  });

  // Ticker 指数数据
  const indices = {};
  TICKER_IDX.forEach(i => {
    const d = tqData.indices[i.tq];
    if (d) indices[i.tq] = d.chg;
  });

  return {
    funds,
    indices,
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

async function checkAndAlert(funds, kv) {
  if (!CONFIG.WECHAT_WEBHOOK) return;

  for (const fund of funds) {
    if (fund.premium == null) continue;

    const isAbove = Math.abs(fund.premium) >= CONFIG.ALERT_THRESHOLD;
    const kvKey = `alert:${fund.code}`;
    const prevState = kv ? await kv.get(kvKey) : null; // "above" | "below" | null

    if (isAbove) {
      // 只在从 below/null → above 的跃迁时推送
      if (prevState !== 'above') {
        await sendWechatAlert(fund);
      }
      if (kv) await kv.put(kvKey, 'above', { expirationTtl: 86400 });
    } else {
      // 回落到阈值以下，重置状态（下次再超过阈值会重新推）
      if (prevState === 'above' && kv) await kv.put(kvKey, 'below', { expirationTtl: 86400 });
    }
  }
}

// ══════════════════════════════════════════════════════
//  Mode A: HTTP Handler
// ══════════════════════════════════════════════════════

async function handleRequest(request) {
  const url = new URL(request.url);
  const path = url.pathname;
  const origin = request.headers.get('Origin') || '';

  // CORS preflight
  if (request.method === 'OPTIONS') {
    return new Response(null, { headers: corsHeaders(origin) });
  }

  // GET /api/nav — 所有基金 T-1 净值（Worker 服务端抓，绕过浏览器 Referer 限制）
  if (path === '/api/nav') {
    try {
      const navMap = await fetchAllNavs();
      return jsonResp(navMap, 200, origin);
    } catch (e) {
      return jsonResp({}, 500, origin);
    }
  }

  // GET /api/snapshot — 聚合快照（主链路，NAV由浏览器端JSONP补充）
  if (path === '/api/snapshot') {
    try {
      const data = await fetchAllData(true);  // skipNav=true，NAV交给前端抓
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
      const data = await fetchAllData();
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

async function handleScheduled(cron, kv) {
  try {
    const data = await fetchAllData();
    if (cron === '15 1 * * 1-5') {
      console.log('[Cron 09:15] 发送开盘汇总...');
      await sendDailySummary(data.funds);
    } else {
      console.log('[Cron 每分钟] 检查跃迁报警...');
      await checkAndAlert(data.funds, kv);
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
    return handleRequest(request);
  },

  async scheduled(event, env, ctx) {
    if (env.WX_KEY) CONFIG.WECHAT_WEBHOOK = `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=${env.WX_KEY}`;
    ctx.waitUntil(handleScheduled(event.cron, env.LOF_STATE));
  },
};
