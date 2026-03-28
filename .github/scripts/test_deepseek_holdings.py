#!/usr/bin/env python3
"""
独立测试脚本：JJGG 找 PDF → 下载 → DeepSeek 解析持仓
用法：DEEPSEEK_API_KEY=sk-xxx python test_deepseek_holdings.py [基金代码]
默认测试基金：161129（南方全球精选，美股+港股混合）
"""
import os, sys, re, json, urllib.request
from pathlib import Path

# ── 允许直接 import sync_fund_data 里的函数 ──
sys.path.insert(0, str(Path(__file__).parent))
from sync_fund_data import (
    _em_find_pdf_url,
    _download_pdf_bytes,
    _extract_holdings_from_pdf_deepseek,
    _match_etf_name,
)

args      = [a for a in sys.argv[1:] if not a.startswith('-')]
flags     = [a for a in sys.argv[1:] if a.startswith('-')]
FUND_CODE = args[0] if args else '161129'
NO_JJGG   = '--no-jjgg' in flags   # 强制走 DeepSeek 搜索路径（跳过 JJGG）
API_KEY   = os.environ.get('DEEPSEEK_API_KEY', '')

if not API_KEY:
    print('❌ 需要设置环境变量 DEEPSEEK_API_KEY', file=sys.stderr)
    sys.exit(1)

print(f'=== 测试基金 {FUND_CODE}  模式: {"DeepSeek 搜索" if NO_JJGG else "JJGG + DeepSeek 解析"} ===\n')

# Step 1: 找 PDF URL
if NO_JJGG:
    print('─── Step 1: DeepSeek 搜索 PDF URL（跳过 JJGG）───')
    from sync_fund_data import _deepseek_find_pdf_url
    try:
        from openai import OpenAI
        _ds_client_pre = OpenAI(api_key=API_KEY, base_url='https://api.deepseek.com')
    except ImportError:
        print('❌ openai 包未安装', file=sys.stderr); sys.exit(1)
    pdf_url = _deepseek_find_pdf_url(FUND_CODE, API_KEY)
    ann_id  = None
    if not pdf_url:
        print('❌ DeepSeek 未找到 PDF URL，退出')
        sys.exit(1)
    print(f'✓ pdf_url: {pdf_url}\n')
else:
    print('─── Step 1: JJGG 查找最新季报 PDF ───')
    pdf_url, ann_id = _em_find_pdf_url(FUND_CODE)
    if not pdf_url:
        print('❌ JJGG 未找到 PDF URL，退出')
        sys.exit(1)
    print(f'✓ ann_id: {ann_id}')
    print(f'✓ pdf_url: {pdf_url}\n')

# Step 2: 下载 PDF
print('─── Step 2: 下载 PDF ───')
pdf_bytes = _download_pdf_bytes(pdf_url)
if not pdf_bytes:
    print('❌ PDF 下载失败，退出')
    sys.exit(1)
print(f'✓ 下载完成，大小: {len(pdf_bytes)//1024} KB\n')

# Step 3: DeepSeek 解析
print('─── Step 3: DeepSeek 解析持仓 ───')
try:
    from openai import OpenAI
    client = OpenAI(api_key=API_KEY, base_url='https://api.deepseek.com')
except ImportError:
    print('❌ openai 包未安装，请 pip install openai', file=sys.stderr)
    sys.exit(1)

holdings, holdings_date = _extract_holdings_from_pdf_deepseek(pdf_bytes, FUND_CODE, client)

if not holdings:
    print('❌ DeepSeek 未返回持仓数据')
    sys.exit(1)

print(f'\n✓ 持仓日期: {holdings_date}')
print(f'✓ 持仓数量: {len(holdings)} 条\n')
print('── 原始持仓 ──')
for i, h in enumerate(holdings, 1):
    name_en  = h.get('name_en', '')
    name_zh  = h.get('name_zh', '')
    ratio    = h.get('ratio', 0)
    tq_code  = _match_etf_name(name_en) or _match_etf_name(name_zh) or '❓ 未匹配'
    print(f'  {i:2d}. [{ratio:5.2f}%] {name_en or name_zh}')
    print(f'       tq映射: {tq_code}')

# 统计未匹配
unmatched = [h for h in holdings
             if not _match_etf_name(h.get('name_en',''))
             and not _match_etf_name(h.get('name_zh',''))]
if unmatched:
    print(f'\n⚠️  {len(unmatched)} 条未匹配到 tq 代码，可能需要更新 _ETF_NAME_TO_TQ：')
    for h in unmatched:
        print(f'   - {h.get("name_en","")} / {h.get("name_zh","")}')
else:
    print('\n✓ 所有持仓均已匹配到 tq 代码')
