// Cloudflare Workers — 新浪财经行情代理
// 用途：为 lof_arb_monitor 提供 hq.sinajs.cn 跨域访问
// 部署后将 HTML 里的 SINA_PROXY_URL 替换为本 Worker 地址
//
// 请求格式：GET https://your-worker.workers.dev/?list=nf_AG0
// 响应：UTF-8 JSON，含 CORS 头

export default {
  async fetch(request) {
    // 只允许 GET
    if (request.method !== 'GET') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    const url = new URL(request.url);
    const list = url.searchParams.get('list');
    if (!list) {
      return new Response('Missing ?list= parameter', { status: 400 });
    }

    // 转发到新浪，带 Referer 绕过其来源校验
    const sinaUrl = `https://hq.sinajs.cn/list=${list}`;
    let sinaRes;
    try {
      sinaRes = await fetch(sinaUrl, {
        headers: {
          'Referer': 'https://finance.sina.com.cn',
          'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        },
      });
    } catch (e) {
      return new Response(`Upstream fetch failed: ${e.message}`, { status: 502 });
    }

    // 新浪返回 GBK，TextDecoder 解码后以 UTF-8 返回
    const buf = await sinaRes.arrayBuffer();
    const text = new TextDecoder('gbk').decode(buf);

    return new Response(text, {
      status: sinaRes.status,
      headers: {
        'Content-Type': 'text/javascript; charset=utf-8',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'no-cache, no-store',
      },
    });
  },
};
