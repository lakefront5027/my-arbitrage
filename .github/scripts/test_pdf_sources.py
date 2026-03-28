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


# ── 2. 东方财富 JJGG API（定期报告，与 lsjz 同域，已验证可从境外访问） ─────────

def test_eastmoney_jjgg(code: str):
    """
    api.fund.eastmoney.com/f10/JJGG — 基金定期报告列表（type=3）
    与 lsjz/jjcc 同域，JSONP 格式，境外 IP 可访问。
    PDF URL 规则：http://pdf.dfcfw.com/pdf/H2_{AN_ID}_1.pdf
    """
    src = 'eastmoney/JJGG'
    url = (f'https://api.fund.eastmoney.com/f10/JJGG'
           f'?callback=jQuery&fundcode={code}&pageIndex=1&pageSize=10&type=3&_=1')
    raw = _fetch(url, headers={'Referer': 'https://fundf10.eastmoney.com/'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    text = raw.decode('utf-8', errors='replace')
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        record(src, code, False, f'非 JSON: {text[:80]}')
        return
    d = json.loads(m.group(0))
    if d.get('ErrCode') != 0:
        record(src, code, False, f'ErrCode={d.get("ErrCode")}: {d.get("ErrMsg")}')
        return
    items = d.get('Data') or []
    for item in items:
        title  = item.get('TITLE', '')
        ann_id = item.get('ID', '')
        date_s = item.get('PUBLISHDATEDesc', '')[:10]
        if re.search(r'[一二三四1-4]季度报告|季报', title) and ann_id:
            pdf_url = f'http://pdf.dfcfw.com/pdf/H2_{ann_id}_1.pdf'
            record(src, code, True, f'{date_s} {title[:40]}')
            ok, msg = _head_ok(pdf_url)
            record('dfcfw/PDF', code, ok, f'{pdf_url[-50:]}  {msg}')
            return
    if items:
        first = items[0]
        record(src, code, None,
               f'共{len(items)}条，首条:{first.get("TITLE","")[:40]}，无季报')
    else:
        record(src, code, False, '定期报告列表为空')


# ── 3. fundf10 旧接口（保留为对照组，实际返回 2022 年前旧数据） ───────────────

def test_fundf10_html(code: str):
    src = 'fundf10/HTML(对照组)'
    # jjgg_{code}_3.html = 定期报告标签页，但数据由 Angular AJAX 加载，静态 HTML 无实际 ID
    url = f'https://fundf10.eastmoney.com/jjgg_{code}_3.html'
    raw = _fetch(url, headers={'Referer': 'https://fund.eastmoney.com/'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    html = raw.decode('utf-8', errors='replace')
    if 'pdf.dfcfw' in html:
        record(src, code, None, '页面可达，但为 Angular 模板，无真实 ID（AJAX 加载）')
    elif '<html' in html.lower():
        record(src, code, False, f'页面可达，无内容（{len(html)} bytes）')
    else:
        record(src, code, False, f'响应非 HTML: {html[:80]}')


# ── 4. fundf10 F10DataApi（旧接口，仅返回 2022 年前数据，保留为对照组） ────────

def test_fundf10_api(code: str):
    src = 'fundf10/F10DataApi(对照组)'
    url = (f'https://fundf10.eastmoney.com/F10DataApi.aspx'
           f'?type=jjgg&code={code}&page=1&per=5&sort=date%20desc')
    raw = _fetch(url, headers={'Referer': f'https://fundf10.eastmoney.com/jjgg_{code}.html'})
    if raw is None:
        record(src, code, False, '连接失败')
        return
    text = raw.decode('utf-8', errors='replace')
    dates = re.findall(r'<td>(\d{4}-\d{2}-\d{2})</td>', text)
    meta = re.search(r'records:(\d+)', text)
    total = meta.group(1) if meta else '?'
    if dates:
        record(src, code, None, f'可达，共{total}条，最新日期={dates[0]}（数据截至2022，非正式接口）')
    else:
        record(src, code, None if len(text) > 50 else False,
               f'可达但无日期，{len(text)} bytes')


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
        test_eastmoney_jjgg(code)    # ★ 主力接口：同域已验证，返回最新数据
        test_fundf10_html(code)      # 对照组：Angular 模板，无实际数据
        test_fundf10_api(code)       # 对照组：旧接口，数据截至 2022
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
