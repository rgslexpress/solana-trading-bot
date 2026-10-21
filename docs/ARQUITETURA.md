# Arquitetura e decisões técnicas (Fase 1)

## Documentação consultada

Consultada por **resumos de busca** (acesso direto bloqueado no ambiente — ver `LIMITACOES.md`):

- Jupiter Swap API: <https://developers.jup.ag/docs/swap> e <https://developers.jup.ag/docs/swap/order-and-execute>
- Migração V1/Ultra → V2: <https://developers.jup.ag/docs/swap> (guia de migração)
- Solana: <https://solana.com/docs> (formato de mensagem/transação usado no validador: legacy e v0)

## Fluxo Jupiter escolhido

`GET /order` (com `taker`) → validação local da cotação e da transação → **assinatura local** → `POST /execute` (`signedTransaction`, `requestId`).

Por quê: é o fluxo gerenciado (a Jupiter faz o envio e o *landing*), tem menos partes móveis que `/build` + envio próprio e mantém a chave no processo local. `/build` (instruções cruas, só Metis) fica como evolução futura. `/quote` e `/swap` (V1) e `ultra-api` não são usados (deprecated segundo a documentação).

## Camadas

```
data/        providers.py (Synthetic | CSV | GeckoTerminal) + validation.py   -> candles fechados e validados
strategy/    indicators.py (causais) + signal_engine.py (features + regras)    -> BUY/HOLD/SELL/NO_TRADE
risk/        sizing.py, stops.py, risk_manager.py, kill_switch.py              -> NADA executa sem passar aqui
execution/   base.py (interface) | paper.py | jupiter_{client,executor}.py
             tx_validator.py, signer.py, rpc.py, live_guard.py, costs.py
storage/     database.py (SQLite; intents com índice único parcial)
backtest/    engine.py, metrics.py, robustness.py, report.py, runner.py
monitoring/  alerts.py, health.py
services/    trading_service.py (ciclo), scheduler.py, reporting.py
factory.py   escolhe o executor pelo modo; PAPER nunca importa/lê assinatura
main.py      CLI
```

Desvios deliberados da estrutura sugerida: módulos menores foram fundidos (`sizing`+`stops` em vez de 4 arquivos de risco; `rpc.py` no lugar de `transaction_manager/reconciler` separados — a reconciliação vive no executor Jupiter porque depende do mesmo RPC/assinador). Sem pydantic: configuração com `dataclasses` + validação própria (menos dependências, 100% testável offline).

## Estratégia (regras exatas, v1.0)

Decisão no **fechamento** do candle de entrada (15m), usando só candles fechados; HTF (1h) só após fechar.

BUY exige **todos** os portões e pontuação ≥ 6 (máx. 8):

1. Timeframe superior favorável: fechamento 1h > EMA(200) e EMA(200) não decrescente (3 candles) — +2.
2. EMA9 > EMA21 — +1; cruzamento de alta nos últimos 3 candles — +1 (evita entrada tardia).
3. MACD(12,26,9): histograma > 0 — +1; crescente — +1.
4. RSI(14) ≤ 65 — +1 (RSI nunca é gatilho isolado).
5. Preço ≤ banda superior de Bollinger(20,2) e ≤ 3 ATR acima da EMA21 — +1.
6. Filtros que forçam NO_TRADE: volume relativo < 0,8; liquidez desconhecida/baixa; custo de ida > 60 bps; sem HTF; (opcional) ativo de referência em tendência contrária com alta correlação.

SELL (com posição): EMA9 < EMA21; ou MACD negativo e em queda com preço < EMA9; ou RSI ≥ 70 com preço > banda superior; ou HTF desfavorável. Saídas por stop/take-profit são avaliadas à parte (monitoramento de preço).

Parâmetros iniciais são **pontos de partida experimentais**, não valores ótimos.

## Risco (definições)

- Patrimônio = caixa + posições marcadas a mercado.
- Risco por operação 0,5% (padrão); acima de 1% exige justificativa em configuração; teto absoluto 2%.
- Tamanho: perda no stop (incluindo taxas e slippage de ida e volta) ≈ risco planejado; limitado por caixa, exposição por token/total, impacto máximo e teto por ordem.
- Stop = entrada − 2·ATR(14); TP = 2R; break-even em +1R **mais custos de ida e volta**; trailing 2,5·ATR após +1,5R; o stop nunca diminui.
- Perda diária: referência fixada na primeira observação de cada dia UTC; bloqueia entradas ao atingir 2%. Perda acumulada ≥ 10% aciona o kill switch. Também: máx. operações/dia, perdas consecutivas + período de espera, reserva de SOL.
- Política ao atingir limites: **bloquear entradas e preservar posições** (única política implementada; liquidar tudo é opt-in futuro).

## Máquina de estados da intenção

`CREATED → VALIDATED → QUOTED → SIGNED → SUBMITTED → CONFIRMED → RECONCILED`, com `FAILED`, `EXPIRED`, `UNKNOWN`. Transições inválidas lançam exceção. `SUBMITTED` e a assinatura são persistidos **antes** do `POST /execute`. Rede/5xx em `/execute` ⇒ `UNKNOWN` (nunca reenvia). Reconciliação por assinatura: confirmada ⇒ `CONFIRMED/RECONCILED` com quantidades de `getTransaction`; com erro ⇒ `FAILED`; ausente após `unknown_expire_after_s` (> validade do blockhash) ⇒ `EXPIRED`. Índice único parcial no SQLite impede duas intenções ativas para o mesmo (token, lado).

## Principais riscos tratados

Chave privada (isolamento por modo, redação de logs, arquivo 0600), transação maliciosa/inesperada (validação fail-closed antes de assinar), duplicidade de ordens (persist-before-send + índice único + sem retry de envio), dados ruins (validação rígida), execução ruim (checagem de preço/impacto/idade da cotação/teto por ordem), falhas em cascata (kill switch automático), LIVE acidental (modo padrão paper + dupla confirmação + guard + executor que exige autorização).
