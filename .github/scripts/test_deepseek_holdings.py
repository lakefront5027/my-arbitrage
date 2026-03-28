#!/usr/bin/env python3
"""
独立测试脚本：JJGG 找 PDF → 下载 → DeepSeek 解析持仓（详细调试版）
用法：DEEPSEEK_API_KEY=sk-xxx python test_deepseek_holdings.py [基金代码[,基金代码...]] [--no-jjgg]
默认测试基金：161129,501018,160644
"""
import os, sys, re, json, io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sync_fund_data import (
    _em_find_pdf_url,
    _download_pdf_bytes,
    _match_etf_name,
)

args      = [a for a in sys.argv[1:] if not a.startswith('-')]
flags     = [a for a in sys.argv[1:] if a.startswith('-')]
codes_arg = args[0] if args else '161129,501018,160644'
FUND_CODES = [c.strip() for c in codes_arg.split(',') if c.strip()]
NO_JJGG   = '--no-jjgg' in flags
API_KEY   = os.environ.get('DEEPSEEK_API_KEY', '')

if not API_KEY:
    print('❌ 需要设置环境变量 DEEPSEEK_API_KEY', file=sys.stderr)
    sys.exit(1)

try:
    from openai import OpenAI
    import pdfplumber
except ImportError as e:
    print(f'❌ 缺少依赖: {e}，请 pip install openai pdfplumber', file=sys.stderr)
    sys.exit(1)

client = OpenAI(api_key=API_KEY, base_url='https://api.deepseek.com')

EXIT_CODE = 0

for FUND_CODE in FUND_CODES:
    print(f'\n{"═"*60}')
    print(f'=== 基金 {FUND_CODE} ===')
    print('═'*60)

    # ── Step 1: 找 PDF ──
    if NO_JJGG:
        from sync_fund_data import _deepseek_find_pdf_url
        print('\n─── Step 1: DeepSeek 搜索 PDF URL ───')
        pdf_url = _deepseek_find_pdf_url(FUND_CODE, API_KEY)
        ann_id  = None
    else:
        print('\n─── Step 1: JJGG 查找季报 PDF ───')
        pdf_url, ann_id = _em_find_pdf_url(FUND_CODE)

    if not pdf_url:
        print('❌ 未找到 PDF URL，跳过')
        EXIT_CODE = 1
        continue
    print(f'✓ ann_id : {ann_id}')
    print(f'✓ pdf_url: {pdf_url}')

    # ── Step 2: 下载 PDF ──
    print('\n─── Step 2: 下载 PDF ───')
    pdf_bytes = _download_pdf_bytes(pdf_url)
    if not pdf_bytes:
        print('❌ 下载失败，跳过')
        EXIT_CODE = 1
        continue
    print(f'✓ 大小: {len(pdf_bytes)//1024} KB')

    # ── Step 3: pdfplumber 提取文本 ──
    print('\n─── Step 3: pdfplumber 提取文本 ───')
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        print(f'✓ 共 {len(pdf.pages)} 页，提取前 25 页')
        for page in pdf.pages[:25]:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    full_text = '\n'.join(text_parts).strip()
    print(f'✓ 提取文本 {len(full_text)} 字符')
    if not full_text:
        print('❌ 文本为空（可能是扫描版 PDF），跳过')
        EXIT_CODE = 1
        continue
    # 打印前 600 字符，帮助判断文本质量
    print('\n── 文本预览（前600字符）──')
    print(full_text[:600])
    print('...')

    # ── Step 4: DeepSeek 解析 ──
    print('\n─── Step 4: DeepSeek 解析持仓 ───')
    prompt = (
        '以下是一份基金季度报告的文本内容。请提取报告期末前十大持仓明细（股票、ETF 或基金）。\n'
        '以JSON格式返回，严格格式如下（不要 markdown 代码块）：\n'
        '{"holdings_date": "YYYY-MM-DD", '
        '"holdings": [{"name_en": "ETF英文名或空字符串", '
        '"name_zh": "ETF中文名或空字符串", "ratio": 占净值比例纯数字}]}\n'
        'ratio 为百分数纯数字（如 5.23，不带 % 符号）。\n'
        'holdings_date 为报告期末日期（如 2025-09-30）。\n'
        '只返回 JSON，不要其他内容。\n\n'
        f'报告文本：\n{full_text[:10000]}'
    )
    try:
        resp = client.chat.completions.create(
            model='deepseek-chat',
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=1500,
        )
        raw_text = (resp.choices[0].message.content or '').strip()
    except Exception as e:
        print(f'❌ DeepSeek API 调用失败: {e}')
        EXIT_CODE = 1
        continue

    print('\n── DeepSeek 原始响应 ──')
    print(raw_text)

    # ── Step 5: 解析 JSON + 显示映射结果 ──
    print('\n─── Step 5: 解析 + tq 映射 ───')
    clean = re.sub(r'^```(?:json)?\s*', '', raw_text)
    clean = re.sub(r'\s*```$', '', clean.strip())
    try:
        result = json.loads(clean)
    except Exception:
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        result = json.loads(m.group(0)) if m else None

    if not result:
        print('❌ JSON 解析失败')
        EXIT_CODE = 1
        continue

    raw_items     = result.get('holdings') or []
    holdings_date = result.get('holdings_date', '')
    print(f'✓ 持仓日期: {holdings_date}')
    print(f'✓ 持仓条数: {len(raw_items)}\n')

    matched, unmatched = [], []
    for i, h in enumerate(raw_items, 1):
        name_en = h.get('name_en', '')
        name_zh = h.get('name_zh', '')
        ratio   = h.get('ratio', 0)
        tq      = _match_etf_name(name_en) or _match_etf_name(name_zh)
        mark    = f'→ {tq}' if tq else '❓ 未匹配'
        print(f'  {i:2d}. [{float(ratio):5.2f}%]  {name_en or name_zh}')
        print(f'       {mark}')
        if tq:
            matched.append((tq, float(ratio)))
        else:
            unmatched.append((name_en, name_zh, float(ratio)))

    print(f'\n✓ 匹配成功: {len(matched)} 条')
    if unmatched:
        print(f'⚠️  未匹配: {len(unmatched)} 条（需补充 _ETF_NAME_TO_TQ）：')
        for en, zh, r in unmatched:
            print(f'   [{r:.2f}%] en="{en}"  zh="{zh}"')
    else:
        print('✓ 全部匹配')

sys.exit(EXIT_CODE)
