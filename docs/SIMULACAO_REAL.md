# Simulação com dados reais (sem dinheiro real)

Tudo aqui é **PAPER**: nenhuma carteira, chave, assinatura ou transação. Os resultados são **simulados** e **não provam rentabilidade**.

| Modo | Dados | Execução | Rede | Serve para |
|---|---|---|---|---|
| 1. Replay sintético (já feito) | passeio aleatório | simulada | não | checar que o robô/banco/relatório funcionam |
| 2. **Backtest histórico real** | CSV baixado do GeckoTerminal | simulada (custos + slippage modelados) | só para baixar | ver como a estratégia teria se comportado, com separação dev/validação/fora da amostra |
| 3. **Paper em tempo real** | GeckoTerminal ao vivo | simulada | sim, contínua | ver decisões do robô no mercado de agora, sem arriscar dinheiro |

**Não validado:** o contrato da API do GeckoTerminal (escrito a partir da documentação; nunca chamado). Seu computador será o primeiro teste real; por isso o passo A é só leitura e vem primeiro.

## Pré-requisito: endereço da pool de cada token

1. Abra geckoterminal.com, busque o token e abra a página da **pool com maior liquidez** (par com USDC).
2. O endereço está na URL: `https://www.geckoterminal.com/solana/pools/<ENDERECO_DA_POOL>`.
3. Edite `config\tokens.json` e preencha `"pool"` (mantenha o `mint` oficial; confira-o em fonte oficial). **Comece com UM token** (apague o outro da `allowlist`) para respeitar o limite de ~30 chamadas/min:
```json
{ "symbol": "JUP", "mint": "<MINT_OFICIAL>", "decimals": 6, "pool": "<ENDERECO_DA_POOL>", "min_liquidity_usd": 500000 }
```
Não use endereços sugeridos por terceiros sem conferir.

## A. Validar o provedor de dados (somente leitura)

No `.env`: `BOT_DATA_SOURCE=geckoterminal`. No PowerShell, na pasta do projeto:
```powershell
.\.venv\Scripts\python.exe scripts\check_data_provider.py
```
Esperado: `[OK] JUP/15m: ... candles ... fechados`, `[OK] JUP/1h ...`, liquidez e último preço. `[INVÁLIDO]` com "lacuna" = pool com pouca negociação (candles ausentes não são inventados). Compare 3–4 candles com o gráfico do GeckoTerminal (timestamp é a **abertura**? o volume é o da pool?). Me envie a saída se algo divergir.

## B. Backtest com histórico real

1. Baixar (somente leitura; não sobrescreve arquivos existentes):
```powershell
.\.venv\Scripts\python.exe scripts\baixar_candles.py --dias 60
```
Gera `data\<SIMBOLO>_15m.csv` e `_1h.csv` e imprime a **liquidez atual da pool**. Se houver lacunas e você aceitar candles "vazios" preenchidos pelo provedor (OHLC = fechamento anterior, volume 0), use `--preencher-vazios`. Para mais robustez use mais dias (`--dias 180`).
2. No `.env`: `BOT_DATA_SOURCE=csv` e `BOT_DATA_LIQUIDITY_USD_OVERRIDE=<valor>` (use um valor **conservador**, ex.: metade da liquidez impressa).
3. Rodar:
```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py --liquidity <mesmo valor> --robustness
```
O relatório fica em `reports\backtest_*.md` (+ `.json` e `_trades.csv`).

**Como ler sem se enganar**
- Olhe o período **fora da amostra** (último quarto, que nunca foi usado para escolher nada). Desenvolvimento e validação não valem como prova.
- Poucas operações (< ~30) = **inconclusivo**, não "bom" nem "ruim".
- Compare com `benchmark_buy_hold_return_pct`, veja `total_costs` e o `max_drawdown_pct`.
- **Não ajuste parâmetros até o resultado ficar bonito**: isso é sobreajuste. A tabela de sensibilidade serve para ver se o resultado é frágil.
- Limites do simulador: liquidez fixa, slippage modelado (não é o livro real), sem MEV, sem congestionamento, `stop_latency_bps` e falhas de execução são premissas **não calibradas**.

## C. Paper trading em tempo real (painel)

No `.env`:
```text
BOT_MODE=paper
BOT_DATA_SOURCE=geckoterminal
BOT_POLL_SECONDS=60
BOT_DATA_LIQUIDITY_USD_OVERRIDE=<teto conservador de liquidez>
```
Abra o painel (`.\scripts\iniciar_painel.bat`) e clique em **Iniciar paper trading**.
- **Esperado:** estado `RODANDO` sem "MODO SEGURO"; em *Fontes de dados* a idade do último candle fechado fica abaixo de ~2,5 candles; em *Histórico de decisões* aparecem as decisões a cada candle novo de 15 min com os motivos (a maioria será NO_TRADE; **dias sem nenhuma entrada são normais**).
- **Se aparecer MODO SEGURO:** leia o aviso (dados inválidos, lacuna, API indisponível, pool ausente, 429).
- O computador precisa ficar **ligado e sem suspender**; parado, o robô não monitora stops simulados.
- Deixe rodar por **semanas** antes de qualquer conclusão e compare com o backtest. Rate limit: ~4 chamadas por token por ciclo; com 60 s e 1–2 tokens fica dentro do limite gratuito.

## O que esta simulação NÃO cobre (importante)

- A execução é **modelada** (taxa + slippage + impacto linear), não vem de cotações reais da Jupiter; a qualidade real de execução, rotas, falhas de transação e MEV **não são medidas**. Para comparar com a Jupiter real existe `scripts\dry_run_quote.py` (somente leitura; exige chave de API da Jupiter e um endereço **público** qualquer; nunca chave privada) — ainda **não validado**.
- Liquidez é um **instantâneo** da pool.
- Resultados simulados bons **não** autorizam LIVE. LIVE continua bloqueado e fora do escopo.
