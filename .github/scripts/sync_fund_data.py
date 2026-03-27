#!/usr/bin/env python3
"""
LOF套利雷达 — 每日数据同步 + 持仓审计脚本
运行时间：北京时间 06:30（UTC 22:30）
  此时美股已收盘 2.5h+（EDT）/ 1.5h+（EST），确保 USO/BNO/QQQ 等收盘涨跌幅稳定可用。
流程：
  1. 拉取 47 只基金 T-1 净值（fundgz → lsjz 兜底，失败保留旧值）
  2. 拉取 47 只基金前十大持仓（东方财富 jjcc API）
  3. 写入 fund_daily.json（含 _meta.sync_time）
  4. 偏差校准 Drift + 链式补偿锚点（est_nav_yesterday）
  5. 持仓审计：对比新持仓 vs BENCH 权重，偏移 > 5% 触发双路报警
     - GitHub Issue（带 Holdings Drift 标签）
     - 企业微信 Webhook Markdown 推送
  容错原则：报警失败不阻塞文件写入，文件写入在报警之前完成。
"""

import json
import random
import re
import sys
import time
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, date, timedelta

try:
    from chinese_calendar import is_workday as _is_workday
    def is_trading_day(d: date) -> bool:
        return _is_workday(d)
except ImportError:
    # 未安装时降级为简单周末判断
    def is_trading_day(d: date) -> bool:
        return d.weekday() < 5

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo('America/New_York')
except ImportError:
    _ET = None  # Python < 3.9 降级，16:00 ET 用固定偏移近似

def gen_trading_dates_for_year(year: int) -> list:
    """生成指定年份所有交易日（YYYY-MM-DD 字符串列表）"""
    result, d = [], date(year, 1, 1)
    while d.year == year:
        if is_trading_day(d):
            result.append(d.isoformat())
        d += timedelta(days=1)
    return result

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(os.path.dirname(SCRIPT_DIR))
JSON_PATH  = os.path.join(REPO_ROOT, 'data', 'fund_daily.json')

DRIFT_THRESHOLD   = 0.05   # 累计权重偏移阈值（5%）
HISTORY_DAYS      = 30     # fund_daily.json 内嵌历史滚动窗口（交易日）
DRIFT_MIN_SAMPLES = 3      # drift_5d 最少有效样本数

# EM 指数代码映射（同 worker-full.js EM_CODES）
# 注意：sinaAG0 (113.AG0) EM 返回 rc=100/data:null，已确认无效，不列入此表
# GitHub Actions 运行于境外服务器，Tencent qt.gtimg.cn 大概率被封；
# 扩展 EM 覆盖全部 A 股/港股指数，US 代码由 Yahoo Finance 单独抓取。
_EM_CODES = {
    # CSI 指数（仅 EM 有）
    'csi930917': '2.930917',
    'csi930914': '2.930914',
    'csi930792': '2.930792',
    # A 股主流指数
    'sh000300':  '1.000300',   # 沪深300
    'sh000985':  '1.000985',   # 中证全指
    'sh518880':  '1.518880',   # 华安黄金 ETF（基准代理）
    'sz399987':  '0.399987',   # 中证800
    'sz399998':  '0.399998',   # 中证100
    'sz399961':  '0.399961',   # 中证资源与环境（收盘后用快照，此处仅 Action 使用）
    'sz399979':  '0.399979',   # 中证大宗商品股票
    # HK 指数
    'hkHSI':     '124.HSI',    # 恒生指数
    'hkHSTECH':  '124.HSTECH', # 恒生科技
    'hkHSCEI':   '124.HSCEI',  # 国企指数
    'hkHSSI':    '124.HSSI',
    'hkHSMI':    '124.HSMI',
    'hkHSCI':    '124.HSCI',
}

# Yahoo Finance 代码映射：our_key → Yahoo symbol
# 用于 GitHub Actions 抓取美股 ETF/指数前一交易日收盘涨跌幅
_YAHOO_CODES = {
    'usQQQ':  'QQQ',   'usUSO':  'USO',   'usBNO':  'BNO',
    'usGLD':  'GLD',   'usGLDM': 'GLDM',  'usIAU':  'IAU',
    'usSGOL': 'SGOL',  'usAAAU': 'AAAU',  'usSLV':  'SLV',
    'usCPER': 'CPER',  'usBCI':  'BCI',   'usCOMT': 'COMT',
    'usXLE':  'XLE',   'usXOP':  'XOP',   'usIXC':  'IXC',
    'usKWEB': 'KWEB',  'usRSPH': 'RSPH',  'usRWR':  'RWR',
    'usXBI':  'XBI',   'usXLK':  'XLK',   'usXLY':  'XLY',
    'usSMH':  'SMH',   'usINDA': 'INDA',  'usAGG':  'AGG',
    'usINX':  '^GSPC',
    # HK 指数 — EM push2 对 ^HSI/^HSCE 返回 rc=100，改走 Yahoo
    'hkHSI':   '^HSI',
    'hkHSCEI': '^HSCE',
}

# 腾讯行情代码别名：our_key → tencent_code
# sinaAG0 在腾讯的代码是 nf_AG0（新浪期货代码格式，腾讯支持）
# 注意：Tencent 在 GitHub Actions 境外服务器通常不可达，此别名主要供本地测试用
_TQ_ALIASES = {
    'sinaAG0': 'nf_AG0',
}

# 商品期货参考价：ref_key → Yahoo Finance 编码符号
# 每个交易日 Action 运行时抓取 16:00 ET 的历史 5 分钟 K 线价格，
# 存入 fund_daily.json._commodity_refs，Worker 用 (现价/参考价 - 1) 替代 regularMarketChangePercent
# 背景：COMEX/NYMEX 结算时间（金 13:30 ET / 原油 14:30 ET）早于 ETF 16:00 ET 收盘 1.5-2.5h，
#       直接用结算价涨跌幅估算 ETF 日内变动存在系统性偏差；16:00 ET 快照更准确
_COMMODITY_FUTURES = {
    'gc': 'GC%3DF',   # COMEX 黄金  → usGLD/usGLDM/usIAU/usSGOL/usAAAU
    'si': 'SI%3DF',   # COMEX 白银  → usSLV
    'cl': 'CL%3DF',   # NYMEX WTI 原油 → usUSO/usIXC/usXOP/usXLE
    'bz': 'BZ%3DF',   # ICE 布伦特原油  → usBNO
    'hg': 'HG%3DF',   # COMEX 铜    → usCPER
}


# ══════════════════════════════════════════════════════
#  基础 HTTP 工具
# ══════════════════════════════════════════════════════

def fetch_url(url: str, referer: str = '', timeout: int = 12, retries: int = 2) -> str | None:
    """HTTP GET，失败后最多重试 retries 次，每次间隔 3s。"""
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    if referer:
        headers['Referer'] = referer
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw.decode('gbk', errors='replace')
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(3)
    print(f'    [http] {url[:80]} → {last_err}', file=sys.stderr)
    return None


def post_json(url: str, payload: dict, extra_headers: dict = None, timeout: int = 15):
    """HTTP POST JSON，返回 (status_code, response_text)"""
    data = json.dumps(payload).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, method='POST', headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode('utf-8')


# ══════════════════════════════════════════════════════
#  净值抓取
# ══════════════════════════════════════════════════════

def fetch_fundgz(code: str) -> dict | None:
    url = f'https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time())}'
    text = fetch_url(url, referer='https://fund.eastmoney.com')
    if not text:
        return None
    m = re.search(r'jsonpgz\s*\((.+)\)\s*;?\s*$', text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        nav = float(d.get('dwjz') or 0)
        if 0 < nav <= 50:
            return {'nav': nav, 'nav_date': d.get('jzrq', ''), 'nav_src': 'fundgz'}
        gsz = float(d.get('gsz') or 0)
        if gsz > 0:
            return {'nav': gsz, 'nav_date': (d.get('gztime') or '')[:10], 'nav_src': 'fundgz_est'}
    except Exception:
        pass
    return None


def fetch_lsjz(code: str) -> dict | None:
    url = (
        f'https://api.fund.eastmoney.com/f10/lsjz'
        f'?fundCode={code}&pageIndex=1&pageSize=1&callback=cb'
    )
    text = fetch_url(url, referer='https://fund.eastmoney.com')
    if not text:
        return None
    m = re.search(r'cb\s*\((.+)\)\s*;?\s*$', text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        items = (d.get('Data') or {}).get('LSJZList') or []
        if items:
            nav = float(items[0].get('DWJZ') or 0)
            if nav > 0:
                return {'nav': nav, 'nav_date': items[0].get('FSRQ', ''), 'nav_src': 'lsjz'}
    except Exception:
        pass
    return None


def fetch_pingzhong(code: str) -> dict | None:
    """
    东方财富 pingzhongdata JS 文件（第三路兜底）。
    解析 Data_netWorthTrend 取最新单位净值；降级读 Data_ACWorthTrend。
    适用于 fundgz 无 dwjz/gsz 且 lsjz 也失败的场景。
    """
    url = f'https://fund.eastmoney.com/pingzhongdata/{code}.js'
    text = fetch_url(url, referer='https://fund.eastmoney.com')
    if not text:
        return None

    def _bj_date(ts_ms: int) -> str:
        from datetime import datetime, timezone, timedelta
        return (
            datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) + timedelta(hours=8)
        ).strftime('%Y-%m-%d')

    # 优先：Data_netWorthTrend（单位净值时序）
    m1 = re.search(r'var\s+Data_netWorthTrend\s*=\s*(\[[\s\S]*?\]);', text)
    if m1:
        try:
            arr = json.loads(m1.group(1))
            if arr:
                last = arr[-1]
                v  = float(last['y'] if isinstance(last, dict) else last[1])
                ts = (last.get('x') if isinstance(last, dict) else last[0])
                if 0 < v <= 50:
                    return {'nav': v, 'nav_date': _bj_date(ts), 'nav_src': 'pingzhong'}
        except Exception:
            pass

    # 降级：Data_ACWorthTrend（累计净值，适用于无分红基金）
    m2 = re.search(r'var\s+Data_ACWorthTrend\s*=\s*(\[[\s\S]*?\]);', text)
    if m2:
        try:
            arr = json.loads(m2.group(1))
            if arr:
                last = arr[-1]
                v  = float(last[1] if isinstance(last, list) else last.get('y', 0))
                ts = (last[0] if isinstance(last, list) else last.get('x', 0))
                if v > 0:
                    return {'nav': v, 'nav_date': _bj_date(ts), 'nav_src': 'pingzhong_ac'}
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════════════
#  总份额抓取
# ══════════════════════════════════════════════════════

def fetch_shares(code: str) -> float | None:
    """
    从东方财富 pingzhongdata.js 解析基金最新总份额（亿份）。
    取 Data_buySedemption 中 "总份额" 系列的最后一个季度值。
    """
    url = f'https://fund.eastmoney.com/pingzhongdata/{code}.js'
    text = fetch_url(url, referer='https://fund.eastmoney.com')
    if not text:
        return None
    m = re.search(r'var\s+Data_buySedemption\s*=\s*(\{[\s\S]*?\});', text)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        for series in (d.get('series') or []):
            if series.get('name') == '总份额':
                data_pts = series.get('data') or []
                if data_pts:
                    return round(float(data_pts[-1]), 4)
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════
#  持仓抓取
# ══════════════════════════════════════════════════════

def _recent_quarters() -> list:
    """返回最近 6 个季报的 (year, end_month) 列表，从近到远排列。"""
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month
    quarters = []
    for _ in range(6):
        # 向下取整到季度末月（3/6/9/12）
        qm = (m - 1) // 3 * 3 + 3
        if qm > m:          # 当季尚未结束，退一个季度
            qm -= 3
        if qm < 1:
            qm = 12
            y -= 1
        quarters.append((y, qm))
        # 移到上个季度
        m = qm - 3
        if m < 1:
            m = 12
            y -= 1
    return quarters


def fetch_holdings(code: str) -> dict | None:
    """
    天天基金 FundArchivesDatas.aspx?type=jjcc 季报披露页面。
    逐季尝试直到找到非空数据，解析 HTML 表格，返回:
      {'holdings': [...], 'holdings_date': 'YYYY-MM-DD'}
    其中 holdings_date 为该季报的季末日期（如 2025-12-31）。
    字段：code（标的代码）、name（中文简称）、ratio（占净值比例%）。
    """
    _quarter_last_day = {3: 31, 6: 30, 9: 30, 12: 31}
    for year, month in _recent_quarters():
        url = (
            f'https://fundf10.eastmoney.com/FundArchivesDatas.aspx'
            f'?type=jjcc&code={code}&topline=10&year={year}&month={month:02d}'
            f'&rt={int(time.time())}'
        )
        text = fetch_url(url, referer=f'https://fundf10.eastmoney.com/ccmx_{code}.html')
        if not text:
            continue

        # 找第一个有内容的 <tbody>
        tbodies = re.findall(r'<tbody>(.*?)</tbody>', text, re.DOTALL)
        for tbody in tbodies:
            if not tbody.strip():
                continue
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbody, re.DOTALL)
            result = []
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                clean = [c for c in clean if c]
                if len(clean) < 3:
                    continue
                # 寻找占净值比例（末尾带 % 的数值格式）
                ratio = None
                for cell in clean:
                    if cell.endswith('%'):
                        try:
                            ratio = float(cell.rstrip('%'))
                            break
                        except ValueError:
                            pass
                if ratio is None:
                    continue
                # 列顺序：序号 | 代码 | 名称 | … | 占净值% | …
                stock_code = clean[1] if len(clean[1]) <= 12 else ''
                stock_name = clean[2] if not clean[2].startswith('--') else clean[1]
                if stock_name:
                    result.append({
                        'code':  stock_code,
                        'name':  stock_name,
                        'ratio': round(ratio, 2),
                    })
            if result:
                last_day = _quarter_last_day.get(month, 30)
                holdings_date = f'{year}-{month:02d}-{last_day}'
                return {'holdings': result[:10], 'holdings_date': holdings_date}

    print(f'    [holdings] {code}: 近 6 季均无数据（可能为单只 ETF 持仓型基金）',
          file=sys.stderr)
    return None


# ══════════════════════════════════════════════════════
#  PDF 持仓提取（巨潮资讯季报）
# ══════════════════════════════════════════════════════
# 对 EM jjcc 无法返回持仓的基金（主要为持有外盘 ETF 的美股/商品类 LOF），
# 在 holdings 为空或距今超过 90 天时，通过巨潮资讯季报 PDF 提取前十大持仓。

PDF_HOLDINGS_MAX_AGE_DAYS = 90   # PDF 持仓刷新阈值（日历天）

# 外盘 ETF 名称关键词 → 内部 tq 代码（优先级从高到低，依序匹配，首中即返）
_ETF_NAME_TO_TQ: list = [
    # ── 原油 WTI ──
    ('WisdomTree WTI',           'usUSO'),
    ('Simplex WTI',              'usUSO'),
    ('Nomura Crude Oil',         'usUSO'),
    ('GSCI Crude Oil',           'usUSO'),  # 须在通用 GSCI 前
    ('Invesco DB Oil',           'usUSO'),
    ('United States Oil Fund',   'usUSO'),
    ('WTI',                      'usUSO'),  # 兜底
    # ── 原油 Brent ──
    ('WisdomTree Brent',         'usBNO'),
    ('United States Brent',      'usBNO'),
    ('Brent',                    'usBNO'),  # 兜底
    # ── 黄金 ──
    ('SPDR Gold',                'usGLD'),
    ('iShares Gold Trust',       'usIAU'),
    ('iShares Gold ETF',         'usIAU'),
    ('Aberdeen Standard Gold',   'usSGOL'),
    ('Invesco Physical Gold',    'usSGOL'),
    ('Perth Mint Gold',          'usAAAU'),
    ('Sprott Physical Gold',     'usAAAU'),
    ('Sprott Gold',              'usAAAU'),
    # ── 白银 ──
    ('iShares Silver',           'usSLV'),
    ('United States Silver',     'usSLV'),
    ('Silver',                   'usSLV'),  # 兜底
    # ── 铜 ──
    ('Copper Index',             'usCPER'),
    ('United States Copper',     'usCPER'),
    # ── 能源 / 油气 ──
    ('Energy Select Sector',     'usXLE'),
    ('Oil & Gas Exploration',    'usXOP'),
    ('Oil and Gas Exploration',  'usXOP'),
    ('Global Energy',            'usIXC'),
    # ── 综合商品 ──
    ('Bloomberg Commodity',      'usBCI'),
    ('S&P GSCI',                 'usCOMT'),  # 通用 GSCI（须在 GSCI Crude Oil 后）
    # ── 纳斯达克 / QQQ ──
    ('Invesco QQQ',              'usQQQ'),
    ('QQQ',                      'usQQQ'),
    # ── 科技 ──
    ('Technology Select Sector', 'usXLK'),
    ('PHLX Semiconductor',       'usSMH'),
    ('Semiconductor',            'usSMH'),  # 兜底
    # ── 印度 ──
    ('MSCI India',               'usINDA'),
    # ── 中国互联网 ──
    ('CSI China Internet',       'usKWEB'),
    ('KraneShares China',        'usKWEB'),
    # ── 生物科技 ──
    ('S&P Biotech',              'usXBI'),
    ('Biotech',                  'usXBI'),  # 兜底
    # ── 消费 ──
    ('Consumer Discretionary',   'usXLY'),
    # ── 医疗 ──
    ('Health Care Equal Weight', 'usRSPH'),
    ('Equal Weight Health',      'usRSPH'),
    # ── 地产 ──
    ('Dow Jones REIT',           'usRWR'),
    ('REIT',                     'usRWR'),  # 兜底
    # ── 债券 ──
    ('Aggregate Bond',           'usAGG'),
    ('Core U.S. Aggregate',      'usAGG'),
    # ── 标普500 ──
    ('SPDR S&P 500',             'usINX'),
    ('Vanguard S&P 500',         'usINX'),
    ('S&P 500 ETF',              'usINX'),
]


def _match_etf_name(name: str) -> str | None:
    """从 ETF 名称关键词匹配内部 tq 代码；无匹配返回 None。"""
    for keyword, tq in _ETF_NAME_TO_TQ:
        if keyword in name:
            return tq
    return None


def _holdings_age_days(holdings_date: str, today: str) -> int:
    """持仓日期距今的日历天数。"""
    try:
        return (date.fromisoformat(today) - date.fromisoformat(holdings_date)).days
    except Exception:
        return 9999


def _fetch_em_pdf_url(code: str) -> tuple:
    """从东方财富季报公告页（fundf10 jjzqbg）查最新季报 PDF。
    返回 (url, title, notice_date)；失败返回 (None, None, None)。
    使用与 jjcc 相同的域名和 fetch_url 通道，境外 IP 可访问。
    """
    url = (
        f'https://fundf10.eastmoney.com/FundArchivesDatas.aspx'
        f'?type=jjzqbg&code={code}&page=1&per=10&rt={int(time.time())}'
    )
    text = fetch_url(url, referer=f'https://fundf10.eastmoney.com/jjgg.html?fundcode={code}')
    if not text:
        print(f'    [em_pdf] {code} 季报公告页请求失败', file=sys.stderr)
        return None, None, None

    # EM jjzqbg 响应格式：var apidata={content:"...HTML...", pages:X}
    # HTML 内通过 openWinFunc('AP202503...', '2024年第四季度报告', ...) 打开 PDF。
    # art code 格式为 AP/AN + 纯数字，无 H2_ 前缀；PDF URL = H2_{art_code}_1.PDF
    entries = []
    seen = set()

    # 主模式：提取 openWinFunc 的前两个参数（art_code + title）
    for m in re.finditer(
        r"openWinFunc\s*\(\s*['\"]([A-Z]{2}[0-9]{14,})['\"]"
        r"\s*,\s*['\"]([^'\"]{4,80})['\"]",
        text, re.IGNORECASE
    ):
        art_code = m.group(1)
        title    = m.group(2).strip()
        if art_code in seen:
            continue
        seen.add(art_code)
        pdf_url = f'https://pdf.dfcfw.com/pdf/H2_{art_code}_1.PDF'
        # 在匹配位置附近找日期
        start = max(0, m.start() - 200)
        ctx   = text[start: m.end() + 200]
        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', ctx)
        notice_date = date_m.group(1) if date_m else ''
        entries.append((pdf_url, title, notice_date))

    # 备用模式：直接扫描引号内的 art code（兼容不同 JS 调用形式）
    if not entries:
        for m in re.finditer(r"['\"]([A-Z]{2}[0-9]{14,})['\"]", text):
            art_code = m.group(1)
            if art_code in seen:
                continue
            seen.add(art_code)
            pdf_url = f'https://pdf.dfcfw.com/pdf/H2_{art_code}_1.PDF'
            start = max(0, m.start() - 300)
            ctx   = text[start: m.end() + 300]
            title_m = re.search(r'[\u4e00-\u9fa5]{4,40}(?:季度?报告|季报|年度报告)', ctx)
            title   = title_m.group(0).strip() if title_m else ''
            date_m  = re.search(r'(\d{4}-\d{2}-\d{2})', ctx)
            notice_date = date_m.group(1) if date_m else ''
            entries.append((pdf_url, title, notice_date))

    if not entries:
        print(f'    [em_pdf] {code} 未找到 art code，页面片段: {text[:400]!r}', file=sys.stderr)
        return None, None, None

    # 优先返回季度报告
    for pdf_url, title, notice_date in entries:
        if re.search(r'[一二三四]季度?报告|季报', title):
            return pdf_url, title, notice_date

    # 备选：第一个条目
    pdf_url, title, notice_date = entries[0]
    return pdf_url, title, notice_date


def _parse_quarter_end_date(title: str) -> str:
    """从季报标题解析季末日期，如 '2025年第四季度报告' → '2025-12-31'。"""
    _quarter_month = {'一': 3, '二': 6, '三': 9, '四': 12}
    _month_day     = {3: 31, 6: 30, 9: 30, 12: 31}
    m = re.search(r'(\d{4})年第?([一二三四])季度', title)
    if m:
        year  = int(m.group(1))
        month = _quarter_month[m.group(2)]
        return f'{year}-{month:02d}-{_month_day[month]}'
    return ''


def _download_pdf_binary(url: str, dest_path: str) -> bool:
    """下载 PDF 二进制到本地路径。"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer':    'https://fund.eastmoney.com/',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(dest_path, 'wb') as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        return True
    except Exception as e:
        print(f'    [pdf] 下载失败: {e}', file=sys.stderr)
        return False


def _extract_holdings_via_gemini(pdf_path: str, code: str) -> list:
    """
    用 Gemini 1.5 Flash 从季报 PDF 提取前十大持仓，返回可映射到 tq 代码的行。
    返回 [{'code': tq, 'name': tq, 'ratio': float}, ...]，空列表表示未找到或校验失败。

    数据熔断校验（警告但不中断主流程）：
      - 提取条数 1–15 之间
      - 权重之和 5%–120%
      - 至少 1 条成功映射到 tq 代码
    """
    import base64
    import os as _os

    api_key = _os.environ.get('GEMINI_API_KEY')
    if not api_key:
        print('    [gemini] GEMINI_API_KEY 未设置，跳过', file=sys.stderr)
        return []

    try:
        import google.generativeai as genai
    except ImportError:
        print('    [gemini] google-generativeai 未安装，跳过', file=sys.stderr)
        return []

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')

    with open(pdf_path, 'rb') as f:
        pdf_b64 = base64.b64encode(f.read()).decode()

    prompt = (
        '请从这份基金季度报告PDF中，找到"前十大重仓基金"、"前十大持仓基金"或"基金投资明细"表格。'
        '如果存在多张表，优先选择"指数投资组合"或"主要投资标的"部分。\n'
        '提取每个持仓的：\n'
        '  - name_en: ETF/基金英文名称（如无英文名则留空字符串）\n'
        '  - name_zh: ETF/基金中文名称（如无中文名则留空字符串）\n'
        '  - ratio: 占基金净值比例，纯数字，去掉%符号\n'
        '只返回JSON数组，不要包含任何其他文字或代码块标记。\n'
        '示例格式：[{"name_en": "United States Oil Fund", "name_zh": "美国石油基金", "ratio": 18.5}]\n'
        '如果找不到相关持仓表格，返回空数组 []。'
    )

    try:
        response = model.generate_content([
            {'mime_type': 'application/pdf', 'data': pdf_b64},
            prompt,
        ])
        text = response.text.strip()
        # 提取 JSON 数组（去掉 markdown 代码块包裹）
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            print(f'    [gemini] {code}: 未返回有效 JSON', file=sys.stderr)
            return []
        raw_items = json.loads(m.group())
    except Exception as e:
        print(f'    [gemini] {code}: API 调用失败: {e}', file=sys.stderr)
        return []

    # ── 数据熔断校验 ──────────────────────────────────
    if not (1 <= len(raw_items) <= 15):
        print(
            f'    [gemini] {code}: 持仓条数异常 ({len(raw_items)})，跳过写入',
            file=sys.stderr,
        )
        return []

    total_ratio_raw = sum(float(it.get('ratio', 0)) for it in raw_items if it.get('ratio') is not None)
    if not (5 <= total_ratio_raw <= 120):
        print(
            f'    [gemini] {code}: 权重之和异常 ({total_ratio_raw:.1f}%)，跳过写入',
            file=sys.stderr,
        )
        return []

    # ── 映射 tq 代码（英文名优先，中文名兜底）────────
    tq_agg: dict = {}
    for item in raw_items:
        ratio = item.get('ratio')
        if ratio is None:
            continue
        ratio = float(ratio)
        if not (0 < ratio < 100):
            continue
        tq = _match_etf_name(item.get('name_en', '')) or _match_etf_name(item.get('name_zh', ''))
        if tq:
            tq_agg[tq] = tq_agg.get(tq, 0) + ratio

    if not tq_agg:
        print(f'    [gemini] {code}: 无法映射任何持仓到 tq 代码，跳过写入', file=sys.stderr)
        return []

    return [
        {'code': tq, 'name': tq, 'ratio': round(ratio, 2)}
        for tq, ratio in sorted(tq_agg.items(), key=lambda x: -x[1])
    ]


def _fetch_holdings_via_pdf(code: str, local_holdings_date: str = '') -> dict | None:
    """
    PDF+Gemini 持仓抓取主入口（公告驱动，增量更新）。

    流程：
      1. 查询东方财富公告 API，获取最新季报 (url, title, notice_date)
      2. 解析季报截止日（_parse_quarter_end_date），与 local_holdings_date 比较
      3. 无更新 → 返回 {'skipped': True, 'holdings_date': ann_quarter_date}
      4. 有更新 → 下载 PDF → Gemini 提取 → 返回 {'holdings': [...], 'holdings_date': ...}
      5. 失败     → 返回 None

    不再使用固定 90 天日历阈值；改由公告日期驱动，只在季报推进时触发下载。
    """
    import tempfile, os as _os

    print(f'    [pdf] {code}: 查询东方财富公告...', flush=True)

    pdf_url, title, notice_date = _fetch_em_pdf_url(code)
    if not pdf_url:
        print(f'    [pdf] {code}: 未找到季报 PDF', file=sys.stderr)
        return None

    # ── 增量检查：季报截止日对比本地已有持仓日期 ──────────
    ann_quarter_date = _parse_quarter_end_date(title)
    if ann_quarter_date and local_holdings_date and ann_quarter_date <= local_holdings_date:
        print(
            f'    [pdf] {code}: 季报无更新'
            f'（公告截止 {ann_quarter_date} ≤ 本地 {local_holdings_date}'
            f'，发布日 {notice_date or "?"}）'
        )
        return {'skipped': True, 'holdings_date': ann_quarter_date}

    print(
        f'    [pdf] {code}: 发现新季报 {ann_quarter_date or "?"}（发布: {notice_date or "?"}），下载中...',
        flush=True,
    )

    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
    pdf_path = tmp.name
    tmp.close()

    try:
        if not _download_pdf_binary(pdf_url, pdf_path):
            return None

        holdings = _extract_holdings_via_gemini(pdf_path, code)
        if not holdings:
            return None

        holdings_date = ann_quarter_date or datetime.now(timezone.utc).strftime('%Y-%m-%d')

        total_ratio = sum(h['ratio'] for h in holdings)
        print(
            f'    [pdf+gemini] {code}: 找到 {len(holdings)} 个 tq 代码，'
            f'合计 {total_ratio:.1f}%  截止: {holdings_date}',
            flush=True,
        )
        return {'holdings': holdings[:10], 'holdings_date': holdings_date}
    finally:
        try:
            _os.unlink(pdf_path)
        except Exception:
            pass


# ══════════════════════════════════════════════════════
#  偏差校准 Drift
# ══════════════════════════════════════════════════════

def fetch_bench_chg_batch(data: dict, t1_date: str) -> tuple[dict, dict]:
    """
    从 fund_daily.json 收集所有基准代码，批量拉取前一交易日收盘涨跌幅。

    返回 (chg_map, date_map)：
      chg_map  = { tq_code: chg_pct }       — 涨跌幅（同旧接口）
      date_map = { tq_code: trading_date }  — 每个值所属的交易日（时间驱动协议）

    date_map 来源优先级：
      EM / Tencent → 无法从响应中获取日期，使用 t1_date（已知交易日上下文）
      Yahoo        → 从 regularMarketTime 解析（美股实际收盘交易日）
      idx_closing  → 使用 entry['date']（收盘快照已含明确日期）

    调用方（update_chain_anchors / update_drift）需校验 date_map 日期
    与预期交易日一致，不一致时拒绝使用对应数据。
    """
    tq_needed: set = set()
    for code, fund in data.items():
        if code.startswith('_'):
            continue
        bench = fund.get('bench')
        if not bench:
            continue
        if isinstance(bench, str):
            tq_needed.add(bench)
        elif isinstance(bench, list):
            for b in bench:
                tq_needed.add(b['tq'])

    em_keys   = {k for k in tq_needed if k in _EM_CODES}
    tq_direct = [c for c in tq_needed if c not in em_keys]

    chg_map:  dict = {}
    date_map: dict = {}   # tq_code → trading date of the fetched value

    # ── Tencent 批量（含别名映射） ──
    if tq_direct:
        # our_key → tencent_code（有别名则替换，否则原样）
        tq_map = {c: _TQ_ALIASES.get(c, c) for c in tq_direct}
        url  = f'https://qt.gtimg.cn/q={",".join(tq_map.values())}'
        text = fetch_url(url, referer='https://gu.qq.com')
        if text:
            for our_key, tq_code in tq_map.items():
                m = re.search(rf'v_{re.escape(tq_code)}="([^"]+)"', text)
                if not m:
                    continue
                p = m.group(1).split('~')
                try:
                    price, prev = float(p[3]), float(p[4])
                    if price > 0 and prev > 0:
                        chg_map[our_key]  = (price - prev) / prev * 100
                        date_map[our_key] = t1_date   # Tencent 响应不含日期，用已知交易日
                except (ValueError, IndexError):
                    pass
        print(f'  [bench] Tencent: '
              f'{sum(1 for c in tq_direct if c in chg_map)}/{len(tq_direct)} 成功')

    # ── East Money 逐个 ──
    for tq_key in em_keys:
        secid = _EM_CODES[tq_key]
        url   = (f'https://push2.eastmoney.com/api/qt/stock/get'
                 f'?secid={secid}&fields=f43,f169,f170')
        text  = fetch_url(url)
        if text:
            try:
                d = json.loads(text)
                dat = d.get('data') or {}
                f170 = dat.get('f170')
                # null ≠ 0：f170=null 表示未取到，不写入（不覆盖为 0）
                # f43（现价）对计算型指数（如399961）可能为0，不用此字段过滤
                # 只判断 f170 是否有效（null≠0）
                if f170 is not None:
                    chg_map[tq_key]  = f170 / 100
                    date_map[tq_key] = t1_date   # EM 响应不含日期，用已知交易日
            except Exception:
                pass
        time.sleep(0.05)
    print(f'  [bench] EM: '
          f'{sum(1 for k in em_keys if k in chg_map)}/{len(em_keys)} 成功')

    # ── Yahoo Finance（US ETF / HK 指数）──────────────────────
    # 请求 regularMarketTime（Unix 时间戳）以提取实际交易日期
    yahoo_keys = {k for k in tq_needed if k in _YAHOO_CODES and k not in chg_map}
    if yahoo_keys:
        symbols = ','.join(_YAHOO_CODES[k] for k in yahoo_keys)
        url = (f'https://query1.finance.yahoo.com/v7/finance/quote'
               f'?symbols={symbols}'
               f'&fields=regularMarketChangePercent,regularMarketTime')
        text = fetch_url(url, referer='https://finance.yahoo.com')
        if text:
            try:
                result = json.loads(text)
                quotes = (result.get('quoteResponse') or {}).get('result') or []
                yahoo_rev = {v: k for k, v in _YAHOO_CODES.items()}  # symbol→our_key
                for q in quotes:
                    sym = q.get('symbol', '')
                    chg = q.get('regularMarketChangePercent')
                    if chg is None or sym not in yahoo_rev:
                        continue
                    our_key = yahoo_rev[sym]
                    chg_map[our_key] = chg

                    # 从 regularMarketTime（Unix 秒）提取交易日期
                    reg_time = q.get('regularMarketTime')
                    if reg_time:
                        from datetime import timedelta
                        # 港股（^HSI / ^HSCE）→ UTC+8；美股 → UTC-5（保守，覆盖盘后）
                        tz_offset = 8 if sym in ('^HSI', '^HSCE') else -5
                        dt = (datetime.fromtimestamp(reg_time, tz=timezone.utc)
                              + timedelta(hours=tz_offset))
                        date_map[our_key] = dt.strftime('%Y-%m-%d')
                    else:
                        date_map[our_key] = t1_date   # 无时间戳，降级到推断日期
            except Exception as e:
                print(f'  [bench] Yahoo parse error: {e}', file=sys.stderr)
    print(f'  [bench] Yahoo: '
          f'{sum(1 for k in yahoo_keys if k in chg_map)}/{len(yahoo_keys)} 成功')

    # ── idx_closing.json fallback（sz399961 / sz399979 / sinaAG0）──
    # 这三个指数 EM/Tencent/Yahoo 均无法从 Actions 环境抓取；
    # sync_closing_idx.py 每日 15:05 写入的收盘快照是唯一来源。
    closing_path = os.path.join(REPO_ROOT, 'data', 'idx_closing.json')
    still_missing = {k for k in tq_needed if k not in chg_map}
    if still_missing and os.path.exists(closing_path):
        try:
            with open(closing_path, encoding='utf-8') as f:
                closing = json.load(f)
            filled = []
            for key in still_missing:
                entry = closing.get(key)
                if entry and entry.get('chg') is not None:
                    chg_map[key]  = entry['chg']
                    date_map[key] = entry.get('date', t1_date)   # 收盘快照含明确日期
                    filled.append(key)
            if filled:
                print(f'  [bench] closing fallback 填补: {filled}')
        except Exception as e:
            print(f'  [bench] closing fallback 读取失败: {e}', file=sys.stderr)

    return chg_map, date_map


def _calc_bench_chg(bench_def, chg_map: dict) -> float | None:
    """加权基准涨跌幅。镜像 Worker calcBenchChg 逻辑。"""
    if isinstance(bench_def, str):
        return chg_map.get(bench_def)
    if isinstance(bench_def, list):
        total_chg = total_w = 0.0
        for b in bench_def:
            c = chg_map.get(b['tq'])
            if c is not None:
                total_chg += c * b['w']
                total_w   += b['w']
        return total_chg / total_w if total_w > 0 else None
    return None


def _get_bench_date(bench_def, date_map: dict, fallback: str) -> str:
    """
    从 date_map 取基准数据的交易日期。
    复合基准取第一个有记录的分量日期；fallback 为兜底值。
    """
    if isinstance(bench_def, str):
        return date_map.get(bench_def) or fallback
    if isinstance(bench_def, list):
        for b in bench_def:
            d = date_map.get(b['tq'])
            if d:
                return d
    return fallback


def _bench_dates_ok(bench_def, date_map: dict, expected_date: str) -> bool:
    """
    时间驱动协议校验：所有在 date_map 中有记录的基准分量，
    其交易日必须等于 expected_date，否则视为日期不匹配。
    对于 date_map 中没有记录的分量（无法确定日期），宽松放行。
    """
    if isinstance(bench_def, str):
        d = date_map.get(bench_def)
        return d is None or d == expected_date
    if isinstance(bench_def, list):
        for b in bench_def:
            d = date_map.get(b['tq'])
            if d and d != expected_date:
                return False
        return True
    return True


def update_drift(data: dict, chg_map: dict, date_map: dict, now_utc: str) -> None:
    """
    偏差校准 — 零外部文件版（全量内嵌于 fund_daily.json）。

    职责：维护 history 序列 + drift_5d 修正因子。
    不再写 est_nav_yesterday（由 update_chain_anchors 专职负责）。

    时间驱动协议：drift 计算公式为
      est_nav(T) = nav(T-1) × (1 + bench_chg(T))
    bench_chg 必须是 T 日（curr_date）的收盘涨跌幅。
    若 date_map 中记录的基准日期与 curr_date 不符，拒绝写入 drift 条目。

    每只基金维护：
      fund['history'] = {
          'date':  ['MM-DD', ...],   # 30条滚动
          'nav':   [float, ...],     # 官方净值
          'est':   [float|None, ...],# 雷达估值（prev_nav × bench_chg）
          'drift': [float|None, ...],# 相对偏差
      }
      fund['drift_5d'] = float  # 近5日均偏差（实时修正因子）
      fund['drift_n']  = int

    逻辑：
      est_nav  = prev_nav × (1 + bench_chg%)   ← 用于 drift 对账，非链式锚点
      drift    = (curr_nav − est_nav) / est_nav
    """
    new_entries = 0

    for code, fund in data.items():
        if code.startswith('_'):
            continue
        curr_nav  = fund.get('nav')
        curr_date = fund.get('nav_date', '')
        bench_def = fund.get('bench')
        if not curr_nav or not curr_date or not bench_def:
            continue

        # ── 加载 / 初始化嵌入式历史（列式存储）──────────────
        raw = fund.get('history')
        if not isinstance(raw, dict):
            raw = {}
        hist: dict[str, list] = {
            'date':  list(raw.get('date',  [])),
            'nav':   list(raw.get('nav',   [])),
            'est':   list(raw.get('est',   [])),
            'drift': list(raw.get('drift', [])),
        }
        last_date = hist['date'][-1] if hist['date'] else ''

        if curr_date > last_date:
            # ── 新交易日：计算偏差 ────────────────────────
            bench_chg = _calc_bench_chg(bench_def, chg_map)

            # 时间驱动协议：bench 日期必须与 nav 日期（curr_date）一致
            dates_ok = _bench_dates_ok(bench_def, date_map, curr_date)
            if not dates_ok:
                bench_date = _get_bench_date(bench_def, date_map, '?')
                print(f'    drift {code}: bench 日期不匹配'
                      f'（nav={curr_date}，bench={bench_date}），跳过本日 drift 条目',
                      file=sys.stderr)
                bench_chg = None   # 日期不符，等同于 bench 缺失

            if bench_chg is not None and hist['nav']:
                prev_nav = hist['nav'][-1]
                est_nav  = round(prev_nav * (1 + bench_chg / 100), 6)
                drift    = round((curr_nav - est_nav) / est_nav, 6)

                hist['date'].append(curr_date[5:])   # MM-DD
                hist['nav'].append(curr_nav)
                hist['est'].append(est_nav)
                hist['drift'].append(drift)

                new_entries += 1
                print(f'    drift {code}: bench={bench_chg:+.2f}%'
                      f'  est={est_nav:.4f}  act={curr_nav:.4f}'
                      f'  drift={drift * 100:+.3f}%')
            else:
                # bench 缺失、日期不符 or 首次记录（无 prev_nav）
                hist['date'].append(curr_date[5:])
                hist['nav'].append(curr_nav)
                hist['est'].append(None)
                hist['drift'].append(None)

        elif curr_date == last_date and hist['nav']:
            # ── 同日净值刷新（保留已有 drift，仅更新 nav）────
            hist['nav'][-1] = curr_nav

        # ── 截断至滚动窗口 ────────────────────────────────
        fund['history'] = {k: v[-HISTORY_DAYS:] for k, v in hist.items()}

        # ── drift_5d：取最近 5 个非 None 值 ──────────────
        drift_vals = [d for d in fund['history']['drift'] if d is not None][-5:]
        if len(drift_vals) >= DRIFT_MIN_SAMPLES:
            fund['drift_5d']          = round(sum(drift_vals) / len(drift_vals), 6)
            fund['drift_n']           = len(drift_vals)
            fund['drift_computed_at'] = now_utc   # 时间戳：本次 Action 计算 drift 的时刻
        else:
            fund.pop('drift_5d',          None)
            fund.pop('drift_n',           None)
            fund.pop('drift_computed_at', None)

    print(f'  [drift] {new_entries} 只新增记录（嵌入 fund_daily.json，无独立文件）')


def update_chain_anchors(data: dict, chg_map: dict, date_map: dict, t1_date: str) -> None:
    """
    链式补偿锚点 — 每次 Action 运行时强制写入 est_nav_yesterday。

    含义：est_nav_yesterday = official_nav × (1 + T-1_bench_chg%)

    Action 在北京 07:00（UTC 23:00）运行，chg_map 内的涨跌幅均为前一交易日
    收盘数据（美股已收盘 3h+），因此此值代表「基于最新官方净值估算昨日净值」。

    Worker 使用方式（navLag ≥ 2 时）：
      nav = est_nav_yesterday × (1 + today_bench_chg%)
          = official_nav × (1 + T-1_bench%) × (1 + today_bench%)   ← 两步链式

    时间驱动协议：
      est_nav_date 取自 date_map（bench 数据的实际交易日），而非 t1_date（nav 日期）。
      两者在正常情况下相同；QDII 净值发布延迟时可能不同，date_map 更准确。

    写入规则：
      - bench_chg 可用 → 写入 est_nav_yesterday + est_nav_date（无条件覆盖）
      - bench_chg 不可用 → 清除旧锚点，防止 Worker 误用过期数据
    """
    updated = cleared = 0
    for code, fund in data.items():
        if code.startswith('_'):
            continue
        official_nav = fund.get('nav')
        bench_def    = fund.get('bench')
        if not official_nav or not bench_def:
            continue

        bench_chg = _calc_bench_chg(bench_def, chg_map)
        if bench_chg is not None:
            # 用 date_map 中的实际交易日，而非 t1_date（nav 日期）
            bench_date = _get_bench_date(bench_def, date_map, t1_date)
            fund['est_nav_yesterday']   = round(official_nav * (1 + bench_chg / 100), 6)
            fund['est_nav_date']        = bench_date
            # 历史公证人：记录本次估算消费的 bench 指数交易日（供 Worker 幂等计算锁使用）
            fund['est_nav_index_date']  = bench_date
            updated += 1
        else:
            # bench 数据不可用，清除旧锚点防止 Worker 使用过期数据推算
            fund.pop('est_nav_yesterday',  None)
            fund.pop('est_nav_date',       None)
            fund.pop('est_nav_index_date', None)
            cleared += 1

    print(f'  [chain] 锚点写入 {updated} 只，bench 缺失清除 {cleared} 只')


# ══════════════════════════════════════════════════════
#  持仓审计：偏移计算
# ══════════════════════════════════════════════════════

def _norm_bench(tq: str) -> str:
    """usXBI → XBI  |  hkHSI → HSI  |  sh518880 → 518880  |  sinaAG0 → AG0"""
    for pfx in ('sina', 'csi', 'us', 'hk', 'sh', 'sz'):
        if tq.startswith(pfx):
            return tq[len(pfx):].upper()
    return tq.upper()


def _norm_hold(code: str) -> str:
    """XBI.US → XBI  |  00700.HK → 00700"""
    return code.split('.')[0].upper().strip()


def calc_drift(bench_def, holdings: list) -> tuple:
    """
    计算 holdings vs bench_def 的累计绝对权重偏离。

    返回 (drift: float | None, details: list)
    - drift=None  → bench 为纯指数型（holdings 无法匹配），跳过审计
    - drift=0.07  → 累计偏离 7%（超过 DRIFT_THRESHOLD 则触发报警）

    details 每项：{bench_code, bench_w(%), hold_w(%), dev(%)}
    """
    if not holdings or not bench_def:
        return None, []

    # holdings → {NORM_CODE: ratio/100}
    hold_map = {
        _norm_hold(h['code']): h['ratio'] / 100.0
        for h in holdings if h.get('code')
    }

    # bench → [(NORM_CODE, weight)]，权重归一化
    if isinstance(bench_def, str):
        bench_weights = [(_norm_bench(bench_def), 1.0)]
    elif isinstance(bench_def, list):
        total_w = sum(b['w'] for b in bench_def) or 1.0
        bench_weights = [(_norm_bench(b['tq']), b['w'] / total_w) for b in bench_def]
    else:
        return None, []

    # 若 holdings 中完全找不到任何 bench 成分
    # → 纯指数类基金（持有成分股而非 ETF），跳过
    if not any(bc in hold_map for bc, _ in bench_weights):
        return None, []

    details = []
    drift = 0.0
    for bench_code, bench_w in bench_weights:
        hold_w = hold_map.get(bench_code, 0.0)
        dev = abs(bench_w - hold_w)
        drift += dev
        details.append({
            'bench_code': bench_code,
            'bench_w':    round(bench_w * 100, 2),
            'hold_w':     round(hold_w * 100, 2),
            'dev':        round(dev * 100, 2),
        })

    return round(drift, 4), details


# ══════════════════════════════════════════════════════
#  报警：GitHub Issue
# ══════════════════════════════════════════════════════

_GH_HEADERS = {
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}

LABEL_NAME  = 'Holdings Drift'
LABEL_COLOR = 'e11d48'  # 深红


def _gh_headers(token: str) -> dict:
    return {**_GH_HEADERS, 'Authorization': f'Bearer {token}'}


def ensure_label(token: str, repo: str) -> bool:
    """确保 Holdings Drift 标签存在，不存在则创建"""
    check_url = f'https://api.github.com/repos/{repo}/labels/{urllib.parse.quote(LABEL_NAME)}'
    req = urllib.request.Request(check_url, headers=_gh_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200   # 已存在
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False
    # 创建标签
    try:
        status, _ = post_json(
            f'https://api.github.com/repos/{repo}/labels',
            {'name': LABEL_NAME, 'color': LABEL_COLOR, 'description': '基准持仓偏移预警'},
            extra_headers=_gh_headers(token),
        )
        return status in (200, 201)
    except Exception as e:
        print(f'    [gh] 创建 label 失败: {e}', file=sys.stderr)
        return False


def send_github_issue(drifted: list, token: str, repo: str):
    if not token or not repo:
        print('    [gh] 未设置 GITHUB_TOKEN / GITHUB_REPOSITORY，跳过', file=sys.stderr)
        return

    ensure_label(token, repo)

    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    lines = [
        '## LOF 基准持仓偏移报告',
        '',
        f'> 同步时间：`{now} UTC`  ',
        f'> 偏移阈值：**{DRIFT_THRESHOLD * 100:.0f}%**  ',
        f'> 触发基金：**{len(drifted)} 只**',
        '',
        '---',
        '',
    ]

    for fund in drifted:
        lines += [
            f'### {fund["name"]}（{fund["code"]}）　偏移 = **{fund["drift"] * 100:.1f}%**',
            '',
            '| 基准成分 | BENCH 权重 | 实际持仓 | 偏差 |',
            '|:---------|----------:|--------:|-----:|',
        ]
        for d in fund['details']:
            flag = ' ⚠️' if d['dev'] >= 5 else ''
            lines.append(
                f'| `{d["bench_code"]}` | {d["bench_w"]:.1f}% | {d["hold_w"]:.1f}% | {d["dev"]:.1f}%{flag} |'
            )
        lines += [
            '',
            '<details><summary>前五大实际持仓</summary>',
            '',
        ]
        for h in fund['holdings'][:5]:
            lines.append(f'- **{h["name"]}** (`{h["code"]}`): {h["ratio"]}%')
        lines += ['', '</details>', '', '---', '']

    lines.append('_由 LOF 套利雷达 GitHub Action 自动生成_')

    title = f'[Holdings Drift] {", ".join(f["code"] for f in drifted)}'
    body  = '\n'.join(lines)

    try:
        status, resp = post_json(
            f'https://api.github.com/repos/{repo}/issues',
            {'title': title, 'body': body, 'labels': [LABEL_NAME]},
            extra_headers=_gh_headers(token),
        )
        if status == 201:
            issue_url = json.loads(resp).get('html_url', '(no url)')
            print(f'    [gh] Issue 已创建: {issue_url}')
        else:
            print(f'    [gh] Issue 创建失败: HTTP {status}: {resp[:200]}', file=sys.stderr)
    except Exception as e:
        print(f'    [gh] Issue 异常: {e}', file=sys.stderr)


# ══════════════════════════════════════════════════════
#  报警：企业微信 Webhook
# ══════════════════════════════════════════════════════

def send_wechat(drifted: list, wx_key: str):
    if not wx_key:
        print('    [wx] 未设置 WX_KEY，跳过', file=sys.stderr)
        return

    lines = [
        '**LOF套利雷达 ⚠️ 持仓基准偏移预警**',
        f'偏移阈值 **{DRIFT_THRESHOLD * 100:.0f}%**，触发 **{len(drifted)}** 只：',
        '',
    ]
    for fund in drifted:
        lines.append(f'> **{fund["name"]}**（{fund["code"]}）偏移 **{fund["drift"] * 100:.1f}%**')
        for d in fund['details']:
            if d['dev'] >= 1.0:   # 只显示实质偏差的分量
                lines.append(
                    f'>  `{d["bench_code"]}`: BENCH {d["bench_w"]:.1f}% → 实际 {d["hold_w"]:.1f}%'
                )
        lines.append('')
    lines.append('> 请检查 BENCH 配置是否需要更新')

    content = '\n'.join(lines)
    url = f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wx_key}'

    try:
        status, resp = post_json(url, {'msgtype': 'markdown', 'markdown': {'content': content}})
        if status == 200:
            print('    [wx] 推送成功')
        else:
            print(f'    [wx] 推送失败: HTTP {status}: {resp[:100]}', file=sys.stderr)
    except Exception as e:
        print(f'    [wx] 推送异常: {e}', file=sys.stderr)


# ══════════════════════════════════════════════════════
#  持仓审计入口
# ══════════════════════════════════════════════════════

def run_drift_audit(data: dict):
    """
    审计全部基金持仓偏移。容错：任何异常均打印警告，不抛出。
    必须在 fund_daily.json 写入之后调用。
    """
    token  = os.environ.get('GITHUB_TOKEN', '')
    repo   = os.environ.get('GITHUB_REPOSITORY', '')   # "owner/repo"
    wx_key = os.environ.get('WX_KEY', '')

    drifted = []
    for code, fund in data.items():
        if code.startswith('_'):
            continue
        holdings = fund.get('holdings')
        bench_def = fund.get('bench')
        if not holdings or not bench_def:
            continue

        drift, details = calc_drift(bench_def, holdings)
        if drift is None:
            continue   # 纯指数类基金，持有成分股，无法用持仓代码匹配 bench

        if drift > DRIFT_THRESHOLD:
            drifted.append({
                'code':     code,
                'name':     fund.get('name', code),
                'drift':    drift,
                'details':  details,
                'holdings': (holdings or [])[:5],
            })
            print(f'  ⚠️  {code} {fund.get("name", ""):16s}  drift={drift * 100:.1f}%')

    if not drifted:
        print('[audit] 全部基金偏移在阈值内，无需报警')
        return

    print(f'[audit] {len(drifted)} 只触发阈值，发送双路报警 ...')

    # 双路独立容错：任一失败不影响另一路，也不影响已写入的 JSON
    try:
        send_github_issue(drifted, token, repo)
    except Exception as e:
        print(f'[audit] GitHub Issue 意外失败: {e}', file=sys.stderr)

    try:
        send_wechat(drifted, wx_key)
    except Exception as e:
        print(f'[audit] 企业微信意外失败: {e}', file=sys.stderr)


# ══════════════════════════════════════════════════════
#  主同步函数
# ══════════════════════════════════════════════════════

def fetch_fx_settlement_rates() -> dict:
    """
    通过 Sina 抓取 USD/CNH 和 HKD/CNH 现价，作为 T-1 结算汇率存入 fund_daily.json._fx。
    Action 在北京 00:05 运行，FX 市场 24/5 开放，所得汇率近似于昨日 15:00 结算价。
    """
    # fx_shkdcnh = 正确的港元代码；fx_shkcnh 为无效代码（返回空串），已修正
    url  = 'https://hq.sinajs.cn/list=fx_susdcnh,fx_shkdcnh'
    text = fetch_url(url, referer='https://finance.sina.com.cn')
    if not text:
        return {}
    result = {}
    for sina_code, key in [('fx_susdcnh', 'usd_cnh'), ('fx_shkdcnh', 'hkd_cnh')]:
        m = re.search(rf'hq_str_{sina_code}="([^"]+)"', text)
        if m:
            parts = m.group(1).split(',')
            try:
                rate = float(parts[1])
                if rate > 0:
                    result[key] = round(rate, 4)
            except (ValueError, IndexError):
                pass
    return result


# ══════════════════════════════════════════════════════
#  商品期货参考价抓取（16:00 ET 快照）
# ══════════════════════════════════════════════════════

def _et_16_unix(run_utc: datetime) -> int:
    """计算当天（美东时间）16:00 ET 对应的 Unix 时间戳（整秒）。
    自动处理 EDT（UTC-4）/ EST（UTC-5）夏令时切换，无需硬编码。
    如果 ZoneInfo 不可用（Python < 3.9），降级为 UTC-5 固定偏移近似。
    """
    if _ET:
        from datetime import datetime as _dt
        # 先转换到美东时区，取日期，再构造当天 16:00 ET，再转 UTC Unix 时间戳
        run_et = run_utc.astimezone(_ET)
        et_16 = _dt(run_et.year, run_et.month, run_et.day, 16, 0, 0, tzinfo=_ET)
        return int(et_16.timestamp())
    else:
        # 降级：用 UTC-5（EST）固定偏移；夏令时期间偏差 1h，可接受（15 分钟窗口内仍能找到 K 线）
        run_date_est = (run_utc - timedelta(hours=5)).date()
        # 16:00 EST = 21:00 UTC
        et_16_utc = datetime(run_date_est.year, run_date_est.month, run_date_est.day,
                             21, 0, 0, tzinfo=timezone.utc)
        return int(et_16_utc.timestamp())


def fetch_commodity_ref_price(sym_encoded: str, target_unix: int) -> float | None:
    """从 Yahoo Finance 5 分钟 K 线历史，找 16:00 ET 最近的收盘价。
    target_unix：16:00 ET 的 Unix 时间戳（整秒）
    最大允许偏差：15 分钟（若最近 K 线超出此范围则视为数据缺失）
    """
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{sym_encoded}'
           f'?interval=5m&range=1d&includePrePost=false')
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
               'Accept': 'application/json'}
    raw = fetch_url(url, timeout=15)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        result = d.get('chart', {}).get('result', [])
        if not result:
            return None
        ts_list   = result[0].get('timestamp', [])
        closes    = result[0].get('indicators', {}).get('quote', [{}])[0].get('close', [])
        if not ts_list or not closes:
            return None
        # 找到距离 target_unix 最近的时间戳索引
        best_idx, best_diff = 0, abs(ts_list[0] - target_unix)
        for i, t in enumerate(ts_list):
            diff = abs(t - target_unix)
            if diff < best_diff:
                best_diff, best_idx = diff, i
        if best_diff > 15 * 60:
            print(f'[WARN] {sym_encoded}: 16:00 ET 偏差 {best_diff//60}min，超过15min，跳过')
            return None
        price = closes[best_idx]
        if price is None or not (price > 0):
            return None
        return round(float(price), 4)
    except Exception as e:
        print(f'[WARN] fetch_commodity_ref_price {sym_encoded}: {e}')
        return None


def fetch_commodity_refs(t1_date: str, fx_rates: dict) -> dict | None:
    """抓取所有商品期货 16:00 ET 的参考价格，并附上 USD/CNH 汇率双重锚定。
    返回写入 fund_daily.json._commodity_refs 的字典；全部失败时返回 None。
    """
    run_utc   = datetime.now(timezone.utc)
    target_ts = _et_16_unix(run_utc)
    # 将目标时间格式化供日志确认
    ref_et_time = datetime.fromtimestamp(target_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'  目标快照时间：{ref_et_time}（16:00 ET）')

    refs: dict = {}
    for ref_key, sym_encoded in _COMMODITY_FUTURES.items():
        price = fetch_commodity_ref_price(sym_encoded, target_ts)
        if price is not None:
            refs[ref_key] = price
            print(f'  [OK]  {ref_key} ({sym_encoded}): {price}')
        else:
            print(f'  [WARN] {ref_key} ({sym_encoded}): 获取失败，Worker 将降级至结算价基准')

    if not refs:
        return None  # 全部失败，不写入；Worker 保留上次数据并降级

    # 附加 USD/CNH 汇率（与期货价格同步锚定，Worker 用于 FX 修正一致性）
    usd_cnh = fx_rates.get('usd_cnh') or fx_rates.get('usd_cnh_t1')
    if usd_cnh:
        refs['usd_cnh'] = round(float(usd_cnh), 4)

    refs['ref_et_time'] = ref_et_time
    refs['sync_at']     = run_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    return refs


def sync():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data: dict = json.load(f)

    # ── 汇率 T-1 结算价（优先于净值循环，失败则保留旧值） ──
    print('--- 汇率 T-1 结算价 ---')
    fx_rates = fetch_fx_settlement_rates()
    today_utc_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if fx_rates:
        data['_fx'] = {
            'usd_cnh_t1': fx_rates.get('usd_cnh', (data.get('_fx') or {}).get('usd_cnh_t1')),
            'hkd_cnh_t1': fx_rates.get('hkd_cnh', (data.get('_fx') or {}).get('hkd_cnh_t1')),
            'date': today_utc_date,
        }
        print(f'  USD/CNH={data["_fx"]["usd_cnh_t1"]}  HKD/CNH={data["_fx"]["hkd_cnh_t1"]}')
    else:
        print('  [fx] Sina 汇率抓取失败，保留上次结算价')

    fund_codes = [k for k in data if not k.startswith('_')]
    total = len(fund_codes)
    nav_ok = nav_kept = nav_fail = hold_ok = hold_fail = 0
    now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')

    print(f'=== 同步开始 | {total} 只基金 | {now_utc} UTC\n')

    for code in fund_codes:
        fund = data[code]
        name = fund.get('name', code)

        # ── 净值：取三路中日期最新的结果 ──────────────────
        # fundgz 有时返回旧数据（如 QDII 基金 jzrq 滞后），lsjz 可能更新
        # 策略：先拿 fundgz 和 lsjz 两路，取 nav_date 较新者；再兜底 pingzhong
        _r_gz = fetch_fundgz(code)
        _r_lsjz = fetch_lsjz(code)
        if _r_gz and _r_lsjz:
            nav_result = _r_lsjz if (_r_lsjz['nav_date'] > _r_gz['nav_date']) else _r_gz
        else:
            nav_result = _r_gz or _r_lsjz or fetch_pingzhong(code)
        if nav_result:
            old_date = fund.get('nav_date') or ''
            new_date = nav_result.get('nav_date') or ''
            if new_date >= old_date:
                fund.update(nav_result)
                fund['nav_fetch_time'] = now_utc          # 时间戳：本次成功抓取时刻
                fund['nav_consecutive_fails'] = 0          # 连续失败计数归零
                nav_ok += 1
                print(
                    f'  ✓ NAV  {code} {name:16s}  '
                    f'{nav_result["nav"]:.4f}  {new_date}  [{nav_result["nav_src"]}]'
                )
            else:
                # 新日期比旧日期更早（异常），保留旧值
                nav_kept += 1
                print(
                    f'  ⟳ NAV  {code} {name:16s}  '
                    f'新日期 {new_date} < 旧日期 {old_date}，保留旧值'
                )
        else:
            nav_kept += 1
            nav_fail += 1
            fails = fund.get('nav_consecutive_fails', 0) + 1
            fund['nav_consecutive_fails'] = fails          # 连续失败计数累加
            print(
                f'  ✗ NAV  {code} {name:16s}  FAILED (连续{fails}次) — 保留 '
                f'{fund.get("nav")} ({fund.get("nav_date")})',
                file=sys.stderr,
            )
        time.sleep(0.08)

        # ── 持仓 ──────────────────────────────────────
        hold_result = fetch_holdings(code)
        if hold_result:
            fund['holdings']            = hold_result['holdings']
            fund['holdings_date']       = hold_result['holdings_date']
            fund['holdings_fetch_time'] = now_utc
            hold_ok += 1
            top = hold_result['holdings'][0]
            print(f'    持仓 {len(hold_result["holdings"]):2d}只  截止:{hold_result["holdings_date"]}  首位: {top["name"]} {top["ratio"]}%')
        else:
            # EM jjcc 无数据 → 公告驱动增量更新（传入本地日期，只在季报推进时下载）
            existing_date = fund.get('holdings_date') or ''
            pdf_result = _fetch_holdings_via_pdf(code, existing_date)
            if pdf_result is None:
                hold_fail += 1
                print(f'    持仓 FAILED (EM+PDF) — 保留旧值', file=sys.stderr)
            elif pdf_result.get('skipped'):
                print(f'    持仓 EM无数据，季报无更新（截止: {pdf_result.get("holdings_date", "")}）')
            else:
                fund['holdings']            = pdf_result['holdings']
                fund['holdings_date']       = pdf_result['holdings_date']
                fund['holdings_fetch_time'] = now_utc
                hold_ok += 1
                top = pdf_result['holdings'][0]
                print(f'    持仓(PDF) {len(pdf_result["holdings"]):2d}只  '
                      f'截止:{pdf_result["holdings_date"]}  '
                      f'首位: {top["name"]} {top["ratio"]}%')
        time.sleep(0.08)

        # ── 总份额 ────────────────────────────────────
        shares = fetch_shares(code)
        if shares is not None:
            fund['shares'] = shares
            print(f'    份额 {shares}亿份')
        time.sleep(0.08)

    # ── 偏差校准 Drift + 链式补偿锚点 ───────────────────────
    print('\n--- 偏差校准 Drift + 链式补偿锚点 ---')

    # t1_date：本次数据对应的交易日（所有基金 nav_date 最大值）
    # 必须在 fetch_bench_chg_batch 之前计算，作为 EM/Tencent 数据的日期推断依据
    all_nav_dates = [v.get('nav_date', '') for k, v in data.items()
                     if not k.startswith('_') and v.get('nav_date')]
    t1_date = max(all_nav_dates) if all_nav_dates else ''

    now_utc_drift = datetime.now(timezone.utc).isoformat(timespec='seconds')
    bench_chg_map, bench_date_map = fetch_bench_chg_batch(data, t1_date)
    update_drift(data, bench_chg_map, bench_date_map, now_utc_drift)
    update_chain_anchors(data, bench_chg_map, bench_date_map, t1_date)

    # ── 商品期货参考价（16:00 ET 快照，Worker 用于精准估值） ──
    print('\n--- 商品期货参考价 (16:00 ET) ---')
    commodity_refs = fetch_commodity_refs(t1_date, fx_rates or {})
    if commodity_refs:
        data['_commodity_refs'] = commodity_refs
        ok_count = sum(1 for k in commodity_refs if k not in ('ref_et_time', 'usd_cnh', 'sync_at'))
        print(f'  写入 _commodity_refs: {ok_count}/{len(_COMMODITY_FUTURES)} 合约成功  '
              f'ref_et_time={commodity_refs.get("ref_et_time")}')
    else:
        print('  [WARN] 全部商品期货参考价获取失败，保留上次数据（Worker 将降级至结算价基准）',
              file=sys.stderr)

    # ── 写 _meta ──────────────────────────────────────
    now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
    data_date = t1_date  # 已在 chain anchors 前计算
    # ── trading_dates：生成当年全量交易日历（Worker 拥有"上帝视角"，无需推测） ──
    # Q4 时附加次年日历，避免跨年边界（如 12 月底 benchDate 落入次年）
    run_utc = datetime.now(timezone.utc)
    trading_dates = gen_trading_dates_for_year(run_utc.year)
    if run_utc.month >= 10:
        trading_dates += gen_trading_dates_for_year(run_utc.year + 1)

    data['_meta'] = {
        'sync_time':     now_utc,
        'data_date':     data_date,          # 本次数据覆盖的交易日
        'nav_ok':        nav_ok,
        'nav_kept':      nav_kept,
        'hold_ok':       hold_ok,
        'total':         total,
        'trading_dates': trading_dates,      # 全年交易日历（当年 + Q4 时附加次年）
    }

    # ── 写文件（必须在报警之前完成） ─────────────────
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(
        f'\n=== 写入完成 | '
        f'NAV {nav_ok} 更新 / {nav_kept} 保留 / {nav_fail} 失败 | '
        f'持仓 {hold_ok}/{total} | {now_utc}'
    )

    # ── 连续失败检查：≥3 次则 Action 报错（强制暴露数据源故障）──
    critical = [(k, v.get('nav_consecutive_fails',0))
                for k, v in data.items()
                if not k.startswith('_') and v.get('nav_consecutive_fails', 0) >= 3]
    if critical:
        msgs = ', '.join(f'{c[0]}({c[1]}次)' for c in critical)
        raise RuntimeError(
            f'NAV 连续抓取失败 ≥3 次，请检查数据源: {msgs}'
        )

    # ── 持仓审计 + 双路报警（容错，失败不影响 commit） ─
    print('\n--- 持仓审计 ---')
    run_drift_audit(data)


if __name__ == '__main__':
    sync()
