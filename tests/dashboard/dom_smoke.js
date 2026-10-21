// Executa app.js REAL contra o servidor REAL, com um DOM mínimo escrito à mão (NÃO é um navegador).
// Uso: node dom_smoke.js <base_url> <app.js> [acao_a_clicar]
const vm = require("vm"), fs = require("fs");
const [base, appPath, click] = process.argv.slice(2);

class Node_ {}
class El extends Node_ {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this._text = ""; this.hidden = false; this.disabled = false; this.dataset = {}; this.listeners = {}; this.className = ""; this.value = ""; }
  setAttribute(k, v) { this.attrs[k] = v; if (k.startsWith("data-")) this.dataset[k.slice(5)] = v; if (k === "class") this.className = v; }
  append(...k) { for (const x of k) this.children.push(typeof x === "string" ? Object.assign(new El("#text"), { _text: x }) : x); }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  set textContent(v) { this.children = []; this._text = String(v); } get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  addEventListener(ev, fn) { (this.listeners[ev] ||= []).push(fn); }
  get content() { return this.attrs.content; }
}
const byId = {}, buttons = [], tbodies = {};
const html = fs.readFileSync(0, "utf8");
for (const m of html.matchAll(/<(\w+)([^>]*)>/g)) {
  const idm = /id="([^"]+)"/.exec(m[2]); const e = new El(m[1]);
  if (idm) { byId[idm[1]] = e; if (m[1] === "table") { const tb = new El("tbody"); e.children.push(tb); tbodies[idm[1]] = tb; } }
  const da = /data-acao="([^"]+)"/.exec(m[2]); if (da) { e.dataset.acao = da[1]; buttons.push(e); }
  if (/hidden/.test(m[2]) && idm) byId[idm[1]].hidden = true;
}
const csrf = /name="csrf-token" content="([^"]+)"/.exec(html)[1];
const document = {
  querySelector: (s) => { if (s.startsWith("meta")) return { content: csrf }; const m = /^#(\S+) tbody$/.exec(s); return m ? tbodies[m[1]] : byId[s.slice(1)]; },
  getElementById: (id) => byId[id], createElement: (t) => new El(t), createElementNS: (_n, t) => new El(t), createTextNode: (t) => Object.assign(new El("#text"), { _text: String(t) }),
  querySelectorAll: () => buttons,
};
const realFetch = fetch;
const ctx = { document, Node: Node_, console, Math, Number, String, Object, JSON, Date, isNaN, Promise, Error, parseInt, setInterval: () => 0, confirm: () => true,
  fetch: (p, o = {}) => realFetch(base + p, { ...o, headers: { ...(o.headers || {}), Origin: base } }), URL };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(appPath, "utf8").replace('"use strict";', ""), ctx);
const tick = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await tick(600);
  const out = { estado_antes: byId.estado.textContent, cards: byId.cards.textContent, avisos: byId.avisos.textContent, live: byId["live-motivo"].textContent, msg: byId.msg.textContent,
    dados: tbodies["tb-dados"].textContent, abertas: tbodies["tb-abertas"].textContent, btn_iniciar_desabilitado: byId["btn-iniciar"].disabled, btn_parar_desabilitado: byId["btn-parar"].disabled };
  if (click) {
    const b = buttons.find((x) => x.dataset.acao === click);
    for (const fn of b.listeners.click || []) await fn();
    await tick(600);
    out.msg_apos = byId.msg.textContent; out.estado_apos = byId.estado.textContent; out.btn_parar_apos_desabilitado = byId["btn-parar"].disabled;
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ erro: String(e && e.stack || e) })); process.exit(1); });
