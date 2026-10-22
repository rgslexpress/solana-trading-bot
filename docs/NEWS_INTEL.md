# News & Market Intelligence (notícias, status da Solana e macroeconomia dos EUA)

Camada **independente** que acrescenta contexto externo à estratégia técnica. Ela **só pode restringir**: bloquear *novas entradas* ou *reduzir* o risco por operação (`risk_scale` sempre em (0, 1]). Nunca compra, nunca vende, nunca altera stops, exposição, kill switch, modo (PAPER/LIVE) ou configuração. O robô continua **exclusivamente em paper trading**; esta camada não assina, não constrói e não envia transações, e **nenhum dado dela autoriza execução real**.

> **Estado de validação — leia primeiro.**
> * **Testado com fontes reais** (3 relatórios de diagnóstico rodados pelo usuário no Windows em 2026-10-22: 16:07, 17:51 e 18:03 UTC): **Solana Status** (RSS e JSON), **Federal Reserve** (política monetária e discursos), **BLS iCalendar** (313 eventos; `TZID=US-Eastern`; 92 reconhecidos), **API de valores do BLS** (1 série: desemprego 4,2% em 2026-09, anterior 4,1%; `REQUEST_SUCCEEDED`) e **CSV do Tesouro** responderam HTTP 200 com o formato esperado — **após correções feitas por causa desses dados** (o parser do BLS reconhecia 0 dos 313 eventos até aceitar `US-Eastern`). A API do BLS deu tempo esgotado na 1ª tentativa e funcionou na 2ª (1,4 s): é intermitente, não está confirmada como estável. **GDELT** deu HTTP 429 nas duas execuções.
> * **Nunca validadas com resposta real:** SEC, CoinDesk, Cointelegraph, Decrypt, e as 5 séries restantes da API do BLS (só LNS14000000 foi consultada). Os parsers dessas fontes só foram testados com amostras sintéticas.
> * **Formato real do Solana Status (observado):** cada incidente é uma linha `"<data> UTC <Estágio> - texto"` com as atualizações **mais recentes primeiro**; os marcadores reais são Investigating, Identified, Monitoring, Update e Resolved; o `pubDate` do item é o instante da **última atualização**. Os 25 itens do feed são incidentes antigos (o mais recente, 2024-02-06), todos com "Resolved"; **um incidente ATIVO ainda não foi observado** (inferido: sem a linha "Resolved"). O relatório de 17:51 mostrou 7/25 "não resolvidos" porque o script de diagnóstico não passava `status_format`; com o catálogo real todos são detectados como resolvidos (`test_intel_real_samples.py`).
> * **Não há evidência de que a camada melhore a rentabilidade.** Nenhuma comparação com dados reais foi feita.

## 1. Arquitetura (cinco camadas separadas)

| Camada | O que faz | Onde |
|---|---|---|
| 1. Status operacional da Solana | disponibilidade e incidentes da rede (Statuspage) | `classifier.py`, fonte `solana_status` (`domain=solana_status`) |
| 2. Notícias cripto | incidentes, segurança, regulação, ETF, listagens/unlocks | `classifier.py`, `engine.py` (`domain=crypto_news`) |
| 3. Macroeconomia dos EUA | calendário, janelas de risco, valores divulgados, juros do Tesouro | `macro.py`, `macro_sources.py`, `ustime.py` (`domain=macro_us`) |
| 4. Motor de risco | recebe só `block` e `risk_scale`; regras existentes continuam valendo | `app/risk/risk_manager.py` (não ganhou poderes novos) |
| 5. Execução/posições | stops, saídas, kill switch, PAPER; LIVE bloqueado | inalterados |

## 2. Modos

| `BOT_INTEL_MODE` | Efeito |
|---|---|
| `off` (padrão) | desligado; o robô se comporta exatamente como antes (teste compara a posição com o baseline) |
| `shadow` | coleta, classifica e **registra** o que mudaria, **sem alterar nenhuma decisão técnica** — nem por falha de auditoria (seção 8) |
| `enforce` | aplica as restrições (bloqueio de novas entradas; `risk_scale` ≤ 1). Só use depois de validar em `shadow` |

## 3. Fontes, limites e estado de validação

Nenhuma exige chave nem credencial. Sem scraping. Catálogo de exemplo: `config/intel_sources.example.json` (copie para `config/intel_sources.json`; o `.bat` já faz isso). Só `solana_status` e `fed_monetary` vêm habilitadas, porque foram as únicas conferidas contra respostas reais.

| Fonte (`id`) | Endpoint | Validação | Chave / limites / termos | Latência e cobertura |
|---|---|---|---|---|
| Solana Status (`solana_status`, **crítica**) | `https://status.solana.com/history.rss` | **REAL OK 2026-10-22** (200, RSS, 25 itens, ~500 ms; formato dos incidentes conferido) | sem chave; cache do servidor 10–15 s | incidentes/manutenções; poucos por ano (último: 2024-02-06) |
| Solana Status JSON | `https://status.solana.com/api/v2/status.json` | **REAL OK** (200, `indicator: none`) — o robô ainda não o usa | idem | estado atual agregado |
| Federal Reserve (`fed_monetary`) | `https://www.federalreserve.gov/feeds/press_monetary.xml` | **REAL OK** (200, RSS com BOM, 15 itens) | conteúdo público do Fed (citar a fonte) | só publica **depois** do evento |
| Fed discursos (`fed_speeches`) | `https://www.federalreserve.gov/feeds/speeches.xml` | **REAL OK 2026-10-22** (200, RSS com BOM, 15 itens; títulos "Nome, Assunto" sem a palavra *speech* → `default_indicator=FED_SPEECH`) | idem | informativo (impacto médio) |
| BLS calendário (`bls_calendar`) | `https://www.bls.gov/schedule/news_release/bls.ics` | **REAL OK 2026-10-22** (200, `text/calendar`, 80 KB, 313 eventos, todos `DTSTART;TZID=US-Eastern`; Employment Situation 25, CPI 23, PPI 23, JOLTS 37 incl. versões estaduais) | sem chave | datas de Employment Situation, CPI, PPI, JOLTS (SUMMARY exato) |
| BLS valores (`bls_values`) | `https://api.bls.gov/publicAPI/v1/timeseries/data/<SÉRIE>` (GET) | **REAL OK 2026-10-22 18:03** (200, `REQUEST_SUCCEEDED`, 33 pontos `M01–M12`, campo `latest`; 1 série testada; 1ª tentativa às 17:51 deu timeout) | sem chave; **25 consultas/dia** (segundo terceiros) — o robô usa `daily_cap` e só consulta quando há divulgação esperada | atual/anterior; **sem expectativa de mercado** |
| Tesouro (`treasury_yields`) | `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve&field_tdr_date_value=2026&page&_format=csv` | **REAL OK 2026-10-22** (200, `text/csv`, 202 linhas, **mais recente primeiro**, datas MM/DD/AAAA, colunas `2 Yr`/`10 Yr`) | sem chave; **atualize o ano na URL a cada janeiro** | fim de dia (informativo) |
| SEC (`sec_press`) | `https://www.sec.gov/news/pressreleases.rss` | não testada | exige User-Agent com contato; ≤10 req/s | regulação |
| CoinDesk / Cointelegraph / Decrypt | RSS públicos | não testadas; **termos de uso não confirmados** | verifique os termos | mídia (confiabilidade 0,65–0,7) |
| GDELT (`gdelt_cripto`) | `https://api.gdeltproject.org/api/v2/doc/doc` | **REAL: HTTP 429 na 1ª requisição, 12,9 s** | 1 req/5 s por IP; pode devolver `{}` com HTTP 200 (tratado como erro) | não recomendada |

**Não integradas (e por quê):** S&P 500 e Nasdaq (sem fonte gratuita, sem chave e com termos confirmados; FRED exige chave), Índice do Dólar/DXY (idem), valores de Jobless Claims (DOL publica PDF) e de PCE (BEA/FRED exigem chave), consenso/expectativa de mercado (nenhuma fonte oficial gratuita) e taxa-alvo do FOMC como número (texto livre). O **calendário** de Jobless Claims e PCE pode ser cadastrado à mão (seção 9); os valores ficam **N/D**.

## 4. Como o texto é interpretado (classificador v2)

* **Resolução só com estado explícito.** Para páginas Statuspage: o estágio mais avançado citado no corpo (`Investigating → Identified → Monitoring → Resolved`; o progresso é monótono, então a ordem de listagem não importa). Para outras fontes: frase inequívoca ("incident resolved", "service restored") **sem** marca de futuro/condição na mesma oração. "until the issue is resolved", "will be resolved soon", "not yet resolved", "investigating" **não** são resolução.
* **Negação, correção e desmentido** ("not hacked", "no outage", "denies hack", "false report", "correction"): o trecho negado é removido antes de procurar eventos. Se não sobra evento, o item vira **desmentido** (neutro, impacto 0); um desmentido **confirmado** (fonte oficial ou marcador de confirmação) fecha alegações anteriores, um desmentido de mídia **não**. Negação parcial ("exploit confirmado; nenhum fundo drenado") continua sendo exploit.
* **Palavras genéricas** ("identified", "investigating") não definem incidente; incidente exige vocabulário de incidente (outage, halt, degraded performance…). Em fontes não oficiais, categorias de risco exigem contexto cripto.
* **Ambiente e criticidade (incidentes de rede):** `mainnet`, `rpc`, `devnet`, `testnet`, `explorer`, `site`, `desconhecido`. Devnet/testnet/explorer/site → impacto **baixo** (informativo). RPC → no máximo **alto** (cautela). Mainnet: parada total (outage/halt/offline) → **crítico** (bloqueia se confirmado); degradação → **alto** (cautela; `BOT_INTEL_MAINNET_DEGRADED_POLICY=block` trata como crítico). Ambiente desconhecido é tratado de forma conservadora (sem rebaixar).
* **Quem pode encerrar um evento:** texto externo não é comando de liberação. Uma resolução/desmentido só encerra um incidente se vier de fonte com confiabilidade ≥ `MIN_RELIABILITY_FOR_BLOCK` **e** de ambiente compatível (um "resolvido" da devnet/explorer não fecha a mainnet), ou se for a nova versão do **mesmo** incidente (`event_key`).
* **Datas.** Só a data de **publicação/atualização** conta. Item com data **no futuro** (> tolerância) não é gravado nessa rodada e é aceito quando a data chegar (não fica marcado para sempre). Anos citados no texto ("since 2022") **não** descartam a notícia. Sem data confiável → `sem_data` (nunca gera alerta); no futuro → `data_futura`; mais velha que `item_max_age_h` → `antiga`.
* **Natureza da afirmação:** `fato` (decisão/dado anunciado), `expectativa` ("expected to cut", "what to expect"), `opiniao` (analistas) e `especulacao` (rumor). Para macro, **só `fato` gera alerta**; expectativa/opinião/especulação aparecem no painel sem efeito.
* **Fed/macro:** reconhece abreviações e nomes por extenso (FOMC, Federal Open Market Committee, ata/minutes, federal funds rate, interest rate decision, rate cut/hike, monetary policy statement, press conference, Fed Chair speech) e indicadores (CPI, Core CPI, PCE, Core PCE, PPI, Nonfarm Payrolls, taxa de desemprego, Jobless Claims, JOLTS). A direção do efeito é **sempre inconclusiva**.
* **Conteúdo é dado, não comando:** instruções dirigidas a sistemas ("ignore as instruções", `BOT_MODE=live`, "desative o kill switch") marcam `instrucao_suspeita` (confiabilidade reduzida, nunca geram alerta). O nome do evento macro vem do catálogo interno, **nunca** do texto da fonte.

**Limitações da classificação:** é heurística (palavras-chave e regras): erra em ironia, negações longas, manchetes ambíguas e idiomas diferentes de EN/PT. Por isso só restringe e exige confirmação para bloquear. Não existe classificação perfeita; a matriz de testes (`tests/intel/test_intel_classifier_v2.py`) documenta os casos cobertos, não todos os casos.

## 5. Deduplicação, versões de incidente e independência de fontes

* **Identificação:** cada item tem `id` por versão. Em páginas de status, o mesmo incidente (mesma URL = `event_key`) gera uma **nova versão** quando muda o estado/texto (nunca é descartado por ter o mesmo título). A última versão visível no instante da decisão é a que vale (calculado em cada decisão: sem estado oculto e sem vazamento de futuro). Se a fonte não atualiza a data, o instante da atualização é o da **coleta**. A gravidade já observada num incidente **não diminui** só porque a atualização seguinte é mais curta; o fim é sinalizado pelo estado `resolved`.
* **Títulos repetidos** entre incidentes diferentes **não** são duplicatas (no feed real do Solana Status 18 dos 25 títulos se repetem). Títulos quase idênticos só são `duplicada` na **mesma fonte** e quando publicados com até `near_dup_max_gap_h` (24 h) de diferença; a mesma URL republicada com data bem posterior é `reapresentada`.
* **Sem identificador confiável** (o mesmo link repetido para vários itens): estratégia **conservadora** — cada item distinto (título + data + conteúdo) é um evento distinto; nada é descartado por coincidência de URL.
* **Independência:** republicações/matérias derivadas contam **uma** origem. Sinais usados: mesmo veículo/domínio, similaridade de título (≥ 0,6) ou de título+texto (≥ 0,5), mesma origem citada ("according to X"), links citados. Se não dá para afirmar independência (similaridade intermediária ≥ 0,35, ou ambas citam uma origem genérica como "um tweet"), a corroboração **não é afirmada** e o alerta mostra "independência INCERTA". Limitação: agências que distribuem a mesma matéria reescrita podem parecer independentes.

## 6. Saúde das fontes e contingência

* **Estado por fonte:** `ok`, `stale` (sem sucesso há mais que `stale_after_s`, por fonte configurável), `unavailable` (≥ 3 falhas seguidas ou nunca coletou), `invalid` (resposta inválida: XML/JSON/HTML inesperado, ou > 50% dos itens malformados) e `disabled`. A recuperação só é confirmada após `BOT_INTEL_RECOVERY_MIN_SUCCESSES` (2) sucessos seguidos; falha durante a recuperação torna a indisponibilidade firme de novo.
* **Fonte crítica manda:** `solana_status` é `critical`. Se uma fonte crítica não estiver `ok`, o módulo **não** é apresentado como saudável (painel "CRÍTICO") e o estado dos dados deixa de ser `ok`, mesmo que o Fed responda. Fontes secundárias com problema só rebaixam para "DEGRADADO".
* **Indisponibilidade curta:** 1–2 falhas com dados ainda frescos são toleradas; a partir de 3, vale `BOT_INTEL_UNAVAILABLE_POLICY` (`block_entries` padrão, só em `enforce`).
* **Contingência (> `BOT_INTEL_CONTINGENCY_AFTER_S`, padrão 6 h):** estado `contingency`, com início, duração, fontes, política e decisões afetadas no painel; evento gravado em `intel_events` (`contingencia_iniciada` / `contingencia_encerrada`). **A passagem do tempo nunca libera operações.** A política padrão `keep_restriction` mantém a restrição em vigor (bloqueio, se `unavailable_policy=block_entries`; com `warn_only` o operador já escolheu não restringir e a contingência apenas é registrada). Só `BOT_INTEL_CONTINGENCY_POLICY=reduced_risk` (escolha **explícita**) libera entradas **com risco reduzido** (`BOT_INTEL_CONTINGENCY_RISK_MULTIPLIER`, (0,1]), e isso fica registrado na justificativa de cada decisão. Retomada automática sem política explícita não existe.
* **Contingência exige falhas reais:** só conta fonte com ≥ 3 falhas seguidas (ou recuperação ainda não confirmada). Um robô que ficou **desligado** por horas (dados velhos, sem tentativa falha) **não** entra em contingência e a política `reduced_risk` não libera nada por causa disso: segue `stale` com a política de indisponibilidade.
* **Eventos de auditoria:** `fonte_indisponivel`, `recuperacao_parcial`, `fonte_recuperada`, `backoff_limite`, `busca_travada`, `contingencia_iniciada`, `contingencia_encerrada`.

## 7. Coleta: prazos, segundo plano e limites

* **Busca em thread de fundo** (`BOT_INTEL_BACKGROUND_FETCH=true`): o ciclo de trading só recolhe o resultado já pronto e **nunca espera pela rede**; os stops continuam sendo monitorados primeiro em cada ciclo. Somente a classificação e a gravação (rápidas) rodam no ciclo. Com `false`, a coleta é síncrona (scripts/testes).
* **Prazo total por requisição** (`BOT_INTEL_TOTAL_TIMEOUT_S`, padrão 20 s) além dos timeouts de conexão/leitura (`BOT_INTEL_SOURCE_TIMEOUT_S`), e corte de tamanho **durante** o download (`BOT_INTEL_MAX_FEED_BYTES`). Limitação: o prazo total é verificado a cada pedaço recebido; uma conexão que não envia nada é limitada pelo timeout de leitura.
* **HTTP 429/`Retry-After`:** a fonte entra em backoff (`BOT_INTEL_BACKOFF_BASE_S` dobrando a cada novo 429, até `BACKOFF_MAX_S`; um `Retry-After` maior é respeitado). O backoff e a contagem diária (`daily_cap`) sobrevivem a reinícios.
* **Registro:** duração, tipo de erro (`http`, `timeout`, `network`, `rate_limit`, `parse`, `invalid`), código HTTP e recuperação ficam em `news_source_status`/`intel_events` e no painel.

## 8. Auditoria é só observação (P0.1)

Cada BUY técnico avaliado grava em `intel_decisions` (modo, estado dos dados, ação, `risk_scale`, alertas, justificativa, posição resultante). Se **gravar falhar** (disco cheio, permissão, banco travado), a falha é **contada** (`audit_failures`), registrada no log e mostrada no relatório do ciclo, e **a decisão de trading não muda** — nem em `shadow` nem em `enforce` (em `enforce` o bloqueio continua valendo). Testes simulam 5 tipos de falha.

## 9. Macroeconomia dos EUA

* **Calendário (`config/macro_calendar.json`, exemplo em `config/macro_calendar.example.json`):** `events` (data/hora **locais** + fuso) e `recurring` (dia da semana, com `exclude` para feriados). Cada evento guarda: nome, fonte, **fuso original**, **horário local**, **horário UTC normalizado**, região, importância (1–3), indicadores relacionados e **confiança do horário** (`oficial` ou `estimado`). Eventos `oficial` (BLS iCalendar) substituem `estimado` do mesmo tipo/dia. O arquivo é recarregado ao mudar; um arquivo quebrado mantém o calendário anterior. As datas do FOMC 2026 no exemplo vieram de site de terceiros e estão marcadas `estimado`: **confirme na página oficial do Fed**.
* **Horário de verão:** `app/intel/ustime.py` implementa a regra dos EUA (2º domingo de março → 1º domingo de novembro, 2007–2099) sem `tzdata` (que não vem no Windows). Verificado contra o `zoneinfo` do sistema numa checagem única (a cada 30 min de 2007 a 2050 = 771 mil instantes UTC, e 35 mil horários locais): **0 divergências**; o teste automatizado repete a checagem ao redor de todas as transições (evidência computacional independente, não de fonte real). Horário local **inexistente** (salto da primavera) levanta erro; **ambíguo** (retrocesso) usa `fold`. Sem offsets fixos: 08:30 ET é 12:30Z no verão e 13:30Z no inverno.
* **Janelas de risco** (configuráveis): alta importância (FOMC, presser, emprego, CPI, PCE) = `MACRO_HIGH_PRE_S` (30 min) antes e `MACRO_HIGH_POST_S` (60 min) depois; média (Jobless Claims, JOLTS, PPI, ata, discursos) = 15/30 min e **só informa**. Dentro da janela de alta importância: `risk_scale = MACRO_WINDOW_RISK_MULTIPLIER` (0,5); com `MACRO_HIGH_POLICY=block`, bloqueia **novas** entradas (padrão `reduce`).
* **Evento futuro = só risco.** O painel mostra "resultado desconhecido". O valor divulgado só entra a partir de `observed_at` (instante em que ficou **disponível** para o robô, = coleta). Valores **não** são interpretados como bons/ruins para cripto. Expectativa/consenso, valor anterior e revisões só aparecem se a fonte os forneceu (BLS não fornece consenso: **N/D**). Revisões do período e do valor anterior geram **novas linhas** (histórico preservado).
* **Valores acompanhados (BLS):** `LNS14000000` (desemprego), `CES0000000001` (NFP, variação mensal), `CUUR0000SA0`/`CUUR0000SA0L1E` (CPI/Core CPI a/a, não ajustado), `WPSFD4` (PPI a/a), `JTS000000000000000JOL` (JOLTS). IDs confirmados só por busca, não por resposta real.
* **Mercado:** rendimentos de 2 e 10 anos do Tesouro (variação diária em bps) — informativos; `MACRO_YIELD_MOVE_BPS` > 0 aciona cautela (padrão 0 = desligado, regra não validada).
* **Sem look-ahead no backtest/replay:** o agendamento vale a partir de `known_at` (ausente = divulgado com antecedência), valores/revisões/mercado a partir de `observed_at`. Arquivo histórico (`app.intel.macro.load_macro_archive`, JSON Lines): sem `observed_at`, usa `released_at + BOT_INTEL_ASSUMED_LATENCY_S` (900 s, **premissa não calibrada**); Tesouro: `obs_date` + 18:00 ET (premissa). Disponibilidade nunca precede a divulgação.

## 10. Painel (seção "News & Market Intelligence")

* **Faixa de situação:** verde = nenhum evento com fontes OK; vermelho = fontes indisponíveis/desatualizadas/inválidas/em contingência (**não** significa ausência de eventos); amarelo = alertas ativos.
* **Saúde do módulo:** SAUDÁVEL / DEGRADADO / CRÍTICO / CONTINGÊNCIA, e a tabela **por fonte** (domínio, crítica?, estado, "desde", última OK, falhas, tipo de erro/HTTP/duração, recuperação "1/2", próxima tentativa após 429).
* **Contingência:** caixa vermelha com há quanto tempo, desde quando, fontes, política aplicada e nº de decisões afetadas.
* **Alertas, notícias** (com natureza e indicador), **histórico de decisões**, **eventos de saúde**, e o bloco **Macro EUA**: janelas ativas, próximos eventos (horário UTC + local original), valores divulgados (expectativa N/D quando a fonte não fornece), mercado e fontes pendentes. Calendário vazio é avisado.
* API somente leitura: `GET /api/intel`. Não existe rota POST de notícias.

## 11. Variáveis de ambiente (sem segredos)

`BOT_INTEL_MODE`, `SOURCES_FILE`, `REFRESH_INTERVAL_S`, `SOURCE_TIMEOUT_S`, `TOTAL_TIMEOUT_S`, `REFRESH_BUDGET_S`, `MAX_FEED_BYTES`, `STALE_AFTER_S`, `RECOVERY_MIN_SUCCESSES`, `UNAVAILABLE_POLICY`, `CONTINGENCY_AFTER_S`, `CONTINGENCY_POLICY`, `CONTINGENCY_RISK_MULTIPLIER`, `BACKGROUND_FETCH`, `BACKOFF_BASE_S`, `BACKOFF_MAX_S`, `MIN_RELIABILITY_FOR_BLOCK`, `CORROBORATION_MIN_SOURCES`, `CAUTION_WINDOW_S`, `CAUTION_RISK_MULTIPLIER`, `MAINNET_DEGRADED_POLICY`, `NEAR_DUP_MAX_GAP_H`, `SYNDICATION_TITLE_JACCARD`, `SYNDICATION_TEXT_JACCARD`, `INDEPENDENCE_UNCERTAIN_JACCARD`, `MACRO_ENABLED`, `MACRO_CALENDAR_FILE`, `MACRO_HIGH_PRE_S`, `MACRO_HIGH_POST_S`, `MACRO_MEDIUM_PRE_S`, `MACRO_MEDIUM_POST_S`, `MACRO_WINDOW_RISK_MULTIPLIER`, `MACRO_HIGH_POLICY`, `MACRO_LOOKAHEAD_H`, `MACRO_YIELD_MOVE_BPS`, `TREASURY_PUBLICATION_ET`, `ASSUMED_LATENCY_S`, `USER_AGENT` (todas com prefixo `BOT_INTEL_`; valores padrão no `.env.example`). Multiplicadores > 1 e políticas desconhecidas são **recusados** na configuração. Nenhuma fonte usada exige chave; **nunca** coloque segredos no `.env.example`, em logs ou nesta documentação.

## 12. Como habilitar a observação (shadow) e validar as fontes reais

1. `copy config\intel_sources.example.json config\intel_sources.json` e `copy config\macro_calendar.example.json config\macro_calendar.json` (o `instalar_windows.bat`/`iniciar_painel.bat` já fazem). Confira as datas do FOMC no site oficial do Fed.
2. Rode o **diagnóstico de fontes** (somente leitura, 1 GET por fonte) na sua máquina: `diagnostico_fontes\diagnostico_fontes.py` (veja `--ajuda`; inclui as fontes macro com `--incluir-macro`). Compartilhe o relatório JSON para análise antes de habilitar qualquer fonte nova.
3. No `.env`: `BOT_INTEL_MODE=shadow`; reinicie o painel/robô. Nada muda nas decisões; o painel e `intel_decisions` mostram o que **teria** mudado.
4. Análise: `python scripts\analisar_noticias_shadow.py --paper` (decisões que mudariam x sem mudança, em janela macro, com dados degradados). Backtest com/sem: `--backtest --arquivo noticias.jsonl [--macro macro.jsonl] --liquidity N`. O relatório sempre traz o aviso: comparação descritiva, não é evidência de melhora.

## 13. Testes

`python scripts\run_tests_fallback.py tests\intel` (ou `pytest tests/intel`). Tudo é **simulado/automatizado**, exceto a verificação do `zoneinfo` do sistema (independente) e o diagnóstico de fontes reais (fora da suíte). Cobertura: classificação (resolução, negação, ambiente, Fed/macro), versões e duplicatas, independência, saúde/contingência/recuperação, coleta (prazo total, segundo plano, 429, teto diário, reinício), fuso e horário de verão, ICS/BLS/Tesouro, valores/revisões/disponibilidade, janelas e política de bloqueio, replay/backtest sem look-ahead, `off`/`shadow`/`enforce` no serviço, falhas de auditoria, migração do banco, painel (API e frontend em Node com DOM mínimo, **não** é navegador real) e invariantes de segurança.

## 14. Como verificar que LIVE continua bloqueado

* `Settings().mode == PAPER`, `Settings().intel.mode == "off"`.
* `tests/intel/test_intel_invariants.py` e `test_intel_security.py`: nenhum módulo de `app/intel` importa assinatura/Jupiter/executor/carteira/validador; nenhum faz POST; `enforce` de notícias, política macro `block` e contingência `reduced_risk` **não** autorizam LIVE (`LiveBlocked`).
* Manualmente: `BOT_MODE=live` sem os bloqueios específicos continua recusado pelo `build_runtime` (`app/execution/live_guard.py` inalterado).

## 15. Limitações e riscos residuais

* Classificação heurística (seção 4); falsos positivos custam oportunidade, falsos negativos deixam a estratégia técnica como está.
* Independência de fontes é aproximada (seção 5).
* O formato real de um incidente ativo do Solana Status, do iCalendar/API do BLS e do CSV do Tesouro **não foi observado**; parâmetros (TTLs, janelas, limiares) são **premissas não calibradas**.
* Calendário macro depende do cadastro do operador (datas do FOMC de terceiros; recorrência de Jobless Claims é estimativa e feriados deslocam o dia). Não há calendário antecipado para PCE/JOLTS/PPI sem o BLS iCalendar validado.
* O prazo total de requisição é verificado a cada pedaço recebido (conexão muda é limitada pelo timeout de leitura).
* O catálogo só aceita **nomes DNS** (recusa IPs em qualquer notação, host terminado em ponto e sufixos internos); a verificação é **sintática**: um nome público que aponte para IP privado ("DNS rebinding") não é detectado — o catálogo é arquivo do operador e redirecionamentos não são seguidos.
* A busca em segundo plano tem um **watchdog** (padrão 120 s): uma busca presa (ex.: DNS pendurado) é abandonada, o evento `busca_travada` é registrado e outra começa.
* Replay/backtest **não** aplica as flags de deduplicação ao vivo (`duplicada`, `similar_antigo`, `reapresentada`, versões de incidente por coleta): tende a bloquear **mais** que o modo ao vivo. Eventos do arquivo macro sem `known_at` valem "desde sempre".
* Janelas macro de importância **média** só **informam** (sem `risk_scale`); a cautela e o bloqueio macro valem para importância ≥ 3.
* Redirecionamentos HTTP **não são seguidos** (a URL deve ser a final); um redirecionamento vira falha com a URL de destino na mensagem.
* Valores macro não têm consenso; não há integração de S&P 500/Nasdaq/DXY.
* Backtest/replay: premissas de latência não calibradas; poucos eventos; **sem prova de rentabilidade**.

## 16. Mapa auditoria → correção

| Item da auditoria (2026-10-22) | Correção |
|---|---|
| A5 `shadow` alterado por falha de auditoria | `IntelRuntime.record_decision`/`link_position` à prova de falhas (seção 8) |
| A1 incidente novo ignorado por título repetido | versões por `event_key`; título repetido ≠ duplicata (seção 5) |
| A2 "will be resolved" fechava o bloqueio | resolução explícita/estruturada (seção 4) |
| A3 painel verde com fonte crítica fora | estado por fonte crítica + saúde do módulo (seção 6) |
| A4 bloqueio sem fim | contingência explícita, sem liberação pelo tempo (seção 6) |
| M1 falsa corroboração por sindicalização | origens independentes + incerteza (seção 5) |
| M2 "since 2022" descartava a notícia | filtro por ano removido (seção 4) |
| M3 "Federal Open Market Committee" por extenso | indicadores macro por extenso/abreviação (seção 4) |
| M4 devnet/explorer bloqueavam | ambiente e criticidade (seção 4) |
| M5 "identified/investigating" | vocabulário de incidente exigido (seção 4) |
| M6 negações/desmentidos | strip de negação + desmentido confirmado (seção 4) |
| M7 coleta síncrona/timeout por fase | thread de fundo, prazo total, 429/backoff (seção 7) |
| B1 redirecionamento não seguido | mantido por segurança; mensagem aponta o destino (seção 15) |
| B2 `max_feed_bytes` só depois do download | corte durante o download no `HttpxTransport` (seção 7) |
| B3 Atom pegava o primeiro `<link>` | escolhe `rel="alternate"` |
| B4 RSS 1.0 (RDF) com 0 itens | suportado |

## 17. Revisão independente (2026-10-22)
Um revisor independente (agente separado, somente leitura) inspecionou o código e rodou sondas próprias. **Não encontrou nenhum caminho** para a camada aumentar risco (> 1), operar, assinar, tocar em stops/kill switch/modo, alcançar código de carteira/Jupiter/RPC ou fazer uma falha de auditoria mudar a decisão. Encontrou problemas de robustez que foram **corrigidos e cobertos** em `tests/intel/test_intel_review_regressions.py`: (1) texto de fonte de baixa confiança fechava um bloqueio crítico; (2) um robô desligado por horas entrava em contingência; (3) o fallback de erro descartava restrições e um erro no módulo macro apagava as de notícias; (4) corrigir/remover um evento do calendário deixava janelas fantasmas, e o upsert não era atômico; (5) o filtro de hosts aceitava IPs em notações exóticas; (6) uma busca presa travava a coleta sem aviso; (7) data futura ficava marcada para sempre; (8) a montagem do registro de auditoria ficava fora do `try`; (9) a mensagem do painel afirmava uma restrição inexistente com `warn_only`. Limitações restantes apontadas e **não** resolvidas estão na seção 15 (replay mais permissivo que o ao vivo, janelas médias só informativas, SSRF sintático).
