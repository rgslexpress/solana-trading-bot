"""Configuração centralizada, tipada e validada.

Fontes (maior precedência primeiro): variáveis de ambiente `BOT_*` > arquivo `.env` > padrões.
Parâmetros de seção usam `BOT_<SECAO>_<CAMPO>`, ex.: `BOT_RISK_RISK_PER_TRADE_PCT`.
Valores inválidos impedem a inicialização (ConfigError).
"""

from __future__ import annotations

import json
import os
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from app.domain import Mode, TokenInfo
from app.utils.base58 import is_valid_pubkey

LIVE_CONFIRM_PHRASE = "EU_ENTENDO_QUE_ISTO_USA_FUNDOS_REAIS"
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
SECRET_FIELDS = {"jupiter_api_key", "wallet_secret", "telegram_bot_token"}


class ConfigError(ValueError):
    """Configuração inválida ou insegura."""


@dataclass
class StrategyParams:
    entry_timeframe: str = "15m"
    htf_timeframe: str = "1h"
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 200  # aplicada ao timeframe superior
    cross_lookback: int = 3
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    rsi_buy_max: float = 65.0
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    volume_ma_period: int = 20
    min_relative_volume: float = 0.8
    max_extension_atr: float = 3.0  # distância máxima (em ATR) acima da EMA lenta
    require_htf_confirmation: bool = True
    min_score: float = 6.0
    use_ref_filter: bool = False  # filtro de correlação/tendência do ativo de referência (SOL)
    corr_window: int = 96
    corr_min: float = 0.5
    max_cost_bps: float = 60.0  # custo de ida (taxa+slippage) máximo tolerado
    min_candles: int = 300
    version: str = "v1.0"


@dataclass
class RiskParams:
    risk_per_trade_pct: float = 0.5
    max_risk_per_trade_pct: float = 1.0
    absolute_risk_cap_pct: float = 2.0
    risk_above_1pct_justification: str = ""
    max_total_exposure_pct: float = 50.0
    max_token_exposure_pct: float = 20.0
    max_open_positions: int = 3
    daily_loss_limit_pct: float = 2.0
    max_cumulative_loss_pct: float = 10.0
    max_trades_per_day: int = 6
    max_consecutive_losses: int = 3
    cooldown_minutes: int = 240
    min_fee_reserve_sol: float = 0.02
    min_order_quote: float = 10.0
    atr_stop_mult: float = 2.0
    rr_target: float = 2.0
    breakeven_trigger_r: float = 1.0
    trailing_activation_r: float = 1.5
    trailing_atr_mult: float = 2.5
    on_limit_policy: str = "block_entries_keep_positions"


@dataclass
class ExecutionParams:
    fee_bps: float = 10.0  # taxa de rota/protocolo estimada (por lado)
    network_fee_quote: float = 0.05  # taxa de rede + prioridade estimada em unidades da moeda de referência
    base_slippage_bps: float = 10.0
    impact_k: float = 1.0  # impacto ~ k * (notional/liquidez)
    max_slippage_bps: float = 50.0
    max_price_impact_bps: float = 100.0
    min_liquidity_usd: float = 100_000.0
    quote_max_age_s: float = 15.0
    max_price_deviation_bps: float = 100.0
    max_order_quote: float = 100.0  # teto absoluto por ordem (LIVE)
    unknown_reconcile_after_s: float = 20.0
    unknown_expire_after_s: float = 180.0
    unknown_kill_after_s: float = 900.0
    http_timeout_s: float = 10.0
    max_retries: int = 3
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    breaker_failures: int = 5
    breaker_reset_s: float = 60.0
    max_consecutive_exec_failures: int = 3
    balance_divergence_pct: float = 2.0  # divergência local x on-chain que aciona o kill switch
    send_slippage_param: bool = True  # envia slippageBps fixo em /order (parâmetro citado na doc; confirmar)
    strict_router_args: bool = True  # rejeita instruções de roteador cujos argumentos não possam ser lidos
    extra_allowed_programs: str = ""  # lista separada por vírgula (fail-closed por padrão)


@dataclass
class DataParams:
    source: str = "synthetic"  # synthetic | csv | geckoterminal
    csv_dir: str = "data"
    cache_ttl_s: float = 30.0
    max_data_staleness_candles: float = 2.5
    history_candles: int = 600
    max_failed_cycles_before_kill: int = 10
    close_grace_s: float = 5.0  # espera após o fechamento do candle antes de usá-lo (provedor pode atrasar)
    max_gap_candles: int = 1  # 1 = nenhum candle ausente tolerado na janela recente
    include_empty_intervals: bool = False  # Gecko: preencher intervalos sem negociação (OHLC=fechamento anterior, vol 0)
    liquidity_usd_override: float = 0.0  # 0 = desconhecida (fail-closed). Premissa manual para CSV; teto para Gecko
    geckoterminal_url: str = "https://api.geckoterminal.com/api/v2"


@dataclass
class BacktestParams:
    initial_capital: float = 1000.0
    dev_fraction: float = 0.5
    val_fraction: float = 0.25  # o restante é fora da amostra (OOS)
    reference_symbol: str = "SOL"
    stop_latency_bps: float = 20.0  # piora adicional em saídas por stop (detecção por polling + venda a mercado). Premissa NÃO calibrada
    execution_failure_rate: float = 0.0  # prob. de uma ordem falhar/ser adiada 1 candle (transação perdida/expirada). 0 = ideal
    failure_seed: int = 1


@dataclass
class Settings:
    mode: Mode = Mode.PAPER
    db_path: str = "data/bot.sqlite3"
    reports_dir: str = "reports"
    log_level: str = "INFO"
    log_json: bool = True
    log_file: str = "logs/bot.log"
    tokens_file: str = "config/tokens.json"
    quote_symbol: str = "USDC"
    paper_capital: float = 1000.0
    poll_seconds: float = 30.0
    panel_port: int = 8765  # painel local (somente 127.0.0.1)
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_base_url: str = "https://api.jup.ag/swap/v2"
    jupiter_api_key: str = field(default="", repr=False)
    wallet_public_key: str = ""
    wallet_secret: str = field(default="", repr=False)  # base58 ou JSON; prefira BOT_WALLET_SECRET_FILE
    wallet_secret_file: str = ""
    live_confirm: str = ""
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    execution: ExecutionParams = field(default_factory=ExecutionParams)
    data: DataParams = field(default_factory=DataParams)
    backtest: BacktestParams = field(default_factory=BacktestParams)

    # ------------------------------------------------------------------ util
    def redacted_dict(self) -> dict[str, Any]:
        """Representação segura para exibição/log: segredos mascarados."""

        def walk(obj: Any) -> Any:
            if is_dataclass(obj):
                out = {}
                for f in fields(obj):
                    v = getattr(obj, f.name)
                    if f.name in SECRET_FIELDS:
                        out[f.name] = "***definido***" if v else ""
                    else:
                        out[f.name] = walk(v)
                return out
            if isinstance(obj, Mode):
                return obj.value
            return obj

        return walk(self)  # type: ignore[no-any-return]

    def load_tokens(self) -> tuple[dict[str, TokenInfo], TokenInfo]:
        """Carrega a lista de permissões. Retorna (tokens_negociáveis, token_de_referência)."""
        path = Path(self.tokens_file)
        if not path.exists():
            raise ConfigError(f"arquivo de tokens não encontrado: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        quote_raw = raw.get("quote")
        if not quote_raw:
            raise ConfigError("tokens.json precisa de uma chave 'quote'")
        quote = _token_from_dict(quote_raw)
        tokens: dict[str, TokenInfo] = {}
        for item in raw.get("allowlist", []):
            t = _token_from_dict(item)
            if t.mint in {x.mint for x in tokens.values()}:
                raise ConfigError(f"mint duplicado na allowlist: {t.mint}")
            tokens[t.symbol] = t
        if quote.symbol != self.quote_symbol:
            raise ConfigError("quote_symbol difere do token 'quote' em tokens.json")
        return tokens, quote

    def allowed_programs(self) -> set[str]:
        return {p.strip() for p in self.execution.extra_allowed_programs.split(",") if p.strip()}


def _token_from_dict(d: dict[str, Any]) -> TokenInfo:
    mint = d.get("mint", "")
    if not is_valid_pubkey(mint):
        raise ConfigError(f"mint inválido para {d.get('symbol')}: {mint!r}")
    dec = d.get("decimals")
    if not isinstance(dec, int) or not (0 <= dec <= 18):
        raise ConfigError(f"decimals inválido para {d.get('symbol')}")
    return TokenInfo(
        symbol=str(d["symbol"]),
        mint=mint,
        decimals=dec,
        pool=d.get("pool"),
        min_liquidity_usd=d.get("min_liquidity_usd"),
        allow_mint_authority=bool(d.get("allow_mint_authority", False)),
        allow_freeze_authority=bool(d.get("allow_freeze_authority", False)),
    )


# ------------------------------------------------------------------ loader
def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _cast(raw: str, typ: Any, name: str) -> Any:
    try:
        if typ is bool:
            if raw.lower() in ("1", "true", "yes", "sim"):
                return True
            if raw.lower() in ("0", "false", "no", "nao", "não"):
                return False
            raise ValueError("booleano inválido")
        if typ is int:
            return int(raw)
        if typ is float:
            return float(raw)
        if typ is Mode:
            return Mode(raw.lower())
        return raw
    except ValueError as exc:
        raise ConfigError(f"valor inválido para {name}: {exc}") from exc


def _apply(obj: Any, prefix: str, env: dict[str, str]) -> None:
    hints = typing.get_type_hints(type(obj))
    for f in fields(obj):
        cur = getattr(obj, f.name)
        key = f"{prefix}{f.name}".upper()
        if is_dataclass(cur):
            _apply(cur, f"{prefix}{f.name}_", env)
        elif key in env:
            setattr(obj, f.name, _cast(env[key], hints[f.name], key))


def load_settings(env_file: str | None = ".env", environ: dict[str, str] | None = None) -> Settings:
    """Carrega e valida as configurações. Lança ConfigError se algo for inválido."""
    env: dict[str, str] = {}
    if env_file:
        env.update(_parse_dotenv(Path(env_file)))
    env.update({k: v for k, v in (environ if environ is not None else os.environ).items()})
    s = Settings()
    _apply(s, "BOT_", env)
    errors = validate_settings(s)
    if errors:
        raise ConfigError("; ".join(errors))
    return s


def validate_settings(s: Settings) -> list[str]:
    """Retorna a lista de erros de configuração (vazia se válida)."""
    e: list[str] = []
    st, r, x, d, b = s.strategy, s.risk, s.execution, s.data, s.backtest
    for tf in (st.entry_timeframe, st.htf_timeframe):
        if tf not in TIMEFRAME_SECONDS:
            e.append(f"timeframe inválido: {tf}")
    if (
        st.entry_timeframe in TIMEFRAME_SECONDS
        and st.htf_timeframe in TIMEFRAME_SECONDS
        and TIMEFRAME_SECONDS[st.htf_timeframe] < TIMEFRAME_SECONDS[st.entry_timeframe]
    ):
        e.append("timeframe superior deve ser >= timeframe de entrada")
    if not (1 <= st.ema_fast < st.ema_slow):
        e.append("ema_fast deve ser menor que ema_slow")
    if st.macd_fast >= st.macd_slow:
        e.append("macd_fast deve ser menor que macd_slow")
    if not (0 < st.rsi_oversold < st.rsi_overbought < 100):
        e.append("níveis de RSI inválidos")
    if min(st.atr_period, st.rsi_period, st.bb_period, st.volume_ma_period) < 2:
        e.append("períodos de indicadores devem ser >= 2")
    if not (0 < r.risk_per_trade_pct <= r.absolute_risk_cap_pct):
        e.append("risk_per_trade_pct inválido")
    if r.absolute_risk_cap_pct > 2.0:
        e.append("absolute_risk_cap_pct não pode exceder 2.0")
    if r.risk_per_trade_pct > r.max_risk_per_trade_pct and not r.risk_above_1pct_justification.strip():
        e.append("risco por operação acima do limite padrão (1%) exige justificativa documentada (BOT_RISK_RISK_ABOVE_1PCT_JUSTIFICATION)")
    if not (0 < r.max_token_exposure_pct <= r.max_total_exposure_pct <= 100):
        e.append("limites de exposição incoerentes")
    if r.daily_loss_limit_pct <= 0 or r.max_cumulative_loss_pct <= 0:
        e.append("limites de perda devem ser positivos")
    if r.atr_stop_mult <= 0 or r.rr_target <= 0:
        e.append("atr_stop_mult e rr_target devem ser positivos")
    if r.on_limit_policy != "block_entries_keep_positions":
        e.append("on_limit_policy suportada: block_entries_keep_positions")
    if x.max_slippage_bps <= 0 or x.max_price_impact_bps <= 0:
        e.append("limites de slippage/impacto devem ser positivos")
    if x.max_retries < 0 or x.http_timeout_s <= 0:
        e.append("timeouts/retries inválidos")
    if d.source not in ("synthetic", "csv", "geckoterminal"):
        e.append(f"data.source inválido: {d.source}")
    if not (0 <= b.execution_failure_rate < 1):
        e.append("execution_failure_rate deve estar em [0,1)")
    if not (0 < b.dev_fraction + b.val_fraction < 1):
        e.append("frações de dev+validação devem somar entre 0 e 1")
    if not (1024 <= s.panel_port <= 65535):
        e.append("panel_port deve estar entre 1024 e 65535")
    if s.paper_capital <= 0 or b.initial_capital <= 0:
        e.append("capital deve ser positivo")
    if s.quote_symbol not in ("USDC", "SOL"):
        e.append("quote_symbol deve ser USDC ou SOL")
    if s.mode == Mode.LIVE:
        # Verificações estruturais; as demais ficam em app.execution.live_guard.
        if s.wallet_public_key and not is_valid_pubkey(s.wallet_public_key):
            e.append("wallet_public_key inválida")
    return e
