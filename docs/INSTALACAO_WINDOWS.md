# Guia: publicar em repositório privado, instalar no Windows e validar (somente PAPER)

> **LIVE permanece bloqueado em todo este procedimento.** Nenhum passo pede chave privada, seed phrase, carteira ou fundos. Nenhum passo assina ou envia transações. O painel escuta **somente em `127.0.0.1`**.
>
> **Honestidade sobre validação:** o projeto foi desenvolvido e testado em **Linux, Python 3.10.12, sem internet**. **Nada foi executado em Windows** e **nenhuma API externa (Jupiter, GeckoTerminal, RPC) foi validada**. As verificações marcadas **[VOCÊ]** só podem ser confirmadas na sua máquina.

Convenções: **[CMD]** = Prompt de Comando; **[PS]** = PowerShell; **[AMBOS]** = o mesmo comando funciona nos dois. `<URL_DO_REPOSITORIO>` e `<SEU_USUARIO>` são valores que **você** fornece.

## 0. Diferenças que importam entre CMD e PowerShell

| Tarefa | CMD | PowerShell |
|---|---|---|
| Ativar o ambiente virtual | `.venv\Scripts\activate.bat` | `.\.venv\Scripts\Activate.ps1` (pode exigir liberar scripts, ver passo 6) |
| Copiar arquivo | `copy .env.example .env` | `Copy-Item .env.example .env` |
| Variável só nesta sessão | `set BOT_PANEL_PORT=8800` | `$env:BOT_PANEL_PORT="8800"` |
| Ver arquivo | `type logs\bot.log` | `Get-Content logs\bot.log` |
| Acompanhar log ao vivo | (não há equivalente simples) | `Get-Content logs\bot.log -Tail 50 -Wait` |
| Procurar texto | `netstat -ano \| findstr :8765` | `netstat -ano \| Select-String ":8765"` |
| Onde está um programa | `where python` | `Get-Command python` |

Os `.bat` do projeto rodam em CMD (também podem ser chamados do PowerShell como `.\scripts\instalar_windows.bat`). **Não misture**: `set X=1` não funciona no PowerShell, e `$env:X` não funciona no CMD.

---

## 1. Publicar em repositório privado (condicionado à autorização)

### 1.1 Confirme ANTES de publicar (impedimentos)

1. **Autorização/propriedade.** O código foi desenvolvido em ambiente corporativo. Verifique contrato de trabalho, política de propriedade intelectual e uso de equipamento/rede da empresa. **Se houver dúvida, não mantenha o código no GitHub até confirmar** (um commit já foi publicado em `origin/main`; tornar o repositório privado e/ou removê-lo é decisão sua).
2. **Visibilidade.** Confirme que o repositório é **privado**: GitHub → repositório → *Settings* → *General* → seção *Danger Zone* mostra a visibilidade atual (ou, se você usa o GitHub CLI: `gh repo view <SEU_USUARIO>/solana-trading-bot --json visibility`).
3. **Identidade nos commits.** Os commits atuais usam um e-mail pessoal. Em repositório privado isso só é visível a colaboradores. Para esconder o e-mail em futuros commits use o e-mail `noreply` do GitHub (*Settings → Emails*).

### 1.2 Auditoria já realizada (resultado)

| Verificação | Resultado |
|---|---|
| Arquivos rastreados que não deveriam estar (`.env`, `*.sqlite3`, logs, `*.pyc`, `tokens.json`, chaves) | **Nenhum** (108 arquivos rastreados; em `config/` só `tokens.example.json`) |
| Padrões de segredo em todo o histórico (chave privada PEM, tokens `ghp_`/`github_pat_`, base58 de 64 bytes, arrays de keypair) | **0 ocorrências** no histórico (1 commit) |
| Dados pessoais/caminhos da máquina em arquivos | **Nenhum** (e-mails/usuário do Windows não aparecem no código) |
| Segredos no histórico a remover | **Não há**; reescrever histórico **não é necessário** |

Ressalva: a varredura é por padrões; ela não prova a ausência de qualquer segredo de formato incomum. **Você** deve revisar `git diff --cached` antes de cada commit.

### 1.3 O que entra e o que fica só na máquina local

| Entra no repositório | Fica SOMENTE local (ignorado pelo `.gitignore`) |
|---|---|
| `app/`, `tests/`, `scripts/`, `docs/`, `config/tokens.example.json` | `.env` e variações (`.env.*`, exceto `.env.example`) |
| `requirements*.txt`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml` | `config/tokens.json` (cópia local; pode conter seus pools) |
| `.env.example`, `.gitignore`, `.gitattributes` | `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` |
| `data/.gitkeep`, `reports/.gitkeep` (marcadores de pasta) | `data/*` (SQLite, `-wal`, `-shm`, locks, `.painel.json`, CSVs), `logs/`, `*.log`, `reports/*` |
| README e documentação | chaves/carteiras (`*.key`, `*.pem`, `keypair*.json`, `wallet*.json`, `id.json`, `secrets/`) |

Consequência: **o estado do paper trading (banco SQLite) NÃO é transferido** pelo GitHub. Se quiser levar posições/histórico, copie `data\bot.sqlite3` manualmente (com o painel **parado**) — é opcional e, por conter apenas dados simulados, não é sensível.

### 1.4 Comandos para publicar as mudanças desta etapa **[AMBOS]**

Dentro da pasta do projeto (ver 3.4):

```text
git status
git diff --stat
git add -n .
```

`git add -n .` é um *dry-run*: lista o que **seria** adicionado, sem adicionar. Confira que não aparecem `.env`, `*.sqlite3`, `config/tokens.json`, `logs`, `.venv`. Então:

```text
git add .
git diff --cached --name-only
git ls-files | findstr /i /r "\.env$ \.sqlite \.log$ tokens\.json$ \.key$ \.pem$ wallet keypair"
```

O último comando **[CMD]** não deve imprimir **nada** (o `.env.example` não casa com `\.env$`). No **[PS]**, use: `git ls-files | Select-String -Pattern '\.env$|\.sqlite|\.log$|tokens\.json$|\.key$|\.pem$|wallet|keypair'` (também deve ficar vazio).

```text
git commit -m "Preparar transferencia: dependencias minimas, .gitattributes, scripts Windows e testes de portabilidade"
git push
```

A branch `main` já acompanha `origin/main`; `git push` basta. **Não use `--force`.** Se o push for recusado, rode `git pull --rebase origin main`, resolva conflitos e tente de novo, ou me envie a mensagem de erro.

### 1.5 Se um dia um segredo for publicado

1. **Revogue/substitua primeiro** (chave de API, token, carteira). Remover do último commit **não** resolve: ele continua no histórico e em clones/forks.
2. Só depois reescreva o histórico (`git filter-repo` ou BFG), o que exige `force push` — **só com sua autorização explícita** — e novo clone em todas as máquinas.
3. Peça ao suporte do GitHub a remoção de visões em cache, se aplicável. Uma carteira com fundos que teve a chave exposta deve ser considerada **comprometida**.

---

## 2. Dependências exatas

| Pacote | Versão fixada | Onde é necessário | Arquivo |
|---|---|---|---|
| Python | **3.12 recomendado** (mínimo 3.10) | tudo | — |
| `pandas` | 2.3.3 | núcleo (obrigatória) | `requirements.txt` |
| `numpy` | 2.2.6 | núcleo (obrigatória) | `requirements.txt` |
| `httpx` | 0.28.1 | **só com rede real** (`BOT_DATA_SOURCE=geckoterminal`, Telegram). Não é usada com `csv`/`synthetic` | `requirements.txt` |
| `cryptography` | 50.0.1 | **só** caminho LIVE (bloqueado) e 3 arquivos de teste | `requirements-live.txt` (e `requirements-dev.txt`) |
| `pytest` | 8.3.5 | testes (dev) | `requirements-dev.txt` |
| `tabulate` | 0.9.0 | opcional: tabelas nos relatórios de backtest | `requirements-dev.txt` |
| `ruff` / `mypy` | 0.11.13 / 1.16.0 | só lint/análise estática (opcionais) | `requirements-dev.txt` |

Confirmado no código: o painel usa **apenas a biblioteca padrão** (`http.server`); não há Flask/FastAPI. **Node.js não é necessário** (usado por 1 teste opcional que é ignorado se `node` não existir). **Docker não é necessário.** **Git** é necessário para clonar e versionar.

**[VOCÊ]** As versões fixadas de `httpx`, `pytest`, `ruff`, `mypy`, `tabulate` e a existência de wheels para Windows/Python 3.12 **não foram verificadas** (o PyPI estava bloqueado). Se `pip` reclamar de "No matching distribution", **me envie a mensagem**; não afrouxe as versões sem checar.

---

## 3. Instalar em um Windows limpo (passo a passo)

### 3.1 Verificar o Windows **[PS]**

```powershell
(Get-CimInstance Win32_OperatingSystem).Caption
[Environment]::Is64BitOperatingSystem
```
Esperado: `Microsoft Windows 10 ...` ou `11 ...` e `True`.

### 3.2 Git **[PS]**

```powershell
git --version
```
Esperado: `git version 2.xx...`. Se o comando não existir:
```powershell
winget install --id Git.Git -e --source winget
```
Feche e **reabra** o terminal e repita `git --version`. (Sem `winget`: baixe o instalador em git-scm.com.)

### 3.3 Python 3.12 **[PS]**

```powershell
py -0p
```
Esperado: lista com `-V:3.12 ...`. Se não houver 3.12:
```powershell
winget install --id Python.Python.3.12 -e
```
Reabra o terminal e confirme:
```powershell
py -3.12 --version
```
Esperado: `Python 3.12.x`. Se `python` abrir a Microsoft Store, desative *Configurações → Aplicativos → Configurações avançadas de aplicativos → Aliases de execução de aplicativo* para `python.exe`/`python3.exe`.

### 3.4 Clonar o repositório privado e entrar na pasta **[PS]**

Escolha um caminho **curto e fora do OneDrive** (o OneDrive pode corromper o SQLite):
```powershell
New-Item -ItemType Directory -Force C:\dev | Out-Null
Set-Location C:\dev
git clone <URL_DO_REPOSITORIO>
```
Para repositório privado o Git abre uma janela/aba do navegador para você **entrar no GitHub** (Git Credential Manager). **Nunca cole tokens neste chat.** Depois:
```powershell
Set-Location C:\dev\solana-trading-bot
git log --oneline -3
dir scripts
```
Esperado: seus commits e os arquivos `instalar_windows.bat`, `iniciar_painel.bat`, `parar_painel.bat`, `testar_windows.bat`, `check_environment.py`. (O nome da pasta é o nome do repositório; se for diferente, use `dir C:\dev` para descobri-lo.)

### 3.5 Instalação automática **[CMD]**

Abra o **Prompt de Comando** nessa pasta (`cd /d C:\dev\solana-trading-bot`) e rode:
```bat
scripts\instalar_windows.bat
```
Ele: escolhe Python 3.12 (ou o `python` do PATH, exigindo ≥ 3.10), cria `.venv`, instala `requirements.txt`, copia `.env.example → .env` e `config\tokens.example.json → config\tokens.json` **somente se não existirem**, e roda `check_environment.py`. Esperado no final: `Instalacao concluida.` e linhas `[OK]`. Não apaga nada existente.

### 3.6 Instalação manual (equivalente) 

**[PS]** (se aparecer "a execução de scripts foi desabilitada", rode **uma vez** `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`; afeta só seu usuário):
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip check
```
**[CMD]**:
```bat
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
pip check
```
Esperado: prompt com `(.venv)` e `No broken requirements found.` Confirme o interpretador: **[CMD]** `where python` / **[PS]** `Get-Command python` → o primeiro caminho deve estar dentro de `...\solana-trading-bot\.venv\Scripts\`.

### 3.7 Configurar `.env` e `tokens.json` (sem copiar segredos de outra máquina)

**[CMD]** `copy .env.example .env` — **[PS]** `Copy-Item .env.example .env` (se ainda não existir). Edite com `notepad .env`. Mantenha:
```text
BOT_MODE=paper
BOT_LIVE_CONFIRM=
BOT_WALLET_SECRET=
BOT_WALLET_SECRET_FILE=
BOT_JUPITER_API_KEY=
```
Nunca copie o `.env` de outra máquina nem preencha carteira/chave. Para dados de mercado escolha **uma** fonte:

* `BOT_DATA_SOURCE=csv` + arquivos `data\<SIMBOLO>_15m.csv`, `_1h.csv` e `_1m.csv` (`timestamp,open,high,low,close,volume`, timestamp em segundos UTC) + `BOT_DATA_LIQUIDITY_USD_OVERRIDE=<liquidez em USD>` (com `0` a liquidez é "desconhecida" e o robô **não opera**).
* `BOT_DATA_SOURCE=geckoterminal` (exige internet e `httpx`; **não validado** contra a API real) e **`pool` preenchido** em `config\tokens.json` para cada token. O endereço da pool aparece na URL da página da pool no GeckoTerminal (`.../solana/pools/<ENDERECO>`). Confira também os mints em fontes oficiais.
* `BOT_DATA_SOURCE=synthetic`: candles de **2025**; o painel mostrará dados **OBSOLETOS** e o robô ficará em modo seguro (correto). Serve só para testes/replay.

O `.env` pode ser salvo pelo Bloco de Notas (com BOM): o leitor aceita.

### 3.8 Verificar ambiente **[AMBOS, com o ambiente ativo]**

```text
python scripts\check_environment.py
```
Esperado (exemplo): `[OK] Python 3.12...`, `[OK] ambiente virtual ativo`, `[OK] pandas 2.3.3`, `[OK] numpy 2.2.6`, `[OK] LIVE BLOQUEADO...`, `[OK] allowlist...`, `[OK] porta 8765 livre em 127.0.0.1`. `[AVISO]` são orientações; `[FALHA]` precisa ser resolvida. O script **não usa rede**.

### 3.9 Executar os testes

```bat
scripts\testar_windows.bat
```
(ou, com o ambiente ativo: `pip install -r requirements-dev.txt` e `python -m pytest -q`). Esperado: **todos passam** (no Linux de origem: 179 com o runner de contingência; `pytest` **nunca foi executado** neste projeto). **[VOCÊ]** Como o Windows nunca foi testado, falhas são possíveis em itens específicos (travas `msvcrt`, soquetes, SQLite). Se falhar, rode `python -m pytest -x -q` e me envie as ~30 últimas linhas. Observação: um teste de verificação de permissões 0600 só roda em Linux/macOS; o teste de front-end é ignorado se `node` não estiver instalado — isso é esperado.

### 3.10 Iniciar o painel **[CMD]**

```bat
scripts\iniciar_painel.bat
```
(equivale a `.venv\Scripts\activate.bat` + `python -m app.dashboard --abrir`). Esperado na janela:
```text
Painel local: http://127.0.0.1:8765/
Escutando SOMENTE em 127.0.0.1 (loopback). O paper trading NÃO foi iniciado: use o botão 'Iniciar'.
```
O navegador abre sozinho; se não, abra manualmente **http://127.0.0.1:8765/** (prefira `127.0.0.1` a `localhost`). **Mantenha a janela aberta.** Se o Firewall do Windows perguntar algo, **não** autorize redes públicas/privadas (não é necessário: é loopback).

### 3.11 Iniciar, pausar, retomar e parar o paper trading (no painel)

Selo "PAPER TRADING — SIMULADO" e "LIVE BLOQUEADO" aparecem no topo.

* **Iniciar paper trading** → estado `RODANDO` e evento `paper_iniciado` na seção *Eventos*. **O resultado depende da fonte de dados** (3.7): sem dados válidos o startup é reprovado e o robô entra em **MODO SEGURO** (sem novas entradas) com a explicação nos avisos — comportamento correto, não um defeito.
* **Pausar novas entradas** → `ENTRADAS_PAUSADAS` (monitoramento e saídas continuam). **Retomar entradas** → volta a `RODANDO` (recusado se o kill switch estiver ativo).
* **Parar paper trading** → não envia ordens nem liquida; o estado fica salvo. **Enquanto parado, stops simulados NÃO são monitorados** (o painel avisa).
* **EMERGÊNCIA** → aciona o kill switch (bloqueia novas entradas; preserva posições). Para retomar: pare o paper trading, digite `RETOMAR_OPERACAO` e uma justificativa.

**Validar o robô de ponta a ponta sem internet e sem dados reais (replay offline, base temporária)** **[AMBOS]**:
```text
python scripts\run_paper_trading.py --replay-days 20
```
Saída esperada (reproduzida no Linux): `startup: OK`, **2 compras e 2 vendas simuladas** de `JUP`, `pnl_liquido` ≈ **-4,17**, `taxa_de_acerto` 0.0, `intencoes: {CONFIRMED: 4}` e o caminho do relatório. O resultado negativo é esperado (dados sintéticos sem padrão); **não é evidência de rentabilidade**. Leva ~1–2 min.

### 3.12 Logs e diagnóstico

| O quê | Onde / como |
|---|---|
| Logs JSON (segredos redigidos) | `logs\bot.log` — **[PS]** `Get-Content logs\bot.log -Tail 50 -Wait` / **[CMD]** `type logs\bot.log` |
| Painel | seções *Eventos*, *Alertas* e *Erros recentes no arquivo de log* |
| Banco SQLite | `data\bot.sqlite3` (abra com Python, exemplo no passo 4) |

| Sintoma | Causa provável e ação |
|---|---|
| "Python nao encontrado" | passo 3.3; reabra o terminal |
| `pip` não acha versão/sem rede | o `pip` precisa de acesso ao PyPI (rede/proxy). Envie a mensagem |
| Painel termina com código 3 | outra instância em execução **ou** porta ocupada. Veja `netstat -ano \| findstr :8765`; mude `BOT_PANEL_PORT` ou use `scripts\parar_painel.bat` |
| Painel termina com código 2 | `.env` inválido: leia a mensagem ("Configuração inválida: ...") |
| "arquivo de tokens não encontrado" | `copy config\tokens.example.json config\tokens.json` |
| Aviso "dados OBSOLETOS/inválidos" ou MODO SEGURO | fonte de dados sem arquivos/sem internet/sem pool/`synthetic` (3.7) |
| `pytest` falha só no Windows | envie `python -m pytest -x -q` (últimas linhas) |

### 3.13 Encerrar corretamente

* Na janela do painel: **Ctrl+C** (encerramento organizado: para o paper trading, mantém as posições simuladas, fecha o servidor e libera a porta); **ou**, em outra janela, `scripts\parar_painel.bat` (= `python -m app.dashboard --parar`).
* **Não feche a janela no "X"**: o processo é encerrado sem o desligamento organizado (os dados continuam íntegros no SQLite, mas fica o arquivo `data\bot.sqlite3.painel.json`; se quiser, apague-o).
* Confirme que não restou processo: **[PS]** `Get-Process python -ErrorAction SilentlyContinue` / **[CMD]** `tasklist | findstr /i python`. Só se algo travar: `taskkill /PID <PID> /T` (o `<PID>` vem do comando anterior; **evite `/F`** salvo se necessário).

---

## 4. Checklist de validação final (você executa e marca)

Com o painel aberto e o ambiente ativo:

| # | Verificação | Como | Esperado |
|---|---|---|---|
| 1 | Ambiente virtual ativo | prompt `(.venv)`; **[CMD]** `where python` | caminho dentro de `.venv\Scripts` |
| 2 | Dependências instaladas | `pip check` | `No broken requirements found.` |
| 3 | Testes passaram | `scripts\testar_windows.bat` | 0 falhas |
| 4 | Painel abre | navegador em `http://127.0.0.1:8765/` | página com "PAPER TRADING — SIMULADO" |
| 5 | **Só loopback** | **[CMD]** `netstat -ano \| findstr :8765` / **[PS]** `Get-NetTCPConnection -LocalPort 8765 \| Select LocalAddress,State` | `127.0.0.1:8765  LISTENING` — **nunca** `0.0.0.0` nem IP da rede |
| 6 | Backend recebe comandos | clique em *Iniciar* e depois *Pausar entradas* | *Eventos* mostram `paper_iniciado` e `entradas_pausadas` |
| 7 | Paper trading funciona | `python scripts\run_paper_trading.py --replay-days 20` | compras/vendas simuladas, relatório gerado |
| 8 | Persistência após reiniciar | pare o painel (Ctrl+C), inicie de novo | eventos/posições/pausa continuam visíveis; o robô **não** inicia sozinho |
| 9 | Sem duas instâncias | com o painel aberto, em outro terminal: `python -m app.dashboard` | `Recusado: já existe outra instância do painel...` |
| 10 | Sem CLI concorrente | com o paper rodando no painel: `python -m app.main --mode paper --once` | `Recusado: ... outra instância do robô...` (código 3) |
| 11 | Kill switch | botão *EMERGÊNCIA* | faixa vermelha; entradas bloqueadas; *Retomar entradas* recusado |
| 12 | LIVE bloqueado | `python -m app.main --mode live --once` | `MODO LIVE BLOQUEADO: ...` (código 2) |
| 13 | Nenhuma transação real | `python -c "import sqlite3; c=sqlite3.connect('data/bot.sqlite3'); print(c.execute('select distinct mode from intents').fetchall())"` | `[]` ou `[('paper',)]` — nunca `live` |

Itens 5, 8–13 só são confirmados por **você** no Windows.

---

## 5. O que depende de internet / APIs

| Funcionalidade | Rede? | Situação |
|---|---|---|
| Testes automatizados | Não | usam dados sintéticos e **mocks** (`FakeNet`, `ArrayProvider`). **Não validam APIs externas** |
| Backtest (sintético ou CSV), replay de paper, painel, kill switch, persistência | Não | funcionam offline |
| Paper trading com dados reais (CSV) | Não (depois de exportar os CSV) | CSV: formato e validação testados com arquivos sintéticos |
| Paper trading com `geckoterminal` | **Sim** (GeckoTerminal + `httpx`) | **NÃO validado com a API real** |
| Cotação Jupiter | **Sim** (+ chave de API) | só `scripts\dry_run_quote.py` (somente leitura, **não assina nem envia**); **NÃO validado** (campos reais, `slippageBps`, unidade de `priceImpact`) |
| RPC Solana | **Sim** | só `scripts\inspect_wallet.py` (leitura, chave **pública**) e o caminho LIVE (bloqueado); **NÃO validado** |
| Execução real / assinatura | — | **BLOQUEADA**; não configure |

`dry_run_quote.py` precisa de `BOT_JUPITER_API_KEY` e de **um endereço público** em `BOT_WALLET_PUBLIC_KEY` (pode ser um endereço sem fundos; **nunca a chave privada**). Use somente quando quiser validar a API real, sem enviar nada.
