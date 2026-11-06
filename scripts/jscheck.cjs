// Validates the BROWSER assets the templates actually load (web/templates/*):
//   web/static/wg-keys.js — X25519 keygen (the private key never goes to the server),
//   web/static/qr.js      — the local QR generator.
// wg-keys.js self-tests against the RFC 7748 §6.1 vectors (WG.selfTest); here we run them in CI
// so that "we do not see your key" stays VERIFIABLE against the code that actually ships.
// Run: node scripts/jscheck.cjs
const fs = require("fs"), vm = require("vm"), path = require("path");
const root = path.join(__dirname, "..");

// the scripts publish their API on `window` (or `this`); give them window === globalThis
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(path.join(root, "web/static/wg-keys.js"), "utf8"));
vm.runInThisContext(fs.readFileSync(path.join(root, "web/static/qr.js"), "utf8"));

let failed = 0;
function check(name, ok, extra) {
  console.log((ok ? "PASS" : "FAIL") + " " + name + (extra ? "  " + extra : ""));
  if (!ok) failed++;
}

// --- wg-keys.js: RFC 7748 §6.1 (X25519 base/ECDH) + round-trip ---
if (typeof WG === "undefined" || !WG.selfTest) {
  check("wg-keys.js exposes WG.selfTest", false);
} else {
  for (const r of WG.selfTest()) {
    check(r.name, r.pass, r.pass ? "" : `got=${r.got} want=${r.want}`);
  }
}

// --- qr.js: structural check (three finder patterns in the corners) ---
if (typeof QRCode === "undefined" || !QRCode.encode) {
  check("qr.js exposes QRCode.encode", false);
} else {
  const conf = "[Interface]\nPrivateKey = AAAA\nAddress = 10.7.0.5/32\n[Peer]\nEndpoint = h:51820\n";
  const qr = QRCode.encode(conf, "M");
  const finder = (ox, oy) => {
    for (let y = 0; y < 7; y++) for (let x = 0; x < 7; x++) {
      const d = Math.max(Math.abs(x - 3), Math.abs(y - 3));
      if (qr.get(ox + x, oy + y) !== (d !== 2 && d !== 4)) return false;
    }
    return true;
  };
  const n = qr.size;
  check("QR finders present", finder(0, 0) && finder(n - 7, 0) && finder(0, n - 7));
}

process.exit(failed ? 1 : 0);
