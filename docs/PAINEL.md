# Painel local — arquitetura, segurança, testes e limitações

## Inspeção do projeto (antes das mudanças)

- Não existia API, interface nem gerenciador de execução contínua. O robô iniciava por `python -m app.main --mode paper` (laço `run_forever`, parada por Ctrl+C).
- Persistência: SQLite já existente (`posições`, `intents`, `sinais`, `equity`, `alertas`, `kill_switch_events`, `meta`). Reaproveitada.
- **Não havia trava contra duas instâncias** sobre a mesma base (risco de operações simuladas duplicadas). Adicionada (`app/instance.py`), inclusive no CLI.

## Escolha arquitetural (menor mudança)

Processo único local: `python -m app.dashboard` = servidor HTTP (`http.server`, biblioteca padrão) + *thread* do laço de paper trading controlada pelo usuário. Sem framework novo (FastAPI/Flask não instaláveis aqui e desnecessários), sem build de frontend (HTML/CSS/JS estáticos servidos pelo próprio servidor).

```
app/dashboard/server.py      HTTP loopback + defesas (Host, Origin, CSRF, CSP, esquema estrito)
app/dashboard/controller.py  iniciar/pausar/retomar/parar/emergência; laço com recuperação de erros
app/dashboard/readmodel.py   leitura SOMENTE LEITURA da base + métricas (None => "N/D")
app/dashboard/static/        index.html, app.js, app.css (sem inline, sem innerHTML)
app/instance.py              trava de instância (msvcrt no Windows / fcntl no POSIX)
```

Alterações em código existente (aditivas): `trading_service.py` (pausa de entradas, `data_status`, `last_cycle`, `ks.refresh()` por ciclo), `kill_switch.py` (`refresh`), `database.py` (tabela `bot_events` + `save_bot_event`), `config.py` (`panel_port`), `main.py` (trava do laço).

## Segurança local

| Ameaça | Defesa | Teste |
|---|---|---|
| Exposição na rede | Bind somente em `127.0.0.x`; `0.0.0.0`, IP de LAN, `::`, `localhost` e vazio são **recusados na criação do servidor**; `SO_EXCLUSIVEADDRUSE` no Windows | `test_server_refuses_non_loopback_hosts_and_binds_127_0_0_1`; processo real inspecionado com `ss -ltn` |
| DNS rebinding | Host deve ser `127.0.0.1:<porta>` ou `localhost:<porta>` | `test_host_header_must_be_loopback_blocks_dns_rebinding` |
| CSRF / página de outro site controlando o robô | POST exige token aleatório por execução (meta tag da página), `Origin` igual ao do painel, `Content-Type: application/json` (impede formulários "simples"), `Sec-Fetch-Site` não cross-site | `test_post_requires_csrf_origin_content_type_size_and_valid_json`, `test_cross_origin_page_cannot_control_the_bot...` |
| Leitura cross-origin das respostas | Nenhum cabeçalho CORS; OPTIONS/PUT/DELETE recusados | `test_index_has_csrf_token_csp...`, `test_methods_other_than_get_post...` |
| XSS | CSP `default-src 'none'; script-src 'self'`, sem JS/CSS inline, frontend só usa `textContent` | `test_frontend_source_has_no_dynamic_html...` |
| Comandos maliciosos / contorno de limites | Esquema estrito: nenhuma rota aceita campos extras (modo, chaves, limites, config); emergência exige `{"confirm": true}` | `test_strict_schema_no_endpoint_accepts_mode_keys_or_config` |
| LIVE ou envio de transações pelo painel | Modo forçado a PAPER; executor verificado como `PaperExecutor`; o pacote do painel não importa assinador/Jupiter/RPC/httpx (teste de AST); `Signer`/`_build_live` nunca tocados num ciclo completo | `test_dashboard_package_has_no_path...`, `test_full_start_stop_cycle_never_touches_signer_or_live_builder` |
| Contornar o kill switch | "Retomar entradas" recusado com kill switch ativo; retomar o kill switch exige robô parado + frase + justificativa (mesmo procedimento do CLI) | `test_emergency_triggers_existing_kill_switch...` |
| Vazamento de segredos | `/api/config` é *whitelist* (sem carteira/RPC/chaves); logs/alertas/eventos redigidos; nenhuma credencial no JS | `test_config_endpoint_is_a_whitelist...`, `test_logs_endpoint_redacts_secrets` |
| Duas instâncias / corridas | Trava do SO para o laço (painel **e** CLI) e para o painel; comandos serializados; leituras em conexão somente leitura | `test_only_one_trading_loop_per_database...`, `test_second_server_on_same_port...` |

Observações: se `BOT_MODE=live` estiver no `.env`, o painel **ignora** e avisa (não falha silenciosamente). O token CSRF fica também em `data\bot.sqlite3.painel.json` enquanto o painel roda (usado por `--parar`); é um segredo local do processo, não uma credencial de fundos, e o arquivo é removido ao encerrar.

## Comportamento operacional

- Abrir o painel não inicia nada. Início/pausa/retomada/parada e emergência chamam o backend real; o estado "pausar entradas" é persistido.
- Parar (ou encerrar o painel) **não envia ordens nem liquida**; registra `paper_parado`/`painel_encerrado_com_posicoes` e avisa que stops **não são monitorados enquanto parado**. No próximo início o serviço restaura posições/caixa/risco e reconcilia.
- Erros no ciclo: o laço sobrevive, registra evento, aplica *backoff*; após N erros consecutivos pausa entradas automaticamente (e **não** retoma sozinho).
- Modo seguro (startup reprovado: dados ruins, kill switch, pendências) continua monitorando posições e tenta se recuperar a cada ciclo.
- Métricas ausentes/incalculáveis aparecem como **N/D** (ex.: Sharpe nunca é calculado no painel; drawdown exige ≥ 2 registros de patrimônio). Todos os valores são rotulados como **SIMULADOS**; não existe fonte de resultados reais.
- P/L não realizado usa o último preço de candle fechado e é **bruto de custos de saída**.

## Testes (ver `RELATORIO_TESTES.md` para a execução mais recente)

Reais (servidor HTTP e controlador reais em loopback, SQLite real, threads reais): segurança do servidor, ciclo de vida, persistência/reinício, emergência/kill switch, recuperação de erros, instância única, encerramento. **Com mocks/determinísticos:** provedor de candles e relógio (`ArrayProvider`), falhas injetadas em `run_cycle`. **Fumaça do frontend:** `app.js` executado em Node com um DOM mínimo escrito à mão — **não é um navegador**.

## Limitações e não verificados

- **Nenhum navegador real foi usado.** Layout, responsividade, `confirm()` e `fetch` em Chrome/Edge não foram verificados visualmente.
- **Windows não foi testado** (ambiente de desenvolvimento é Linux): `msvcrt.locking`, `SO_EXCLUSIVEADDRUSE`, URI SQLite `file:///C:/...`, `.bat`, `signal.SIGTERM`, `webbrowser.open`. Implementados conforme a documentação do Python/SQLite, mas sem execução. Verifique com `netstat -ano | findstr :8765`.
- O painel não valida nem substitui a validação das integrações externas (Jupiter, GeckoTerminal, RPC) — continuam pendentes como em `AUDITORIA.md`. Com `synthetic` os dados são obsoletos; `csv`/`geckoterminal` precisam ser configurados e validados.
- Servidor `http.server` é adequado para uso local de um usuário; não é endurecido para internet (e não deve ser exposto).
- Alterar configuração/limites **não** é possível pela interface (somente leitura) — deliberado nesta fase.
- Perdas por indisponibilidade: se o computador dormir/desligar, o robô não monitora; em paper isso só afeta a simulação.
- O painel mostra apenas PAPER; a conclusão do frontend **não** é aprovação para operar com dinheiro real.

## Seção "News & Market Intelligence"
Somente leitura (`GET /api/intel`). Mostra a **saúde de cada fonte** (SAUDÁVEL/DEGRADADO/CRÍTICO/CONTINGÊNCIA; fonte crítica fora do ar nunca aparece como saudável), a caixa de **contingência** (há quanto tempo, política aplicada, decisões afetadas), alertas e bloqueios, últimas notícias, o **histórico de como o módulo influenciou cada decisão**, os eventos de saúde e o bloco **Macro EUA** (janelas de risco, próximos eventos com horário UTC e local, valores divulgados com expectativa N/D quando a fonte não fornece, rendimentos do Tesouro e fontes pendentes). "Nenhum evento relevante" (verde) só aparece com fontes saudáveis; fontes indisponíveis **não** significam ausência de eventos. Veja `docs/NEWS_INTEL.md`, seção 10.
