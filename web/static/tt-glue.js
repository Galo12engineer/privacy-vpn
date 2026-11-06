/* privacyvpn — keygen→config glue. Progressive enhancement.
   Carries the locally generated private key from the "device" step to the "config" step
   (through sessionStorage) and assembles the full .conf and the QR LOCALLY.
   The private key never goes to the server. Without JS the server placeholder stays. */
(function (global) {
  "use strict";
  var KEY = "tt_priv";

  // Does this private key actually belong to that public key? Returns null when we cannot tell
  // (wg-keys.js absent, malformed input) — callers treat null as "don't know", never as "matches".
  function derivesTo(priv, pub) {
    if (!priv || !pub || !global.WG || !global.WG.publicFromPrivate) return null;
    try { return global.WG.publicFromPrivate(priv) === pub; } catch (e) { return null; }
  }

  // add-device: on submit, stash the local private key for the next step.
  // Only stash a key that matches the pubkey being submitted: the customer may have pasted his own
  // pubkey over a generated one (the form offers that path), which leaves a stale private key in
  // the field. Stashing it would produce a .conf whose key the server never registered.
  document.addEventListener("submit", function () {
    var pk = document.getElementById("privkey");
    var pub = document.getElementById("pubkey");
    if (!pk || !pk.value) return;
    if (pub && derivesTo(pk.value, pub.value.trim()) === false) {
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      return;
    }
    try { sessionStorage.setItem(KEY, pk.value); } catch (e) {}
  }, true);

  // config: assemble the full .conf (with the private key) + QR
  document.addEventListener("DOMContentLoaded", function () {
    var tpl = document.getElementById("conf-tpl");
    var pre = document.getElementById("conf");
    if (!tpl || !pre) return;
    var priv = null;
    try { priv = sessionStorage.getItem(KEY); sessionStorage.removeItem(KEY); } catch (e) {}
    var template = tpl.textContent;

    // The stash is a single slot: a second add-device page (another tab, a reload, going Back)
    // overwrites it, and then the key sitting here belongs to a DIFFERENT keypair than the one the
    // server registered. Injecting it hands out a .conf that never handshakes — the node drops
    // unknown keys without a word, so the customer sees "Received 0 B" and nothing else. Verify
    // against the pubkey the server actually stored; on a proven mismatch drop the key entirely.
    var wantPub = tpl.getAttribute("data-pubkey") || "";
    var mismatch = wantPub ? derivesTo(priv, wantPub) === false : false;
    if (mismatch) {
      priv = null;
      var warn = document.getElementById("conf-mismatch");
      if (warn) warn.hidden = false;
    }
    // No private key to inject — the customer pasted his own pubkey (the manual path the form
    // itself offers), sessionStorage is blocked, or the page was reopened. Fall back to the
    // placeholder the SERVER rendered, never to a literal: a literal here is a fixed language and
    // it ends up inside the customer's downloaded .conf.
    var placeholder = tpl.getAttribute("data-priv-placeholder") || "<INSERT_PRIVATE_KEY_IN_BROWSER>";
    var full = priv
      ? template.replace("__PRIV__", priv)
      : template.replace("__PRIV__", placeholder);
    pre.textContent = full;

    var dl = document.getElementById("dl");
    if (dl) {
      // iOS ignores a data: URL on a download link (Safari AND Chrome — same WebKit underneath):
      // the customer taps "Download .conf" and literally nothing happens, which is where the iPhone
      // path used to dead-end (the QR is no help either — WireGuard scans with the camera, so a code
      // on the same screen cannot be read). A Blob URL lands in Files, where the app's "Create from
      // file or archive" picks it up. Serving the .conf from the server instead is not an option:
      // it carries the private key, which never leaves the browser.
      var href;
      try {
        href = URL.createObjectURL(new Blob([full], { type: "application/octet-stream" }));
      } catch (e) {
        href = "data:application/octet-stream;charset=utf-8," + encodeURIComponent(full);
      }
      dl.href = href;
    }

    // Share sheet — the only reliable import path on iOS (see the template note). Feature-detected
    // with canShare({files}), so the button stays hidden where it would do nothing: desktop
    // browsers, and any non-secure context (plain http, e.g. over .onion) where the API is absent.
    var share = document.getElementById("share");
    if (share && priv) {
      var file = null;
      try {
        file = new File([full], (dl && dl.getAttribute("download")) || "privacyvpn.conf", {
          type: "application/octet-stream",
        });
      } catch (e) { /* no File constructor — leave the button hidden */ }
      if (file && global.navigator && navigator.canShare && navigator.canShare({ files: [file] })) {
        share.hidden = false;
        share.addEventListener("click", function () {
          // A rejected share is the user tapping Cancel — not an error worth showing.
          navigator.share({ files: [file] }).catch(function () {});
        });
      }
    }

    var canvas = document.querySelector("canvas[data-qr-conf]");
    if (canvas && priv && global.QRCode) {
      var styles = getComputedStyle(document.documentElement);
      var dark = (styles.getPropertyValue("--qr-dark") || "#16201f").trim();
      try { global.QRCode.toCanvas(canvas, full, { ecl: "M", quiet: 3, dark: dark, light: "#ffffff" }); }
      catch (e) { canvas.hidden = true; }
    } else if (canvas) {
      canvas.hidden = true; // without the private key the QR is incomplete — download only
    }
  });
})(typeof window !== "undefined" ? window : this);
