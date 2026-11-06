/* ============================================================
   qr.js — self-contained QR Code encoder (byte mode, versions 1–40).
   No external resources. Public-domain algorithm (Nayuki reference,
   "QR Code generator", https://www.nayuki.io/page/qr-code-generator-library).
   Reimplemented compactly for privacyvpn. CSP-friendly.

   Public API:
     QRCode.toCanvas(canvasEl, text, { ecl, quiet, dark, light })
     QRCode.encode(text, ecl) -> { size, get(x,y) }
   ============================================================ */
(function (global) {
  "use strict";

  // ECC level ordinals + format bits
  var ECL = { L: { ord: 0, fmt: 1 }, M: { ord: 1, fmt: 0 }, Q: { ord: 2, fmt: 3 }, H: { ord: 3, fmt: 2 } };

  // Error-correction codewords per block  [ecl.ord][version]
  var ECC_CW = [
    [-1,7,10,15,20,26,18,20,24,30,18,20,24,26,30,22,24,28,30,28,28,28,28,30,30,26,28,30,30,30,30,30,30,30,30,30,30,30,30,30,30],
    [-1,10,16,26,18,24,16,18,22,22,26,30,22,22,24,24,28,28,26,26,26,26,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28],
    [-1,13,22,18,26,18,24,18,22,20,24,28,26,24,20,30,24,28,28,26,30,28,30,30,30,30,28,30,30,30,30,30,30,30,30,30,30,30,30,30,30],
    [-1,17,28,22,16,22,28,26,26,24,28,24,28,22,24,24,30,28,28,26,28,30,24,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30]
  ];
  // Number of error-correction blocks  [ecl.ord][version]
  var ECC_BLK = [
    [-1,1,1,1,1,1,2,2,2,2,4,4,4,4,4,6,6,6,6,7,8,8,9,9,10,12,12,12,13,14,15,16,17,18,19,19,20,21,22,24,25],
    [-1,1,1,1,2,2,4,4,4,5,5,5,8,9,9,10,10,11,13,14,16,17,17,18,20,21,23,25,26,28,29,31,33,35,37,38,40,43,45,47,49],
    [-1,1,1,2,2,4,4,6,6,8,8,8,10,12,16,12,17,16,18,21,20,23,23,25,27,29,34,34,35,38,40,43,45,48,51,53,56,59,62,65,68],
    [-1,1,1,2,4,4,4,5,6,8,8,11,11,16,16,18,16,19,21,25,25,25,34,30,32,35,37,40,42,45,48,51,54,57,60,63,66,70,74,77,81]
  ];

  function numRawDataModules(ver) {
    var r = (16 * ver + 128) * ver + 64;
    if (ver >= 2) {
      var n = Math.floor(ver / 7) + 2;
      r -= (25 * n - 10) * n - 55;
      if (ver >= 7) r -= 36;
    }
    return r;
  }
  function numDataCodewords(ver, eclOrd) {
    return Math.floor(numRawDataModules(ver) / 8) - ECC_CW[eclOrd][ver] * ECC_BLK[eclOrd][ver];
  }

  // ---- Reed–Solomon over GF(256) ----
  function rsMul(x, y) {
    var z = 0;
    for (var i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11D); z ^= ((y >>> i) & 1) * x; }
    return z & 0xFF;
  }
  function rsDivisor(degree) {
    var result = [], i, j;
    for (i = 0; i < degree - 1; i++) result.push(0);
    result.push(1);
    var root = 1;
    for (i = 0; i < degree; i++) {
      for (j = 0; j < result.length; j++) {
        result[j] = rsMul(result[j], root);
        if (j + 1 < result.length) result[j] ^= result[j + 1];
      }
      root = rsMul(root, 0x02);
    }
    return result;
  }
  function rsRemainder(data, divisor) {
    var result = divisor.map(function () { return 0; });
    data.forEach(function (b) {
      var factor = b ^ result.shift();
      result.push(0);
      divisor.forEach(function (coef, i) { result[i] ^= rsMul(coef, factor); });
    });
    return result;
  }

  function utf8bytes(str) {
    if (global.TextEncoder) return Array.from(new global.TextEncoder().encode(str));
    var out = [], i, c;
    for (i = 0; i < str.length; i++) {
      c = str.charCodeAt(i);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xC0 | (c >> 6), 0x80 | (c & 0x3F));
      else out.push(0xE0 | (c >> 12), 0x80 | ((c >> 6) & 0x3F), 0x80 | (c & 0x3F));
    }
    return out;
  }

  function encode(text, eclName) {
    var ecl = ECL[eclName] || ECL.M;
    var data = utf8bytes(String(text));

    // choose smallest version that fits
    var ver = 0, capBits = 0, ccBits = 0, usedBits = 0;
    for (var v = 1; v <= 40; v++) {
      ccBits = v < 10 ? 8 : 16;
      usedBits = 4 + ccBits + 8 * data.length;
      capBits = numDataCodewords(v, ecl.ord) * 8;
      if (usedBits <= capBits) { ver = v; break; }
    }
    if (!ver) throw new Error("QR: data too long");

    var size = ver * 4 + 17;

    // ---- build bit stream ----
    var bits = [];
    function appendBits(val, len) { for (var i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); }
    appendBits(0x4, 4);                 // byte mode
    appendBits(data.length, ccBits);    // char count
    data.forEach(function (b) { appendBits(b, 8); });

    var dataCap = numDataCodewords(ver, ecl.ord) * 8;
    appendBits(0, Math.min(4, dataCap - bits.length));        // terminator
    if (bits.length % 8 !== 0) appendBits(0, 8 - (bits.length % 8)); // byte align
    for (var pad = 0xEC; bits.length < dataCap; pad ^= 0xEC ^ 0x11) appendBits(pad, 8);

    // bits -> codewords
    var dataCw = [];
    for (var i = 0; i < bits.length; i += 8) {
      var b = 0; for (var k = 0; k < 8; k++) b = (b << 1) | bits[i + k];
      dataCw.push(b);
    }

    // ---- ECC + interleave ----
    var numBlocks = ECC_BLK[ecl.ord][ver];
    var blockEccLen = ECC_CW[ecl.ord][ver];
    var rawCw = Math.floor(numRawDataModules(ver) / 8);
    var numShort = numBlocks - (rawCw % numBlocks);
    var shortLen = Math.floor(rawCw / numBlocks);
    var blocks = [], rsDiv = rsDivisor(blockEccLen), kk = 0;
    for (i = 0; i < numBlocks; i++) {
      var dat = dataCw.slice(kk, kk + shortLen - blockEccLen + (i < numShort ? 0 : 1));
      kk += dat.length;
      var ecc = rsRemainder(dat, rsDiv);
      if (i < numShort) dat.push(0);
      blocks.push(dat.concat(ecc));
    }
    var allCw = [];
    for (i = 0; i < blocks[0].length; i++) {
      for (var j = 0; j < blocks.length; j++) {
        if (i !== shortLen - blockEccLen || j >= numShort) allCw.push(blocks[j][i]);
      }
    }

    // ---- matrix ----
    var mods = [], isFn = [];
    for (i = 0; i < size; i++) { mods.push(new Array(size).fill(false)); isFn.push(new Array(size).fill(false)); }
    function set(x, y, dark) { mods[y][x] = dark; isFn[y][x] = true; }

    // timing
    for (i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    // finders
    function finder(cx, cy) {
      for (var dy = -4; dy <= 4; dy++) for (var dx = -4; dx <= 4; dx++) {
        var d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy;
        if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
      }
    }
    finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
    // alignment positions
    function alignPos() {
      if (ver === 1) return [];
      var n = Math.floor(ver / 7) + 2;
      var step = ver === 32 ? 26 : Math.ceil((ver * 4 + 4) / (n * 2 - 2)) * 2;
      var res = [6];
      for (var pos = size - 7; res.length < n; pos -= step) res.splice(1, 0, pos);
      return res;
    }
    var ap = alignPos();
    for (i = 0; i < ap.length; i++) for (j = 0; j < ap.length; j++) {
      if ((i === 0 && j === 0) || (i === 0 && j === ap.length - 1) || (i === ap.length - 1 && j === 0)) continue;
      for (var ddy = -2; ddy <= 2; ddy++) for (var ddx = -2; ddx <= 2; ddx++)
        set(ap[i] + ddx, ap[j] + ddy, Math.max(Math.abs(ddx), Math.abs(ddy)) !== 1);
    }
    function getBit(x, i) { return ((x >>> i) & 1) !== 0; }

    function drawFormat(mask) {
      var d = (ecl.fmt << 3) | mask, rem = d, i;
      for (i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      var b = ((d << 10) | rem) ^ 0x5412;
      for (i = 0; i <= 5; i++) set(8, i, getBit(b, i));
      set(8, 7, getBit(b, 6)); set(8, 8, getBit(b, 7)); set(7, 8, getBit(b, 8));
      for (i = 9; i < 15; i++) set(14 - i, 8, getBit(b, i));
      for (i = 0; i < 8; i++) set(size - 1 - i, 8, getBit(b, i));
      for (i = 8; i < 15; i++) set(8, size - 15 + i, getBit(b, i));
      set(8, size - 8, true);
    }
    function drawVersion() {
      if (ver < 7) return;
      var rem = ver, i; for (i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1F25);
      var b = (ver << 12) | rem;
      for (i = 0; i < 18; i++) {
        var bit = getBit(b, i), a = size - 11 + (i % 3), c = Math.floor(i / 3);
        set(a, c, bit); set(c, a, bit);
      }
    }
    drawFormat(0); drawVersion();

    // place codewords
    var ci = 0;
    for (var right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (var vert = 0; vert < size; vert++) {
        for (j = 0; j < 2; j++) {
          var x = right - j, upward = ((right + 1) & 2) === 0, y = upward ? size - 1 - vert : vert;
          if (!isFn[y][x] && ci < allCw.length * 8) {
            mods[y][x] = getBit(allCw[ci >>> 3], 7 - (ci & 7));
            ci++;
          }
        }
      }
    }

    function applyMask(mask) {
      for (var y = 0; y < size; y++) for (var x = 0; x < size; x++) {
        if (isFn[y][x]) continue;
        var inv;
        switch (mask) {
          case 0: inv = (x + y) % 2 === 0; break;
          case 1: inv = y % 2 === 0; break;
          case 2: inv = x % 3 === 0; break;
          case 3: inv = (x + y) % 3 === 0; break;
          case 4: inv = (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0; break;
          case 5: inv = (x * y) % 2 + (x * y) % 3 === 0; break;
          case 6: inv = ((x * y) % 2 + (x * y) % 3) % 2 === 0; break;
          case 7: inv = ((x + y) % 2 + (x * y) % 3) % 2 === 0; break;
        }
        if (inv) mods[y][x] = !mods[y][x];
      }
    }

    function addHist(run, hist) { if (hist[0] === 0) run += size; hist.pop(); hist.unshift(run); }
    function countPat(h) {
      var n = h[1];
      var core = n > 0 && h[2] === n && h[3] === n * 3 && h[4] === n && h[5] === n;
      return (core && h[0] >= n * 4 && h[6] >= n ? 1 : 0) + (core && h[6] >= n * 4 && h[0] >= n ? 1 : 0);
    }
    function termCount(col, run, hist) {
      if (col) { addHist(run, hist); run = 0; }
      run += size; addHist(run, hist);
      return countPat(hist);
    }
    function penalty() {
      var result = 0, x, y;
      for (y = 0; y < size; y++) {
        var col = false, run = 0, hist = [0,0,0,0,0,0,0];
        for (x = 0; x < size; x++) {
          if (mods[y][x] === col) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
          else { addHist(run, hist); if (!col) result += countPat(hist) * 40; col = mods[y][x]; run = 1; }
        }
        result += termCount(col, run, hist) * 40;
      }
      for (x = 0; x < size; x++) {
        col = false; run = 0; hist = [0,0,0,0,0,0,0];
        for (y = 0; y < size; y++) {
          if (mods[y][x] === col) { run++; if (run === 5) result += 3; else if (run > 5) result++; }
          else { addHist(run, hist); if (!col) result += countPat(hist) * 40; col = mods[y][x]; run = 1; }
        }
        result += termCount(col, run, hist) * 40;
      }
      for (y = 0; y < size - 1; y++) for (x = 0; x < size - 1; x++) {
        var c = mods[y][x];
        if (c === mods[y][x + 1] && c === mods[y + 1][x] && c === mods[y + 1][x + 1]) result += 3;
      }
      var dark = 0; for (y = 0; y < size; y++) for (x = 0; x < size; x++) if (mods[y][x]) dark++;
      var total = size * size;
      var kpen = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
      result += kpen * 10;
      return result;
    }

    // choose best mask
    var best = 0, minPen = Infinity;
    for (var m = 0; m < 8; m++) {
      applyMask(m); drawFormat(m);
      var p = penalty();
      if (p < minPen) { minPen = p; best = m; }
      applyMask(m); // undo
    }
    applyMask(best); drawFormat(best);

    return { size: size, get: function (x, y) { return mods[y][x]; } };
  }

  function toCanvas(canvas, text, opts) {
    opts = opts || {};
    var qr = encode(text, opts.ecl || "M");
    var quiet = opts.quiet == null ? 4 : opts.quiet;
    var dim = qr.size + quiet * 2;
    var px = canvas.width || 200;
    var scale = Math.max(1, Math.floor(px / dim));
    var real = dim * scale;
    canvas.width = real; canvas.height = real;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = opts.light || "#ffffff";
    ctx.fillRect(0, 0, real, real);
    ctx.fillStyle = opts.dark || "#16201f";
    for (var y = 0; y < qr.size; y++) for (var x = 0; x < qr.size; x++) {
      if (qr.get(x, y)) ctx.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale);
    }
    return qr;
  }

  global.QRCode = { encode: encode, toCanvas: toCanvas };
})(typeof window !== "undefined" ? window : this);
