#!/usr/bin/env python3
"""
LOF套利雷达 — 每日数据同步脚本
运行时间：北京时间 00:05（UTC 16:05 前一天）
任务：
  1. 拉取 47 只基金的 T-1 净值（fundgz → lsjz 兜底），若均失败保留旧值
  2. 拉取 47 只基金前十大持仓（东方财富 jjcc API）
  3. 在 JSON 根部写入 _meta.sync_time（UTC ISO-8601）
写回 data/fund_daily.json，由 GitHub Actions 自动 commit & push。
"""

import json
import re
import sys
import time
import os
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(os.path.dirname(SCRIPT_DIR))
JSON_PATH  = os.path.join(REPO_ROOT, 'data', 'fund_daily.json')

# 东方财富 JJCC 持仓接口使用的 callback 名
JJCC_CB = 'apidata'


# ──────────────────────────────────────────────────────
#  基础 HTTP 工具
# ──────────────────────────────────────────────────────

def fetch_url(url: str, referer: str = '', timeout: int = 12) -> str | None:
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
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
        print(f'    [fetch_url] {url[:80]} → {e}', file=sys.stderr)
        return None


# ──────────────────────────────────────────────────────
#  净值抓取
# ──────────────────────────────────────────────────────

def fetch_fundgz(code: str) -> dict | None:
    """天天基金估值接口（优先 dwjz 单位净值，备用 gsz 估算净值）"""
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
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def fetch_lsjz(code: str) -> dict | None:
    """东方财富历史净值接口（JSONP，需 fund.eastmoney.com Referer）"""
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
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


# ──────────────────────────────────────────────────────
#  持仓抓取
# ──────────────────────────────────────────────────────

def fetch_holdings(code: str) -> list | None:
    """
    东方财富 jjcc 接口，返回前十大持仓。
    字段：code（标的代码）、name（中文简称）、ratio（占净值比例%）。
    stockList 为股票/ETF，bondList 为债券，otherList 为期货/其他。
    若全部为空则返回 None。
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
        raw_data = d.get('Data') or {}

        # 合并 stockList / bondList / otherList，按 JZBL 降序取前10
        all_items = (
            (raw_data.get('stockList') or [])
            + (raw_data.get('bondList') or [])
            + (raw_data.get('otherList') or [])
        )
        if not all_items:
            return None

        result = []
        for item in all_items:
            name = (
                item.get('GPJC')        # 股票简称
                or item.get('GPMC')     # 股票名称
                or item.get('ZWMC')     # 中文名称（期货/债券）
                or item.get('GPDM')     # 代码兜底
                or ''
            )
            ratio_str = item.get('JZBL') or '0'
            try:
                ratio = float(ratio_str)
            except ValueError:
                ratio = 0.0
            if name:
                result.append({
                    'code':  item.get('GPDM', ''),
                    'name':  name,
                    'ratio': round(ratio, 2),
                })

        # 按比例降序，取前10
        result.sort(key=lambda x: x['ratio'], reverse=True)
        return result[:10] if result else None

    except (ValueError, KeyError, json.JSONDecodeError) as e:
        print(f'    [holdings] {code}: parse error {e}', file=sys.stderr)
        return None


# ──────────────────────────────────────────────────────
#  主同步函数
# ──────────────────────────────────────────────────────

def sync():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data: dict = json.load(f)

    # 跳过 _meta 键
    fund_codes = [k for k in data if not k.startswith('_')]
    total = len(fund_codes)

    nav_ok, nav_kept, nav_fail = 0, 0, 0
    hold_ok, hold_fail = 0, 0

    print(f'=== 同步开始 | {total} 只基金 | {datetime.now(timezone.utc).isoformat(timespec="seconds")} UTC')
    print()

    for code in fund_codes:
        fund = data[code]
        name = fund.get('name', code)

        # ── 净值 ──
        nav_result = fetch_fundgz(code) or fetch_lsjz(code)
        if nav_result:
            # 只在新净值日期 ≥ 旧净值日期时更新（避免用估算值覆盖已有真实净值）
            old_date = fund.get('nav_date') or ''
            new_date = nav_result.get('nav_date') or ''
            if new_date >= old_date:
                fund.update(nav_result)
                nav_ok += 1
                flag = '✓'
            else:
                # 新值日期更旧（不应出现），保留旧值
                nav_kept += 1
                flag = '⟳'
        else:
            # 获取失败 → 保留已有 nav，仅打印警告
            nav_kept += 1
            nav_fail += 1
            flag = '✗'
            print(f'  {flag} NAV  {code} {name:16s}  FAILED — 保留旧值 {fund.get("nav")} ({fund.get("nav_date")})',
                  file=sys.stderr)

        if nav_result:
            print(f'  {flag} NAV  {code} {name:16s}  {nav_result["nav"]:.4f}  {nav_result["nav_date"]}  [{nav_result["nav_src"]}]')

        time.sleep(0.08)

        # ── 持仓 ──
        holdings = fetch_holdings(code)
        if holdings:
            fund['holdings'] = holdings
            hold_ok += 1
            top = holdings[0]
            print(f'    持仓 {len(holdings)}只  首位: {top["name"]} {top["ratio"]}%')
        else:
            hold_fail += 1
            print(f'    持仓 FAILED — 保留旧值', file=sys.stderr)

        time.sleep(0.08)

    # ── 写入 _meta ──
    now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
    data['_meta'] = {
        'sync_time':  now_utc,
        'nav_ok':     nav_ok,
        'nav_kept':   nav_kept,
        'hold_ok':    hold_ok,
        'total':      total,
    }

    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print()
    print(f'=== 完成 | NAV: {nav_ok} 更新 / {nav_kept} 保留旧值 / {nav_fail} 失败 | 持仓: {hold_ok}/{total}')
    print(f'    sync_time: {now_utc}')
    # 不再因部分失败而退出非零，Action 只需判断文件是否有更新


if __name__ == '__main__':
    sync()
