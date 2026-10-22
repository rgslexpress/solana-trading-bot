"""Classificador por REGRAS transparentes (sem ML e sem pontuação de sentimento de terceiros). Versão 2.

Cada decisão registra seus motivos. A classificação é uma estimativa heurística e pode errar (ver docs/NEWS_INTEL.md, "Limitações");
por isso o motor só usa o resultado para RESTRINGIR operações, e bloqueios exigem fato confirmado ou corroboração independente.

Princípios desta versão (correções da auditoria de 2026-10-22):
  * Resolução de incidente só vale com ESTADO EXPLÍCITO: o estado estruturado da página de status ("Resolved - ...") ou uma frase inequívoca
    no passado/presente ("incident resolved", "service restored"), nunca frases condicionais/futuras ("until the issue is resolved", "will be resolved soon").
  * Negações, desmentidos e correções ("not hacked", "no outage", "denies hack", "false report", "correction") NÃO geram evento negativo.
  * Palavras genéricas ("identified", "investigating") não bastam para definir incidente.
  * Ambiente e severidade: devnet/testnet/explorer/site têm impacto baixo; a mainnet com parada total é crítica; degradação é "alta" (cautela).
  * Datas citadas no texto ("since 2022") NÃO descartam notícia: só a data de publicação/atualização conta.
  * Macro (EUA): reconhece nomes por extenso e abreviações e separa FATO anunciado de EXPECTATIVA, OPINIÃO e ESPECULAÇÃO."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.config import IntelParams
from app.intel.dedup import canonical_url, content_fingerprint, item_id
from app.intel.models import NewsRecord, RawItem
from app.intel.sources import SourceConfig

# ---------------------------------------------------------------------------------------------------------- vocabulário
_EVENT = (r"(?:hack(?:ed|s|ers?|ing)?|exploit(?:ed|s|ing)?|drain(?:ed|s|ing)?|breach(?:ed|es)?|attack(?:ed|s)?|outages?|downtime|halt(?:ed|s)?|stall(?:ed)?|"
          r"insolven\w+|bankrupt\w*|stolen|theft|rug ?pull\w*|compromis\w+|delist\w*|incidents?|vulnerabilit(?:y|ies)|phishing)")
_W = r"(?:\w+['’]?\w*\s+)"
# Negação/desmentido: o trecho casado é REMOVIDO antes de procurar eventos (o texto restante é classificado normalmente).
_NEGATIONS = [
    re.compile(rf"\b(?:no|not|never|zero|without|nor)\s+{_W}{{0,3}}?{_EVENT}\b", re.I),
    re.compile(rf"\b(?:isn['’]?t|wasn['’]?t|weren['’]?t|aren['’]?t|didn['’]?t|did\s+not|was\s+not|is\s+not|were\s+not|are\s+not|has\s+not\s+been|have\s+not\s+been|hasn['’]?t\s+been|haven['’]?t\s+been)\s+{_W}{{0,3}}?{_EVENT}\b", re.I),
    re.compile(rf"\b(?:denies|denied|deny|denying|dismiss(?:es|ed)|refut(?:es|ed)|debunk(?:s|ed)|reject(?:s|ed)|disputes?|disputed|rebuffs?|rebuffed|desmente\w*|desmentiu)\s+{_W}{{0,6}}?{_EVENT}\b", re.I),
    re.compile(rf"\b{_EVENT}\s+{_W}{{0,4}}?(?:denied|dismissed|refuted|debunked|rejected|disputed|false|untrue|unfounded|baseless|wrong|incorrect|fake|not\s+true|never\s+happened)\b", re.I),
    re.compile(rf"\b(?:false|fake|untrue|unfounded|baseless|incorrect|erroneous|mistaken|bogus|falso|falsa)\s+{_W}{{0,2}}?(?:reports?|claims?|rumou?rs?|alerts?|news|stor(?:y|ies)|posts?|information|alarm\w*|notícia|boato)\b", re.I),
    re.compile(rf"\b(?:reports?|claims?|rumou?rs?|alerts?|stor(?:y|ies)|posts?)\s+{_W}{{0,2}}?(?:(?:are|is|were|was)\s+)?(?:false|untrue|unfounded|baseless|fake|wrong|incorrect|mistaken)\b", re.I),
    re.compile(r"\b(?:não|nunca|sem)\s+(?:\w+\s+){0,2}?(?:hack\w*|ataque|invas\w+|roubo|queda|falência|exploit\w*)\b", re.I),
    re.compile(r"\bfalso\s+alarme\b|\bfalse\s+alarm\b", re.I),
]
_RETRACTION = re.compile(r"\b(?:correction|corrected|retract\w*|withdraws?\s+(?:the\s+)?(?:report|claim|story)|retrac\w+|retificação|errata)\b", re.I)

# categoria -> (regex, impacto base 0-4, polaridade padrão: neg | pos | None)
_CAT: dict[str, tuple[re.Pattern[str], int, str | None]] = {
    "exploit_hack": (re.compile(r"\b(hack(?:ed|s|ers?)?|exploit(?:ed|s)?|drain(?:ed|s)?|stolen|theft|breach(?:ed)?|rug ?pull|compromised|vulnerabilit(?:y|ies)|attack(?:ed|s)?|phishing|invadid[oa]s?|roubo)\b", re.I), 3, "neg"),
    "insolvencia": (re.compile(r"\b(insolven\w+|bankrupt\w*|chapter 11|(?:halts?|halted|pauses?|paused|suspend(?:s|ed)?)\s+(?:all\s+)?withdrawals?|withdrawals?\s+(?:halted|paused|suspended)|falência|insolvência)\b", re.I), 3, "neg"),
    # incidente de rede: exige vocabulário de incidente. "investigating"/"identified" sozinhos NÃO bastam.
    "incidente_rede": (re.compile(
        r"\b(?:(?:major|partial|full|total|complete)\s+outage|outage|downtime|network\s+(?:halt|stall|down|congestion|instability|disruption)|"
        r"(?:mainnet|cluster|network|chain)\W+(?:\w+\W+){0,3}?(?:halt\w*|stall\w*|down|offline|restart\w*|degraded|unavailable)|"
        r"degraded\s+performance|performance\s+degradation|service\s+(?:disruption|interruption)|consensus\s+(?:failure|bug|halt)|"
        r"validators?\s+(?:offline|down|not\s+producing)|block\s+production\s+(?:halt\w*|stopped|stalled|paused)|(?:rpc|api)\s+(?:outage|errors?|degraded|unavailable)|"
        r"instabilidade\s+na\s+rede|queda\s+da\s+rede|rede\s+(?:parada|fora\s+do\s+ar))\b", re.I), 3, "neg"),
    "exclusao_listagem": (re.compile(r"\b(delist\w*|removes?\s+\w+\s+from\s+trading|trading\s+suspended)\b", re.I), 3, "neg"),
    "regulacao": (re.compile(r"\b(lawsuit|sues?|sued|crackdown|enforcement|subpoena|charges?\s+against|regulat\w+|ban(?:s|ned)?\s+(?:crypto|trading|exchange)\w*)\b|\b(?:SEC|CFTC)\b", re.I), 2, None),
    "etf_institucional": (re.compile(r"\b(spot\s+ETF|ETFs?|BlackRock|Fidelity|Grayscale|institutional)\b", re.I), 2, None),
    "desbloqueio_tokens": (re.compile(r"\b(token\s+unlock\w*|unlock(?:s|ed|ing)?|vesting|cliff|token\s+release)\b", re.I), 2, "neg"),
    "listagem": (re.compile(r"\b(will\s+list|lists?\s+\w+|listing|adds?\s+support\s+for)\b", re.I), 1, "pos"),
}
_CRYPTO_CATS = frozenset(_CAT)  # categorias que exigem contexto cripto quando a fonte não é oficial/de status
_CRYPTO_CTX = re.compile(r"\b(crypto\w*|token|defi|dex|swap|solana|blockchain|airdrop|wallet|on-?chain|stablecoin|validator|memecoin|bitcoin|ethereum|main\s*net|dev\s*net|test\s*net)\b", re.I)

# ---- macro (EUA): indicador -> (regex, impacto, nome). A ordem importa (o mais específico primeiro).
_FED = r"(?:fed\b|federal\s+reserve|fomc|federal\s+open\s+market)"
_INDICATORS: list[tuple[str, re.Pattern[str], int]] = [
    ("FOMC_MINUTES", re.compile(r"\bminutes\s+of\s+the\s+(?:federal\s+open\s+market\s+committee|fomc)\b|\b(?:fomc|federal\s+open\s+market\s+committee)\s+minutes\b|\bminutes\b.{0,40}\b(?:federal\s+open\s+market|fomc)\b|\bminutes\b.{0,60}\bdiscount\s+rate\b", re.I), 2),
    ("FED_PRESSER", re.compile(rf"\bpress\s+conference\b(?=.*{_FED})|{_FED}.*\bpress\s+conference\b", re.I), 3),
    ("FOMC_DECISION", re.compile(
        r"\bfomc\b|\bfederal\s+open\s+market\s+committee\b|\bfederal\s+funds\s+rate\b|\bfed\s+funds\b|\btarget\s+range\b|\binterest[-\s]rate\s+decision\b|\brate\s+decisions?\b|"
        r"\brate\s+(?:hikes?|cuts?|increases?|decreases?)\b|\b(?:raises?|lowers?|cuts?|holds?|keeps?|maintains?|leaves?|hikes?)\s+(?:the\s+)?(?:interest\s+)?(?:federal\s+funds\s+)?(?:target\s+)?(?:range|rates?)\b|"
        r"\bmonetary\s+policy\s+statement\b|\bdiscount\s+rate\b|\bsummary\s+of\s+economic\s+projections\b|\bdot\s+plot\b|\btaxa\s+básica\b|\bjuros\s+nos\s+eua\b", re.I), 3),
    ("FED_SPEECH", re.compile(rf"\b(?:speech|testimony|remarks)\b(?=.*{_FED})|{_FED}.*\b(?:speech|testimony|remarks)\b|\bfed\s+(?:chair|vice\s+chair|governor)\w*\b|\bjackson\s+hole\b", re.I), 2),
    ("CORE_CPI", re.compile(r"\bcore\s+(?:cpi|consumer\s+price)", re.I), 3),
    ("CPI", re.compile(r"\bconsumer\s+price\s+index\b|\bcpi\b|\binflation\s+(?:report|data|rate|reading)\b", re.I), 3),
    ("CORE_PCE", re.compile(r"\bcore\s+pce\b", re.I), 3),
    ("PCE", re.compile(r"\bpersonal\s+consumption\s+expenditures?\b|\bpce\b|\bpersonal\s+income\s+and\s+outlays\b", re.I), 3),
    ("PPI", re.compile(r"\bproducer\s+price\b|\bppi\b", re.I), 2),
    ("NFP", re.compile(r"\bnon-?farm\b|\bpayrolls?\b", re.I), 3),
    ("UNEMPLOYMENT", re.compile(r"\bunemployment\s+rate\b", re.I), 3),
    ("EMPLOYMENT_SITUATION", re.compile(r"\bemployment\s+situation\b|\bjobs\s+report\b", re.I), 3),
    ("JOBLESS_CLAIMS", re.compile(r"\bjobless\s+claims\b|\binitial\s+claims\b|\bcontinuing\s+claims\b|\bunemployment\s+insurance\s+weekly\s+claims\b", re.I), 2),
    ("JOLTS", re.compile(r"\bjolts\b|\bjob\s+openings\b", re.I), 2),
]
_MACRO_GENERIC = re.compile(r"\b(?:monetary\s+policy|interest[-\s]rates?|inflation|inflação|juros|beige\s+book)\b", re.I)

# ---- natureza da afirmação
_EXPECT_FORWARD = re.compile(
    r"\b(?:ahead\s+of|what\s+to\s+(?:expect|watch)|anticipat\w+|projected|poised\s+to|set\s+to|likely\s+to|could|may|might|odds|probabilit\w+|"
    r"priced?\s+in|pricing\s+in|bets?|betting|traders?\s+(?:see|bet|expect)|(?:going\s+)?to\s+(?:cut|raise|hike|lower|hold|keep|announce|release|publish|decide)|"
    r"will\s+(?:cut|raise|hike|lower|hold|keep|announce|release|publish|decide)|upcoming|next\s+(?:week|meeting)|tomorrow|this\s+week|preview|expectativa\w*|previsão|"
    r"deve\s+(?:cortar|subir|manter)|(?:is|are|was|were)\s+expected\s+to|expects?\s+to|expected\s+(?:to|in|on|at|by)\b|(?:markets?|analysts?|economists?|traders?)\s+expect\w*|forecast\s+to)\b", re.I)
_EXPECT_WORD = re.compile(r"\b(?:expect\w*|forecast\w*|consensus)\b", re.I)
_RESULT_VS_EXPECT = re.compile(r"\b(?:beat|beats|miss(?:ed|es)?|topped|exceed(?:ed|s)?|above|below|than\s+expected|versus|vs\.?|in\s+line\s+with|came\s+in)\b", re.I)
_OPINION = re.compile(r"\b(?:analysts?|strategists?|economists?\s+(?:say|says|think|believe)|opinion|commentary|we\s+(?:think|believe)|i\s+(?:think|believe)|outlook|predict\w*|argues?|warns?\s+that|in\s+my\s+view)\b", re.I)
_ANNOUNCE = re.compile(r"\b(?:announce[sd]?|releases?d?|reported|decid(?:es|ed)|voted|votes|raised|lowered|cut|held|maintain(?:s|ed)|kept|left\s+unchanged|rose|fell|increased|decreased|unchanged|comes?\s+in|came\s+in|issues?d?|approv\w+|confirm\w*|anunciou|divulgou|manteve|raises?|lowers?|cuts?|holds?|keeps?|leaves?|hikes?|maintains?|beats?|misses|missed|tops|topped|exceeds?|exceeded|disappoints?)\b|\b(?:falls?|fell|rises?|rose|jumps?|jumped|drops?|dropped|climbs?|climbed|slips?|slipped|surges?|surged|tumbl\w+|edges?|edged)\b(?=[^.]*\d)", re.I)

_RUMOR = re.compile(r"\b(rumou?rs?|unconfirmed|speculat\w+|sources\s+say|people\s+familiar|could\s+be|may\s+be\s+planning|boato)\b", re.I)
_CLAIM = re.compile(r"\b(allegedly|reportedly|claims?|claimed|according\s+to|suspected|alleges?)\b", re.I)
_CONFIRMED = re.compile(r"\b(confirm(?:s|ed)|announc(?:es|ed)|official(?:ly)?|statement|press\s+release|filed|approves?|approved|resolved|postmortem|post-mortem|confirmou|anunciou)\b", re.I)

# ---- resolução EXPLÍCITA (ver docstring)
_RESOLVED_STRONG = re.compile(
    r"\b(?:(?:has|have|had)\s+been\s+(?:fully\s+)?(?:resolved|restored|fixed|mitigated)|(?:is|are|was|were)\s+(?:now\s+|fully\s+)?(?:resolved|restored|fixed|operational|back\s+online|back\s+to\s+normal)|"
    r"(?:issue|incident|outage|problem)\s+(?:is\s+|was\s+|has\s+been\s+)?(?:now\s+)?(?:fully\s+)?resolved|resolved\s+(?:the\s+)?(?:issue|incident|outage)|"
    r"(?:service|network|operations?|block\s+production)\s+(?:has\s+been\s+|have\s+been\s+|is\s+now\s+)?(?:fully\s+)?(?:restored|resumed|recovered)|"
    r"(?:back|returned?)\s+(?:online|to\s+normal)|operational\s+again|normal\s+operations?\s+(?:have\s+)?resumed|fully\s+operational|incidente\s+resolvido|rede\s+restabelecida|resolvido|restabelecid\w+)\b", re.I)
_FUTURE_COND = re.compile(
    r"\b(?:until|till|once|when|whenever|if|unless|will|would|shall|should|to\s+be|expected|expect\w*|soon|pending|awaiting|waiting|working|trying|attempt\w*|aim\w*|hope\w*|before|"
    r"not\s+yet|yet\s+to|not\s+(?:been|resolved|fixed|restored)|hasn['’]?t|haven['’]?t|has\s+not|have\s+not|no\s+(?:eta|timeline)|remains?|still|ongoing|continu\w+|unresolved)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"[.;!?]+|\s[-–—]\s|:\s|,\s*(?:but|while|and)\s", re.I)
_NET_CTX = re.compile(r"\b(incident|outage|network|mainnet|cluster|rpc|validators?|block\s+production|performance|instabilidade|rede)\b", re.I)

# ---- ambiente e severidade
_ENV_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("mainnet", re.compile(r"\bmain\s*net(?:[- ]beta)?\b|\bconsensus\b|\bblock\s+production\b", re.I)),
    ("testnet", re.compile(r"\btest\s*net\b|\btest\s+network\b", re.I)),
    ("devnet", re.compile(r"\bdev\s*net\b|\bdevelopment\s+network\b", re.I)),
    ("explorer", re.compile(r"\bexplorer\b|\bsolscan\b|\bsolana\s+fm\b", re.I)),
    ("site", re.compile(r"\b(?:web\s*site|docs?|documentation|blog|forum|status\s+page|dashboard)\b", re.I)),
    ("rpc", re.compile(r"\b(?:public\s+)?rpc\b|\bpublic\s+(?:rpc\s+)?endpoints?\b|\bapi\.(?:mainnet|devnet|testnet)", re.I)),
]
_HALT = re.compile(r"\b(?:outage|downtime|down|halt\w*|stall\w*|offline|not\s+producing|stopped|consensus\s+(?:failure|halt)|restart\w*|unavailable|queda|parada)\b", re.I)
_DEGRADED = re.compile(r"\b(?:degraded|degradation|delay\w*|intermittent|elevated|slow\w*|congest\w*|latency|lag\w*|instabilidade)\b", re.I)
_NO_IMPACT = re.compile(r"\b(?:no|without)\s+(?:\w+\s+){0,2}?(?:impact|effect)\b|\b(?:not|n['’]t)\s+(?:been\s+)?(?:impacted|affected)\b|\bunaffected\b", re.I)
_MAINTENANCE = re.compile(r"\b(?:scheduled\s+maintenance|maintenance|planned\s+(?:upgrade|work)|manutenção)\b", re.I)

_ORIGIN_GENERIC = re.compile(r"\b(?:according\s+to|reported\s+by|citing|sources?\s+(?:say|said))\s+(?:a\s+|an\s+|the\s+|unnamed\s+|anonymous\s+|several\s+)?(?:tweet|post|social\s+media|x\s+post|telegram|discord|anonymous|unnamed|sources?|people|users?|rumou?rs?|reports?)\b", re.I)
_ORIGIN_NAMED = re.compile(r"\b(?:according\s+to|reported\s+by|first\s+reported\s+by|as\s+reported\s+by)\s+(?:the\s+)?([A-Z][A-Za-z0-9&\-]*(?:\.[a-z]{2,4})?(?:\s+[A-Z][A-Za-z0-9&\-]*(?:\.[a-z]{2,4})?){0,3})")

_INJECTION = re.compile(
    r"(ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+instructions|disregard\s+(?:the\s+)?(?:previous|prior)|system\s+prompt|you\s+are\s+now\b|"
    r"\bBOT_[A-Z_]{3,}|enable\s+live|set\s+mode\s+(?:to\s+)?live|disable\s+(?:the\s+)?(?:kill\s*switch|stop[- ]?loss|risk)|seed\s+phrase|private\s+key|"
    r"transfer\s+(?:all\s+)?(?:your\s+)?funds|execute\s+(?:a\s+)?(?:trade|swap|transaction)|ignore\s+risk|aumente\s+a\s+exposi)", re.I)
CERT_FACTOR = {"confirmed": 1.0, "claim": 0.7, "rumor": 0.35}
INDICATOR_IMPACT = {name: imp for name, _rx, imp in _INDICATORS}


def strip_negations(text: str) -> tuple[str, list[str]]:
    """Remove do texto os trechos de negação/desmentido; devolve (texto restante, trechos removidos)."""
    removed: list[str] = []
    for rx in _NEGATIONS:
        def _sub(m: re.Match[str]) -> str:
            removed.append(m.group(0))
            return " "
        text = rx.sub(_sub, text)
    return text, removed


def detect_indicator(text: str) -> str | None:
    for name, rx, _imp in _INDICATORS:
        if rx.search(text):
            return name
    return None


def explicit_resolution(text: str) -> bool:
    """True só se alguma ORAÇÃO contém uma expressão inequívoca de resolução e nenhuma marca de futuro/condição/negação."""
    for clause in _CLAUSE_SPLIT.split(text):
        if _RESOLVED_STRONG.search(clause) and not _FUTURE_COND.search(clause):
            return True
    return False


_ENV_RANK = {"mainnet": 5, "rpc": 4, "testnet": 3, "devnet": 3, "explorer": 2, "site": 1}  # mais crítico vence (conservador)


def detect_environment(title: str, summary: str) -> str:
    """O ambiente é decidido pelo TÍTULO quando ele cita algum; senão pelo resumo. Com vários ambientes, vale o MAIS CRÍTICO
    (ex.: "Public Endpoints and Explorer offline" = rpc, não explorer)."""
    for text in (title, summary):
        found = [name for name, rx in _ENV_PATTERNS if rx.search(text)]
        if found:
            return max(found, key=lambda n: _ENV_RANK[n])
    return "desconhecido"


class Classifier:
    def __init__(self, tokens: list[tuple[str, tuple[str, ...]]], params: IntelParams | None = None):
        """`tokens`: [(símbolo, aliases)]. Símbolos curtos casam só em MAIÚSCULAS; aliases exigem contexto cripto."""
        self.tokens = [(sym, aliases) for sym, aliases in tokens]
        self.params = params

    # ------------------------------------------------------------------ ativos
    def detect_assets(self, title: str, summary: str) -> dict[str, float]:
        found: dict[str, float] = {}

        def hit(pattern: str, asset: str, flags: int = 0, needs_ctx: bool = False) -> None:
            rx = re.compile(pattern, flags)
            for text, weight in ((title, 1.0), (summary, 0.7)):
                if rx.search(text) and (not needs_ctx or _CRYPTO_CTX.search(title + " " + summary)):
                    found[asset] = max(found.get(asset, 0.0), weight)

        hit(r"\bSOL\b", "SOL")
        hit(r"\bSolana\b", "SOL", re.I)
        hit(r"\bBTC\b", "BTC")
        hit(r"\bBitcoin\b", "BTC", re.I)
        hit(r"\b(?:crypto(?:currenc\w+)?|digital\s+assets?)\b", "MARKET", re.I)
        for sym, aliases in self.tokens:
            if sym not in ("SOL", "BTC"):
                hit(rf"\b{re.escape(sym)}\b", sym)
            for al in aliases:
                hit(rf"\b{re.escape(al)}\b", sym, re.I, needs_ctx=True)
        return found

    # ------------------------------------------------------------------ principal
    def classify(self, raw: RawItem, source: SourceConfig, first_seen: datetime) -> NewsRecord:
        title, summary = raw.title, raw.summary
        full = f"{title}. {summary}"
        reasons: list[str] = []
        flags: list[str] = []
        assets = self.detect_assets(title, summary)
        for implied in source.assets:  # ex.: feed oficial de status da Solana
            assets[implied] = max(assets.get(implied, 0.0), 0.9)
        structured = source.status_format == "statuspage"
        status_state = raw.status_state if structured else None

        # --- negação / desmentido / correção: removidos antes de procurar eventos
        text, removed = strip_negations(full)
        event_cats = ("exploit_hack", "insolvencia", "incidente_rede", "exclusao_listagem")
        # "Desmentido" só se, removido o trecho negado, NÃO sobra nenhum evento: "exploit confirmado; nenhum fundo foi drenado" continua sendo exploit.
        remaining_event = any(_CAT[c][0].search(text) for c in event_cats)
        retraction = (bool(removed) or bool(_RETRACTION.search(full))) and not remaining_event
        denied_category = None
        if retraction:
            blob = " ".join(removed) if removed else full
            for cat in event_cats:
                if _CAT[cat][0].search(blob):
                    denied_category = cat
                    break
            reasons.append("negação/desmentido/correção detectado" + (f" ('{removed[0][:40]}')" if removed else "") + ": o evento negado NÃO é tratado como ocorrido")
        elif removed:
            reasons.append(f"negação parcial ('{removed[0][:40]}'): o restante do texto ainda descreve um evento e foi classificado normalmente")

        # --- macro
        indicator = detect_indicator(text)
        if source.default_indicator:  # feed de um único tipo (ex.: discursos do Fed): o tipo vem do CATÁLOGO, não do texto do título
            indicator = source.default_indicator
        is_macro_item = indicator is not None or (source.domain == "macro_us" and _MACRO_GENERIC.search(text) is not None)
        # --- categoria
        category, base, default_pol = "outros", 0, None
        crypto_gate = (not source.official) and (not structured) and source.domain != "macro_us"
        has_crypto_ctx = bool(assets) or bool(_CRYPTO_CTX.search(text))
        if is_macro_item and not (retraction and denied_category):
            category, base = "macro", INDICATOR_IMPACT.get(indicator or "", 2)
        else:
            for cat, (rx, imp, pol) in _CAT.items():
                if rx.search(text) and imp > base:
                    if crypto_gate and not has_crypto_ctx:
                        continue  # texto genérico sem contexto cripto (ex.: "attack" em outro assunto)
                    category, base, default_pol = cat, imp, pol
        if structured and category in ("outros", "mercado_geral") and not retraction:
            category, base, default_pol = "incidente_rede", 3, "neg"  # todo item de uma página de status de incidentes é, por definição, um incidente/manutenção
            reasons.append("fonte estruturada de incidentes (Statuspage)")
        if category == "outros" and not retraction and ("BTC" in assets or "MARKET" in assets or "SOL" in assets):
            category, base = "mercado_geral", 1
        if retraction and denied_category:
            category, base, default_pol = denied_category, 0, None
        if category != "outros":
            reasons.append(f"categoria '{category}' por palavras-chave" + (f" (indicador {indicator})" if indicator else ""))

        # --- certeza (rumor tem prioridade: "unconfirmed" contém "confirmed")
        if retraction:
            if source.official or _CONFIRMED.search(full):
                certainty = "confirmed"
                reasons.append("desmentido/correção de fonte oficial ou com marcador de confirmação")
            else:
                certainty = "claim"
        elif _RUMOR.search(text):
            certainty = "rumor"
            reasons.append("marcadores de rumor")
        elif _CLAIM.search(text):
            certainty = "claim"
            reasons.append("marcadores de alegação")
        elif source.official or _CONFIRMED.search(text):
            certainty = "confirmed"
            reasons.append("fonte oficial" if source.official else "marcadores de confirmação")
        else:
            certainty = "claim"
            reasons.append("sem marcador de confirmação (tratado como alegação)")

        # --- natureza da afirmação (fato anunciado x expectativa x opinião x especulação)
        if _RUMOR.search(text):
            nature = "especulacao"
        elif (_EXPECT_FORWARD.search(text) or (_EXPECT_WORD.search(text) and not _RESULT_VS_EXPECT.search(text))) and not (source.official and status_state):
            nature = "expectativa"
        elif _OPINION.search(text) and not source.official:
            nature = "opiniao"
        elif source.official or _ANNOUNCE.search(text) or structured:
            nature = "fato"
        else:
            nature = "desconhecido"
        if category == "macro" and nature != "fato":
            reasons.append(f"macro: texto classificado como {nature} (não é decisão/dado anunciado): não gera alerta")

        # --- ambiente, severidade e resolução (incidentes)
        environment, severity, resolution = "desconhecido", 0, False
        if category == "incidente_rede" and not retraction:
            environment = detect_environment(title, summary)
            if _MAINTENANCE.search(full) and not _HALT.search(text):
                severity = 1
            elif _NO_IMPACT.search(text) and not _HALT.search(text):
                severity = 2  # a própria página diz que não há impacto (ex.: "Mainnet Beta Clock Drift": "no impact on performance"): informativo
            elif _HALT.search(text):
                severity = 4
            elif _DEGRADED.search(text):
                severity = 3
            else:
                severity = 3
            base = severity
            if environment in ("devnet", "testnet", "explorer", "site"):
                base = 1
                reasons.append(f"ambiente '{environment}': impacto limitado a baixo (não é a infraestrutura crítica da mainnet)")
            elif environment == "rpc":
                base = min(base, 3)
                reasons.append("ambiente 'rpc': impacto limitado a alto (cautela, sem bloqueio)")
            if structured and status_state:
                resolution = status_state in ("resolved", "completed")
                reasons.append(f"estado estruturado do incidente: {status_state}" + (" (resolução EXPLÍCITA)" if resolution else " (ainda ativo)"))
            elif explicit_resolution(text):
                resolution = True
                reasons.append("expressão inequívoca de resolução (sem marcas de futuro/condição)")
        elif category in ("outros", "mercado_geral") and not retraction and not structured and ((_NET_CTX.search(text) or "SOL" in assets) and explicit_resolution(text)):
            category, base, default_pol, resolution = "incidente_rede", 3, "neg", True
            environment = detect_environment(title, summary)
            reasons.append("categoria 'incidente_rede' por aviso de resolução explícita")
        if resolution and category == "incidente_rede" and not structured:
            reasons.append("indica resolução do incidente")

        # --- polaridade
        neg_rx = _NEG_WORDS
        neg, pos = len(neg_rx.findall(text)), len(_POS_WORDS.findall(text))
        if category == "macro":
            polarity = "inconclusive"
            reasons.append("macro: direção do efeito é indeterminada")
        elif retraction:
            polarity = "neutral"
        elif resolution:
            polarity = "positive"
        elif default_pol == "neg" and pos <= neg:
            polarity = "negative"
        elif default_pol == "neg" and structured:
            polarity = "negative"
        elif default_pol == "pos" and neg == 0:
            polarity = "positive"
        elif neg and pos:
            polarity = "inconclusive" if abs(neg - pos) <= 1 else ("negative" if neg > pos else "positive")
        elif neg:
            polarity = "negative"
        elif pos:
            polarity = "positive"
        else:
            polarity = "neutral" if category != "outros" else "inconclusive"
        if neg and pos and not retraction:
            reasons.append("sinais positivos e negativos no mesmo texto")

        # --- integridade/segurança do conteúdo (NÃO usa o ano citado no texto: só a data de publicação conta)
        if _INJECTION.search(full):
            flags.append("instrucao_suspeita")
            reasons.append("texto contém instruções dirigidas a sistemas/robôs: IGNORADO como dado suspeito")
        if raw.published_at is None:
            flags.append("sem_data")
            reasons.append("fonte não informou data confiável")

        # --- origem citada (para independência de fontes)
        origin_key, origin_generic = None, False
        if _ORIGIN_GENERIC.search(full):
            origin_generic = True
            m = _ORIGIN_GENERIC.search(full)
            origin_key = "generic:" + re.sub(r"\s+", " ", (m.group(0).split()[-1] if m else "x").lower())
        else:
            m2 = _ORIGIN_NAMED.search(full)
            if m2:
                origin_key = re.sub(r"[^a-z0-9]+", " ", m2.group(1).lower()).strip()

        reliability = source.reliability * CERT_FACTOR[certainty] * (0.5 if "instrucao_suspeita" in flags else 1.0)
        relevance = max(assets.values(), default=0.0) if assets else (0.3 if category == "macro" else 0.0)
        pub_iso = raw.published_at.astimezone(timezone.utc).isoformat() if raw.published_at else None
        version = content_fingerprint(summary, status_state) if structured else ""
        return NewsRecord(
            id=item_id(raw.url, title, version), source_id=source.id, source_name=source.name, title=title, summary=summary, url=raw.url,
            published_at=pub_iso, collected_at=raw.collected_at.astimezone(timezone.utc).isoformat(), first_seen=first_seen.astimezone(timezone.utc).isoformat(),
            assets=sorted(assets), category=category, polarity=polarity, certainty=certainty, relevance=round(relevance, 2), reliability=round(reliability, 3),
            impact=0 if retraction else base, flags=flags, reasons=reasons, resolution=resolution,
            event_key=canonical_url(raw.url) if structured else None, event_time=pub_iso, nature=nature, environment=environment,
            severity=severity, status_state=status_state, origin_key=origin_key, origin_generic=origin_generic, refs=list(raw.refs),
            domain=source.domain, retraction=retraction, indicator=indicator,
        )


_NEG_WORDS = re.compile(
    r"\b(hack(?:ed|s|ers?|ing)?|exploit\w*|drain\w*|stolen|outage|halt\w*|crash\w*|plunge\w*|lawsuit|sues?|sued|ban(?:s|ned)?|insolven\w+|bankrupt\w*|delist\w*|suspend\w*|rug|liquidat\w+|down|"
    r"fall(?:s|en)?|reject(?:s|ed)?|delay(?:s|ed)?|fraud|queda|proíbe|degraded|degradation)\b", re.I)
_POS_WORDS = re.compile(
    r"\b(approv\w+|launch\w*|upgrade\w*|partnership|partners?\s+with|adopt\w*|integrat\w+|record\s+\w+|surpass\w*|wins?|resolved|restored|recovered|operational|back\s+online|aprova\w*)\b", re.I)

__all__ = ["Classifier", "canonical_url", "strip_negations", "explicit_resolution", "detect_indicator", "detect_environment", "INDICATOR_IMPACT", "CERT_FACTOR"]
