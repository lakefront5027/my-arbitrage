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

JJCC_CB         = 'apidata'
DRIFT_THRESHOLD = 0.05   # 累计权重偏移阈值（5%）


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


# ══════════════════════════════════════════════════════
#  持仓抓取
# ══════════════════════════════════════════════════════

def fetch_holdings(code: str) -> list | None:
    """
    东方财富 jjcc 接口，前十大持仓。
    合并 stockList / bondList / otherList，按 JZBL 降序，取 Top-10。
    字段：code（标的代码）、name（中文简称）、ratio（占净值比例%）。
    """
    url = (
        f'https://api.fund.eastmoney.com/f10/jjcc'
        f'?fundCode={code}&pageIndex=1&pageSize=200&callback={JJCC_CB}'
    )
    text = fetch_url(url, referer='https://fundf10.eastmoney.com')
    if not text:
        return None
    m = re.search(rf'{JJCC_CB}\s*\((.+)\)\s*;?\s*$', text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        raw = d.get('Data') or {}
        items = (
            (raw.get('stockList') or [])
            + (raw.get('bondList') or [])
            + (raw.get('otherList') or [])
        )
        if not items:
            return None
        result = []
        for item in items:
            name = (
                item.get('GPJC') or item.get('GPMC')
                or item.get('ZWMC') or item.get('GPDM') or ''
            )
            try:
                ratio = float(item.get('JZBL') or 0)
            except ValueError:
                ratio = 0.0
            if name:
                result.append({
                    'code':  item.get('GPDM', ''),
                    'name':  name,
                    'ratio': round(ratio, 2),
                })
        result.sort(key=lambda x: x['ratio'], reverse=True)
        return result[:10] if result else None
    except Exception as e:
        print(f'    [holdings] {code}: parse error {e}', file=sys.stderr)
        return None


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

        # ── 净值 ──────────────────────────────────────
        nav_result = fetch_fundgz(code) or fetch_lsjz(code)
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
