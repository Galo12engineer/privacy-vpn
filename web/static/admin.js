/* privacyvpn admin — progressive enhancement only (loaded after app.js,
   which already provides theme-toggle + copy-to-clipboard). Served from
   /static (CSP script-src 'self'). The console works fully without this. */
(function () {
  "use strict";

  // abuse block-peer: the route is POST <admin>/peers/<uuid>/block, so build the
  // path from the entered id. The admin base path is a server-side secret
  // (CP_ADMIN_PATH), passed in via data-peer-block-base. Without JS the form does
  // a harmless GET back to <admin>/nodes (see the <noscript> note beside it).
  var bf = document.querySelector("[data-block-form]");
  if (!bf) return;
  var input = bf.querySelector("#pb-id");
  var base = bf.getAttribute("data-peer-block-base") || "/admin/peers";
  bf.addEventListener("submit", function (e) {
    var id = ((input && input.value) || "").trim();
    if (!id) {
      e.preventDefault();
      if (input) input.focus();
      return;
    }
    // The prompt text comes from the template (data-confirm, "%s" = the peer id): this file is
    // served from /static to anyone, and a localized literal in it attributes the operator.
    var ask = bf.getAttribute("data-confirm");
    if (ask && !window.confirm(ask.replace("%s", id))) {
      e.preventDefault();
      return;
    }
    bf.method = "post";
    bf.action = base + "/" + encodeURIComponent(id) + "/block";
  });
})();
