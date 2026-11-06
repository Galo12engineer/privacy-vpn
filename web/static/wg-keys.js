/* ============================================================
   wg-keys.js — real WireGuard (Curve25519 / X25519) keypair
   generation, entirely in the browser. The private key never
   leaves the device. No external resources.

   Curve25519 scalar multiplication is the public-domain
   TweetNaCl implementation (D. J. Bernstein et al.,
   https://tweetnacl.cr.yp.to/), reimplemented here verbatim
   in its essential subset. Randomness comes from the platform
   CSPRNG (crypto.getRandomValues).

   Public API:
     WG.generateKeypair() -> { privateKey: <b64>, publicKey: <b64> }
     WG.publicFromPrivate(b64priv) -> <b64 pub>
   ============================================================ */
(function (global) {
  "use strict";

  var _9 = new Uint8Array(32); _9[0] = 9;

  function gf(init) {
    var r = new Float64Array(16);
    if (init) for (var i = 0; i < init.length; i++) r[i] = init[i];
    return r;
  }
  var _121665 = gf([0xDB41, 1]);

  function car25519(o) {
    var i, v, c = 1;
    for (i = 0; i < 16; i++) {
      v = o[i] + c + 65535;
      c = Math.floor(v / 65536);
      o[i] = v - c * 65536;
    }
    o[0] += c - 1 + 37 * (c - 1);
  }
  function sel25519(p, q, b) {
    var t, c = ~(b - 1);
    for (var i = 0; i < 16; i++) { t = c & (p[i] ^ q[i]); p[i] ^= t; q[i] ^= t; }
  }
  function pack25519(o, n) {
    var i, j, b, m = gf(), t = gf();
    for (i = 0; i < 16; i++) t[i] = n[i];
    car25519(t); car25519(t); car25519(t);
    for (j = 0; j < 2; j++) {
      m[0] = t[0] - 0xffed;
      for (i = 1; i < 15; i++) { m[i] = t[i] - 0xffff - ((m[i - 1] >> 16) & 1); m[i - 1] &= 0xffff; }
      m[15] = t[15] - 0x7fff - ((m[14] >> 16) & 1);
      b = (m[15] >> 16) & 1; m[14] &= 0xffff;
      sel25519(t, m, 1 - b);
    }
    for (i = 0; i < 16; i++) { o[2 * i] = t[i] & 0xff; o[2 * i + 1] = t[i] >> 8; }
  }
  function unpack25519(o, n) {
    for (var i = 0; i < 16; i++) o[i] = n[2 * i] + (n[2 * i + 1] << 8);
    o[15] &= 0x7fff;
  }
  function A(o, a, b) { for (var i = 0; i < 16; i++) o[i] = a[i] + b[i]; }
  function Z(o, a, b) { for (var i = 0; i < 16; i++) o[i] = a[i] - b[i]; }
  function M(o, a, b) {
    var t = new Float64Array(31), i, j;
    for (i = 0; i < 31; i++) t[i] = 0;
    for (i = 0; i < 16; i++) for (j = 0; j < 16; j++) t[i + j] += a[i] * b[j];
    for (i = 0; i < 15; i++) t[i] += 38 * t[i + 16];
    for (i = 0; i < 16; i++) o[i] = t[i];
    car25519(o); car25519(o);
  }
  function S(o, a) { M(o, a, a); }
  function inv25519(o, i) {
    var c = gf(), a;
    for (a = 0; a < 16; a++) c[a] = i[a];
    for (a = 253; a >= 0; a--) { S(c, c); if (a !== 2 && a !== 4) M(c, c, i); }
    for (a = 0; a < 16; a++) o[a] = c[a];
  }
  function scalarmult(q, n, p) {
    var z = new Uint8Array(32), x = new Float64Array(80), r, i;
    var a = gf(), b = gf(), c = gf(), d = gf(), e = gf(), f = gf();
    for (i = 0; i < 31; i++) z[i] = n[i];
    z[31] = (n[31] & 127) | 64; z[0] &= 248;
    unpack25519(x, p);
    for (i = 0; i < 16; i++) { b[i] = x[i]; d[i] = a[i] = c[i] = 0; }
    a[0] = d[0] = 1;
    for (i = 254; i >= 0; --i) {
      r = (z[i >>> 3] >>> (i & 7)) & 1;
      sel25519(a, b, r); sel25519(c, d, r);
      A(e, a, c); Z(a, a, c); A(c, b, d); Z(b, b, d);
      S(d, e); S(f, a); M(a, c, a); M(c, b, e);
      A(e, a, c); Z(a, a, c); S(b, a); Z(c, d, f);
      M(a, c, _121665); A(a, a, d); M(c, c, a); M(a, d, f);
      M(d, b, x); S(b, e);
      sel25519(a, b, r); sel25519(c, d, r);
    }
    for (i = 0; i < 16; i++) { x[i + 16] = a[i]; x[i + 32] = c[i]; x[i + 48] = b[i]; x[i + 64] = d[i]; }
    var x32 = x.subarray(32), x16 = x.subarray(16);
    inv25519(x32, x32); M(x16, x16, x32); pack25519(q, x16);
    return 0;
  }

  function b64(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  function fromB64(str) {
    var bin = atob(str), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function clamp(k) { k[0] &= 248; k[31] &= 127; k[31] |= 64; return k; }

  function publicFromPrivate(privB64) {
    var priv = fromB64(privB64), pub = new Uint8Array(32);
    scalarmult(pub, priv, _9);
    return b64(pub);
  }

  function generateKeypair() {
    if (!global.crypto || !global.crypto.getRandomValues) throw new Error("CSPRNG unavailable");
    var priv = new Uint8Array(32);
    global.crypto.getRandomValues(priv);
    clamp(priv);
    var pub = new Uint8Array(32);
    scalarmult(pub, priv, _9);
    return { privateKey: b64(priv), publicKey: b64(pub) };
  }

  // RFC 7748 §6.1 X25519 test vector + round-trip self-test.
  function selfTest() {
    var hex = function (h) { var a = new Uint8Array(32); for (var i = 0; i < 32; i++) a[i] = parseInt(h.substr(i * 2, 2), 16); return a; };
    var toHex = function (a) { var s = ""; for (var i = 0; i < a.length; i++) s += ("0" + a[i].toString(16)).slice(-2); return s; };
    // Alice scalar/public from RFC 7748 §6.1
    var aPriv = hex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a");
    var aPubExpect = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a";
    var bPubExpect = "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f";
    var bPriv = hex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb");
    var ssExpect = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742";
    var out = new Uint8Array(32), results = [];
    scalarmult(out, aPriv, _9);
    results.push({ name: "X25519 base · Alice pub", pass: toHex(out) === aPubExpect, got: toHex(out), want: aPubExpect });
    scalarmult(out, bPriv, _9);
    results.push({ name: "X25519 base · Bob pub", pass: toHex(out) === bPubExpect, got: toHex(out), want: bPubExpect });
    var ss1 = new Uint8Array(32), ss2 = new Uint8Array(32);
    scalarmult(ss1, aPriv, hex(bPubExpect));
    scalarmult(ss2, bPriv, hex(aPubExpect));
    results.push({ name: "ECDH shared secret (Alice·Bob)", pass: toHex(ss1) === ssExpect, got: toHex(ss1), want: ssExpect });
    results.push({ name: "ECDH symmetric (Alice·Bob == Bob·Alice)", pass: toHex(ss1) === toHex(ss2), got: toHex(ss2), want: toHex(ss1) });
    // round-trip: derive pub from generated priv
    var kp = generateKeypair();
    results.push({ name: "Keygen round-trip (pub == derive(priv))", pass: publicFromPrivate(kp.privateKey) === kp.publicKey, got: kp.publicKey.slice(0, 16) + "…", want: "match" });
    return results;
  }

  global.WG = { generateKeypair: generateKeypair, publicFromPrivate: publicFromPrivate, selfTest: selfTest };
})(typeof window !== "undefined" ? window : this);
