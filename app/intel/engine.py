"""Motor de decisão da News & Market Intelligence.

PRINCÍPIO DE SEGURANÇA (garantido por testes): a saída só pode RESTRINGIR.
  * `block`  => impede NOVAS ENTRADAS (nunca fecha posições, nunca mexe em stops);
  * `risk_scale` ∈ (0, 1] => só REDUZ o risco por operação;
  * nada aqui compra, vende, aumenta exposição, altera limites, kill switch, modo ou configuração.
Notícias positivas jamais geram compra. O texto das notícias é dado, nunca instrução.

Separação por domínio: solana_status (rede), crypto_news (incidentes, segurança, regulação) e macro_us (calendário/indicadores dos EUA);
o motor de RISCO existente só recebe `block` e `risk_scale`."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from app.config import IntelParams
from app.intel.classifier import CERT_FACTOR
from app.intel.dedup import canonical_url, jaccard, registrable_domain, text_tokens, title_tokens
from app.intel.macro import MacroEngine
from app.intel.models import IMPACT_NAMES, ActiveAlert, IntelAssessment, NewsRecord, SourceState, parse_iso
from app.intel.store import NewsView

log = logging.getLogger("intel.engine")

# Validade (h) de cada categoria: após isso o sinal expira (depois ainda há a janela de cautela).
TTL_H = {"exploit_hack": 24.0, "insolvencia": 48.0, "incidente_rede": 12.0, "exclusao_listagem": 24.0, "regulacao": 24.0,
         "etf_institucional": 24.0, "desbloqueio_tokens": 24.0, "listagem": 12.0, "macro": 1.0, "mercado_geral": 6.0}
AMPLIFY = frozenset({"exploit_hack", "insolvencia", "exclusao_listagem"})  # incidente_rede já traz severidade/ambiente do classificador
_STATE_ORDER = {"unavailable": 3, "invalid": 2, "stale": 1}
_STATE_LABEL = {"stale": "DESATUALIZADAS", "unavailable": "INDISPONÍVEIS", "invalid": "COM RESPOSTA INVÁLIDA", "disabled": "NÃO CONFIGURADAS", "contingency": "EM CONTINGÊNCIA"}


def relevance_for(rec: NewsRecord, symbol: str) -> float:
    if symbol in rec.assets:
        return 1.0
    if "SOL" in rec.assets:
        return 0.8  # ecossistema Solana (rede, incidentes): afeta todos os tokens da allowlist
    if "BTC" in rec.assets or "MARKET" in rec.assets:
        return 0.4
    return 0.3 if rec.category == "macro" else 0.0


def scope_for(rec: NewsRecord, symbol: str) -> str:
    if symbol in rec.assets:
        return "ativo"
    if "SOL" in rec.assets:
        return "SOL"
    if "BTC" in rec.assets or "MARKET" in rec.assets:
        return "mercado"
    return "macro"


def impact_for(rec: NewsRecord, rel: float, params: IntelParams | None = None) -> int:
    imp = rec.impact
    if rec.category in AMPLIFY:
        if rel >= 0.8:
            imp += 1
        elif rel < 0.5:
            imp -= 1
    elif rec.category == "incidente_rede":
        if rel < 0.5:
            imp -= 1
        elif params is not None and params.mainnet_degraded_policy == "block" and rec.environment in ("mainnet", "desconhecido") and imp == 3 and rec.severity == 3:
            imp = 4  # política explícita: degradação da mainnet tratada como crítica
    return max(0, min(4, imp))


def source_reliability(rec: NewsRecord) -> float:
    return min(1.0, rec.reliability / CERT_FACTOR.get(rec.certainty, 1.0))


def collapse_versions(recs: list[NewsRecord]) -> list[NewsRecord]:
    """Fica só com a ÚLTIMA versão VISÍVEL de cada incidente (mesma event_key). Calculado no instante da decisão: não depende de nada futuro."""
    best: dict[str, NewsRecord] = {}
    out: list[NewsRecord] = []
    for r in recs:
        if r.event_key is None:
            out.append(r)
            continue
        cur = best.get(r.event_key)
        if cur is None or (r.collected_dt, r.event_dt or r.collected_dt) >= (cur.collected_dt, cur.event_dt or cur.collected_dt):
            best[r.event_key] = r
    return out + list(best.values())


def independent_origins(recs: list[NewsRecord], p: IntelParams) -> tuple[int, bool, list[str]]:
    """Agrupa itens que são REPUBLICAÇÃO/derivação da mesma matéria. Retorna (nº de origens independentes, independência_incerta, notas).
    Dois veículos que reproduzem a mesma notícia contam UMA vez. Se não dá para afirmar independência (similaridade intermediária, ou ambos citam uma
    origem genérica como "um tweet"), `incerta=True` e a corroboração NÃO deve ser afirmada."""
    n = len(recs)
    parent = list(range(n))
    notes: list[str] = []
    uncertain = False

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    tt = [title_tokens(r.title) for r in recs]
    xt = [text_tokens(r.title, r.summary) for r in recs]
    canon = [canonical_url(r.url) for r in recs]
    dom = [registrable_domain(r.url) for r in recs]
    pending: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = recs[i], recs[j]
            if a.source_id == b.source_id or dom[i] == dom[j]:
                union(i, j)
                continue
            if jaccard(tt[i], tt[j]) >= p.syndication_title_jaccard or jaccard(xt[i], xt[j]) >= p.syndication_text_jaccard:
                union(i, j)
                notes.append("títulos/textos muito parecidos: republicação da mesma matéria")
                continue
            if a.origin_key and a.origin_key == b.origin_key:
                union(i, j)
                if a.origin_generic and b.origin_generic:
                    uncertain = True
                    notes.append("ambos citam a mesma origem genérica (ex.: um tweet/fontes anônimas): independência não confirmável")
                else:
                    notes.append(f"ambos citam a mesma origem ('{a.origin_key}')")
                continue
            if canon[i] in b.refs or canon[j] in a.refs or (set(a.refs) & set(b.refs)):
                union(i, j)
                notes.append("uma matéria cita o link da outra (ou ambas citam a mesma página)")
                continue
            na, nb = (a.origin_key or ""), (b.origin_key or "")
            if (na and (na in re.sub(r"[^a-z0-9]+", " ", b.source_name.lower()) or na.replace(" ", "") in dom[j].replace(".", ""))) or \
               (nb and (nb in re.sub(r"[^a-z0-9]+", " ", a.source_name.lower()) or nb.replace(" ", "") in dom[i].replace(".", ""))):
                union(i, j)
                notes.append("uma matéria diz ter origem na outra fonte")
                continue
            pending.append((i, j))
    for i, j in pending:  # similaridade intermediária entre componentes DIFERENTES: não dá para afirmar independência
        if find(i) != find(j) and max(jaccard(tt[i], tt[j]), jaccard(xt[i], xt[j])) >= p.independence_uncertain_jaccard:
            uncertain = True
            notes.append("similaridade intermediária entre matérias: pode ser derivação; independência incerta")
    groups = len({find(i) for i in range(n)})
    return groups, uncertain, sorted(set(notes))



class IntelEngine:
    def __init__(self, params: IntelParams, view: NewsView, macro: MacroEngine | None = None):
        self.p, self.view, self.macro = params, view, macro

    # ------------------------------------------------------------------ estado dos dados
    def data_state(self, now: datetime) -> tuple[str, list[str], dict | None]:
        """(estado, notas, contingência). Fontes CRÍTICAS decidem o estado: uma fonte secundária saudável NÃO esconde uma crítica fora do ar.
        Estado 'contingency' = o problema persiste por mais que `contingency_after_s` (nunca libera operações sozinho)."""
        states = self.view.source_states(now)
        enabled = [s for s in states if s.state != "disabled"]
        if not enabled:
            return "disabled", ["nenhuma fonte de notícias habilitada"], None
        crit_bad = [s for s in enabled if s.critical and s.state != "ok"]
        ok = [s for s in enabled if s.state == "ok"]

        def label(s: SourceState) -> str:
            extra = f" ({s.last_error})" if s.last_error else ""
            rec = f" [recuperando {s.recovering}]" if s.recovering else ""
            return f"fonte {s.name}: {s.state}{extra}{rec}"

        notes = [label(s) for s in enabled if s.state != "ok"]
        if crit_bad:
            bad_set = crit_bad
            state = max((s.state for s in crit_bad), key=lambda x: _STATE_ORDER.get(x, 0))
        elif len(ok) >= self.p.min_sources_ok:
            return "ok", notes, None
        else:
            bad_set = [s for s in enabled if s.state != "ok"]
            state = max((s.state for s in bad_set), key=lambda x: _STATE_ORDER.get(x, 0))
        # Contingência exige EVIDÊNCIA de falhas reais (tentativas que falharam), não apenas "dados velhos": um robô que ficou desligado por horas
        # não entra em contingência (nem libera entradas por uma política de contingência) só porque a última coleta bem-sucedida é antiga.
        failing = [s for s in bad_set if s.consecutive_failures >= 3 or s.recovering]
        since = min((parse_iso(s.down_since) for s in failing if parse_iso(s.down_since)), default=None)
        cont = None
        if since is not None and (now - since).total_seconds() >= self.p.contingency_after_s:
            cont = {"desde": since.isoformat(), "horas": round((now - since).total_seconds() / 3600, 2), "politica": self.p.contingency_policy,
                    "fontes": [s.name for s in failing], "limite_h": round(self.p.contingency_after_s / 3600, 2)}
            state = "contingency"
        return state, notes, cont

    def health(self, now: datetime) -> dict:
        """Saúde do módulo para o painel: nunca 'saudável' se uma fonte crítica não estiver ok."""
        states = self.view.source_states(now)
        enabled = [s for s in states if s.state != "disabled"]
        state, notes, cont = self.data_state(now)
        crit_bad = [s.name for s in enabled if s.critical and s.state != "ok"]
        other_bad = [s.name for s in enabled if not s.critical and s.state != "ok"]
        if not enabled:
            overall, msg = "desligado", "Nenhuma fonte habilitada."
        elif cont:
            overall, msg = "contingencia", f"CONTINGÊNCIA: {', '.join(cont['fontes'])} fora há {cont['horas']:g} h (limite {cont['limite_h']:g} h). Política: {cont['politica']}."
        elif crit_bad:
            overall, msg = "critico", f"FONTE CRÍTICA com problema: {', '.join(crit_bad)}. O módulo NÃO está saudável."
        elif other_bad or state != "ok":
            overall, msg = "degradado", f"Fontes secundárias com problema: {', '.join(other_bad) or '—'}."
        else:
            overall, msg = "saudavel", "Todas as fontes habilitadas estão saudáveis."
        return {"geral": overall, "mensagem": msg, "estado_dados": state, "criticas_com_problema": crit_bad, "secundarias_com_problema": other_bad, "contingencia": cont, "notas": notes}

    def _closing_time(self, closer: NewsRecord, target: NewsRecord) -> datetime | None:
        """Instante em que `closer` encerra `target`, ou None se `closer` não tem credibilidade/compatibilidade para isso."""
        if source_reliability(closer) < self.p.min_reliability_for_block:
            return None  # uma fonte de baixa confiança não levanta um bloqueio crítico
        low = ("devnet", "testnet", "explorer", "site")
        if target.category == "incidente_rede" and closer.environment != target.environment and "desconhecido" not in (closer.environment, target.environment):
            return None  # ambientes diferentes (ex.: devnet resolvido x mainnet ativo)
        if target.category == "incidente_rede" and closer.environment in low and target.environment not in low:
            return None
        return closer.event_dt or closer.published_dt

    # ------------------------------------------------------------------ avaliação
    def assess(self, symbol: str, now: datetime, technical: str = "BUY") -> IntelAssessment:
        p = self.p
        state, state_notes, cont = self.data_state(now)
        out = IntelAssessment(mode=p.mode, data_state=state, no_events=False, contingency=cont)
        out.rationale.append(f"estado das fontes: {state}" + (" — " + "; ".join(state_notes) if state_notes else ""))

        groups: dict[tuple[str, str], list[tuple[NewsRecord, float]]] = {}
        for rec in collapse_versions(self.view.records(now)):
            if not rec.actionable or rec.category == "outros" or rec.published_dt is None or rec.published_dt > now:
                continue
            if rec.category == "macro" and rec.nature != "fato":
                continue  # expectativa/opinião/especulação sobre macro NÃO é evento (o calendário cobre o risco de eventos futuros)
            rel = relevance_for(rec, symbol)
            if rel > 0:
                groups.setdefault((rec.category, scope_for(rec, symbol)), []).append((rec, rel))

        caution_win = timedelta(seconds=p.caution_window_s)
        for (cat, scope), items in groups.items():
            ttl = timedelta(hours=TTL_H.get(cat, 6.0))
            neg = [(r, rel) for r, rel in items if ((r.polarity in ("negative", "inconclusive") and not r.resolution and not r.retraction) or r.closed_episode)]
            if not neg:
                continue
            closer_recs = [c for c, _ in items if not c.closed_episode and c.certainty == "confirmed" and (c.resolution or c.retraction)]
            considered: list[tuple[NewsRecord, float, datetime | None]] = []  # (item, relevância, instante de resolução)
            for r, rel in neg:
                start = r.published_dt if r.closed_episode else (r.event_dt or r.published_dt)
                assert start is not None
                # Quem pode ENCERRAR um evento: texto externo NÃO é comando de liberação. Exige fonte confiável (≥ min_reliability_for_block) e ambiente
                # compatível (um aviso de "resolvido" da devnet/explorer não fecha um incidente da mainnet).
                res_candidates = [t for t in (self._closing_time(c, r) for c in closer_recs) if t is not None and t >= (r.published_dt or start)]
                if r.closed_episode and r.event_dt:
                    res_candidates.append(max(r.event_dt, r.published_dt or r.event_dt))
                res_at = min(res_candidates) if res_candidates else None
                if now >= (res_at or (start + ttl)) + caution_win:
                    continue  # expirou
                considered.append((r, rel, res_at))
            if not considered:
                continue
            open_items = [(r, rel) for r, rel, res in considered if res is None]
            if open_items:
                last = max((r.event_dt or r.published_dt) for r, _ in open_items)  # type: ignore[type-var]
                block_until = last + ttl
                resolved_at = None
            else:
                resolved_at = max(res for _, _, res in considered if res is not None)
                block_until = resolved_at
            caution_until = block_until + caution_win
            pool = open_items or [(r, rel) for r, rel, _ in considered]
            impact = max(impact_for(r, rel, p) for r, rel in pool)
            if impact == 0:
                continue
            nonrumor = [r for r, _ in pool if r.certainty != "rumor" and "similar_antigo" not in r.flags and source_reliability(r) >= 0.5]
            n_origins, uncertain, ind_notes = independent_origins(nonrumor, p) if nonrumor else (0, False, [])
            confirmed_ok = any(r.certainty == "confirmed" and source_reliability(r) >= p.min_reliability_for_block and "similar_antigo" not in r.flags for r, _ in pool)
            corroborated = n_origins >= p.corroboration_min_sources and not uncertain
            best = max(pool, key=lambda x: (x[0].reliability, x[0].published_dt))[0]
            certainty = "confirmed" if any(r.certainty == "confirmed" for r, _ in pool) else ("claim" if any(r.certainty == "claim" for r, _ in pool) else "rumor")
            reliability = min(1.0, best.reliability + 0.1 * max(0, n_origins - 1))
            blocks = impact == 4 and (confirmed_ok or corroborated) and resolved_at is None and now < block_until
            if uncertain and not confirmed_ok:
                independence = "incerta"
            elif corroborated:
                independence = "confirmada"
            else:
                independence = "n/a"
            if blocks:
                why = "fato confirmado por fonte confiável" if confirmed_ok else f"corroborado por {n_origins} origens independentes"
                reason = f"evento crítico ({cat}) {why}; bloqueio até {block_until.isoformat(timespec='minutes')}"
            elif impact == 4:
                extra = ""
                if uncertain:
                    extra = " (a INDEPENDÊNCIA entre as matérias é incerta: não conta como confirmação independente)"
                reason = f"evento potencialmente crítico ({cat}), mas {'resolvido' if resolved_at else 'sem confirmação/corroboração suficiente'}{extra}: apenas cautela"
            else:
                reason = f"evento de impacto {IMPACT_NAMES[impact]} ({cat})" + (" — resolvido, janela de cautela" if resolved_at else "")
            if cat == "incidente_rede":
                reason += f" [ambiente: {best.environment}]"
            if ind_notes and len(nonrumor) > 1:
                reason += " | " + "; ".join(ind_notes[:2])
            out.alerts.append(ActiveAlert(cat, scope if scope != "ativo" else symbol, impact, certainty, reliability, n_origins, best.title, best.url, best.published_at,
                                          caution_until.isoformat(), blocks, reason, domain=best.domain, independence=independence, environment=best.environment))

        out.alerts.sort(key=lambda a: (-a.impact, a.category))
        for a in out.alerts:
            if a.blocks:
                out.block = True
                out.block_reasons.append(a.reason)
            if a.impact >= 3:
                out.caution = True
                out.caution_reasons.append(a.reason)
        if technical == "BUY":
            neg_alerts = [a for a in out.alerts if a.impact >= 2 and a.category not in ("macro",)]
            if neg_alerts:
                out.divergence = f"indicadores técnicos sinalizam COMPRA, mas há notícia negativa relevante: {neg_alerts[0].title[:120]}"
        out.risk_scale = max(1e-6, min(1.0, p.caution_risk_multiplier if out.caution else 1.0))

        # ---- macroeconomia (janelas antes/depois de divulgações; só restringe)
        if self.macro is not None:
            try:
                m = self.macro.assess(now)
            except Exception:  # noqa: BLE001 - erro do módulo macro: aplica CAUTELA e mantém as restrições de notícias já calculadas
                log.exception("erro na avaliação macro")
                out.risk_scale = max(1e-6, min(out.risk_scale, p.macro_window_risk_multiplier))
                out.caution = True
                out.caution_reasons.append("erro interno na camada macro (cautela aplicada por segurança)")
                out.rationale.append("erro interno na camada macro; ver logs")
                m = None
            if m is not None:
                out.macro = m.to_public()
                out.alerts.extend(m.alerts)
                out.risk_scale = max(1e-6, min(out.risk_scale, m.risk_scale))
                if m.risk_scale < 1.0:
                    out.caution = True
                    out.caution_reasons += m.reasons
                if m.block:
                    out.block = True
                    out.block_reasons += m.block_reasons

        out.no_events = state == "ok" and not out.alerts

        # ---- fontes indisponíveis / desatualizadas / inválidas / contingência
        if state != "ok":
            msg = "fontes de notícias " + _STATE_LABEL.get(state, state.upper())
            if cont:
                msg += f" há {cont['horas']:g} h (CONTINGÊNCIA; limite {cont['limite_h']:g} h; fontes: {', '.join(cont['fontes'])})"
            if state == "contingency" and p.contingency_policy == "reduced_risk":
                # política EXPLÍCITA do operador: libera entradas COM risco reduzido (nunca acima do normal). Registrada na justificativa.
                out.risk_scale = max(1e-6, min(out.risk_scale, p.contingency_risk_multiplier))
                out.rationale.append(msg + f" — POLÍTICA DE CONTINGÊNCIA 'reduced_risk' (configurada explicitamente): entradas liberadas com risco × {p.contingency_risk_multiplier:g}")
                out.caution_reasons.append("contingência com risco reduzido (política explícita)")
                out.caution = True
            elif p.unavailable_policy == "block_entries":
                out.block = True
                out.block_reasons.append(msg + " (política: bloquear novas entradas" + ("; contingência: restrição mantida, nada é liberado pela passagem do tempo" if cont else "") + ")")
            else:
                out.rationale.append(msg + " (política warn_only: sem restrição" + ("; CONTINGÊNCIA registrada" if cont else "") + ")")
        if out.no_events:
            out.rationale.append("nenhum evento relevante detectado (fontes OK)")
        out.rationale += [f"alerta: {a.reason}" for a in out.alerts]
        if out.divergence:
            out.rationale.append("divergência: " + out.divergence)
        if out.caution and not out.block:
            out.rationale.append(f"cautela: risco por operação multiplicado por {out.risk_scale:g}")
        return out
