#!/usr/bin/env python3
"""
多数据源可达性测试：验证 GitHub Actions 环境能从哪些来源获取基金季报 PDF

测试对象：
  161129 (易方达原油 LOF，深市 sz)
  501018 (南方原油 LOF，沪市 sh)

每个来源独立测试，互不依赖。最终打印汇总表。
"""

import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ── 全局 SSL（跳过证书验证，兼容本地macOS与Actions Ubuntu）─────────────
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode   = ssl.CERT_NONE

RESULTS: list[dict] = []  # {source, code, status, detail}

# ─────────────────────────────────────────────────────────────────────────────

def _get(url, *, headers=None, timeout=15, method='GET', data=None) -> bytes | None:
    h = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept':     'application/json, text/html, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.read()
    except Exception as e:
        return None


def _post(url, payload: dict, *, headers=None, timeout=15) -> bytes | None:
    h = {
        'User-Agent':   'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer':      url,
        'Accept':       'application/json, */*',
    }
    if headers:
        h.update(headers)
    data = urllib.parse.urlencode(payload).encode()
    return _get(url, headers=h, timeout=timeout, method='POST', data=data)


def record(source: str, code: str, ok: bool, detail: str):
    icon = '✅' if ok else '❌'
    print(f'  {icon} [{source}] {code}: {detail}')
    RESULTS.append({'source': source, 'code': code, 'ok': ok, 'detail': detail})


def try_pdf_head(url: str) -> tuple[bool, str]:
    """HEAD 请求验证 PDF 可达（不下载全文）"""
    h = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer':    'https://fund.eastmoney.com/',
    }
    req = urllib.request.Request(url, headers=h, method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            ct   = r.headers.get('Content-Type', '')
            size = r.headers.get('Content-Length', '?')
            return True, f'HTTP {r.status}  {ct}  {size} bytes'
    except Exception as e:
        return False, str(e)[:80]

# ─────────────────────────────────────────────────────────────────────────────
# 1. 巨潮 cninfo（已知境外 IP 失败，但仍验证）
# ─────────────────────────────────────────────────────────────────────────────

def test_cninfo(code: str):
    src = 'cninfo(latest)'
    column = 'sse' if code.startswith(('5', '6')) else 'szse'
    raw = _post('https://www.cninfo.com.cn/new/hisAnnouncement/query', {
        'stock': code, 'category': 'category_jjgg_szsh', 'searchkey': '',
        'pageNum': 1, 'pageSize': 30, 'column': column,
        'tabName': 'latest', 'sortName': '', 'sortType': '', 'isHLtitle': 'true',
    }, headers={'Referer': 'https://www.cninfo.com.cn/'})

    if raw is None:
        record(src, code, False, '连接超时/拒绝')
        return
    try:
        data = json.loads(raw)
        anns = data.get('announcements') or []
        if not anns:
            record(src, code, False, f'返回空列表（服务器响应正常，区域限制）')
            return
        # 找季报
        for a in anns:
            title = a.get('announcementTitle', '')
            if re.search(r'[一二三四]季度?报告|季报', title):
                pdf  = a.get('adjunctUrl', '')
                url  = f'https://static.cninfo.com.cn/{pdf}' if pdf else ''
                record(src, code, True, f'找到：{title}  PDF={url[:60]}')
                return
        record(src, code, False, f'有{len(anns)}条公告但无季报，最新：{anns[0].get("announcementTitle","")[:40]}')
    except Exception as e:
        record(src, code, False, f'解析异常: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# 2. 东方财富 基金公告 API（np-anotice-fund）+ pdf.dfcfw.com
# ─────────────────────────────────────────────────────────────────────────────

def test_eastmoney_ann(code: str):
    src = 'eastmoney(ann)'
    url = (
        'https://np-anotice-fund.eastmoney.com/api/security/ann'
        f'?sr=-1&page=1&pageSize=30&ann_type=JJGG&client_source=web&stock_list={code}'
    )
    raw = _get(url, headers={
        'Referer': 'https://fund.eastmoney.com/',
        'Origin':  'https://fund.eastmoney.com',
    })
    if raw is None:
        record(src, code, False, '连接失败')
        return
    try:
        data = json.loads(raw)
        items = data.get('data', {}).get('list') or []
        if not items:
            record(src, code, False, f'返回空列表')
            return
        for item in items:
            title = item.get('TITLE', '') or item.get('title', '')
            if re.search(r'[一二三四]季度?报告|季报', title):
                # PDF URL 格式：https://pdf.dfcfw.com/pdf/H2_{art_code}_1.PDF
                art_code = item.get('ART_CODE') or item.get('art_code') or item.get('NOTICE_ID') or ''
                pdf_url  = f'https://pdf.dfcfw.com/pdf/H2_{art_code}_1.PDF' if art_code else ''
                record(src, code, True, f'找到：{title[:40]}  art_code={art_code}')
                if pdf_url:
                    ok, msg = try_pdf_head(pdf_url)
                    record('dfcfw PDF HEAD', code, ok, f'{pdf_url[-50:]}  →  {msg}')
                return
        # 没找到季报，打印最新标题
        latest = (items[0].get('TITLE') or items[0].get('title') or '')[:40]
        record(src, code, False, f'有{len(items)}条公告但无季报，最新：{latest}')
    except Exception as e:
        record(src, code, False, f'解析异常: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# 3. 天天基金 jjbg 报告 API（另一个 eastmoney 接口）
# ─────────────────────────────────────────────────────────────────────────────

def test_ttjj_jjbg(code: str):
    src = 'ttjj(jjbg)'
    # type=3 季报；token 是公开默认 token
    url = (
        f'https://api.fund.eastmoney.com/f10/jjbg'
        f'?fundCode={code}&page=1&per=20&type=3'
        f'&token=54dea4ef5b334571bb77b78065b55cc0'
    )
    raw = _get(url, headers={'Referer': 'https://fund.eastmoney.com/'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    try:
        text = raw.decode('utf-8', errors='replace')
        # strip JSONP wrapper if present
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            record(src, code, False, f'响应非JSON: {text[:80]}')
            return
        data  = json.loads(m.group(0))
        items = data.get('Data', {}).get('LSJJBGList') or []
        if not items:
            record(src, code, False, f'空列表，原始: {text[:100]}')
            return
        item = items[0]
        title   = item.get('BGNAME', '')
        pdf_url = item.get('PDFURL', '')
        record(src, code, True, f'{title}  PDF={pdf_url[:60]}')
        if pdf_url:
            ok, msg = try_pdf_head(pdf_url)
            record('ttjj PDF HEAD', code, ok, f'{pdf_url[-50:]}  →  {msg}')
    except Exception as e:
        record(src, code, False, f'解析异常: {e}  raw={raw[:100]}')


# ─────────────────────────────────────────────────────────────────────────────
# 4. 深交所 SZSE（仅深市基金，16xxxx/15xxxx）
# ─────────────────────────────────────────────────────────────────────────────

def test_szse(code: str):
    src = 'szse'
    if code.startswith(('5', '6')):
        record(src, code, None, '沪市基金，跳过 SZSE')
        return
    # 深交所基金信息披露 API
    url = (
        'https://www.szse.cn/api/report/show/fund/announcement/fund_annquery'
        f'?pageNum=1&pageSize=30&fundcode={code}&keyword=季度报告'
    )
    raw = _get(url, headers={
        'Referer': 'https://www.szse.cn/disclosure/fund/',
        'Origin':  'https://www.szse.cn',
    })
    if raw is None:
        record(src, code, False, '连接失败')
        return
    try:
        data  = json.loads(raw)
        items = (data.get('data') or {}).get('announcements') or data.get('announcements') or []
        if not items:
            record(src, code, False, f'空列表，原始: {raw[:120].decode()}')
            return
        item = items[0]
        title   = item.get('announcementTitle') or item.get('title') or ''
        adj_url = item.get('adjunctUrl') or item.get('pdfUrl') or ''
        pdf_url = f'https://disc.szse.cn/download{adj_url}' if adj_url else ''
        record(src, code, True, f'找到：{title[:40]}  pdf={pdf_url[:60]}')
        if pdf_url:
            ok, msg = try_pdf_head(pdf_url)
            record('szse PDF HEAD', code, ok, f'{pdf_url[-50:]}  →  {msg}')
    except Exception as e:
        record(src, code, False, f'解析异常: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# 5. 上交所 SSE（仅沪市基金，501xxx）
# ─────────────────────────────────────────────────────────────────────────────

def test_sse(code: str):
    src = 'sse'
    if not code.startswith(('5', '6')):
        record(src, code, None, '深市基金，跳过 SSE')
        return
    url = (
        'https://query.sse.com.cn/infodisclosure/pubinfo/fund/getSeasonalReportDetail.do'
        f'?jsonCallBack=cb&pageNo=1&pageSize=20&fundType=all&fundcode={code}'
    )
    raw = _get(url, headers={
        'Referer': 'https://www.sse.com.cn/',
        'Origin':  'https://www.sse.com.cn',
    })
    if raw is None:
        record(src, code, False, '连接失败')
        return
    try:
        text = raw.decode('utf-8', errors='replace')
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            record(src, code, False, f'响应非JSON: {text[:80]}')
            return
        data  = json.loads(m.group(0))
        items = data.get('pageHelp', {}).get('data') or data.get('result') or []
        if not items:
            record(src, code, False, f'空列表')
            return
        item = items[0]
        title   = item.get('FILE_DESC', '') or item.get('fileDesc', '')
        pdf_url = item.get('URL', '') or item.get('url', '')
        if pdf_url and not pdf_url.startswith('http'):
            pdf_url = 'https://www.sse.com.cn' + pdf_url
        record(src, code, True, f'找到：{title[:40]}  pdf={pdf_url[:60]}')
        if pdf_url:
            ok, msg = try_pdf_head(pdf_url)
            record('sse PDF HEAD', code, ok, f'{pdf_url[-50:]}  →  {msg}')
    except Exception as e:
        record(src, code, False, f'解析异常: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# 6. 新浪财经（HTML 抓取，解析 PDF 链接）
# ─────────────────────────────────────────────────────────────────────────────

def test_sina(code: str):
    src = 'sina'
    # 新浪基金公告页
    url = f'https://vip.stock.finance.sina.com.cn/fund_center/data/jsonp.php/IO.XSRV2.CallbackList[\'annual\'].annual/Fund_PublicNotice_Ann_Get?page=1&num=20&fo_type=&symbol={code}'
    raw = _get(url, headers={'Referer': 'https://finance.sina.com.cn/'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    try:
        text = raw.decode('utf-8', errors='replace')
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            record(src, code, False, f'响应非数组: {text[:80]}')
            return
        items = json.loads(m.group(0))
        if not items:
            record(src, code, False, '空列表')
            return
        for item in items:
            title   = item.get('title', '')
            pdf_url = item.get('url', '')
            if re.search(r'季度?报告|季报', title):
                record(src, code, True, f'找到：{title[:40]}  pdf={pdf_url[:60]}')
                if pdf_url:
                    ok, msg = try_pdf_head(pdf_url)
                    record('sina PDF HEAD', code, ok, f'→  {msg}')
                return
        record(src, code, False, f'有{len(items)}条但无季报')
    except Exception as e:
        record(src, code, False, f'解析异常: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def main():
    codes = sys.argv[1:] if len(sys.argv) > 1 else ['161129', '501018']
    print(f'测试时间: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}')
    print(f'测试基金: {codes}\n')

    for code in codes:
        print(f'── {code} ───────────────────────────────────────────')
        test_cninfo(code)
        test_eastmoney_ann(code)
        test_ttjj_jjbg(code)
        test_szse(code)
        test_sse(code)
        test_sina(code)
        print()
        time.sleep(1)

    # ── 汇总表 ─────────────────────────────────────────────────────────
    print('=' * 60)
    print('汇总')
    print('=' * 60)
    sources = {}
    for r in RESULTS:
        if r['ok'] is None:
            continue  # 跳过（不适用）
        k = r['source']
        sources.setdefault(k, {'ok': 0, 'fail': 0})
        if r['ok']:
            sources[k]['ok'] += 1
        else:
            sources[k]['fail'] += 1
    for src, cnt in sources.items():
        total = cnt['ok'] + cnt['fail']
        icon  = '✅' if cnt['ok'] == total else ('⚠️' if cnt['ok'] > 0 else '❌')
        print(f'  {icon} {src}: {cnt["ok"]}/{total} 成功')

    any_ok = any(r['ok'] for r in RESULTS if r['ok'] is not None)
    sys.exit(0 if any_ok else 1)


if __name__ == '__main__':
    main()
