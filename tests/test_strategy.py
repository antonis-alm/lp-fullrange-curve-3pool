import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from almanak.framework.market import (
    PoolAnalyticsUnavailableError,
    SlippageEstimateUnavailableError,
)
from strategy import LPFullRangeCurve3poolStrategy


@pytest.fixture
def config() -> dict:
    config_path = Path(__file__).parent.parent / "config.json"
    with open(config_path) as f:
        return json.load(f)


@pytest.fixture
def strategy(config: dict) -> LPFullRangeCurve3poolStrategy:
    return LPFullRangeCurve3poolStrategy(
        config=config,
        chain="ethereum",
        wallet_address="0x" + "1" * 40,
    )


def _balance(amount: str) -> SimpleNamespace:
    return SimpleNamespace(balance=Decimal(amount), balance_usd=Decimal(amount))


def _market(
    *,
    prices: dict[str, Decimal] | None = None,
    balances: dict[str, str] | None = None,
    concentration: Decimal = Decimal("50"),
    slippage_bps: Decimal = Decimal("5"),
) -> MagicMock:
    prices = prices or {
        "DAI": Decimal("1.0"),
        "USDC": Decimal("1.0"),
        "USDT": Decimal("1.0"),
    }
    balances = balances or {"DAI": "1000", "USDC": "1000", "USDT": "1000"}

    market = MagicMock()
    market.price.side_effect = lambda token: prices[token]
    market.balance.side_effect = lambda token: _balance(balances.get(token, "0"))

    analytics = SimpleNamespace(token_weights={"DAI": Decimal("0.3"), "USDC": Decimal("0.4"), "USDT": Decimal("0.3")})
    if concentration is not None:
        analytics.token_weights = {
            "DAI": Decimal("0.2"),
            "USDC": concentration / Decimal("100"),
            "USDT": Decimal("0.8") - concentration / Decimal("100"),
        }
    market.pool_analytics.return_value = SimpleNamespace(value=analytics)

    slippage = SimpleNamespace(slippage_bps=Decimal(slippage_bps))
    market.estimate_slippage.return_value = SimpleNamespace(value=slippage)
    return market


def _intent_type(intent) -> str:
    return getattr(intent.intent_type, "value", str(intent.intent_type))


def test_opens_lp_with_all_assets(strategy: LPFullRangeCurve3poolStrategy) -> None:
    market = _market()
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_OPEN"
    assert intent.protocol == "curve"
    assert intent.pool == "3pool"
    assert intent.coin_amounts[0] == Decimal("1000")
    assert intent.coin_amounts[1] == Decimal("1000")
    assert intent.coin_amounts[2] == Decimal("1000")


def test_opens_lp_with_subset_assets(strategy: LPFullRangeCurve3poolStrategy) -> None:
    market = _market(balances={"DAI": "1000", "USDC": "500", "USDT": "0"})
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_OPEN"
    assert intent.coin_amounts[2] == Decimal("0")


def test_holds_without_balances(strategy: LPFullRangeCurve3poolStrategy) -> None:
    market = _market(balances={"DAI": "0", "USDC": "0", "USDT": "0"})
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_holds_when_position_healthy(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    market = _market(concentration=Decimal("50"), slippage_bps=Decimal("5"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_exits_on_peg_breach(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    market = _market(prices={"DAI": Decimal("1.0"), "USDC": Decimal("1.0"), "USDT": Decimal("0.99")})
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_exits_on_concentration_breach(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    market = _market(concentration=Decimal("75"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_holds_on_high_exit_slippage_without_depeg(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    market = _market(concentration=Decimal("75"), slippage_bps=Decimal("30"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_depeg_override_ignores_exit_slippage(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    strategy._last_prices = {"DAI": Decimal("1"), "USDC": Decimal("1"), "USDT": Decimal("1")}
    market = _market(
        prices={"DAI": Decimal("1"), "USDC": Decimal("1"), "USDT": Decimal("0.995")},
        concentration=Decimal("50"),
        slippage_bps=Decimal("50"),
    )
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_reentry_blocked_by_concentration(strategy: LPFullRangeCurve3poolStrategy) -> None:
    market = _market(concentration=Decimal("65"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_reentry_blocked_by_deposit_slippage(strategy: LPFullRangeCurve3poolStrategy) -> None:
    market = _market(concentration=Decimal("50"), slippage_bps=Decimal("15"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_force_action_open(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy.force_action = "open"
    market = _market(balances={"DAI": "100", "USDC": "200", "USDT": "300"})
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_OPEN"


def test_force_action_swap(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy.force_action = "swap"
    strategy.force_swap_from = "USDT"
    strategy.force_swap_to = "USDC"
    strategy.force_swap_amount_pct_of_balance = Decimal("0.5")
    market = _market(balances={"DAI": "0", "USDC": "0", "USDT": "200"})
    intent = strategy.decide(market)
    assert _intent_type(intent) == "SWAP"
    assert intent.amount == Decimal("100")


def test_force_action_swap_holds_when_no_source_balance(
    strategy: LPFullRangeCurve3poolStrategy,
) -> None:
    strategy.force_action = "swap"
    strategy.force_swap_from = "USDT"
    strategy.force_swap_to = "USDC"
    market = _market(balances={"DAI": "0", "USDC": "0", "USDT": "0"})
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_force_action_close(strategy: LPFullRangeCurve3poolStrategy) -> None:
    strategy.force_action = "close"
    strategy.force_position_id = "forced-position"
    market = _market()
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"
    assert intent.position_id == "forced-position"


def test_holds_on_pool_analytics_unavailable(strategy: LPFullRangeCurve3poolStrategy) -> None:
    market = _market()
    market.pool_analytics.side_effect = PoolAnalyticsUnavailableError("gateway unavailable")
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_holds_on_slippage_estimate_unavailable_reentry(
    strategy: LPFullRangeCurve3poolStrategy,
) -> None:
    market = _market()
    market.estimate_slippage.side_effect = SlippageEstimateUnavailableError("USDC/DAI")
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_holds_on_slippage_estimate_unavailable_exit(
    strategy: LPFullRangeCurve3poolStrategy,
) -> None:
    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    market = _market(concentration=Decimal("75"))
    market.estimate_slippage.side_effect = SlippageEstimateUnavailableError("USDC/DAI")
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_uses_curve_pool_address_for_pool_analytics(
    strategy: LPFullRangeCurve3poolStrategy,
) -> None:
    market = _market()
    strategy.decide(market)
    args, kwargs = market.pool_analytics.call_args
    assert str(args[0]).lower() == "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7"
    assert kwargs["protocol"] == "curve"


def test_teardown_with_and_without_position(strategy: LPFullRangeCurve3poolStrategy) -> None:
    assert strategy.generate_teardown_intents() == []

    strategy._has_position = True
    strategy._lp_position_ref = "lp-ref"
    intents = strategy.generate_teardown_intents()
    assert len(intents) == 1
    assert _intent_type(intents[0]) == "LP_CLOSE"


def test_state_persistence_roundtrip(config: dict) -> None:
    s1 = LPFullRangeCurve3poolStrategy(config=config, chain="ethereum", wallet_address="0x" + "1" * 40)
    s1._has_position = True
    s1._lp_position_ref = "lp-ref"
    s1._last_prices = {"USDT": Decimal("1.0")}
    state = s1.get_persistent_state()

    s2 = LPFullRangeCurve3poolStrategy(config=config, chain="ethereum", wallet_address="0x" + "1" * 40)
    s2.load_persistent_state(state)

    assert s2._has_position is True
    assert s2._lp_position_ref == "lp-ref"
    assert s2._last_prices["USDT"] == Decimal("1.0")
