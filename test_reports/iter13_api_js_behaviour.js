/**
 * Iter13 behavioural harness for api.js retry-with-backoff.
 *
 * Mocks global.fetch and exercises the four behavioural invariants:
 *   (1) single 520 then 200 on a GET → resolves to 200 body
 *   (2) four 520s on a GET → rejects with `HTTP 520` (retries exhausted)
 *   (3) single 520 on a POST → rejects immediately (no retry)
 *   (4) fetch throws once then succeeds on a GET → resolves successfully
 *
 * Loads api.js directly. We bypass Jest because the project's Jest setup
 * pulls in CRA's full toolchain. This is a plain node script.
 *
 * Doctrine: matches the source-level tripwires in
 *   /app/backend/tests/test_api_js_transient_5xx_retry.py
 * but covers behaviour, not just source-string presence.
 */
const path = require('path');
const fs = require('fs');
const Module = require('module');

// Shim browser globals
global.window = { location: { host: 'preview', protocol: 'https:' } };
global.localStorage = {
  _s: {},
  getItem(k) { return this._s[k] ?? null; },
  setItem(k, v) { this._s[k] = String(v); },
  removeItem(k) { delete this._s[k]; },
};
global.process = process;
process.env.REACT_APP_BACKEND_URL = 'https://test.local';
global.URLSearchParams = require('url').URLSearchParams;
global.CustomEvent = function(){};

// Stub axios import (api.js imports axios but doesn't use it for the
// transient retry path; only `_axios` export uses it).
const origResolve = Module._resolveFilename;
Module._resolveFilename = function(req, parent, ...rest) {
  if (req === 'axios') return path.join(__dirname, 'axios_stub.js');
  return origResolve.call(this, req, parent, ...rest);
};
fs.writeFileSync(path.join(__dirname, 'axios_stub.js'), 'module.exports = {default:{}};');

// Now require api.js by transpiling its ES module syntax. The file uses
// `import`/`export` so we need a quick rewrite.
const srcPath = '/app/frontend/src/lib/api.js';
let src = fs.readFileSync(srcPath, 'utf8');
// Strip the axios import — already shimmed via the resolver above; we
// just need to convert the ES module syntax.
src = src.replace(/^import axios from "axios";\s*$/m, 'const axios = require("axios").default;');
src = src.replace(/^export const /gm, 'const ');
src = src.replace(/^export function /gm, 'function ');
src = src.replace(/^export \{[^}]+\};?\s*$/gm, '');
// Append exports at bottom
src += '\nmodule.exports = { api, API, BACKEND_URL, getToken, setToken };\n';
const tmpPath = path.join(__dirname, 'api_compiled.js');
fs.writeFileSync(tmpPath, src);

const { api } = require(tmpPath);

// ── Mock fetch helper ─────────────────────────────────────────────
function makeFetch(responses) {
  let i = 0;
  return async function mockFetch(url, opts) {
    const r = responses[i++] || responses[responses.length - 1];
    if (r.throws) throw new Error(r.throws);
    return {
      status: r.status,
      ok: r.status >= 200 && r.status < 300,
      headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      text: async () => JSON.stringify(r.body ?? {}),
      json: async () => r.body ?? {},
    };
  };
}

let passed = 0, failed = 0;
async function assert(name, fn) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
    passed++;
  } catch (e) {
    console.error(`  FAIL  ${name}\n        ${e.message}`);
    failed++;
  }
}

(async () => {
  console.log('\n── Iter13 api.js behavioural retry tests ──');

  // (1) GET: 520 then 200 → resolves to 200 body
  await assert('GET 520→200 self-recovers and returns 200 body', async () => {
    global.fetch = makeFetch([
      { status: 520, body: { detail: 'cf 520' } },
      { status: 200, body: { ok: true, msg: 'recovered' } },
    ]);
    const t0 = Date.now();
    const res = await api.get('/anything');
    const elapsed = Date.now() - t0;
    if (res.status !== 200) throw new Error(`status ${res.status}`);
    if (!res.data || res.data.msg !== 'recovered') throw new Error(`body ${JSON.stringify(res.data)}`);
    if (elapsed < 350 || elapsed > 1500) throw new Error(`elapsed ${elapsed}ms — expected ~400ms backoff`);
  });

  // (2) GET: 4× 520 → rejects with HTTP 520
  await assert('GET 520×4 exhausts retries → rejects with HTTP 520', async () => {
    global.fetch = makeFetch([
      { status: 520 }, { status: 520 }, { status: 520 }, { status: 520 },
    ]);
    let threw = false;
    try {
      await api.get('/anything');
    } catch (e) {
      threw = true;
      if (!String(e.message).includes('520')) throw new Error(`wrong err: ${e.message}`);
      if (!e.response || e.response.status !== 520) throw new Error(`no err.response.status=520, got ${JSON.stringify(e.response)}`);
    }
    if (!threw) throw new Error('expected rejection but resolved');
  });

  // (3) POST: 520 once → rejects immediately, NO retry. Critical doctrine.
  await assert('POST 520 fails immediately (no retry)', async () => {
    let calls = 0;
    global.fetch = async () => {
      calls++;
      return {
        status: 520, ok: false,
        headers: { get: () => 'application/json' },
        text: async () => JSON.stringify({ detail: 'cf 520' }),
        json: async () => ({ detail: 'cf 520' }),
      };
    };
    const t0 = Date.now();
    let threw = false;
    try {
      await api.post('/arm-all', { go: true });
    } catch (e) {
      threw = true;
      if (!String(e.message).includes('520') && !String(e.message).includes('cf 520')) {
        throw new Error(`wrong err: ${e.message}`);
      }
    }
    const elapsed = Date.now() - t0;
    if (!threw) throw new Error('POST resolved — but it should have rejected!');
    if (calls !== 1) throw new Error(`POST was called ${calls} times — MUST be 1 (no retry doctrine)`);
    if (elapsed > 300) throw new Error(`POST took ${elapsed}ms — likely retried`);
  });

  // (4) GET: fetch throws once, then succeeds → resolves
  await assert('GET network error then 200 self-recovers', async () => {
    let i = 0;
    global.fetch = async () => {
      i++;
      if (i === 1) throw new TypeError('Failed to fetch');
      return {
        status: 200, ok: true,
        headers: { get: () => 'application/json' },
        text: async () => JSON.stringify({ ok: true }),
        json: async () => ({ ok: true }),
      };
    };
    const res = await api.get('/anything');
    if (res.status !== 200) throw new Error(`status ${res.status}`);
    if (i !== 2) throw new Error(`fetch called ${i} times — expected 2 (1 throw + 1 retry)`);
  });

  // (5) POST: fetch throws → no retry, rejects
  await assert('POST network error fails immediately (no retry)', async () => {
    let i = 0;
    global.fetch = async () => { i++; throw new TypeError('Failed to fetch'); };
    let threw = false;
    try {
      await api.post('/flip-flag', { v: 1 });
    } catch (e) {
      threw = true;
    }
    if (!threw) throw new Error('POST resolved on network error — bug!');
    if (i !== 1) throw new Error(`fetch called ${i} times on POST network error — MUST be 1`);
  });

  console.log(`\n── Results: ${passed} pass / ${failed} fail ──`);
  process.exit(failed > 0 ? 1 : 0);
})();
