/* ============================================================
   privacyvpn — progressive enhancement only.
   The site is 100% functional with this file absent
   (Tor "Safest", .onion). Nothing here is required.
   No external resources. CSP-friendly (no inline eval).
   ============================================================ */
(function (global) {
  "use strict";

  /* ---- 1. Theme toggle (respects prefers-color-scheme by default) ---- */
  var THEME_KEY = "tt-theme";
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }
  try { var saved = localStorage.getItem(THEME_KEY); if (saved) applyTheme(saved); } catch (e) {}

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-theme-toggle]");
    if (!t) return;
    var cur = document.documentElement.getAttribute("data-theme");
    var sysDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var next = (cur || (sysDark ? "dark" : "light")) === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e2) {}
    t.setAttribute("aria-pressed", next === "dark");
  });

  /* ---- 2. Copy-to-clipboard ---- */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var sel = btn.getAttribute("data-copy");
    var src = sel ? document.querySelector(sel) : null;
    var text = src ? (src.value !== undefined && src.value !== "" ? src.value : src.textContent) : btn.getAttribute("data-copy-text");
    if (!text) return;
    var done = function () {
      var old = btn.getAttribute("data-label") || btn.textContent;
      btn.setAttribute("data-label", old);
      // The localized label comes from data-copied. The fallback must stay language-neutral:
      // this file is served to both locales, so any literal here is wrong for one of them.
      btn.textContent = btn.getAttribute("data-copied") || "✓";
      setTimeout(function () { btn.textContent = old; }, 1600);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(text.trim()).then(done, fallback);
    else fallback();
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text.trim(); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e3) {}
      document.body.removeChild(ta);
    }
  });

  /* ---- 3. WireGuard keygen (real X25519) entirely in the browser ----
     Uses WG.generateKeypair() from wg-keys.js (public-domain TweetNaCl
     Curve25519). The private key never touches the network. The no-JS
     fallback (wg genkey) stays visible in the markup regardless. */
  function setVal(el, v) { if (!el) return; if (el.value !== undefined) el.value = v; else el.textContent = v; }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-keygen]");
    if (!btn) return;
    e.preventDefault();
    if (!global.WG) return; // lib absent → leave manual fallback
    var pair = global.WG.generateKeypair();
    setVal(document.querySelector(btn.getAttribute("data-pub")), pair.publicKey);
    setVal(document.querySelector(btn.getAttribute("data-priv")), pair.privateKey);
    var note = document.querySelector("[data-keygen-note]");
    if (note) note.hidden = false;
    // if a .conf preview references the private key placeholder, fill it
    var confEl = document.querySelector("[data-conf]");
    if (confEl) confEl.setAttribute("data-privkey", pair.privateKey);
  });

  /* ---- 4. QR rendering — real QR codes via qr.js (self-hosted) ----
     Pure progressive enhancement; a static .conf / address text is
     always available without it. Renders locally; no network. */
  function renderQR(c) {
    var data = c.getAttribute("data-qr") || "";
    data = data.replace(/\\n/g, "\n");
    var styles = getComputedStyle(document.documentElement);
    var dark = (styles.getPropertyValue("--qr-dark") || "#16201f").trim();
    try {
      global.QRCode.toCanvas(c, data, { ecl: c.getAttribute("data-ecl") || "M", quiet: 3, dark: dark, light: "#ffffff" });
    } catch (e) {
      // The QR is pure progressive enhancement — the address/.conf text is always on the page.
      // Hide the canvas rather than painting a message: a painted literal is a fixed language,
      // and this file is served to both locales (same reason as data-copied above).
      c.hidden = true;
    }
  }
  if (global.QRCode) {
    document.querySelectorAll("canvas[data-qr]").forEach(renderQR);
  }
})(typeof window !== "undefined" ? window : this);
