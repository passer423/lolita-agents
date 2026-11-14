/* 「清理工」Service Worker，不是离线缓存。
 *
 * 背景：8787 端口曾经跑过 lo-life，它的 Service Worker 用 cache-first 缓存了
 * /chat.js、/style.css。换成本应用后，服务端已是新代码，浏览器却一直拿旧缓存，
 * 于是页面还显示「我的偏好 / 关注项」这些已经删掉的面板。
 *
 * 这里只做一件事：接管一次 → 清掉本 origin 的所有缓存 → 注销自己 → 让受控页面
 * 重新加载。之后本应用不再注册任何 Service Worker，静态资源每次走网络。
 *
 * 谁注册它：index.html 首屏那段内联脚本。它先 `getRegistrations()`，只有发现
 * 同端口还留着旧注册时才注册本文件，并带 `?v=<资源版本>` 绕过旧 worker 对
 * sw.js 自身的缓存；本文件 activate 后立即注销，下一次整页导航就不再注册。
 *
 * 为什么选「自动清理」而不是「删掉本文件 + 文档写手工清理步骤」：
 * 受影响的是普通用户而不是开发者，他们不会去翻文档清站点数据；旧外壳会一直
 * 被 cache-first 返回。自动清理只多几行内联脚本，且清理完不常驻，没有长期
 * 维护成本。真正失效的旧缓存只有这个 origin 的 Cache Storage，清理范围明确。
 */

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((key) => caches.delete(key)));
    await self.registration.unregister();
    const clients = await self.clients.matchAll({ type: 'window' });
    for (const client of clients) {
      client.navigate(client.url).catch(() => {});
    }
  })());
});
