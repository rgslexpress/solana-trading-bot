# Limitações, riscos e itens não verificados

Este documento é parte da entrega. Leia antes de qualquer uso com fundos reais.

## 1. O que NÃO foi verificado (honestidade sobre o ambiente de desenvolvimento)

O ambiente em que o código foi escrito **não tinha acesso à rede** (domínio `dev.jup.ag` bloqueado, PyPI inacessível). Consequências:

| Item | Situação |
|---|---|
| Documentação da Jupiter | Consultada apenas via **resumos de busca** (Swap API V2: `GET /order`, `POST /execute`, `GET /build`, cabeçalho `x-api-key`, base `https://api.jup.ag/swap/v2`). Não foi possível abrir as páginas oficiais. |
| Nomes de campos da resposta de `/order` e `/execute` | `transaction`, `requestId`, `inAmount`, `outAmount`, `inputMint`, `outputMint`, `priceImpactPct`, `signedTransaction`, `status`, `code`, `signature`, `inputAmountResult`, `outputAmountResult` vêm de resumos. **Confirme contra a resposta real** (o parser falha de forma fechada se faltar algo essencial). |
| Parâmetro de slippage em `/order` | Não confirmado. Por isso a proteção de preço é feita **no cliente** (preço efetivo vs. referência, quantidade máxima de entrada, impacto). `send_slippage_param` existe, desligado por padrão. |
| Unidade de `priceImpactPct` | Não confirmada; tratada como percentual (conservador). A verificação independente de desvio de preço vs. referência cobre o caso. |
| GeckoTerminal (candles e `reserve_in_usd`) | Formato **não verificado** contra a API real. Validado apenas com respostas simuladas. |
| Execução LIVE | **Nunca foi executada** contra Jupiter/Solana reais. Apenas testes com rede simulada. |
| Python 3.12 | Testes rodaram em Python **3.10.12**. O código evita recursos exclusivos do 3.11+, mas 3.12 não foi exercitado. |
| `pytest`, `ruff`, `mypy`, `httpx` | Não instaláveis no ambiente. A suíte foi executada com o runner de contingência (`scripts/run_tests_fallback.py`); `httpx` só é importado em produção (`HttpxTransport`). **Rode `pytest`, `ruff check` e `mypy app` no seu ambiente.** |
| Docker | Dockerfile/compose escritos mas **a imagem não foi construída**. |
| Telegram | Testado apenas com função de envio injetada. |
| Versões fixadas | `pandas`, `numpy` e `cryptography` foram as versões efetivamente usadas; `httpx`, `pytest`, `ruff`, `mypy` são pins **não confirmados** no PyPI. |

## 2. Limitações da estratégia e do backtest

- Os backtests incluídos usam dados **sintéticos** (passeio aleatório): validam a mecânica do motor, **não dizem nada sobre o mercado**. Nessas séries a estratégia teve resultado líquido negativo (esperado para dados sem padrão explorável).
- **Não há evidência de rentabilidade.** Nenhum parâmetro foi otimizado.
- Liquidez histórica de pools não está disponível: o backtest usa uma **premissa fixa** (`--liquidity`). Slippage/impacto são modelos simples (`base + k·notional/liquidez`).
- Stop e take-profit são avaliados com high/low do candle; ordem dentro do candle é desconhecida (assume-se stop primeiro). Latência de stop (`stop_latency_bps`) e falhas de execução (`execution_failure_rate`) são parâmetros **não calibrados**. Ver `docs/AUDITORIA.md`.
- Saída parcial de lucro **não foi implementada** (apenas saída total).
- Correlação com SOL é um filtro opcional simples (`use_ref_filter`); correlação com BTC não está implementada.
- Multi-timeframe: o timeframe de 4h é suportado como `htf_timeframe`, mas não há um terceiro nível simultâneo.

## 3. Limitações de execução e segurança

- **Stops são monitorados pelo processo, não são ordens on-chain.** Se o processo cair, perder conexão ou ficar sem dados, o stop **não executa**. Perdas podem exceder o planejado (gaps, slippage, token impossível de vender). O risco de 0,5% é um **limite de planejamento**, não uma garantia.
- Verificação do token é **manual**: allowlist de mints + liquidez mínima + custo/impacto. Autoridades de mint/freeze, extensões Token-2022 e decimais são verificados no startup do LIVE (`token_checks.py`; formato do RPC não verificado). **Não implementado:** concentração de liquidez, bases de fraude, teste automático de capacidade de venda.
- A validação de transações (`tx_validator.py`) é *fail-closed* mas tem limites: não enxerga CPIs internas das DEXs (confia nos programas da allowlist); destinos de SOL só são aceitos se forem a ATA de wSOL da carteira (derivação PDA **sem vetor de teste real**) ou contas criadas na própria transação; argumentos do roteador só são lidos para os discriminadores `route`/`shared_accounts_route` do Jupiter v6 (**não confirmados para a V2**) — por isso o modo estrito pode rejeitar transações legítimas até a validação com `scripts/dry_run_quote.py`; contas de Address Lookup Tables são rejeitadas quando relevantes. Rotas que usem programas fora da allowlist serão rejeitadas até você adicioná-los (`BOT_EXECUTION_EXTRA_ALLOWED_PROGRAMS`) **após verificá-los**.
- A taxa de prioridade só é **limitada** na validação (a transação vem pronta da Jupiter); não é definida pelo robô.
- Taxa de rede real é lida de `getTransaction` quando disponível, mas o custo é registrado em lamports e **estimado** em moeda de referência (`network_fee_quote`).
- `SOL` como moeda de referência está implementado mas foi menos exercitado que `USDC`.
- Um único RPC/provedor por função; sem failover automático entre provedores.
- Reconciliação depende de `getSignatureStatuses`/`getTransaction`. Um RPC lento pode deixar intenções em `UNKNOWN`; novas entradas ficam bloqueadas e, após `unknown_kill_after_s`, o kill switch é acionado.
- O kill switch **não cancela** transações já enviadas nem liquida posições.
- `max_order_quote` limita o tamanho por ordem; comece com valores irrisórios.

## 4. Superfície de ataque

- Chave privada: lida só do arquivo/variável configurado, apenas no modo LIVE, após a confirmação dupla; nunca registrada (filtro de redação nos logs/alertas/relatórios) e não serializável (`Signer.__reduce__`).
- Não há API/porta de administração. A administração é pela linha de comando local.
- Respostas de APIs/metadados de tokens **não** alteram limites de risco nem configurações.
- Dependências externas: Jupiter (cotação/envio), RPC Solana, provedor de candles. Qualquer um pode falhar ou mentir; por isso há validação de dados, checagem de preço vs. referência e confirmação on-chain.
