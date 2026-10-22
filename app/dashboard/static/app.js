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
function linkOrText(url, text) {
  if (typeof url === "string" && /^https?:\/\//i.test(url)) {
    const a = el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text);
    return a;
  }
  return document.createTextNode(String(text));
}
const ESTADO_FONTE = { ok: "OK", stale: "DESATUALIZADA", unavailable: "INDISPONÍVEL", invalid: "RESPOSTA INVÁLIDA", disabled: "DESABILITADA" };
const DOMINIO = { solana_status: "Status da Solana", crypto_news: "Notícias cripto", macro_us: "Macro EUA" };
const SAUDE_CLASSE = { saudavel: "ok", degradado: "warn", critico: "bad", contingencia: "bad", desligado: "" };
const FASE = { antes: "antes da divulgação", depois: "depois da divulgação" };
function fmt(v, d = 2) { return isNum(v) ? num(v, d) : ND; }
function renderMacro(m) {
  const box = $("macro-msg");
  if (!m) { box.textContent = "Camada macro desligada ou sem dados."; box.className = "intel-msg"; for (const id of ["tb-macro-janelas", "tb-macro-proximos", "tb-macro-divulgados", "tb-macro-mercado"]) fill(id, [], 9, "—"); $("macro-pendentes").replaceChildren(); return; }
  const j = m.janelas_ativas || [];
  box.textContent = j.length ? j.length + " janela(s) de risco macro ativa(s): " + (m.bloqueia ? "novas entradas BLOQUEADAS (política macro_high_policy=block)." : "risco por operação reduzido (× " + fmt(m.escala_de_risco, 2) + ").")
    : (m.calendario_vazio ? "Calendário macro VAZIO: sem janelas de risco (crie config/macro_calendar.json). Isto NÃO significa ausência de eventos." : "Nenhuma janela de risco macro ativa agora.");
  box.className = "intel-msg " + (j.length ? "warn" : m.calendario_vazio ? "bad" : "ok");
  fill("tb-macro-janelas", j.map((w) => [w.evento, FASE[w.fase] || w.fase, String(w.importancia), when(w.horario_utc), w.horario_local, when(w.inicio_utc) + " → " + when(w.fim_utc), w.confianca_do_horario, w.motivo]), 8, "Nenhuma janela ativa.");
  fill("tb-macro-proximos", (m.proximos || []).map((e) => [e.nome, String(e.importancia), when(e.horario_utc), e.horario_local + " " + e.fuso_original, fmt(e.faltam_horas, 1), e.confianca_do_horario, e.resultado]), 7, "Nenhum evento no calendário para o período.");
  fill("tb-macro-divulgados", (m.divulgados || []).map((v) => [v.indicador, v.periodo, when(v.divulgado_em), fmt(v.valor_divulgado, 2), isNum(v.expectativa) ? fmt(v.expectativa, 2) : "N/D (a fonte não fornece)", fmt(v.anterior, 2),
    isNum(v.anterior_revisado) ? fmt(v.anterior_revisado, 2) : "—", v.unidade || "", v.nota || "—"]), 9, "Nenhum valor divulgado coletado ainda.");
  fill("tb-macro-mercado", (m.mercado || []).map((x) => [x.indicador, x.data, fmt(x.valor, 3), fmt(x.anterior, 3), fmt(x.variacao_bps, 1), when(x.disponivel_em)]), 6, "Sem dados de mercado.");
  const ul = $("macro-pendentes"); ul.replaceChildren();
  for (const t of m.pendentes || []) ul.append(el("li", null, t));
}
function renderIntel(js) {
  $("intel-modo").textContent = "— modo: " + js.modo;
  $("intel-aviso").textContent = js.aviso || "";
  const box = $("intel-msg");
  box.textContent = js.mensagem + (js.ultima_atualizacao ? "  Última atualização das fontes: " + when(js.ultima_atualizacao) + "." : "") + ((js.observacoes || []).length ? "  Obs.: " + js.observacoes.join("; ") : "");
  box.className = "intel-msg " + (js.situacao === "fontes_indisponiveis" ? "bad" : js.situacao === "alertas" ? "warn" : js.situacao === "nenhum_evento" ? "ok" : "");
  const sa = js.saude || {};
  const sbox = $("intel-saude");
  sbox.textContent = "Saúde do módulo: " + String(sa.geral || ND).toUpperCase() + " — " + (sa.mensagem || "");
  sbox.className = "intel-msg " + (SAUDE_CLASSE[sa.geral] || "");
  const cbox = $("intel-conting");
  const ct = js.contingencia;
  if (ct) {
    cbox.hidden = false;
    cbox.textContent = "CONTINGÊNCIA — fonte(s) " + (ct.fontes || []).join(", ") + " indisponível(is) há " + fmt(ct.horas, 1) + " h (desde " + when(ct.desde) + "; limite " + fmt(ct.limite_h, 1) + " h). Política aplicada: " + ct.politica + ". "
      + (ct.mensagem_politica || "") + " Decisões de entrada afetadas desde então: " + (isNum(ct.decisoes_afetadas) ? ct.decisoes_afetadas : ND) + ".";
  } else { cbox.hidden = true; cbox.textContent = ""; }
  fill("tb-intel-fontes", (js.fontes || []).map((f) => [f.name, DOMINIO[f.domain] || f.domain || ND, f.critical ? "SIM" : "não", ESTADO_FONTE[f.state] || f.state, f.state === "ok" || f.state === "disabled" ? "—" : when(f.down_since),
    when(f.last_ok_at), when(f.last_attempt_at), String(f.consecutive_failures || 0),
    [f.error_kind || "", f.http_status ? "HTTP " + f.http_status : "", isNum(f.duration_ms) ? f.duration_ms + " ms" : ""].filter(Boolean).join(" / ") || "—",
    [f.last_error || "", f.recovering ? "recuperando " + f.recovering : "", f.retry_at ? "nova tentativa após " + when(f.retry_at) : ""].filter(Boolean).join(" | ") || "—"]), 10,
    js.modo === "off" ? "Camada desligada." : "Nenhuma fonte configurada (veja config/intel_sources.json).");
  fill("tb-intel-alertas", (js.alertas || []).map((a) => [a.impacto, a.categoria + (a.ambiente && a.ambiente !== "desconhecido" ? " [" + a.ambiente + "]" : ""), (a.tokens || [a.ativo]).join(", "), a.certeza, String(a.confiabilidade),
    String(a.fontes_distintas) + (a.independencia === "incerta" ? " (independência INCERTA)" : ""), el("td", null, linkOrText(a.link, a.titulo)), when(a.expira_em), a.bloqueia ? "SIM" : "não", a.motivo]), 10,
    js.situacao === "fontes_indisponiveis" ? "Sem dados confiáveis (fontes indisponíveis/desatualizadas)." : "Nenhum alerta ativo.");
  fill("tb-intel-noticias", (js.noticias || []).map((n) => [when(n.publicado_em), when(n.coletado_em), n.fonte, el("td", null, linkOrText(n.link, n.titulo)), (n.ativos || []).join(", ") || "—",
    n.categoria + (n.indicador ? " (" + n.indicador + ")" : ""), n.classificacao + (n.desmentido ? " (desmentido/correção)" : ""), n.certeza + " / " + (n.natureza || ND), String(n.confiabilidade), n.impacto, (n.sinalizadores || []).join(", ") || "—"]), 11, "Nenhuma notícia coletada ainda.");
  fill("tb-intel-decisoes", (js.decisoes || []).map((d) => [when(d.ts), d.simbolo, d.tecnico, d.modo, d.acao, isNum(d.escala_de_risco) ? num(d.escala_de_risco, 2) : ND, d.divergencia || "—", (d.justificativa || []).join(" | ")]), 8,
    "Nenhuma decisão influenciada/avaliada ainda.");
  fill("tb-intel-eventos", (js.eventos || []).map((e) => [when(e.ts), e.tipo, e.fonte || "—", e.gravidade, e.detalhe]), 5, "Nenhum evento de saúde registrado.");
  renderMacro(js.macro);
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
    try { renderIntel(await api("/api/intel?limite=50")); } catch (e) { $("intel-msg").textContent = "Não foi possível ler a camada de notícias: " + e.message; $("intel-msg").className = "intel-msg bad"; }
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
