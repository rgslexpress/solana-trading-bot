"use strict";
// Painel local. Somente textContent/DOM (sem atribuir HTML): dados de logs/decisões nunca são interpretados como HTML.
// Nenhuma credencial é lida ou armazenada aqui. O token CSRF vem da meta tag da própria página (mesma origem).
const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const $ = (id) => document.getElementById(id);
const ND = "N/D";

function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) { if (k === "class") e.className = v; else e.setAttribute(k, v); }
  for (const kid of kids) e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return e;
}
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const num = (v, d = 2) => (isNum(v) ? v.toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d }) : ND);
const pct = (v) => (isNum(v) ? num(v, 2) + " %" : ND);
const money = (v) => (isNum(v) ? num(v, 2) : ND);
const when = (s) => { if (!s) return ND; const d = new Date(s); return isNaN(d) ? String(s) : d.toLocaleString("pt-BR"); };
function age(s) {
  if (!isNum(s)) return ND;
  s = Math.max(0, Math.round(s));
  if (s < 90) return s + " s";
  if (s < 5400) return Math.round(s / 60) + " min";
  if (s < 172800) return (s / 3600).toFixed(1).replace(".", ",") + " h";
  return Math.round(s / 86400) + " dias";
}
function signed(v, text) { const c = isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "nd"; return el("span", { class: c }, text); }
function fill(tbodyId, rows, ncols, empty) {
  const tb = document.querySelector("#" + tbodyId + " tbody");
  tb.replaceChildren();
  if (!rows.length) { const tr = el("tr"); tr.append(el("td", { colspan: ncols, class: "nd" }, empty)); tb.append(tr); return; }
  for (const r of rows) { const tr = el("tr"); for (const c of r) tr.append(c instanceof Node ? (c.tagName === "TD" ? c : el("td", null, c)) : el("td", { class: "wrap" }, c)); tb.append(tr); }
}

async function api(path, body) {
  const opt = body === undefined ? { method: "GET", credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let js = null; try { js = await r.json(); } catch (_) { /* resposta sem JSON */ }
  if (!r.ok) throw new Error((js && js.erro) || ("HTTP " + r.status));
  return js;
}
function say(text, ok) { const m = $("msg"); m.textContent = text; m.className = "msg " + (ok ? "ok" : "err"); }

let ultimoStatus = null;
function renderStatus(s) {
  ultimoStatus = s;
  const b = $("estado"); b.textContent = "Estado: " + s.estado.replaceAll("_", " ");
  b.className = "badge " + (s.estado === "RODANDO" ? "run" : s.estado === "PARADO" ? "" : "warn");
  $("live-motivo").textContent = s.live.motivo;
  const kb = $("kill-banner");
  kb.hidden = !s.kill_switch.ativo;
  kb.textContent = s.kill_switch.ativo ? "KILL SWITCH ATIVO — novas entradas bloqueadas. Motivo: " + s.kill_switch.motivo : "";
  $("avisos").replaceChildren(...s.avisos.map((a) => el("li", null, a)));
  const a = s.acoes_permitidas;
  for (const btn of document.querySelectorAll("button[data-acao]")) btn.disabled = !a[btn.dataset.acao];
  $("ks-resume").hidden = !a.retomar_kill_switch;
  fill("tb-dados", Object.entries(s.dados.tokens).map(([sym, d]) => [
    sym, d.fonte || ND,
    el("td", null, el("span", { class: "tag " + (d.ok && !d.obsoleto ? "BUY" : "SELL") }, !d.ok ? "INVÁLIDO/INDISPONÍVEL" : d.obsoleto ? "OBSOLETO" : "OK"), d.problemas.length ? " " + d.problemas.join("; ") : ""),
    age(d.idade_ultimo_candle_s), age(d.idade_ultima_verificacao_s) + (isNum(d.idade_ultima_verificacao_s) ? " atrás" : ""), isNum(d.ultimo_preco) ? num(d.ultimo_preco, 6) : ND,
  ]), 6, "Nenhuma verificação de dados registrada ainda (inicie o paper trading).");
  $("atualizado").textContent = when(s.agora) + " (hora do robô) — ciclos nesta sessão: " + s.ciclos;
}

function card(k, vnode) { return el("div", { class: "card" }, el("div", { class: "k" }, k), el("div", { class: "v" }, vnode)); }
function renderMetrics(js) {
  const m = js.metricas;
  $("rotulo-metricas").textContent = "— " + m.rotulo;
  const cards = [
    ["Saldo inicial (" + (m.saldo_inicial_origem || "") + ")", money(m.saldo_inicial)],
    ["Patrimônio simulado", money(m.patrimonio)],
    ["Caixa simulado", money(m.caixa)], ["Exposição", money(m.exposicao)],
    ["Lucro/prejuízo REALIZADO", signed(m.pnl_realizado, money(m.pnl_realizado))],
    ["Lucro/prejuízo NÃO realizado", signed(m.pnl_nao_realizado, money(m.pnl_nao_realizado))],
    ["Resultado total", signed(m.pnl_total, money(m.pnl_total))],
    ["Rentabilidade", signed(m.rentabilidade_pct, pct(m.rentabilidade_pct))],
    ["Drawdown máximo", signed(m.drawdown_max_pct, pct(m.drawdown_max_pct))],
    ["Operações encerradas", isNum(m.operacoes_encerradas) ? String(m.operacoes_encerradas) : ND],
    ["Taxa de acerto", isNum(m.taxa_acerto) ? pct(m.taxa_acerto * 100) : ND],
    ["Ganho médio", money(m.ganho_medio)], ["Perda média", money(m.perda_media)],
    ["Profit factor", isNum(m.profit_factor) ? num(m.profit_factor, 2) : ND],
    ["Custos totais", money(m.custos_totais)], ["Sharpe", ND],
  ];
  $("cards").replaceChildren(...cards.map(([k, v]) => card(k, v)));
  $("curva-legenda").textContent = m.ultimo_registro_patrimonio ? "Último registro de patrimônio: " + when(m.ultimo_registro_patrimonio) : "Sem registros de patrimônio ainda.";
  const pts = js.curva_patrimonio.filter((p) => isNum(p.equity));
  const svg = $("curva"); svg.replaceChildren();
  if (pts.length >= 2) {
    const ys = pts.map((p) => p.equity), lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const poly = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    poly.setAttribute("points", pts.map((p, i) => (i / (pts.length - 1) * 600).toFixed(1) + "," + (115 - (p.equity - lo) / span * 110).toFixed(1)).join(" "));
    svg.append(poly);
  }
}

function renderPositions(js) {
  fill("tb-abertas", js.abertas.map((p) => [p.simbolo, num(p.quantidade, 6), num(p.preco_entrada, 6), isNum(p.preco_atual) ? num(p.preco_atual, 6) : ND,
    num(p.stop, 6), num(p.alvo, 6), el("td", null, signed(p.pnl_nao_realizado, money(p.pnl_nao_realizado))), when(p.entrada_em)]), 8, "Nenhuma posição aberta.");
  fill("tb-encerradas", js.encerradas.map((p) => [p.simbolo, num(p.quantidade, 6), num(p.preco_entrada, 6), isNum(p.preco_saida) ? num(p.preco_saida, 6) : ND,
    p.motivo_saida || ND, el("td", null, signed(p.pnl_liquido, money(p.pnl_liquido))), money(p.custos), when(p.saida_em)]), 8, "Nenhuma operação encerrada ainda.");
}
function renderDecisions(js) {
  fill("tb-decisoes", js.decisoes.slice(0, 80).map((d) => [when(d.ts), d.simbolo, el("td", null, el("span", { class: "tag " + d.tipo }, d.tipo)), isNum(d.pontuacao) ? num(d.pontuacao, 0) : ND,
    d.motivos.join("; ") || "—", d.rejeicoes.join("; ") || "—"]), 6, "Nenhuma decisão registrada ainda.");
}
function renderLogs(js) {
  fill("tb-eventos", js.eventos.slice(0, 40).map((e) => [when(e.ts), e.evento, e.origem, e.detalhe || "—"]), 4, "Nenhum evento.");
  fill("tb-alertas", js.alertas.slice(0, 40).map((a) => [when(a.ts), a.nivel, a.mensagem]), 3, "Nenhum alerta.");
  fill("tb-erros", js.erros.slice(0, 40).map((l) => [when(l.ts), l.nivel || ND, l.origem || ND, l.mensagem]), 4, "Nenhum erro no arquivo de log.");
  $("log-info").textContent = js.arquivo_existe ? "Arquivo de log: " + js.arquivo_de_log : "Arquivo de log ainda não existe: " + (js.arquivo_de_log || "(desabilitado)");
}
let cfgDone = false;
function renderConfig(c) {
  if (cfgDone) return; cfgDone = true;
  const root = $("cfg"); root.replaceChildren();
  const groups = { "Geral": { "Modo": c.modo_configurado, "Capital (paper)": c.capital_paper, "Moeda de referência": c.moeda_de_referencia, "Intervalo do ciclo (s)": c.intervalo_ciclo_s },
    "Limites de risco": c.risco, "Execução simulada": c.execucao_simulada, "Estratégia": c.estrategia, "Dados": c.dados };
  for (const [title, obj] of Object.entries(groups)) {
    const dl = el("dl");
    for (const [k, v] of Object.entries(obj)) { dl.append(el("dt", null, k), el("dd", null, String(v))); }
    root.append(el("div", { class: "g" }, el("h3", null, title), dl));
  }
}

async function refresh() {
  try {
    const [s, m, p, d, l] = await Promise.all([api("/api/status"), api("/api/metrics"), api("/api/positions"), api("/api/decisions?limite=80"), api("/api/logs?limite=60")]);
    renderStatus(s); renderMetrics(m); renderPositions(p); renderDecisions(d); renderLogs(l);
    if (!cfgDone) renderConfig(await api("/api/config"));
  } catch (e) { say("Não foi possível ler o estado: " + e.message, false); }
}

const ACOES = {
  iniciar: () => api("/api/bot/start", {}),
  pausar_entradas: () => api("/api/bot/pause-entries", {}),
  retomar_entradas: () => api("/api/bot/resume-entries", {}),
  parar: () => { if (!confirm("Parar o paper trading? Nenhuma ordem será enviada, mas stops simulados deixam de ser monitorados enquanto parado.")) return null; return api("/api/bot/stop", {}); },
  emergencia: () => { if (!confirm("EMERGÊNCIA: acionar o kill switch e bloquear novas entradas? Posições simuladas são preservadas (nada é liquidado).")) return null; return api("/api/bot/emergency", { confirm: true }); },
  retomar_kill_switch: () => api("/api/kill-switch/resume", { phrase: $("ks-frase").value, note: $("ks-nota").value }),
};
for (const btn of document.querySelectorAll("button[data-acao]")) {
  btn.addEventListener("click", async () => {
    const acao = btn.dataset.acao; btn.disabled = true;
    try {
      const p = ACOES[acao](); if (p === null) { return; }
      const r = await p; say((r.mensagem || "OK") + (r.aviso ? " " + r.aviso : ""), true);
      if (acao === "retomar_kill_switch") { $("ks-frase").value = ""; $("ks-nota").value = ""; }
    } catch (e) { say(e.message, false); }
    finally { await refresh(); }
  });
}
refresh();
setInterval(refresh, 5000);
