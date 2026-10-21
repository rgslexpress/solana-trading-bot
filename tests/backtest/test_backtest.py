import numpy as np
import pandas as pd

from app.backtest.engine import BacktestEngine, TokenData
from app.backtest.robustness import split_periods
from app.config import Settings
from app.data.providers import SyntheticProvider
from _helpers import JUP, make_candles


def _settings():
    s = Settings()
    s.strategy.ema_trend = 50
    s.strategy.entry_timeframe = "15m"
    s.strategy.htf_timeframe = "1h"
    return s


def _synthetic_data(days=45):
    prov = SyntheticProvider(seed=11, n_1m=60 * 24 * days)
    return {"JUP": TokenData(JUP, prov.full_history(JUP, "15m"), prov.full_history(JUP, "1h"))}


def test_backtest_runs_and_is_deterministic():
    data, s = _synthetic_data(), _settings()
    r1 = BacktestEngine(s, data, 5e6).run()
    r2 = BacktestEngine(s, data, 5e6).run()
    assert r1.metrics["num_trades"] == r2.metrics["num_trades"]
    assert abs(r1.metrics["final_equity"] - r2.metrics["final_equity"]) < 1e-9
    assert r1.signal_counts  # sinais foram avaliados
    assert (r1.equity["equity"] > 0).all()


def test_equity_accounting_matches_trade_ledger():
    data, s = _synthetic_data(), _settings()
    r = BacktestEngine(s, data, 5e6).run()
    ledger = r.trades["net_pnl"].sum()
    assert abs((r.metrics["final_equity"] - 1000.0) - ledger) < 1e-6  # caixa final = capital + PnL líquido (sem posições abertas)


def test_costs_reduce_result_and_fees_reported():
    data = _synthetic_data()
    cheap, dear = _settings(), _settings()
    dear.execution.fee_bps = 30.0  # custo total ainda abaixo do filtro max_cost_bps (60)
    a = BacktestEngine(cheap, data, 5e6).run()
    b = BacktestEngine(dear, data, 5e6).run()
    # custos acima do limite configurado bloqueiam todas as entradas (filtro de custo)
    prohibitive = _settings()
    prohibitive.execution.fee_bps = 80.0
    assert BacktestEngine(prohibitive, data, 5e6).run().metrics["num_trades"] == 0
    # O dimensionamento muda com os custos (caminhos diferentes), então compara-se o custo por unidade negociada.
    assert a.metrics["num_trades"] > 0 and b.metrics["num_trades"] > 0
    rate = lambda r: (r.trades["fees"] / (r.trades["qty"] * r.trades["entry_fill"])).mean()  # noqa: E731
    assert rate(b) > rate(a) * 1.3
    assert b.metrics["total_costs"] > 0


def test_signal_on_close_executes_next_open_not_same_bar():
    """Todo trade deve entrar na abertura de um candle posterior ao do sinal => entry_ref == open do candle de entrada."""
    data, s = _synthetic_data(), _settings()
    r = BacktestEngine(s, data, 5e6).run()
    entry_df = data["JUP"].entry
    for _, t in r.trades.iterrows():
        ts = pd.Timestamp(t["entry_ts"])
        assert abs(entry_df.loc[ts, "open"] - t["entry_ref"]) < 1e-9
        assert t["entry_fill"] >= t["entry_ref"]  # compra nunca melhor que a referência (slippage)
        if t["exit_reason"] not in ("stop", "take_profit"):
            assert t["exit_fill"] <= t["exit_ref"]


def test_stop_loss_exit_bounds_loss_near_planned_risk():
    data, s = _synthetic_data(60), _settings()
    r = BacktestEngine(s, data, 5e6).run()
    stops = r.trades[r.trades["exit_reason"] == "stop"]
    if len(stops):
        # perda de um stop (sem gap extremo) não deve passar de ~3x o risco planejado (0,5% do patrimônio)
        assert (stops["net_pnl"] > -1000.0 * 0.005 * 3).all()


def test_future_data_does_not_change_past_trades():
    """Cortar o futuro não pode alterar operações já concluídas antes do corte (ausência de look-ahead)."""
    data, s = _synthetic_data(60), _settings()
    full = BacktestEngine(s, data, 5e6).run()
    cut_ts = data["JUP"].entry.index[int(len(data["JUP"].entry) * 0.6)]
    cut = {k: TokenData(v.token, v.entry[v.entry.index < cut_ts], v.htf[v.htf.index < cut_ts]) for k, v in data.items()}
    part = BacktestEngine(s, cut, 5e6).run()
    done_full = full.trades[pd.to_datetime(full.trades["exit_ts"]) < cut_ts - pd.Timedelta(hours=2)].reset_index(drop=True)
    done_part = part.trades[pd.to_datetime(part.trades["exit_ts"]) < cut_ts - pd.Timedelta(hours=2)].reset_index(drop=True)
    assert len(done_full) == len(done_part)
    if len(done_full):
        assert np.allclose(done_full["net_pnl"], done_part["net_pnl"])


def test_period_split_is_chronological_and_disjoint():
    idx = make_candles(np.linspace(1, 2, 1000)).index
    p = split_periods(idx, 0.5, 0.25)
    assert p["desenvolvimento"][1] == p["validacao"][0] and p["validacao"][1] == p["fora_da_amostra"][0]
    assert p["desenvolvimento"][0] < p["validacao"][0] < p["fora_da_amostra"][0]


def test_drawdown_limit_halts_new_entries():
    data, s = _synthetic_data(60), _settings()
    s.risk.max_cumulative_loss_pct = 0.0001  # qualquer perda mínima aciona
    r = BacktestEngine(s, data, 5e6).run()
    # se houve perda acima do limite, nenhuma entrada ocorre depois do evento
    if r.kill_events:
        t0 = pd.Timestamp(r.kill_events[0].split(": ")[0])
        later = r.trades[pd.to_datetime(r.trades["entry_ts"]) > t0 + pd.Timedelta(hours=1)]
        assert len(later) == 0
