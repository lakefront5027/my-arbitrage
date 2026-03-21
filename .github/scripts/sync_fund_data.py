#!/usr/bin/env python3
"""
LOF套利雷达 — 每日数据同步 + 持仓审计脚本
运行时间：北京时间 00:05（UTC 16:05）
流程：
  1. 拉取 47 只基金 T-1 净值（fundgz → lsjz 兜底，失败保留旧值）
  2. 拉取 47 只基金前十大持仓（东方财富 jjcc API）
  3. 写入 fund_daily.json（含 _meta.sync_time）
  4. 持仓审计：对比新持仓 vs BENCH 权重，偏移 > 5% 触发双路报警
     - GitHub Issue（带 Holdings Drift 标签）
     - 企业微信 Webhook Markdown 推送
  容错原则：报警失败不阻塞文件写入，文件写入在报警之前完成。
"""

import json
import re
import sys
import time
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(os.path.dirname(SCRIPT_DIR))
JSON_PATH  = os.path.join(REPO_ROOT, 'data', 'fund_daily.json')

DRIFT_THRESHOLD     = 0.05   # 累计权重偏移阈值（5%）
DRIFT_HISTORY_PATH  = os.path.join(REPO_ROOT, 'data', 'drift_history.json')
DRIFT_HISTORY_DAYS  = 30     # 滚动保留天数
DRIFT_MIN_SAMPLES   = 3      # drift_5d 最少有效样本数

# EM 指数代码映射（同 worker-full.js EM_CODES）
_EM_CODES = {
    'csi930917': '2.930917',
    'csi930914': '2.930914',
    'csi930792': '2.930792',
    'sh000985':  '1.000985',
    'hkHSSI':    '124.HSSI',
    'hkHSMI':    '124.HSMI',
    'hkHSCI':    '124.HSCI',
    'sinaAG0':   '113.AG0',
}


# ══════════════════════════════════════════════════════
#  基础 HTTP 工具
# ══════════════════════════════════════════════════════

def fetch_url(url: str, referer: str = '', timeout: int = 12) -> str | None:
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
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw.decode('gbk', errors='replace')
    except Exception as e:
        print(f'    [http] {url[:80]} → {e}', file=sys.stderr)
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


def fetch_holdings(code: str) -> list | None:
    """
    天天基金 FundArchivesDatas.aspx?type=jjcc 季报披露页面。
    逐季尝试直到找到非空数据，解析 HTML 表格，返回前十大持仓。
    字段：code（标的代码）、name（中文简称）、ratio（占净值比例%）。

    原 api.fund.eastmoney.com/f10/jjcc 对 QDII 基金全部返回 ErrCode=4，
    改用此 HTML 接口可正常获取季报数据。
    """
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
                return result[:10]

    print(f'    [holdings] {code}: 近 6 季均无数据（可能为单只 ETF 持仓型基金）',
          file=sys.stderr)
    return None


# ══════════════════════════════════════════════════════
#  偏差校准 Drift
# ══════════════════════════════════════════════════════

def fetch_bench_chg_batch(data: dict) -> dict:
    """
    从 fund_daily.json 收集所有基准代码，批量拉取当日涨跌幅。
    Tencent 一次批量请求；EM 代码逐个请求（与 Worker fetchEastmoney 等价）。
    返回 { tq_code: chg_pct }。
    注意：Action 运行于北京 00:05（UTC 16:05），此时：
      • A 股 / 港股指数：当日收盘完毕，chg 为准确的全天变化。
      • 美股 ETF：美市仍开盘（ET 12:05），chg 为盘中值；5日均值可平滑此噪声。
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

    chg_map: dict = {}

    # ── Tencent 批量 ──
    if tq_direct:
        url  = f'https://qt.gtimg.cn/q={",".join(tq_direct)}'
        text = fetch_url(url, referer='https://gu.qq.com')
        if text:
            for code in tq_direct:
                m = re.search(rf'v_{re.escape(code)}="([^"]+)"', text)
                if not m:
                    continue
                p = m.group(1).split('~')
                try:
                    price, prev = float(p[3]), float(p[4])
                    if price > 0 and prev > 0:
                        chg_map[code] = (price - prev) / prev * 100
                except (ValueError, IndexError):
                    pass
        print(f'  [drift/bench] Tencent: '
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
                if d.get('data') and d['data'].get('f43', 0) > 0:
                    chg_map[tq_key] = (d['data'].get('f170') or 0) / 100
            except Exception:
                pass
        time.sleep(0.05)
    print(f'  [drift/bench] EM: '
          f'{sum(1 for k in em_keys if k in chg_map)}/{len(em_keys)} 成功')

    return chg_map


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


def update_drift(data: dict, chg_map: dict) -> None:
    """
    对每只基金：
      est_nav = prev_nav × (1 + bench_chg%)
      drift   = (curr_nav − est_nav) / est_nav
    追加到 drift_history.json（滚动 30 天），
    计算 drift_5d（近 5 个有效样本均值），写回 data[code]['drift_5d']。
    """
    try:
        with open(DRIFT_HISTORY_PATH, 'r', encoding='utf-8') as f:
            history: dict = json.load(f)
    except FileNotFoundError:
        history = {}

    new_entries = 0
    for code, fund in data.items():
        if code.startswith('_'):
            continue
        curr_nav  = fund.get('nav')
        curr_date = fund.get('nav_date', '')
        bench_def = fund.get('bench')
        if not curr_nav or not curr_date or not bench_def:
            continue

        hist = history.setdefault(code, [])

        if hist:
            prev      = hist[-1]
            prev_date = prev.get('date', '')
            prev_nav  = prev.get('nav')

            if curr_date > prev_date and prev_nav:
                # 新交易日：计算本次偏差
                bench_chg = _calc_bench_chg(bench_def, chg_map)
                if bench_chg is not None:
                    est_nav = prev_nav * (1 + bench_chg / 100)
                    drift   = (curr_nav - est_nav) / est_nav
                    hist.append({
                        'date':      curr_date,
                        'nav':       curr_nav,
                        'prev_nav':  prev_nav,
                        'bench_chg': round(bench_chg, 4),
                        'est_nav':   round(est_nav, 6),
                        'drift':     round(drift, 6),
                    })
                    new_entries += 1
                    print(f'    drift {code}: bench={bench_chg:+.2f}% '
                          f'est={est_nav:.4f} act={curr_nav:.4f} '
                          f'drift={drift * 100:+.3f}%')
                else:
                    # bench 数据缺失：仅记录 nav，不计 drift
                    hist.append({'date': curr_date, 'nav': curr_nav})

            elif curr_date == prev_date:
                # 同日净值刷新（不重新计算 drift）
                hist[-1] = {**prev, 'nav': curr_nav}
        else:
            # 首次记录：无前值，无法计算 drift
            hist.append({'date': curr_date, 'nav': curr_nav})

        # 滚动窗口截断
        history[code] = hist[-DRIFT_HISTORY_DAYS:]

        # 计算 drift_5d（最近 5 个含 drift 字段的条目）
        drift_vals = [e['drift'] for e in history[code] if 'drift' in e][-5:]
        if len(drift_vals) >= DRIFT_MIN_SAMPLES:
            fund['drift_5d'] = round(sum(drift_vals) / len(drift_vals), 6)
            fund['drift_n']  = len(drift_vals)

    with open(DRIFT_HISTORY_PATH, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f'  [drift] {new_entries} 只新增记录 | 已保存 {DRIFT_HISTORY_PATH}')


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

def sync():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data: dict = json.load(f)

    fund_codes = [k for k in data if not k.startswith('_')]
    total = len(fund_codes)
    nav_ok = nav_kept = nav_fail = hold_ok = hold_fail = 0

    print(f'=== 同步开始 | {total} 只基金 | {datetime.now(timezone.utc).isoformat(timespec="seconds")} UTC\n')

    for code in fund_codes:
        fund = data[code]
        name = fund.get('name', code)

        # ── 净值：fundgz → lsjz → pingzhong 三路兜底 ──
        nav_result = fetch_fundgz(code) or fetch_lsjz(code) or fetch_pingzhong(code)
        if nav_result:
            old_date = fund.get('nav_date') or ''
            new_date = nav_result.get('nav_date') or ''
            if new_date >= old_date:
                fund.update(nav_result)
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
            print(
                f'  ✗ NAV  {code} {name:16s}  FAILED — 保留 '
                f'{fund.get("nav")} ({fund.get("nav_date")})',
                file=sys.stderr,
            )
        time.sleep(0.08)

        # ── 持仓 ──────────────────────────────────────
        holdings = fetch_holdings(code)
        if holdings:
            fund['holdings'] = holdings
            hold_ok += 1
            top = holdings[0]
            print(f'    持仓 {len(holdings):2d}只  首位: {top["name"]} {top["ratio"]}%')
        else:
            hold_fail += 1
            print(f'    持仓 FAILED — 保留旧值', file=sys.stderr)
        time.sleep(0.08)

        # ── 总份额 ────────────────────────────────────
        shares = fetch_shares(code)
        if shares is not None:
            fund['shares'] = shares
            print(f'    份额 {shares}亿份')
        time.sleep(0.08)

    # ── 偏差校准 Drift ──────────────────────────────────────
    print('\n--- 偏差校准 Drift ---')
    bench_chg_map = fetch_bench_chg_batch(data)
    update_drift(data, bench_chg_map)

    # ── 写 _meta ──────────────────────────────────────
    now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
    data['_meta'] = {
        'sync_time': now_utc,
        'nav_ok':    nav_ok,
        'nav_kept':  nav_kept,
        'hold_ok':   hold_ok,
        'total':     total,
    }

    # ── 写文件（必须在报警之前完成） ─────────────────
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(
        f'\n=== 写入完成 | '
        f'NAV {nav_ok} 更新 / {nav_kept} 保留 / {nav_fail} 失败 | '
        f'持仓 {hold_ok}/{total} | {now_utc}'
    )

    # ── 持仓审计 + 双路报警（容错，失败不影响 commit） ─
    print('\n--- 持仓审计 ---')
    run_drift_audit(data)


if __name__ == '__main__':
    sync()
