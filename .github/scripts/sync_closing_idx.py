#!/usr/bin/env python3
"""
LOF套利雷达 — 收盘指数快照同步
运行时间：北京时间 15:05（UTC 07:05），仅交易日

功能：
  抓取收盘后数据源停止提供实时数据的指数涨跌幅，写入 idx_closing.json。
  Worker 在非交易时段读取此文件作为 fallback，避免显示"指数缺失"。

扩展方法：
  在 CLOSING_IDX 字典中新增一行 { our_key: em_secid } 即可自动纳入采集与回填。

容错原则：
  - 单个指数抓取失败 → 保留上次成功值（不覆盖为 null）
  - 全部指数抓取失败 → 抛出 RuntimeError，Actions 红叉报警
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(os.path.dirname(SCRIPT_DIR))
OUT_PATH   = os.path.join(REPO_ROOT, 'data', 'idx_closing.json')

# ── 需要收盘快照的指数 ────────────────────────────────────
# key = Worker/BENCH 内使用的代码，value = 东方财富 push2 secid
# 新增收盘后失效的指数：在此加一行，Worker fallback 自动生效
CLOSING_IDX = {
    'sz399961': '0.399961',  # 中证资源与环境（161217 国投上游资源LOF）
    'sz399979': '0.399979',  # 中证大宗商品股票（161715 招商大宗商品LOF）
    'sinaAG0':  '113.AG0',   # 上期所白银主力（161226 国投白银LOF）
}


def fetch_url(url: str, timeout: int = 12, extra_headers: dict = None) -> str | None:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    if extra_headers:
        headers.update(extra_headers)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            # 尝试 UTF-8，失败则 GBK（新浪）
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw.decode('gbk', errors='replace')
    except Exception as e:
        print(f'[WARN] fetch failed {url}: {e}')
        return None


def fetch_em_chg(secid: str) -> float | None:
    """东方财富 push2 API：返回涨跌幅（百分比，如 -0.52）"""
    url = (f'https://push2.eastmoney.com/api/qt/stock/get'
           f'?secid={secid}&fields=f43,f170')
    raw = fetch_url(url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        f170 = (d.get('data') or {}).get('f170')
        if f170 is None:
            return None
        return round(f170 / 100, 4)
    except Exception as e:
        print(f'[WARN] parse EM {secid}: {e}')
        return None


def fetch_sina_ag0() -> float | None:
    """新浪财经：白银主力合约涨跌幅（EM 失败时备用）"""
    url = 'https://hq.sinajs.cn/list=nf_AG0'
    raw = fetch_url(url, extra_headers={'Referer': 'https://finance.sina.com.cn'})
    if not raw:
        return None
    m = re.search(r'hq_str_nf_AG0="([^"]+)"', raw)
    if not m:
        return None
    try:
        parts = m.group(1).split(',')
        # 字段: [0]=名称,[1]=时间,[2]=开,[3]=高,[4]=低,[5]=0,[6]=现价,[10]=昨结算
        cur  = float(parts[6])
        prev = float(parts[10])
        if cur > 0 and prev > 0:
            return round((cur - prev) / prev * 100, 4)
    except Exception as e:
        print(f'[WARN] parse Sina AG0: {e}')
    return None


def main():
    now_utc      = datetime.now(timezone.utc)
    trading_date = now_utc.strftime('%Y-%m-%d')
    sync_at      = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

    # 读取现有数据（失败时保留旧值）
    existing: dict = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding='utf-8') as f:
                existing = json.load(f)
        except Exception as e:
            print(f'[WARN] 读取现有 idx_closing.json 失败: {e}')

    results  = {k: v for k, v in existing.items() if not k.startswith('_')}
    failures = []

    for key, secid in CLOSING_IDX.items():
        chg = fetch_em_chg(secid)

        # sinaAG0：EM 失败时走 Sina 备用路径
        if chg is None and key == 'sinaAG0':
            print(f'[INFO] sinaAG0 EM 失败，尝试 Sina 备用')
            chg = fetch_sina_ag0()

        if chg is not None:
            results[key] = {
                'chg':     chg,
                'date':    trading_date,
                'sync_at': sync_at,
            }
            print(f'[OK]   {key}: {chg:+.4f}%')
        else:
            failures.append(key)
            old = existing.get(key)
            print(f'[WARN] {key}: 抓取失败，保留旧值 {old}')

    results['_meta'] = {
        'sync_at':      sync_at,
        'trading_date': trading_date,
        'failed':       failures,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f'[DONE] idx_closing.json 写入完成 | 成功 {len(CLOSING_IDX) - len(failures)} / {len(CLOSING_IDX)}')

    if len(failures) == len(CLOSING_IDX):
        raise RuntimeError('全部收盘指数抓取失败，请检查东方财富/新浪数据源')


if __name__ == '__main__':
    main()
