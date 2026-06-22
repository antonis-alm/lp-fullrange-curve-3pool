from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from dashboard import ui


class _Column:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _columns(count: int):
    return [_Column() for _ in range(count)]


def test_render_overview_renders_core_metrics() -> None:
    state = {"has_position": True}
    config = {"chain": "ethereum", "protocol": "curve", "pool": "3pool"}

    with patch.object(ui.st, "columns", return_value=_columns(4)), patch.object(ui.st, "metric") as metric:
        ui._render_overview(config, state)

    assert metric.call_count == 4


def test_render_peg_monitor_uses_prices_and_warns_on_breach() -> None:
    config = {
        "assets": ["DAI", "USDC", "USDT"],
        "peg_lower_bound": "0.995",
        "peg_upper_bound": "1.005",
    }
    state = {"last_prices": {"DAI": "1.0", "USDC": "1.0", "USDT": "0.99"}}

    with patch.object(ui.st, "columns", return_value=_columns(3)), patch.object(ui.st, "metric") as metric, patch.object(
        ui.st, "warning"
    ) as warning, patch.object(ui.st, "success") as success:
        ui._render_peg_monitor(config, state)

    assert metric.call_count == 4
    warning.assert_called_once()
    success.assert_not_called()


def test_render_position_risk_renders_threshold_metrics() -> None:
    config = {
        "pool_single_asset_exit_threshold_pct": 70,
        "pool_single_asset_reentry_max_pct": 60,
    }
    state = {
        "pending_reentry_block": True,
        "last_exit_reason": "depeg",
        "lp_position_ref": "lp-1",
    }

    with patch.object(ui.st, "columns", side_effect=[_columns(3), _columns(2)]), patch.object(ui.st, "metric") as metric:
        ui._render_position_risk(config, state)

    assert metric.call_count == 5


def test_render_custom_dashboard_calls_audit_sections() -> None:
    api_client = MagicMock()
    api_client.get_state.return_value = {"has_position": True, "last_prices": {"DAI": "1", "USDC": "1", "USDT": "1"}}

    with patch.object(ui.st, "title"), patch.object(ui.st, "columns", side_effect=[_columns(4), _columns(3), _columns(3), _columns(2)]), patch.object(
        ui.st, "metric"
    ), patch.object(ui.st, "success"), patch.object(ui.st, "warning"), patch.object(
        ui, "render_pnl_section"
    ) as pnl, patch.object(ui, "render_cost_stack_section") as cost, patch.object(
        ui, "render_trade_tape_section"
    ) as tape:
        ui.render_custom_dashboard(
            deployment_id="dep-1",
            strategy_config={
                "chain": "ethereum",
                "protocol": "curve",
                "pool": "3pool",
                "assets": ["DAI", "USDC", "USDT"],
            },
            api_client=api_client,
            session_state={},
        )

    pnl.assert_called_once_with("dep-1")
    cost.assert_called_once_with("dep-1")
    tape.assert_called_once_with("dep-1")


def test_to_decimal_returns_decimal_defaults() -> None:
    assert ui._to_decimal("1.23") == Decimal("1.23")
    assert ui._to_decimal("bad") == Decimal("0")
