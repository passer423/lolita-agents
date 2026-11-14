/* 「清理工」Service Worker，不是离线缓存。
 *
 * 背景：8787 端口曾经跑过 lo-life，它的 Service Worker 用 cache-first 缓存了
 * /chat.js、/style.css。换成本应用后，服务端已是新代码，浏览器却一直拿旧缓存，
 * 于是页面还显示「我的偏好 / 关注项」这些已经删掉的面板。
 *
 * 这里只做一件事：接管一次 → 清掉本 origin 的所有缓存 → 注销自己 → 让受控页面
 * 重新加载。之后本应用不再注册任何 Service Worker，静态资源每次走网络。
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
