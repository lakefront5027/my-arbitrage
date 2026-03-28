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
from datetime import datetime, timezone

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode   = ssl.CERT_NONE

RESULTS: list[dict] = []

# ── 底层 HTTP ─────────────────────────────────────────────────────────────────

def _fetch(url, *, method='GET', data=None, headers=None, timeout=15) -> bytes | None:
    h = {
        'User-Agent':    'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept':        'application/json, text/html, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection':    'keep-alive',
    }
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h, method=method, data=data)
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.read()
    except Exception as e:
        return None


def _post(url, payload: dict, *, referer='', timeout=15) -> bytes | None:
    data = urllib.parse.urlencode(payload).encode()
    return _fetch(url, method='POST', data=data, timeout=timeout, headers={
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Referer': referer or url,
    })


def _head_ok(url, referer='') -> tuple[bool, str]:
    """HEAD 请求检测 URL 可达性（不下载正文）"""
    h = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer':    referer or 'https://fund.eastmoney.com/',
    }
    try:
        req = urllib.request.Request(url, headers=h, method='HEAD')
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            ct   = r.headers.get('Content-Type', '')
            size = r.headers.get('Content-Length', '?')
            return True, f'HTTP {r.status}  {ct}  size={size}'
    except Exception as e:
        return False, str(e)[:100]


def record(source: str, code: str, ok, detail: str):
    icon = '✅' if ok is True else ('⚠️' if ok is None else '❌')
    print(f'  {icon} [{source}] {code}: {detail}')
    RESULTS.append({'source': source, 'code': code, 'ok': ok, 'detail': detail})


# ── 0. 基线：验证 api.fund.eastmoney.com 可达（lsjz 已知工作） ───────────────

def test_baseline_lsjz(code: str):
    """复用 sync_fund_data.py 已验证的 lsjz 接口，确认 api.fund.eastmoney.com 可达"""
    src = 'eastmoney/lsjz(基线)'
    url = (f'https://api.fund.eastmoney.com/f10/lsjz'
           f'?callback=jQuery&fundCode={code}&pageIndex=1&pageSize=3&_=1')
    raw = _fetch(url, headers={'Referer': 'https://fund.eastmoney.com/'})
    if raw is None:
        record(src, code, False, '连接失败——api.fund.eastmoney.com 不可达！')
        return False
    text = raw.decode('utf-8', errors='replace')
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        d = json.loads(m.group(0))
        if d.get('ErrCode') == 0:
            record(src, code, True, f'OK，api.fund.eastmoney.com 可达')
            return True
    record(src, code, None, f'响应异常: {text[:80]}')
    return False


# ── 1. 巨潮 cninfo（境外 IP 软封对照组） ─────────────────────────────────────

def test_cninfo(code: str):
    src = 'cninfo'
    column = 'sse' if code.startswith(('5', '6')) else 'szse'
    raw = _post('https://www.cninfo.com.cn/new/hisAnnouncement/query', {
        'stock': code, 'category': 'category_jjgg_szsh', 'searchkey': '',
        'pageNum': 1, 'pageSize': 30, 'column': column,
        'tabName': 'latest', 'sortName': '', 'sortType': '', 'isHLtitle': 'true',
    }, referer='https://www.cninfo.com.cn/')
    if raw is None:
        record(src, code, False, '连接超时/拒绝')
        return
    anns = json.loads(raw).get('announcements') or []
    if not anns:
        record(src, code, False, '返回空列表（区域限制，仅境内 IP 有数据）')
        return
    for a in anns:
        t = a.get('announcementTitle', '')
        if re.search(r'[一二三四]季度?报告|季报', t):
            pdf = f"https://static.cninfo.com.cn/{a.get('adjunctUrl','')}"
            record(src, code, True, f'{t[:40]}  {pdf[:60]}')
            return
    record(src, code, False, f'有{len(anns)}条公告但无季报')


# ── 2. 东方财富 jjbg API（修正参数：JSONP 格式，无 token，同 lsjz） ──────────

def test_eastmoney_jjbg(code: str):
    src = 'eastmoney/jjbg'
    # type: 1=年报 2=半年报 3=季报 4=季报(另一写法)；先试 type=3
    for type_val in ('3', '0', ''):
        suffix = f'&type={type_val}' if type_val else ''
        url = (f'https://api.fund.eastmoney.com/f10/jjbg'
               f'?callback=jQuery&fundCode={code}&pageIndex=1&pageSize=20{suffix}&_=1')
        raw = _fetch(url, headers={'Referer': 'https://fund.eastmoney.com/'})
        if raw is None:
            continue
        text = raw.decode('utf-8', errors='replace')
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            continue
        d = json.loads(m.group(0))
        if d.get('ErrCode') != 0 or d.get('Data') is None:
            continue
        items = d['Data'].get('LSJJBGList') or []
        if not items:
            continue
        for item in items:
            title   = item.get('BGNAME', '')
            pdf_url = item.get('PDFURL', '')
            if re.search(r'季度?报告|季报', title):
                record(src, code, True, f'type={type_val} {title[:40]}  {pdf_url[:60]}')
                if pdf_url:
                    ok, msg = _head_ok(pdf_url)
                    record('dfcfw/PDF', code, ok, msg)
                return
        # 找到列表但无季报
        first = items[0].get('BGNAME', '')[:40]
        record(src, code, None, f'type={type_val} 有{len(items)}条但无季报，首条:{first}')
        return
    record(src, code, False, '所有 type 值均返回空/错误')


# ── 3. fundf10.eastmoney.com HTML 页面（季报列表） ────────────────────────────

def test_fundf10_html(code: str):
    src = 'fundf10/HTML'
    # fundf10 基金公告页，page=1 季报，ann_type=JJGG
    url = f'https://fundf10.eastmoney.com/jjgg_{code}_1.html'
    raw = _fetch(url, headers={'Referer': 'https://fund.eastmoney.com/'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    html = raw.decode('utf-8', errors='replace')
    # 提取 PDF 链接
    pdfs = re.findall(r'href="(https://pdf\.dfcfw\.com/[^"]+\.PDF)"', html, re.IGNORECASE)
    # 提取标题
    titles = re.findall(r'<a[^>]*title="([^"]*季[^"]*报[^"]*)"', html)
    if pdfs:
        record(src, code, True, f'找到 {len(pdfs)} 个 PDF，首个: {pdfs[0][-50:]}')
        ok, msg = _head_ok(pdfs[0])
        record('dfcfw/PDF', code, ok, msg)
    elif 'class="gdwt"' in html or 'pdf.dfcfw' in html:
        record(src, code, None, f'页面可达但未匹配 PDF 链接，titles={titles[:2]}')
    elif '<html' in html.lower():
        record(src, code, False, f'页面可达但无季报内容（{len(html)} bytes）')
    else:
        record(src, code, False, f'响应非 HTML: {html[:80]}')


# ── 4. fundf10 F10DataApi（JSON 接口） ───────────────────────────────────────

def test_fundf10_api(code: str):
    src = 'fundf10/F10DataApi'
    url = (f'https://fundf10.eastmoney.com/F10DataApi.aspx'
           f'?type=jjgg&code={code}&page=1&per=10&sort=date%20desc')
    raw = _fetch(url, headers={'Referer': f'https://fundf10.eastmoney.com/jjgg_{code}.html'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    text = raw.decode('utf-8', errors='replace')
    # 响应可能是 HTML 或 JSON
    pdfs = re.findall(r'href="(https://pdf\.dfcfw\.com/[^"]+\.PDF)"', text, re.IGNORECASE)
    if pdfs:
        record(src, code, True, f'找到 PDF: {pdfs[0][-50:]}')
        ok, msg = _head_ok(pdfs[0])
        record('dfcfw/PDF', code, ok, msg)
        return
    record(src, code, None if '<' in text else False,
           f'可达但无 PDF 链接，{len(text)} bytes: {text[:80]}')


# ── 5. 深交所 SZSE（深市基金） ────────────────────────────────────────────────

def test_szse(code: str):
    src = 'szse'
    if code.startswith(('5', '6')):
        record(src, code, None, '沪市基金，跳过')
        return
    url = (f'https://www.szse.cn/api/report/show/fund/announcement/fund_annquery'
           f'?pageNum=1&pageSize=30&fundcode={code}&keyword=季度报告')
    raw = _fetch(url, headers={'Referer': 'https://www.szse.cn/'})
    if raw is None:
        record(src, code, False, '连接失败（境外 IP 被拒）')
        return
    try:
        data  = json.loads(raw)
        items = (data.get('data') or {}).get('announcements') or []
        if not items:
            record(src, code, False, f'空列表: {raw[:80].decode()}')
            return
        t   = items[0].get('announcementTitle', '')
        adj = items[0].get('adjunctUrl', '')
        pdf = f'https://disc.szse.cn/download{adj}' if adj else ''
        record(src, code, True, f'{t[:40]}  {pdf[:50]}')
        if pdf:
            ok, msg = _head_ok(pdf, referer='https://www.szse.cn/')
            record('szse/PDF', code, ok, msg)
    except Exception as e:
        record(src, code, False, f'解析异常: {e}')


# ── 6. 上交所 SSE（沪市基金） ─────────────────────────────────────────────────

def test_sse(code: str):
    src = 'sse'
    if not code.startswith(('5', '6')):
        record(src, code, None, '深市基金，跳过')
        return
    url = (f'https://query.sse.com.cn/infodisclosure/pubinfo/fund/'
           f'getSeasonalReportDetail.do?jsonCallBack=cb&pageNo=1&pageSize=20'
           f'&fundType=all&fundcode={code}')
    raw = _fetch(url, headers={
        'Referer': 'https://www.sse.com.cn/',
        'Origin':  'https://www.sse.com.cn',
    })
    if raw is None:
        record(src, code, False, '连接失败')
        return
    text = raw.decode('utf-8', errors='replace')
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        record(src, code, False, f'非 JSON: {text[:80]}')
        return
    d     = json.loads(m.group(0))
    items = d.get('pageHelp', {}).get('data') or d.get('result') or []
    if not items:
        record(src, code, False, '空列表')
        return
    t   = items[0].get('FILE_DESC', '') or items[0].get('fileDesc', '')
    pdf = items[0].get('URL', '') or items[0].get('url', '')
    if pdf and not pdf.startswith('http'):
        pdf = 'https://www.sse.com.cn' + pdf
    record(src, code, True, f'{t[:40]}  {pdf[:50]}')
    if pdf:
        ok, msg = _head_ok(pdf, referer='https://www.sse.com.cn/')
        record('sse/PDF', code, ok, msg)


# ── 7. 新浪财经 基金公告 ──────────────────────────────────────────────────────

def test_sina(code: str):
    src = 'sina'
    # 新浪基金公告接口（非 JSONP，直接 JSON）
    url = (f'https://finance.sina.com.cn/fund/quotes/{code}/bc.shtml')
    raw = _fetch(url, headers={'Referer': 'https://finance.sina.com.cn/'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    html = raw.decode('utf-8', errors='replace')
    # 找 PDF 链接
    pdfs = re.findall(r'(https?://[^\s"\'<>]+\.pdf)', html, re.IGNORECASE)
    if pdfs:
        record(src, code, True, f'找到 PDF: {pdfs[0][:60]}')
        return
    if '季度报告' in html or '季报' in html:
        record(src, code, None, f'页面可达，含季报文字但未提取到 PDF 链接')
    elif '<html' in html.lower():
        record(src, code, None, f'页面可达，无季报内容（{len(html)} bytes）')
    else:
        record(src, code, False, f'非 HTML 响应: {html[:80]}')


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    codes = sys.argv[1:] if len(sys.argv) > 1 else ['161129', '501018']
    print(f'测试时间: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    print(f'测试基金: {codes}\n')

    for code in codes:
        print(f'── {code} ─────────────────────────────────────')
        reachable = test_baseline_lsjz(code)  # 确认 api.fund.eastmoney.com 可达
        test_cninfo(code)
        test_eastmoney_jjbg(code)
        test_fundf10_html(code)
        test_fundf10_api(code)
        test_szse(code)
        test_sse(code)
        test_sina(code)
        print()
        time.sleep(1)

    print('=' * 60)
    print('汇总')
    print('=' * 60)
    by_src: dict[str, dict] = {}
    for r in RESULTS:
        if r['ok'] is None:
            continue
        k = r['source']
        by_src.setdefault(k, {'ok': 0, 'fail': 0})
        by_src[k]['ok' if r['ok'] else 'fail'] += 1
    for src, cnt in by_src.items():
        total = cnt['ok'] + cnt['fail']
        icon  = '✅' if cnt['ok'] == total else ('⚠️' if cnt['ok'] > 0 else '❌')
        print(f'  {icon} {src}: {cnt["ok"]}/{total} 成功')

    sys.exit(0 if any(r['ok'] is True for r in RESULTS) else 1)


if __name__ == '__main__':
    main()
