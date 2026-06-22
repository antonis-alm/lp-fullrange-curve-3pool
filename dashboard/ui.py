from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

try:
    import streamlit as st
except ModuleNotFoundError:
    class _Column:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _StreamlitFallback:
        def title(self, *args: Any, **kwargs: Any) -> None:
            return None

        def metric(self, *args: Any, **kwargs: Any) -> None:
            return None

        def warning(self, *args: Any, **kwargs: Any) -> None:
            return None

        def success(self, *args: Any, **kwargs: Any) -> None:
            return None

        def columns(self, count: int):
            return [_Column() for _ in range(count)]

    st = _StreamlitFallback()

try:
    from almanak.framework.dashboard import (
        render_cost_stack_section,
        render_pnl_section,
        render_trade_tape_section,
    )
except ModuleNotFoundError:
    def render_pnl_section(deployment_id: str) -> None:
        return None

    def render_cost_stack_section(deployment_id: str) -> None:
        return None

    def render_trade_tape_section(deployment_id: str) -> None:
        return None


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _format_decimal(value: Decimal, places: int = 4) -> str:
    quant = Decimal("1") if places == 0 else Decimal(f"1e-{places}")
    return format(value.quantize(quant), "f")


def _load_live_state(api_client: Any, deployment_id: str) -> dict[str, Any]:
    if api_client is None:
        return {}
    try:
        state = api_client.get_state(deployment_id)
        if isinstance(state, dict):
            return state
    except Exception as exc:
        st.warning(f"Live state unavailable: {exc}")
    return {}


def _render_overview(strategy_config: dict[str, Any], state: dict[str, Any]) -> None:
    chain = str(strategy_config.get("chain", "ethereum"))
    protocol = str(strategy_config.get("protocol", "curve"))
    pool = str(strategy_config.get("pool", "3pool"))
    has_position = bool(state.get("has_position", False))

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Chain", chain)
    with col2:
        st.metric("Protocol", protocol)
    with col3:
        st.metric("Pool", pool)
    with col4:
        st.metric("Position", "Active" if has_position else "Inactive")


def _render_peg_monitor(strategy_config: dict[str, Any], state: dict[str, Any]) -> None:
    assets = strategy_config.get("assets", ["DAI", "USDC", "USDT"])
    lower = _to_decimal(strategy_config.get("peg_lower_bound", "0.995"))
    upper = _to_decimal(strategy_config.get("peg_upper_bound", "1.005"))
    prices = state.get("last_prices", {})

    cols = st.columns(max(len(assets), 1))
    max_deviation_bps = Decimal("0")

    for idx, asset in enumerate(assets):
        price = _to_decimal(prices.get(asset, "0"))
        deviation_bps = abs(price - Decimal("1")) * Decimal("10000") if price > 0 else Decimal("0")
        max_deviation_bps = max(max_deviation_bps, deviation_bps)
        label = f"{asset} Price"
        value = _format_decimal(price, 6) if price > 0 else "N/A"
        with cols[idx]:
            st.metric(label, value)

    st.metric("Max Deviation", f"{_format_decimal(max_deviation_bps, 2)} bps")

    in_band = True
    for asset in assets:
        price = _to_decimal(prices.get(asset, "0"))
        if price <= 0 or price < lower or price > upper:
            in_band = False
            break

    if in_band:
        st.success("All assets are inside peg bounds.")
    else:
        st.warning("One or more assets are outside peg bounds or missing price data.")


def _render_position_risk(strategy_config: dict[str, Any], state: dict[str, Any]) -> None:
    pending_reentry_block = bool(state.get("pending_reentry_block", False))
    last_exit_reason = str(state.get("last_exit_reason", "none"))
    position_ref = str(state.get("lp_position_ref", "none"))

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("LP Position Ref", position_ref)
    with col2:
        st.metric("Re-entry Block", "Yes" if pending_reentry_block else "No")
    with col3:
        st.metric("Last Exit Reason", last_exit_reason)

    exit_threshold = _to_decimal(strategy_config.get("pool_single_asset_exit_threshold_pct", "70"))
    reentry_threshold = _to_decimal(strategy_config.get("pool_single_asset_reentry_max_pct", "60"))

    col4, col5 = st.columns(2)
    with col4:
        st.metric("Exit Concentration Threshold", f"{_format_decimal(exit_threshold, 0)}%")
    with col5:
        st.metric("Re-entry Concentration Max", f"{_format_decimal(reentry_threshold, 0)}%")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title("LP-FullRange-Curve-3pool")

    live_state = _load_live_state(api_client, deployment_id)
    merged_state = dict(session_state or {})
    merged_state.update(live_state)

    _render_overview(strategy_config, merged_state)
    _render_peg_monitor(strategy_config, merged_state)
    _render_position_risk(strategy_config, merged_state)

    render_pnl_section(deployment_id)
    render_cost_stack_section(deployment_id)
    render_trade_tape_section(deployment_id)
