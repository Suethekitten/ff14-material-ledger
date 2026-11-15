/* 页面心跳：页面开着就每 3 秒 POST /api/page/ping。
   后端按 visible 分两档超时：前台 12 秒、后台（会被浏览器节流）90 秒。
   算料页、旧测试页共用。 */
(function () {
  var sid = "p" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  var timer = null;

  function ping() {
    try {
      fetch("/api/page/ping", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sid: sid,
          visible: document.visibilityState === "visible",
        }),
        keepalive: true,
      }).catch(function () {});
    } catch (e) { /* 忽略 */ }
  }

  function start() {
    if (timer) return;
    ping();
    timer = setInterval(ping, 3000);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") ping();
  });
  window.addEventListener("pageshow", start);
  window.addEventListener("pagehide", stop);
  start();
})();
