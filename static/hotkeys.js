/* 页面切换快捷键（可自己录，两页共用） */
(function () {
  const STORE = "ff14_page_hotkeys_v1";
  const DEFAULTS = { stock: "alt+1", calc: "alt+2" };
  const PAGES = { stock: "/index.html", calc: "/calculator.html" };

  function load() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE) || "{}");
      return {
        stock: typeof raw.stock === "string" ? raw.stock : DEFAULTS.stock,
        calc: typeof raw.calc === "string" ? raw.calc : DEFAULTS.calc,
      };
    } catch (e) { return { ...DEFAULTS }; }
  }

  let cfg = load();
  let recording = null;

  function save() {
    try { localStorage.setItem(STORE, JSON.stringify(cfg)); } catch (e) { /* ignore */ }
  }

  function comboOf(e) {
    if (!(e.ctrlKey || e.altKey || e.metaKey)) return "";
    const mods = [];
    if (e.ctrlKey) mods.push("ctrl");
    if (e.altKey) mods.push("alt");
    if (e.metaKey) mods.push("meta");
    if (e.shiftKey) mods.push("shift");
    let key = "";
    const code = e.code || "";
    if (code.startsWith("Key")) key = code.slice(3).toLowerCase();
    else if (code.startsWith("Digit")) key = code.slice(5);
    else if (code === "Space") key = "space";
    else if (/^F\d+$/.test(code)) key = code.toLowerCase();
    else if (e.key && e.key.length === 1) key = e.key.toLowerCase();
    if (!key) return "";
    mods.sort();
    return mods.join("+") + "+" + key;
  }

  function label(combo) {
    if (!combo) return "未设置";
    const map = { ctrl: "Ctrl", alt: "Alt", meta: "Meta", shift: "Shift", space: "Space" };
    return combo.split("+").map((p) => map[p] || (p.length === 1 ? p.toUpperCase() : p)).join("+");
  }

  function go(page) {
    const url = PAGES[page];
    if (!url) return;
    if (page === "stock" && location.pathname === "/") return;
    if (page === "calc" && location.pathname.endsWith("calculator.html")) return;
    location.href = url;
  }

  document.addEventListener("keydown", (e) => {
    // 录制中：只处理这次按键
    if (recording) {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") { recording = null; refresh(); return; }
      const combo = comboOf(e);
      if (!combo) { recording = null; refresh(); return; }
      cfg[recording] = combo;
      save();
      recording = null;
      refresh();
      return;
    }
    const combo = comboOf(e);
    if (!combo) return;
    if (combo === cfg.stock) { e.preventDefault(); go("stock"); }
    else if (combo === cfg.calc) { e.preventDefault(); go("calc"); }
  }, true);

  let panel = null;
  let btn = null;

  function refresh() {
    if (!panel) return;
    Object.keys(cfg).forEach((key) => {
      const el = panel.querySelector('[data-combo="' + key + '"]');
      if (!el) return;
      el.textContent = recording === key ? "按下组合键…" : label(cfg[key]);
      el.classList.toggle("recording", recording === key);
    });
    if (btn) {
      btn.title = "自定义快捷键（库存 " + label(cfg.stock) + " / 算料 " + label(cfg.calc) + "）";
    }
  }

  function placePanel() {
    if (!btn || !panel) return;
    const r = btn.getBoundingClientRect();
    panel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 368)) + "px";
    panel.style.top = (r.bottom + 6) + "px";
  }

  function mount() {
    const nav = document.querySelector(".pagenav");
    if (!nav || document.querySelector(".hk-open")) return;

    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "hk-open";
    btn.textContent = "⌨";
    btn.setAttribute("aria-label", "自定义快捷键");
    nav.insertBefore(btn, nav.firstChild);

    panel = document.createElement("div");
    panel.className = "hk-panel";
    panel.hidden = true;
    panel.innerHTML =
      '<div class="hk-title">+ 快捷键（可自己录）</div>' +
      '<div class="hk-row"><span class="hk-name">库存</span>' +
        '<button type="button" class="hk-combo" data-combo="stock" data-record="stock"></button>' +
        '<button type="button" class="hk-record" data-record="stock">录制</button></div>' +
      '<div class="hk-row"><span class="hk-name">算料清单</span>' +
        '<button type="button" class="hk-combo" data-combo="calc" data-record="calc"></button>' +
        '<button type="button" class="hk-record" data-record="calc">录制</button></div>' +
      '<div class="hk-tip">录制时按下组合键即可（必须带 Ctrl / Alt / Meta），Esc 取消录制。</div>' +
      '<div class="hk-actions"><button type="button" data-reset>恢复默认</button></div>';
    const header = nav.closest("header") || nav.parentNode;
    header.parentNode.insertBefore(panel, header.nextSibling);

    btn.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) { refresh(); placePanel(); }
      else { recording = null; refresh(); }
    });

    panel.querySelectorAll("[data-record]").forEach((el) => {
      el.addEventListener("click", () => {
        recording = el.dataset.record;
        refresh();
      });
    });

    panel.querySelector("[data-reset]").addEventListener("click", () => {
      cfg = { ...DEFAULTS };
      recording = null;
      save();
      refresh();
    });

    document.addEventListener("click", (e) => {
      if (panel.hidden) return;
      if (panel.contains(e.target) || btn.contains(e.target)) return;
      panel.hidden = true;
      recording = null;
      refresh();
    });

    window.addEventListener("resize", () => {
      if (!panel.hidden) placePanel();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !panel.hidden && !recording) {
        panel.hidden = true;
      }
    });

    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
