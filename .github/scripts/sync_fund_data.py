#!/usr/bin/env python3
"""
LOF套利雷达 — 每日净值同步脚本
从 fundgz.1234567.com.cn 和 api.fund.eastmoney.com/f10/lsjz 拉取 T-1 净值，
写回 data/fund_daily.json。由 GitHub Actions 每个交易日 17:30 北京时间触发。
"""

import json
import re
import time
import urllib.request
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(os.path.dirname(SCRIPT_DIR))
JSON_PATH   = os.path.join(REPO_ROOT, 'data', 'fund_daily.json')


def fetch_url(url: str, referer: str = '', timeout: int = 10) -> str | None:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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
        return None


def fetch_fundgz(code: str) -> dict | None:
    """天天基金估值接口（优先 dwjz，备用 gsz）"""
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
        if nav > 0 and nav <= 50:
            return {'nav': nav, 'nav_date': d.get('jzrq', ''), 'nav_src': 'fundgz'}
        gsz = float(d.get('gsz') or 0)
        if gsz > 0:
            date_str = (d.get('gztime') or '')[:10]
            return {'nav': gsz, 'nav_date': date_str, 'nav_src': 'fundgz_est'}
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def fetch_lsjz(code: str) -> dict | None:
    """东方财富历史净值接口（带 Referer 的 JSONP）"""
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
            item = items[0]
            nav = float(item.get('DWJZ') or 0)
            if nav > 0:
                return {'nav': nav, 'nav_date': item.get('FSRQ', ''), 'nav_src': 'lsjz'}
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def sync_navs():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data: dict = json.load(f)

    ok = 0
    fail_codes = []

    for code, fund in data.items():
        result = fetch_fundgz(code) or fetch_lsjz(code)
        if result:
            fund.update(result)
            ok += 1
            print(f'  ✓ {code} {fund["name"]:16s}  {result["nav"]:.4f}  {result["nav_date"]}  [{result["nav_src"]}]')
        else:
            fail_codes.append(code)
            print(f'  ✗ {code} {fund["name"]:16s}  FAILED', file=sys.stderr)
        time.sleep(0.08)   # 80 ms 间隔，对服务器友好

    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    total = len(data)
    print(f'\n=== Done: {ok}/{total} updated', end='')
    if fail_codes:
        print(f', {len(fail_codes)} failed: {",".join(fail_codes)}', end='')
    print()

    # 若超过半数失败则视为网络异常，退出码非零以便 Action 报错
    if ok < total // 2:
        print('ERROR: too many failures, aborting commit', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    sync_navs()
