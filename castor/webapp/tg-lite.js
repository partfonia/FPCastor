/* Лёгкая замена telegram-web-app.js: работает без загрузки скрипта с telegram.org.
   Если провайдер или VPN не пускает на telegram.org, официальный скрипт «висит» —
   и мини-апп показывает белый экран. Здесь только то, что нужно панели:
   initData, тема, кнопки «Назад» и «Сохранить», вибрация, подтверждение. */
(function () {
  "use strict";
  if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) return;

  // ── параметры, которые Telegram передаёт в адресе (#tgWebAppData=…) ──
  function parseHash() {
    var out = {}, h = (location.hash || "").replace(/^#/, "");
    h.split("&").forEach(function (kv) {
      if (!kv) return;
      var dec = function (x) { try { return decodeURIComponent(x.replace(/\+/g, "%20")); } catch (e) { return x; } };
      var i = kv.indexOf("="), k = dec(i < 0 ? kv : kv.slice(0, i));
      out[k] = i < 0 ? "" : dec(kv.slice(i + 1));
    });
    return out;
  }
  var p = parseHash(), store = {};
  try {
    if (p.tgWebAppData) sessionStorage.setItem("__tg_params", JSON.stringify(p));
    else store = JSON.parse(sessionStorage.getItem("__tg_params") || "{}");
  } catch (e) {}
  if (!p.tgWebAppData && store.tgWebAppData) p = store;

  var initData = p.tgWebAppData || "";
  var theme = {};
  try { theme = JSON.parse(p.tgWebAppThemeParams || "{}"); } catch (e) {}

  // ── отправка событий в Telegram (Android/iOS — TelegramWebviewProxy, Desktop/Web — postMessage) ──
  function post(type, data) {
    var json = JSON.stringify(data || {});
    try {
      if (window.TelegramWebviewProxy && window.TelegramWebviewProxy.postEvent) {
        window.TelegramWebviewProxy.postEvent(type, json); return;
      }
      if (window.external && "notify" in window.external) {
        window.external.notify(JSON.stringify({eventType: type, eventData: data || {}})); return;
      }
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(JSON.stringify({eventType: type, eventData: data || {}}), "*");
      }
    } catch (e) {}
  }

  // ── приём событий от Telegram ──
  var handlers = {};
  function on(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); }
  function off(ev, fn) { handlers[ev] = (handlers[ev] || []).filter(function (f) { return f !== fn; }); }
  function emit(ev, data) { (handlers[ev] || []).slice().forEach(function (f) { try { f(data); } catch (e) {} }); }
  function receive(ev, data) {
    if (ev === "back_button_pressed") emit("backButtonClicked");
    else if (ev === "main_button_pressed") emit("mainButtonClicked");
    else if (ev === "popup_closed") emit("popupClosed", data || {});
    else if (ev === "theme_changed" && data && data.theme_params) { theme = data.theme_params; applyTheme(); emit("themeChanged"); }
    else if (ev === "viewport_changed") emit("viewportChanged", data);
  }
  window.Telegram = window.Telegram || {};
  window.Telegram.WebView = {receiveEvent: receive, onEvent: on, offEvent: off, postEvent: post};
  window.addEventListener("message", function (e) {
    try { var m = typeof e.data === "string" ? JSON.parse(e.data) : e.data; if (m && m.eventType) receive(m.eventType, m.eventData); } catch (x) {}
  });

  function applyTheme() {
    var r = document.documentElement.style;
    Object.keys(theme).forEach(function (k) { r.setProperty("--tg-theme-" + k.replace(/_/g, "-"), theme[k]); });
    WebApp.themeParams = theme;
    WebApp.colorScheme = isDark(theme.bg_color) ? "dark" : "light";
  }
  function isDark(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || ""); if (!m) return false;
    var n = parseInt(m[1], 16); return ((n >> 16) * 299 + ((n >> 8) & 255) * 587 + (n & 255) * 114) / 1000 < 128;
  }

  // ── кнопки ──
  var back = {isVisible: false};
  back.show = function () { back.isVisible = true; post("web_app_setup_back_button", {is_visible: true}); return back; };
  back.hide = function () { back.isVisible = false; post("web_app_setup_back_button", {is_visible: false}); return back; };
  back.onClick = function (fn) { on("backButtonClicked", fn); return back; };
  back.offClick = function (fn) { off("backButtonClicked", fn); return back; };

  var main = {text: "Продолжить", isVisible: false, isActive: true, isProgressVisible: false,
              color: theme.button_color || "#2481cc", textColor: theme.button_text_color || "#ffffff"};
  function sendMain() {
    post("web_app_setup_main_button", {is_visible: main.isVisible, is_active: main.isActive,
      is_progress_visible: main.isProgressVisible, text: main.text, color: main.color, text_color: main.textColor});
  }
  main.setText = function (t) { main.text = String(t); sendMain(); return main; };
  main.show = function () { main.isVisible = true; sendMain(); return main; };
  main.hide = function () { main.isVisible = false; sendMain(); return main; };
  main.showProgress = function () { main.isProgressVisible = true; sendMain(); return main; };
  main.hideProgress = function () { main.isProgressVisible = false; sendMain(); return main; };
  main.onClick = function (fn) { on("mainButtonClicked", fn); return main; };
  main.offClick = function (fn) { off("mainButtonClicked", fn); return main; };

  var popupCb = null;
  on("popupClosed", function (d) { var cb = popupCb; popupCb = null; if (cb) cb(d.button_id || null); });

  var user = {};
  try { user = JSON.parse(new URLSearchParams(initData).get("user") || "{}"); } catch (e) {}

  var WebApp = {
    initData: initData,
    initDataUnsafe: {user: user},
    version: p.tgWebAppVersion || "6.0",
    platform: p.tgWebAppPlatform || "unknown",
    themeParams: theme, colorScheme: "light", isLite: true,
    BackButton: back, MainButton: main,
    HapticFeedback: {
      notificationOccurred: function (t) { post("web_app_trigger_haptic_feedback", {type: "notification", notification_type: t}); },
      impactOccurred: function (s) { post("web_app_trigger_haptic_feedback", {type: "impact", impact_style: s}); },
      selectionChanged: function () { post("web_app_trigger_haptic_feedback", {type: "selection_change"}); }
    },
    ready: function () { post("web_app_ready"); },
    setHeaderColor: function (c) { post("web_app_set_header_color", {color: c}); },
    setBackgroundColor: function (c) { post("web_app_set_background_color", {color: c}); },
    setBottomBarColor: function (c) { post("web_app_set_bottom_bar_color", {color: c}); },
    expand: function () { post("web_app_expand"); },
    close: function () { post("web_app_close"); },
    onEvent: on, offEvent: off,
    openLink: function (url) { post("web_app_open_link", {url: url}); },
    openTelegramLink: function (url) { post("web_app_open_tg_link", {path_full: url.replace(/^https:\/\/t\.me/, "")}); },
    showConfirm: function (message, cb) {
      if (!(window.TelegramWebviewProxy || (window.parent && window.parent !== window))) { cb && cb(window.confirm(message)); return; }
      popupCb = function (id) { cb && cb(id === "ok"); };
      post("web_app_open_popup", {message: String(message).slice(0, 256),
        buttons: [{id: "ok", type: "ok"}, {id: "cancel", type: "cancel"}]});
    }
  };
  applyTheme();
  window.Telegram.WebApp = WebApp;
})();
