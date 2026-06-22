import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.market import (
    MarketSnapshot,
    PoolAnalyticsUnavailableError,
    PoolHistoryUnavailableError,
    PriceUnavailableError,
    SlippageEstimateUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)

CURVE_3POOL_ETHEREUM_ADDRESS = "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7"


@almanak_strategy(
    name="l_p_full_range_curve_3pool",
    description="Passive full-range Curve 3pool LP with depeg risk exits",
    version="1.0.0",
    author="Almanak",
    tags=["curve", "lp", "stablecoin", "passive"],
    supported_chains=["ethereum"],
    supported_protocols=["curve"],
    intent_types=["LP_OPEN", "LP_CLOSE", "SWAP", "HOLD"],
    default_chain="ethereum",
    quote_asset="USD",
)
class LPFullRangeCurve3poolStrategy(IntentStrategy):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.pool = str(self.get_config("pool", "3pool"))
        self.protocol = str(self.get_config("protocol", "curve"))
        self.assets = list(self.get_config("assets", ["DAI", "USDC", "USDT"]))

        self.single_position_only = bool(self.get_config("single_position_only", True))

        self.initial_lp_allocation_pct = Decimal(str(self.get_config("initial_lp_allocation_pct", "1.0")))
        self.reentry_lp_allocation_pct = Decimal(str(self.get_config("reentry_lp_allocation_pct", "1.0")))
        self.dust_reserve_pct_per_asset = Decimal(str(self.get_config("dust_reserve_pct_per_asset", "0")))

        self.peg_lower_bound = Decimal(str(self.get_config("peg_lower_bound", "0.995")))
        self.peg_upper_bound = Decimal(str(self.get_config("peg_upper_bound", "1.005")))
        self.exit_concentration_pct = Decimal(
            str(self.get_config("pool_single_asset_exit_threshold_pct", "70"))
        )
        self.reentry_concentration_pct = Decimal(
            str(self.get_config("pool_single_asset_reentry_max_pct", "60"))
        )

        self.withdraw_slippage_limit_bps = Decimal(
            str(self.get_config("withdraw_slippage_limit_bps", "20"))
        )
        self.deposit_slippage_limit_bps = Decimal(
            str(self.get_config("deposit_slippage_limit_bps", "10"))
        )

        self.depeg_detection_enabled = bool(self.get_config("depeg_detection_enabled", True))
        self.depeg_exit_overrides_slippage = bool(
            self.get_config("depeg_exit_overrides_slippage", True)
        )
        self.material_depeg_move_bps = Decimal(str(self.get_config("material_depeg_move_bps", "30")))

        self.preferred_withdrawal_order = list(
            self.get_config("preferred_withdrawal_order", ["USDC", "DAI", "USDT"])
        )
        self.fallback_to_proportional_withdrawal_on_excessive_slippage = bool(
            self.get_config("fallback_to_proportional_withdrawal_on_excessive_slippage", True)
        )
        self.do_not_reopen_until_all_assets_in_peg_band = bool(
            self.get_config("do_not_reopen_until_all_assets_in_peg_band", True)
        )

        self.force_action = str(self.get_config("force_action", "")).lower().strip()
        self.force_swap_from = str(self.get_config("force_swap_from", "USDT")).upper()
        self.force_swap_to = str(self.get_config("force_swap_to", "USDC")).upper()
        self.force_swap_amount_pct_of_balance = Decimal(
            str(self.get_config("force_swap_amount_pct_of_balance", "0.1"))
        )
        self.force_position_id = str(self.get_config("force_position_id", "")).strip()
        self.pool_analytics_address = str(self.get_config("pool_analytics_address", "")).strip().lower()

        self._has_position = False
        self._lp_position_ref: str | None = None
        self._has_ever_opened = False
        self._last_prices: dict[str, Decimal] = {}
        self._pending_reentry_block = False
        self._last_exit_reason: str | None = None

    def decide(self, market: MarketSnapshot) -> Intent | None:
        if self.force_action:
            return self._forced_intent(market)

        try:
            prices = self._get_prices(market)
            balances = self._get_balances(market)
        except (
            PriceUnavailableError,
            PoolAnalyticsUnavailableError,
            PoolHistoryUnavailableError,
            SlippageEstimateUnavailableError,
            ValueError,
            KeyError,
        ) as exc:
            return Intent.hold(reason=f"market data unavailable: {exc}")

        material_depeg = self._material_depeg_detected(prices)
        self._last_prices = prices

        if self._has_position:
            try:
                concentration_pct = self._pool_concentration_pct(market)
            except (
                PoolAnalyticsUnavailableError,
                PoolHistoryUnavailableError,
                ValueError,
                KeyError,
            ) as exc:
                return Intent.hold(reason=f"pool composition unavailable: {exc}")

            peg_breach = not self._prices_in_peg_band(prices)
            concentration_breach = concentration_pct > self.exit_concentration_pct
            risk_exit = peg_breach or concentration_breach or material_depeg

            if not risk_exit:
                return Intent.hold(reason="position healthy; passive hold")

            if not self._lp_position_ref and not self.force_position_id:
                return Intent.hold(reason="cannot exit: missing LP position identifier")

            try:
                exit_slippage_bps = self._estimate_guard_slippage_bps(market, balances)
            except (
                SlippageEstimateUnavailableError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                return Intent.hold(reason=f"exit deferred: slippage estimate unavailable ({exc})")

            depeg_override = (
                self.depeg_exit_overrides_slippage and (peg_breach or material_depeg)
            )
            if exit_slippage_bps > self.withdraw_slippage_limit_bps and not depeg_override:
                return Intent.hold(reason="exit deferred: estimated withdrawal slippage too high")

            self._last_exit_reason = "depeg" if (peg_breach or material_depeg) else "concentration"
            return Intent.lp_close(
                position_id=self.force_position_id or self._lp_position_ref,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.chain,
            )

        if self._pending_reentry_block and self._needs_post_close_normalization(balances):
            normalization = self._build_preferred_normalization_swap(market, balances)
            if normalization is not None:
                return normalization

        if self.single_position_only and self._has_position:
            return Intent.hold(reason="single position mode")

        if self.do_not_reopen_until_all_assets_in_peg_band and not self._prices_in_peg_band(prices):
            return Intent.hold(reason="re-entry gated: peg band not restored")

        try:
            concentration_pct = self._pool_concentration_pct(market)
        except (
            PoolAnalyticsUnavailableError,
            PoolHistoryUnavailableError,
            ValueError,
            KeyError,
        ) as exc:
            return Intent.hold(reason=f"pool composition unavailable: {exc}")

        if concentration_pct > self.reentry_concentration_pct:
            return Intent.hold(reason="re-entry gated: pool concentration too high")

        try:
            deposit_slippage_bps = self._estimate_guard_slippage_bps(market, balances)
        except (
            SlippageEstimateUnavailableError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            return Intent.hold(reason=f"re-entry gated: slippage estimate unavailable ({exc})")

        if deposit_slippage_bps >= self.deposit_slippage_limit_bps:
            return Intent.hold(reason="re-entry gated: estimated deposit slippage too high")

        if all(balance <= Decimal("0") for balance in balances.values()):
            return Intent.hold(reason="no stablecoin balances available")

        allocation_pct = (
            self.reentry_lp_allocation_pct if self._has_ever_opened else self.initial_lp_allocation_pct
        )
        coin_amounts = self._build_coin_amounts(balances, allocation_pct)

        if all(amount <= Decimal("0") for amount in coin_amounts):
            return Intent.hold(reason="insufficient balances after reserve")

        return Intent.lp_open(
            pool=self.pool,
            coin_amounts=coin_amounts,
            protocol=self.protocol,
            chain=self.chain,
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        balances = self._get_balances(market)

        if self.force_action == "open":
            coin_amounts = self._build_coin_amounts(balances, self.initial_lp_allocation_pct)
            return Intent.lp_open(
                pool=self.pool,
                coin_amounts=coin_amounts,
                protocol=self.protocol,
                chain=self.chain,
            )

        if self.force_action == "swap":
            from_token = self.force_swap_from
            from_balance = balances.get(from_token, Decimal("0"))

            if from_balance <= Decimal("0"):
                for candidate in self.assets:
                    if candidate == self.force_swap_to:
                        continue
                    candidate_balance = balances.get(candidate, Decimal("0"))
                    if candidate_balance > Decimal("0"):
                        from_token = candidate
                        from_balance = candidate_balance
                        break

            amount = from_balance * self.force_swap_amount_pct_of_balance
            if amount <= Decimal("0"):
                return Intent.hold(reason="force swap skipped: no positive source balance")
            if from_token == self.force_swap_to:
                return Intent.hold(reason="force swap skipped: source and destination token match")

            return Intent.swap(
                from_token=from_token,
                to_token=self.force_swap_to,
                amount=amount,
                max_slippage=self.withdraw_slippage_limit_bps / Decimal("10000"),
                protocol=self.protocol,
                chain=self.chain,
            )

        if self.force_action == "close":
            return Intent.lp_close(
                position_id=self.force_position_id or self._lp_position_ref or self.pool,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.chain,
            )

        raise ValueError(f"unknown force_action: {self.force_action}")

    def _get_prices(self, market: MarketSnapshot) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for symbol in self.assets:
            prices[symbol] = Decimal(str(market.price(symbol)))
        return prices

    def _get_balances(self, market: MarketSnapshot) -> dict[str, Decimal]:
        balances: dict[str, Decimal] = {}
        for symbol in self.assets:
            token_balance = market.balance(symbol)
            balances[symbol] = Decimal(str(token_balance.balance))
        return balances

    def _prices_in_peg_band(self, prices: dict[str, Decimal]) -> bool:
        return all(self.peg_lower_bound <= price <= self.peg_upper_bound for price in prices.values())

    def _material_depeg_detected(self, prices: dict[str, Decimal]) -> bool:
        if not self.depeg_detection_enabled or not self._last_prices:
            return False
        for symbol, current in prices.items():
            previous = self._last_prices.get(symbol)
            if previous is None or previous <= Decimal("0"):
                continue
            move_bps = abs((current - previous) / previous) * Decimal("10000")
            if move_bps >= self.material_depeg_move_bps:
                return True
        return False

    def _pool_concentration_pct(self, market: MarketSnapshot) -> Decimal:
        analytics_envelope = market.pool_analytics(
            self._pool_analytics_identifier(),
            chain=self.chain,
            protocol=self.protocol,
        )
        analytics = getattr(analytics_envelope, "value", analytics_envelope)

        if hasattr(analytics, "token_weights"):
            token_weights = getattr(analytics, "token_weights")
            if isinstance(token_weights, dict) and token_weights:
                return max(Decimal(str(v)) for v in token_weights.values()) * Decimal("100")
            if isinstance(token_weights, list) and token_weights:
                return max(Decimal(str(v)) for v in token_weights) * Decimal("100")

        weights: list[Decimal] = []
        for field in ("token0_weight", "token1_weight", "token2_weight"):
            value = getattr(analytics, field, None)
            if value is not None:
                weights.append(Decimal(str(value)))

        if not weights:
            raise ValueError("pool concentration weights not available")

        return max(weights) * Decimal("100")

    def _pool_analytics_identifier(self) -> str:
        if self.pool_analytics_address:
            return self.pool_analytics_address
        if self.protocol == "curve" and self.chain == "ethereum" and self.pool.lower() == "3pool":
            return CURVE_3POOL_ETHEREUM_ADDRESS
        return self.pool

    def _estimate_guard_slippage_bps(
        self,
        market: MarketSnapshot,
        balances: dict[str, Decimal],
    ) -> Decimal:
        amount = balances.get("USDC", Decimal("0"))
        if amount <= Decimal("0"):
            amount = balances.get("DAI", Decimal("0"))
        if amount <= Decimal("0"):
            amount = balances.get("USDT", Decimal("0"))
        if amount <= Decimal("0"):
            amount = Decimal("100")

        envelope = market.estimate_slippage(
            token_in="USDC",
            token_out="DAI",
            amount=amount,
            chain=self.chain,
            protocol=self.protocol,
        )
        estimate = getattr(envelope, "value", envelope)
        slippage_bps = getattr(estimate, "slippage_bps", None)
        if slippage_bps is not None:
            return Decimal(str(slippage_bps))
        price_impact_bps = getattr(estimate, "price_impact_bps", None)
        if price_impact_bps is not None:
            return Decimal(str(price_impact_bps))
        raise ValueError("slippage estimate did not include bps fields")

    def _build_coin_amounts(
        self,
        balances: dict[str, Decimal],
        allocation_pct: Decimal,
    ) -> list[Decimal]:
        deploy_pct = max(Decimal("0"), min(Decimal("1"), allocation_pct))
        reserve_pct = max(Decimal("0"), min(Decimal("0.25"), self.dust_reserve_pct_per_asset))

        ordered = ["DAI", "USDC", "USDT"]
        amounts: list[Decimal] = []
        for symbol in ordered:
            balance = balances.get(symbol, Decimal("0"))
            amount = balance * deploy_pct * (Decimal("1") - reserve_pct)
            amounts.append(max(amount, Decimal("0")))
        return amounts

    def _needs_post_close_normalization(self, balances: dict[str, Decimal]) -> bool:
        preferred = self.preferred_withdrawal_order[0]
        non_preferred = [s for s in self.assets if s != preferred]
        return any(balances.get(symbol, Decimal("0")) > Decimal("0") for symbol in non_preferred)

    def _build_preferred_normalization_swap(
        self,
        market: MarketSnapshot,
        balances: dict[str, Decimal],
    ) -> Intent | None:
        preferred = self.preferred_withdrawal_order[0]

        for symbol in self.preferred_withdrawal_order[1:]:
            amount = balances.get(symbol, Decimal("0"))
            if amount <= Decimal("0"):
                continue

            try:
                estimated_slippage = self._estimate_guard_slippage_bps(market, balances)
            except (
                SlippageEstimateUnavailableError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                return Intent.hold(reason=f"normalization deferred: slippage estimate unavailable ({exc})")

            if estimated_slippage > self.withdraw_slippage_limit_bps:
                if self.fallback_to_proportional_withdrawal_on_excessive_slippage:
                    self._pending_reentry_block = False
                    return None
                return Intent.hold(reason="normalization deferred: slippage too high")

            return Intent.swap(
                from_token=symbol,
                to_token=preferred,
                amount=amount,
                max_slippage=self.withdraw_slippage_limit_bps / Decimal("10000"),
                protocol=self.protocol,
                chain=self.chain,
            )

        self._pending_reentry_block = False
        return None

    def on_intent_executed(self, intent: Intent, success: bool, result: Any) -> None:
        if not success:
            return

        intent_type = intent.intent_type.value
        if intent_type == "LP_OPEN":
            self._has_position = True
            self._has_ever_opened = True
            self._pending_reentry_block = False
            position_id = getattr(result, "position_id", None)
            if position_id is not None:
                self._lp_position_ref = str(position_id)
            elif self._lp_position_ref is None:
                self._lp_position_ref = self.pool

        if intent_type == "LP_CLOSE":
            self._has_position = False
            self._pending_reentry_block = True

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "has_position": self._has_position,
            "lp_position_ref": self._lp_position_ref,
            "has_ever_opened": self._has_ever_opened,
            "last_prices": {k: str(v) for k, v in self._last_prices.items()},
            "pending_reentry_block": self._pending_reentry_block,
            "last_exit_reason": self._last_exit_reason,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        self._has_position = bool(state.get("has_position", False))
        self._lp_position_ref = state.get("lp_position_ref")
        self._has_ever_opened = bool(state.get("has_ever_opened", False))
        saved_prices = state.get("last_prices", {})
        self._last_prices = {
            str(k): Decimal(str(v))
            for k, v in saved_prices.items()
            if v is not None
        }
        self._pending_reentry_block = bool(state.get("pending_reentry_block", False))
        self._last_exit_reason = state.get("last_exit_reason")

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "l_p_full_range_curve_3pool",
            "chain": self.chain,
            "pool": self.pool,
            "protocol": self.protocol,
            "has_position": self._has_position,
            "lp_position_ref": self._lp_position_ref,
            "pending_reentry_block": self._pending_reentry_block,
            "last_exit_reason": self._last_exit_reason,
        }

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions: list[PositionInfo] = []
        if self._has_position:
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=str(self._lp_position_ref or self.pool),
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=Decimal("0"),
                    details={"pool": self.pool},
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", "l_p_full_range_curve_3pool"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        if not self._has_position:
            return []

        return [
            Intent.lp_close(
                position_id=self._lp_position_ref or self.pool,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.chain,
            )
        ]
