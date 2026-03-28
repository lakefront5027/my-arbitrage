#!/usr/bin/env python3
"""
LOF持仓PDF解析可行性验证脚本
流程：巨潮资讯查询最新季报URL → 下载PDF → pdfplumber提取持仓表格

用法：
  python test_pdf_holdings.py [基金代码]
  python test_pdf_holdings.py 161129
"""

import json
import re
import sys
import os
import ssl
import tempfile
import urllib.request
import urllib.parse
from datetime import datetime

# macOS 本地测试用：跳过证书验证（Actions 环境无此问题）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

try:
    import pdfplumber
except ImportError:
    print('请先安装: pip3 install pdfplumber')
    sys.exit(1)

CODE = sys.argv[1] if len(sys.argv) > 1 else '161129'


# ── CNINFO 查询 ──────────────────────────────────────────────────

def cninfo_search(code: str) -> list[dict]:
    """查询巨潮资讯，返回该基金最近的季报/年报公告列表"""
    # 深市基金代码以 0/1/2/3/5/6 开头，沪市以 5/6 开头
    # LOF 基金：15xxxx/16xxxx/16xxxx = 深市；501xxx = 沪市
    column = 'sse' if code.startswith(('5', '6')) else 'szse'

    url = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
    payload = urllib.parse.urlencode({
        'stock':      code,
        'category':   'category_jjgg_szsh',   # 基金公告（深市）
        'searchkey':  '季度报告',
        'pageNum':    1,
        'pageSize':   10,
        'column':     column,
        'tabName':    'fulltext',
        'sortName':   '',
        'sortType':   '',
        'isHLtitle':  'true',
    }).encode()

    headers = {
        'User-Agent':   'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer':      'https://www.cninfo.com.cn/',
        'Accept':       'application/json, text/plain, */*',
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            result = json.loads(r.read().decode('utf-8'))
        return result.get('announcements') or []
    except Exception as e:
        print(f'[CNINFO] 查询失败: {e}')
        return []


def find_latest_quarterly_pdf(code: str) -> tuple[str | None, str | None]:
    """返回 (pdf_url, report_title)，找不到返回 (None, None)"""
    anns = cninfo_search(code)
    if not anns:
        print(f'[CNINFO] 未找到 {code} 的公告记录')
        return None, None

    print(f'[CNINFO] 找到 {len(anns)} 条公告，筛选季报中...')
    for ann in anns:
        title = ann.get('announcementTitle', '')
        pdf_path = ann.get('adjunctUrl', '')
        ann_date = ann.get('announcementTime', '')
        print(f'  {ann_date}  {title}')
        # 匹配季度报告（不要年度报告，避免混淆）
        if re.search(r'[一二三四]季度?报告|季报', title) and pdf_path:
            url = f'https://static.cninfo.com.cn/{pdf_path}'
            print(f'\n→ 选中: {title}')
            print(f'  URL: {url}')
            return url, title

    print('[CNINFO] 未找到季度报告，尝试匹配所有报告...')
    for ann in anns:
        pdf_path = ann.get('adjunctUrl', '')
        if pdf_path and pdf_path.endswith('.pdf'):
            url = f'https://static.cninfo.com.cn/{pdf_path}'
            title = ann.get('announcementTitle', '')
            print(f'→ 备选: {title}\n  URL: {url}')
            return url, title

    return None, None


# ── PDF 下载 ──────────────────────────────────────────────────────

def download_pdf(url: str, dest: str) -> bool:
    """下载PDF到本地路径"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer':    'https://www.cninfo.com.cn/',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        print(f'[下载] {url}')
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r, open(dest, 'wb') as f:
            size = 0
            while chunk := r.read(65536):
                f.write(chunk)
                size += len(chunk)
        print(f'[下载] 完成，{size/1024:.0f} KB → {dest}')
        return True
    except Exception as e:
        print(f'[下载] 失败: {e}')
        return False


# ── pdfplumber 持仓提取 ────────────────────────────────────────────

# 匹配持仓百分比行的关键词（覆盖各种基金类型的表头变体）
HOLDINGS_SECTION_KEYWORDS = [
    '前十大基金', '基金持仓', '主要投资标的', '投资明细',
    '前十大权益', '前十大债券', '前十大股票', '重仓股',
    '持有的基金份额', 'ETF',
]

# 百分比数字模式
PCT_PATTERN = re.compile(r'(\d{1,3}(?:\.\d{1,4})?)\s*%?$')


def extract_holdings_from_pdf(pdf_path: str) -> list[dict]:
    """
    从季报PDF提取持仓，返回 [{name, pct, raw_pct_str}, ...]
    策略：
      1. 优先用 pdfplumber 的表格提取（最准确）
      2. 退而求其次用文本正则匹配
    """
    holdings = []
    with pdfplumber.open(pdf_path) as pdf:
        print(f'[PDF] 共 {len(pdf.pages)} 页')

        # ── 策略1：扫描所有表格 ──────────────────────────────
        print('[PDF] 策略1：扫描所有表格...')
        for page_num, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            for tbl in tables:
                if not tbl:
                    continue
                result = _parse_holdings_table(tbl, page_num)
                if result:
                    holdings.extend(result)

        if holdings:
            print(f'[PDF] 表格策略找到 {len(holdings)} 条持仓')
            return holdings

        # ── 策略2：文本正则 ──────────────────────────────────
        print('[PDF] 策略1无结果，尝试策略2：文本正则...')
        full_text = ''
        for page in pdf.pages:
            full_text += (page.extract_text() or '') + '\n'

        holdings = _parse_holdings_text(full_text)
        if holdings:
            print(f'[PDF] 文本策略找到 {len(holdings)} 条持仓')
        else:
            print('[PDF] 两种策略均未找到持仓数据')

    return holdings


def _parse_holdings_table(table: list, page_num: int) -> list[dict]:
    """尝试从一张表格中提取持仓，失败返回空列表"""
    if len(table) < 2:
        return []

    # 判断表头是否含持仓相关关键词
    header_text = ' '.join(str(c) for c in (table[0] or []) if c)
    is_holdings = any(kw in header_text for kw in HOLDINGS_SECTION_KEYWORDS)
    if not is_holdings:
        # 再检查前3行
        sample_text = ' '.join(
            str(c) for row in table[:3] for c in (row or []) if c
        )
        is_holdings = any(kw in sample_text for kw in HOLDINGS_SECTION_KEYWORDS)

    if not is_holdings:
        return []

    print(f'  [表格] 第{page_num}页发现疑似持仓表，行数={len(table)}，表头={header_text[:80]}')

    # 找百分比所在列（通常是最后一列或倒数第二列）
    results = []
    for row in table[1:]:
        if not row:
            continue
        cells = [str(c).strip() if c else '' for c in row]
        # 找最后一个含百分比数字的单元格
        pct = None
        pct_idx = -1
        for i in range(len(cells) - 1, -1, -1):
            m = PCT_PATTERN.search(cells[i])
            if m:
                val = float(m.group(1))
                if 0 < val < 100:
                    pct = val
                    pct_idx = i
                    break
        if pct is None:
            continue

        # 基金名：通常是第一个非序号的长文本列
        name = ''
        for i, c in enumerate(cells):
            if i == pct_idx:
                continue
            if len(c) > 3 and not c.isdigit():
                name = c
                break

        if name and pct:
            results.append({'name': name, 'pct': pct})

    return results


def _parse_holdings_text(text: str) -> list[dict]:
    """文本正则回退：匹配"基金名 ... xx.xx%"模式"""
    # 找到持仓部分
    section_start = -1
    for kw in HOLDINGS_SECTION_KEYWORDS:
        idx = text.find(kw)
        if idx != -1:
            section_start = idx
            break

    if section_start == -1:
        return []

    section = text[section_start:section_start + 3000]
    pattern = re.compile(
        r'([A-Za-z\u4e00-\u9fa5][A-Za-z0-9\u4e00-\u9fa5 &()（）\-\.]{5,80}?)'
        r'\s+[\d,]+\s+'     # 份额或金额
        r'[\d,]+\s+'        # 公允价值
        r'(\d{1,3}\.\d{1,4})',  # 占比%
    )
    results = []
    for m in pattern.finditer(section):
        name = m.group(1).strip()
        pct  = float(m.group(2))
        if 0 < pct < 100 and len(name) > 5:
            results.append({'name': name, 'pct': pct})

    return results


# ── 主流程 ────────────────────────────────────────────────────────

def main():
    print(f'=== 测试基金: {CODE} ===\n')

    # 1. 查询 PDF URL
    pdf_url, title = find_latest_quarterly_pdf(CODE)
    if not pdf_url:
        print('\n❌ 无法找到季报PDF，请检查基金代码或手动提供URL')
        print('   用法: python test_pdf_holdings.py 161129 [手动PDF路径]')
        sys.exit(1)

    # 2. 下载 PDF（或直接使用本地路径）
    if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
        pdf_path = sys.argv[2]
        print(f'[本地] 使用已有PDF: {pdf_path}')
    else:
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        pdf_path = tmp.name
        tmp.close()
        if not download_pdf(pdf_url, pdf_path):
            sys.exit(1)

    # 3. 提取持仓
    print(f'\n[解析] 开始解析PDF...')
    holdings = extract_holdings_from_pdf(pdf_path)

    # 4. 输出结果
    sep = '=' * 60
    print(f'\n{sep}')
    print(f'基金: {CODE}  季报: {title}')
    print(sep)
    if holdings:
        total_pct = sum(h['pct'] for h in holdings)
        print(f'找到 {len(holdings)} 条持仓（合计 {total_pct:.2f}%）:\n')
        for i, h in enumerate(holdings, 1):
            print(f'  {i:2d}. {h["name"][:50]:50s}  {h["pct"]:6.2f}%')
    else:
        print('❌ 未提取到持仓数据')
        print('\n提示：可以手动下载PDF后传入路径:')
        print(f'  python test_pdf_holdings.py {CODE} /path/to/report.pdf')

    # 清理临时文件
    if len(sys.argv) <= 2:
        os.unlink(pdf_path)

    print(f'\n临时PDF: {"已删除" if len(sys.argv) <= 2 else pdf_path}')


if __name__ == '__main__':
    main()
