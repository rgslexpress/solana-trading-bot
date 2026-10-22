# Robô de trading spot na Solana via Jupiter

Robô modular em Python para **testar hipóteses de trading** com preservação de capital: backtest realista, paper trading e (opcionalmente, e só com confirmação dupla) execução real pela Jupiter Swap API V2.

> **Não há promessa de lucro.** Nenhum parâmetro foi otimizado e os backtests incluídos usam dados **sintéticos**. A rentabilidade é uma hipótese a ser testada por você, com dados reais, antes de arriscar qualquer valor. O modo **LIVE vem desabilitado** e **nunca foi executado contra a Jupiter/Solana reais** neste projeto — leia `docs/LIMITACOES.md` antes de tudo.

## Início rápido (modo seguro)

```bash
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime = requirements.txt; LIVE (bloqueado) = requirements-live.txt
cp .env.example .env
cp config/tokens.example.json config/tokens.json            # revise os mints!
python scripts/check_environment.py
pytest                                                      # ou: python scripts/run_tests_fallback.py
python scripts/run_backtest.py --robustness                 # dados SINTÉTICOS: só valida a mecânica
python scripts/run_paper_trading.py --replay-days 20        # paper em replay offline
```

Com dados reais: exporte candles para `data/<SIMBOLO>_<tf>.csv` (`timestamp,open,high,low,close,volume`, timestamp em segundos UTC), defina `BOT_DATA_SOURCE=csv` e `BOT_DATA_LIQUIDITY_USD_OVERRIDE=<liquidez em USD da pool>` (0 = desconhecida ⇒ o robô **não opera**). Ou use `geckoterminal` (não verificado, ver limitações) com `pool` no `tokens.json`.

> **Transferindo para outro computador?** Veja `docs/INSTALACAO_WINDOWS.md` (publicação em repositório privado, instalação passo a passo, checklist e o que depende de internet).

> **Simulação com dados reais (sem dinheiro real):** backtest histórico e paper em tempo real — veja `docs/SIMULACAO_REAL.md`.

> **News & Market Intelligence (opcional, só restringe entradas, `off` por padrão):** status da Solana, notícias cripto e macroeconomia dos EUA (calendário FOMC/CPI/NFP, janelas de risco). Fontes, limites, estado de validação e como ativar o modo `shadow`: `docs/NEWS_INTEL.md`.

## Painel local no Windows (somente PAPER, somente 127.0.0.1)

Arquitetura: o robô roda **no seu computador**; o painel é um servidor HTTP local (biblioteca padrão do Python, sem dependências novas) que escuta **apenas em `127.0.0.1`**. Sem Vercel, VPS, túnel ou acesso remoto. O painel **só executa paper trading**: não existe botão, endpoint ou configuração que habilite LIVE, assine ou envie transações, e nenhuma chave/carteira é necessária.

**Instalar (uma vez)** — requer Python 3.10+ (alvo 3.12) no PATH. No Prompt de Comando, dentro da pasta do projeto:

```bat
scripts\instalar_windows.bat
```

(equivale a: `py -3 -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt`, e copia `.env.example` → `.env` e `config\tokens.example.json` → `config\tokens.json` se não existirem.) Revise `.env` e `config\tokens.json` (confira os mints).

**Iniciar o painel**:

```bat
scripts\iniciar_painel.bat
```

ou manualmente `.venv\Scripts\activate` e `python -m app.dashboard --abrir`. URL: **http://127.0.0.1:8765/** (porta em `BOT_PANEL_PORT`). Abrir o painel **não inicia o robô**.

**Iniciar / pausar / retomar / parar o paper trading**: pelos botões do painel (chamam o backend real) — *Iniciar paper trading*, *Pausar novas entradas*, *Retomar entradas*, *Parar paper trading*, *EMERGÊNCIA* (kill switch existente). Parar não envia ordens nem liquida: o estado fica salvo e é reconciliado ao iniciar; **enquanto parado, stops simulados não são monitorados** (o painel avisa). Procedimento alternativo sem painel: `python -m app.main --mode paper` (Ctrl+C para parar). O laço é único por base: painel e CLI **não** rodam ao mesmo tempo (o segundo é recusado).

**Parar o painel**: `Ctrl+C` na janela dele, ou `scripts\parar_painel.bat` (= `python -m app.dashboard --parar`). Encerrar o painel para o paper trading de forma organizada, sem ordens e sem apagar dados.

**Verificar que só há acesso local** (Prompt de Comando):

```bat
netstat -ano | findstr :8765
```

A coluna de endereço local deve mostrar **`127.0.0.1:8765`** (nunca `0.0.0.0:8765` nem o IP da rede). Qualquer tentativa de iniciar com outro `--host` é recusada.

**Dados e logs** (caminhos relativos à pasta do projeto, configuráveis no `.env`):

| O quê | Onde |
|---|---|
| Banco SQLite (posições, operações, sinais/decisões, intenções, patrimônio, alertas, estado do robô, kill switch) | `data\bot.sqlite3` (`BOT_DB_PATH`) |
| Logs JSON (segredos redigidos) | `logs\bot.log` (`BOT_LOG_FILE`) |
| Relatórios | `reports\` |
| Travas de instância / arquivo de execução do painel | `data\bot.sqlite3.robo.lock`, `.painel.lock`, `.painel.json` (removido ao encerrar) |

Nada é apagado automaticamente. Detalhes de segurança, limitações e testes: `docs/PAINEL.md`. **Não verificado:** uso em navegador real e execução no Windows (ver limitações).

**Importante sobre dados:** com `BOT_DATA_SOURCE=synthetic` (padrão do código) os candles são de 2025 e o painel mostrará dados **obsoletos** e o robô ficará em modo seguro. Para paper trading útil você precisa de `csv` (+ `BOT_DATA_LIQUIDITY_USD_OVERRIDE`) ou `geckoterminal` — este último **não foi validado contra a API real**.

## Comandos

```bash
python -m app.main --mode paper                  # padrão; relógio real + provedor configurado
python -m app.main --mode paper --once           # um ciclo
python -m app.main --mode backtest [--robustness] [--liquidity 5000000]
python -m app.main --health-check                # não opera
python -m app.main --show-config                 # segredos mascarados
python -m app.main --show-positions
python -m app.main --reconcile                   # resolve intenções pendentes/desconhecidas
python -m app.main --report                      # relatório operacional (paper/live)
python -m app.main --kill-switch "motivo"        # bloqueia novas entradas
python -m app.main --resume-kill-switch RETOMAR_OPERACAO --note "causa corrigida"
python -m app.main --mode live --confirm-live EU_ENTENDO_QUE_ISTO_USA_FUNDOS_REAIS   # ver docs/OPERACAO.md
```

Exemplo real de saída (sem confirmação de LIVE): `MODO LIVE BLOQUEADO: - confirmação dupla de LIVE ausente (BOT_LIVE_CONFIRM + --confirm-live)` (código de saída 2).

## Modos e proteções

- **BACKTEST**: só dados históricos. **PAPER** (padrão): mesmos limites de risco e custos, sem assinar/enviar nada — o `PaperExecutor` não importa carteira, assinador, Jupiter nem HTTP (verificado por teste de AST). **LIVE**: exige `BOT_MODE=live` + `BOT_LIVE_CONFIRM` + `--confirm-live` + assinador válido e coincidente com `BOT_WALLET_PUBLIC_KEY` + API key + allowlist + health OK + dados válidos + kill switch inativo. A presença de uma chave privada no ambiente **nunca** habilita o LIVE; em PAPER ela nem é lida.
- Toda ordem passa por `RiskManager`; transações só são assinadas depois de `validate_transaction` (tipo `ValidatedTransaction` não forjável).
- Envio nunca é repetido; resultado incerto ⇒ `UNKNOWN` + reconciliação on-chain.
- Detalhes: `docs/ARQUITETURA.md` (regras exatas da estratégia, risco, máquina de estados) e `docs/OPERACAO.md`.

## Estrutura

```
app/  config.py domain.py factory.py main.py http.py resilience.py logging_config.py
      data/ strategy/ risk/ execution/ storage/ backtest/ monitoring/ services/ utils/
tests/ unit/ integration/ security/ backtest/      scripts/      config/      docs/
Dockerfile docker-compose.yml pyproject.toml requirements*.txt .env.example .gitignore
```

## Funcionalidades

**Implementadas e testadas (rede simulada / dados sintéticos):** indicadores (EMA, MACD, RSI Wilder, Bollinger, ATR Wilder, volume relativo, correlação); validação de candles; features causais com HTF só após fechamento; regras BUY/HOLD/SELL/NO_TRADE; dimensionamento por risco; stop ATR, TP por R:R, break-even líquido de custos, trailing; limites diários/acumulados/consecutivos/cooldown/exposição; kill switch persistente; backtest com custos, slippage, liquidez, métricas, divisão dev/validação/OOS, sensibilidade e walk-forward; paper trading persistente com recuperação após reinício; SQLite com intents idempotentes e auditoria; cliente Jupiter `/order`+`/execute`; validador de transações; assinatura Ed25519 local; cliente RPC; reconciliação; live guard; logs JSON com redação; alertas (Telegram opcional); CLI; Docker.

**Pendentes / não implementadas:** take-profit parcial; correlação com BTC; concentração de liquidez e bases de fraude (autoridades de mint/freeze, Token-2022 e decimais já são verificados no startup do LIVE); teste automático de capacidade de venda; failover de RPC; liquidação automática de posições em falha (política opcional); ordens condicionais on-chain (a integração escolhida não as oferece — não foram inventadas).

**Auditoria técnica:** ver `docs/AUDITORIA.md` (achados, correções e o que ainda precisa ser validado com rede). Scripts somente leitura para validar APIs reais: `scripts/dry_run_quote.py` (cotação Jupiter + validação da transação, sem assinar) e `scripts/check_data_provider.py`.

**Dependem de validação manual sua:** campos reais da API Jupiter V2; provedor de candles/liquidez real; `pytest`/`ruff`/`mypy` no seu ambiente; build do Docker; Python 3.12; qualquer execução LIVE.

## Critérios de aceitação — evidências

| Critério | Evidência |
|---|---|
| Executar testes automatizados | 172 testes passam (`docs/RELATORIO_TESTES.md`), via runner de contingência em Python 3.10 |
| Indicadores corretos | `tests/unit/test_indicators.py` (EMA manual, RSI Wilder com valor publicado, MACD, Bollinger, ATR, causalidade) |
| Sinais sem dados futuros | `test_no_lookahead_*`, `test_htf_candle_not_used_before_it_closes`, `test_future_data_does_not_change_past_trades` |
| Risco, tamanho e limites | `tests/unit/test_risk.py` (perda no stop ≈ 0,5%; caps; diário; consecutivas; acumulada) |
| Simular compras/vendas, persistir, recuperar após reinício | `test_full_paper_lifecycle_and_persistence_across_restart` |
| Resultados líquidos de custos | `test_equity_accounting_matches_trade_ledger`, `test_costs_reduce_result...` |
| Rejeitar dados inválidos / cotações desatualizadas | `test_data_validation.py`; `test_expired_quote_before_signing`; `test_stale_or_unavailable_data...` |
| Impedir operações duplicadas | `test_duplicate_intent_is_rejected_by_database`; `test_network_failure_on_execute...never_resubmits`; `test_failed_buy_is_not_retried...` |
| Bloquear entradas ao atingir limites | `test_kill_switch_blocks_entries...`; `test_repeated_execution_failures_trip_kill_switch` |
| LIVE desabilitado por padrão / exige habilitação explícita | `test_default_mode_is_paper...`; `test_live_requires_double_confirmation...`; `test_live_guard_lists_every_blocker...` |
| Paper não envia transações reais | `test_paper_executor_source_has_no_signing_or_network_capability`; `test_paper_runtime_never_loads_signer...` |
| Validar transações antes de assinar | `test_tx_validation_and_signer.py` (14 casos) |
| Resultados desconhecidos tratados por reconciliação | `test_reconcile_failed_expired_and_signed_never_sent`; `test_network_failure_on_execute...` |
| Não registrar segredos | `test_logging_redaction.py`; `test_settings_repr_never_contains_secrets`; `test_cli_show_config_masks_secrets...` |
| Documentar limitações | `docs/LIMITACOES.md` |
| **Instalar em ambiente limpo** | **NÃO verificado** (sem acesso ao PyPI no ambiente de desenvolvimento) |
| **Qualidade estática (ruff/mypy)** | **NÃO executada** |

## Licença e aviso

Uso por sua conta e risco. Não é aconselhamento financeiro. Criptoativos podem perder todo o valor.
