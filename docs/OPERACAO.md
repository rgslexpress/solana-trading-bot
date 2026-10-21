# Procedimentos operacionais

## Ativação manual e consciente do modo LIVE

**Não faça isso antes de concluir o paper trading com dados reais por um período significativo.**

1. Crie uma carteira Solana **nova e dedicada** ao robô. Nunca use a carteira principal nem informe frase-semente.
2. Deposite um valor **que você aceita perder integralmente**, mais ~0,05 SOL para taxas.
3. Salve a chave privada em um **arquivo** fora do repositório (`chmod 600`), formato JSON de 64 bytes ou Base58. Configure `BOT_WALLET_SECRET_FILE` e `BOT_WALLET_PUBLIC_KEY` (a chave pública derivada precisa coincidir).
4. Obtenha uma API key da Jupiter (`BOT_JUPITER_API_KEY`) e use um RPC confiável (`BOT_RPC_URL`, https).
5. **Verifique contra a documentação oficial atual** os campos listados em `LIMITACOES.md` §1 (nomes de campos de `/order` e `/execute`). Faça uma cotação manual (`GET /order` sem `taker`) e compare com o que o parser espera.
6. Revise a allowlist (`config/tokens.json`): confira cada mint e pool em fontes oficiais.
7. Defina limites **irrisórios**: `BOT_EXECUTION_MAX_ORDER_QUOTE=5`, `BOT_RISK_MAX_OPEN_POSITIONS=1`.
8. Rode `python -m app.main --health-check` e `python scripts/inspect_wallet.py` (somente leitura).
9. Configure `BOT_MODE=live` e `BOT_LIVE_CONFIRM=EU_ENTENDO_QUE_ISTO_USA_FUNDOS_REAIS`.
10. Inicie com a segunda confirmação explícita:
    `python -m app.main --mode live --confirm-live EU_ENTENDO_QUE_ISTO_USA_FUNDOS_REAIS`
11. Acompanhe os logs e o banco durante as primeiras operações. Só aumente limites depois de várias operações reais corretas.

Se qualquer verificação falhar, o robô **recusa** o LIVE e lista os motivos.

## Kill switch

- Ativar manualmente: `python -m app.main --kill-switch "motivo"` (bloqueia novas entradas; preserva posições; não cancela transações já enviadas).
- Ativação automática: perda acumulada ≥ limite, dados indisponíveis por N ciclos, falhas de execução consecutivas, intenção desconhecida por tempo excessivo.
- Retomar (exige causa corrigida): `python -m app.main --resume-kill-switch RETOMAR_OPERACAO --note "causa e correção"`.
- Depois de qualquer kill switch: `python -m app.main --reconcile`, `--show-positions` e confira a carteira **on-chain** manualmente.

## Reinício / recuperação

Na inicialização o robô: valida config, integridade do banco, restaura posições/stops/caixa virtual/estado de risco, encerra intenções interrompidas antes do envio, **reconcilia** intenções enviadas/desconhecidas por assinatura, valida dados, checa kill switch e saúde. Qualquer falha crítica ⇒ **modo seguro** (monitora posições, sem novas entradas).

## Atualização de dependências

`pip install -r requirements-dev.txt`, `pytest`, `ruff check .`, `mypy app`, depois execute backtest e paper antes de promover qualquer atualização. Reproduza versões com `pip freeze > requirements.lock`.
