"""
MCX Gold Relative Value & Execution Intelligence Dashboard
Designed for Hackathon Demonstration:
- Contract Term Structure & Normalization Inspector
- Realistic Execution & Cost-Stress Analytics
- Non-lookahead Walk-Forward Backtester
- Auditable Alert Gatekeeper (Silence by default)
"""
from __future__ import annotations

import statistics as stats
from collections import Counter
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from pipeline.synthetic import generate_synthetic_bhavcopy
from pipeline.validate import validate_rows, flag_price_jumps
from pipeline.normalize import build_contract_series, concurrent_pairs
from backtest.engine import walk_forward_backtest
from backtest.attribution import analyze
from alerts.engine import evaluate_day, Alert, SuppressedSignal

# ---------------------------------------------------------
# Page Configuration & Styling
# ---------------------------------------------------------
st.set_page_config(
    page_title="MCX Gold RV Intelligence",
    page_icon="🪙",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .metric-card {
        background-color: #1e2130;
        padding: 15px;
        border-radius: 8px;
        border-left: 4px solid #f0b90b;
    }
    .stAlert {
        border-radius: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🪙 MCX Gold Cross-Contract Relative Value")
st.caption(
    "Quantitative Arbitrage & Microstructure Defense: Turning Exchange Bhavcopy "
    "into Defensible Signals Across Standardized Gold Contracts."
)

# ---------------------------------------------------------
# Sidebar Controls
# ---------------------------------------------------------
st.sidebar.header("Strategy Configuration")

scenario = st.sidebar.selectbox(
    "Market Scenario",
    options=["injected", "null"],
    index=0,
    help="Null: Pure noise + common gold price (Honest exchange baseline).\n"
         "Injected: Transient AR(1) mispricing (Tests engine detection).",
)

symbols = ["GOLDM", "GOLDTEN", "GOLDGUINEA", "GOLDPETAL"]
c_leg1, c_leg2 = st.sidebar.columns(2)
with c_leg1:
    leg_a = st.selectbox("Base Leg (A)", symbols, index=0)
with c_leg2:
    leg_b = st.selectbox("Cross Leg (B)", symbols, index=1)

max_expiry_gap = st.sidebar.slider(
    "Nominal Expiry Gap Tolerance (Days)",
    min_value=1,
    max_value=25,
    value=10,
    help="GOLDM expires 3rd–5th; others expire 27th–31st. Sets allowable gap for matching delivery months.",
)

st.sidebar.markdown("---")
st.sidebar.markdown("### 🔍 Model Assumptions")
st.sidebar.info(
    "• **Walk-Forward**: [t-30, t-1] strictly rolling window.\n"
    "• **Entry**: |z| >= 2.0 | **Exit**: |z| <= 0.5 or 20d hold.\n"
    "• **Liquidity Floor**: Minimum open-interest gate enforced."
)

# ---------------------------------------------------------
# Data Pipeline Caching
# ---------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_and_process_pipeline(scen: str, sym_a: str, sym_b: str, gap: int):
    raw_rows = generate_synthetic_bhavcopy(scenario=scen)
    structural_issues = validate_rows(raw_rows)
    jump_issues = flag_price_jumps(raw_rows)
    series_map = build_contract_series(raw_rows)
    matched_pairs = concurrent_pairs(series_map, sym_a, sym_b, max_expiry_gap_days=gap)
    return raw_rows, structural_issues, jump_issues, series_map, matched_pairs


with st.spinner("Processing Bhavcopy normalization & lifecycle calendars..."):
    rows, issues, jumps, contract_series, pairs = load_and_process_pipeline(
        scenario, leg_a, leg_b, max_expiry_gap
    )

# ---------------------------------------------------------
# Tabbed Demonstration
# ---------------------------------------------------------
tab_theory, tab_backtest, tab_attribution, tab_alerts = st.tabs(
    [
        "📐 Purity & Normalization Engine",
        "⚡ Walk-Forward Backtest",
        "⚖️ Execution Realism & Attribution",
        "🚨 Auditable Alert System",
    ]
)

# Contract specifications (purity in parts per thousand, quotation unit in grams)
CONTRACT_SPECS = {
    "GOLDM": {"quoted_per": 10.0, "purity": 995.0},
    "GOLDTEN": {"quoted_per": 10.0, "purity": 999.0},
    "GOLDGUINEA": {"quoted_per": 8.0, "purity": 999.0},
    "GOLDPETAL": {"quoted_per": 1.0, "purity": 999.0},
}


def get_norm_price(bar, symbol: str) -> float:
    """Extract or calculate the normalized price per gram of fine gold."""
    if hasattr(bar, "price_per_gram_fine"):
        val = getattr(bar, "price_per_gram_fine")
        return val() if callable(val) else val
    spec = CONTRACT_SPECS.get(symbol, {"quoted_per": 10.0, "purity": 995.0})
    close_px = getattr(bar, "close", getattr(bar, "settlement_price", 0.0))
    return (close_px / spec["quoted_per"]) / (spec["purity"] / 1000.0)


# =========================================================
# TAB 1: Purity & Normalization Engine
# =========================================================
with tab_theory:
    st.subheader("Contract Specifications & Normalized Base")
    st.markdown(
        """
        Exchange-traded gold quotes are structurally incomparable out of the box. 
        Settlement prices must be normalized to **₹ per gram of 100% fine gold**:
        $$\\text{Price per Gram Fine} = \\frac{\\text{Close Price}}{\\text{Grams Quoted}} \\times \\frac{1}{\\text{Purity} / 1000}$$
        """
    )

    specs = pd.DataFrame(
        [
            {"Symbol": "GOLDM", "Trading Unit": "100 g", "Quoted Per": "10 g", "Purity": "995 (99.5%)", "Expiry Window": "3rd – 5th"},
            {"Symbol": "GOLDTEN", "Trading Unit": "10 g", "Quoted Per": "10 g", "Purity": "999 (99.9%)", "Expiry Window": "27th – 31st"},
            {"Symbol": "GOLDGUINEA", "Trading Unit": "8 g", "Quoted Per": "8 g", "Purity": "999 (99.9%)", "Expiry Window": "27th – 31st"},
            {"Symbol": "GOLDPETAL", "Trading Unit": "1 g", "Quoted Per": "1 g", "Purity": "999 (99.9%)", "Expiry Window": "27th – 31st"},
        ]
    )
    st.table(specs)

    if pairs:
        st.markdown(f"### Normalization in Action: `{leg_a}` vs `{leg_b}`")
        sample_a, sample_b = pairs[-1]
        common_dates = sorted(set(sample_a.bars) & set(sample_b.bars))

        if common_dates:
            norm_records = []
            for d in common_dates[-90:]:
                bar_a = sample_a.bars[d]
                bar_b = sample_b.bars[d]

                norm_a = get_norm_price(bar_a, sample_a.symbol)
                norm_b = get_norm_price(bar_b, sample_b.symbol)

                raw_close_a = getattr(bar_a, "close", getattr(bar_a, "settlement_price", 0.0))
                raw_close_b = getattr(bar_b, "close", getattr(bar_b, "settlement_price", 0.0))

                spread_bps = ((norm_a - norm_b) / norm_b) * 10000.0 if norm_b != 0 else 0.0

                norm_records.append(
                    {
                        "Date": d,
                        f"{sample_a.symbol} Raw Close": raw_close_a,
                        f"{sample_b.symbol} Raw Close": raw_close_b,
                        f"{sample_a.symbol} Norm (₹/g)": norm_a,
                        f"{sample_b.symbol} Norm (₹/g)": norm_b,
                        "Normalized Spread (bps)": spread_bps,
                    }
                )
            df_norm = pd.DataFrame(norm_records)

            fig = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                subplot_titles=(
                    "Raw Settlement Prices (Non-comparable Units)",
                    "Normalized Price per Pure Gram (True Relative Value)",
                ),
                vertical_spacing=0.12,
            )
            fig.add_trace(
                go.Scatter(x=df_norm["Date"], y=df_norm[f"{sample_a.symbol} Raw Close"], name=f"Raw {sample_a.symbol}"),
                row=1,
                col=1,
            )
            fig.add_trace(
                go.Scatter(x=df_norm["Date"], y=df_norm[f"{sample_b.symbol} Raw Close"], name=f"Raw {sample_b.symbol}"),
                row=1,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df_norm["Date"],
                    y=df_norm[f"{sample_a.symbol} Norm (₹/g)"],
                    name=f"Norm {sample_a.symbol}",
                    line=dict(color="#00CC96"),
                ),
                row=2,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=df_norm["Date"],
                    y=df_norm[f"{sample_b.symbol} Norm (₹/g)"],
                    name=f"Norm {sample_b.symbol}",
                    line=dict(color="#AB63FA"),
                ),
                row=2,
                col=1,
            )
            fig.update_layout(height=520, template="plotly_dark", hovermode="x unified")
            st.plotly_chart(fig, use_container_width=True)

# =========================================================
# TAB 2: Walk-Forward Backtest
# =========================================================
with tab_backtest:
    st.subheader(f"Walk-Forward Engine: {leg_a} vs {leg_b}")

    if not pairs:
        st.warning(f"No matched contract cycles found between {leg_a} and {leg_b}. Try widening the expiry gap.")
    else:
        all_trades = []
        for a, b in pairs:
            trades = walk_forward_backtest(a, b)
            all_trades.extend((t, a, b) for t in trades)

        closed = [t for t, _, _ in all_trades if t.exit_date is not None]

        if closed:
            net_bps = [t.net_pnl_bps for t in closed]
            gross_bps = [t.gross_pnl_bps for t in closed]
            cost_bps = [t.cost_bps for t in closed]
            win_rate = sum(1 for t in closed if t.net_pnl_bps > 0) / len(closed)

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Closed Trades", len(closed))
            c2.metric("Win Rate", f"{win_rate:.1%}")
            c3.metric("Gross Alpha / Trade", f"{stats.mean(gross_bps):+.1f} bps")
            c4.metric("Avg Drag (Spread+Slip)", f"{stats.mean(cost_bps):.1f} bps")
            c5.metric(
                "Net Realized Edge",
                f"{stats.mean(net_bps):+.1f} bps",
                delta=f"{sum(net_bps):+.0f} total bps",
            )

            df_trades = pd.DataFrame(
                [
                    {
                        "entry_date": t.entry_date,
                        "exit_date": t.exit_date,
                        "gross_pnl_bps": t.gross_pnl_bps,
                        "cost_bps": t.cost_bps,
                        "net_pnl_bps": t.net_pnl_bps,
                        "exit_reason": t.exit_reason,
                    }
                    for t in closed
                ]
            ).sort_values("exit_date")

            df_trades["cum_gross"] = df_trades["gross_pnl_bps"].cumsum()
            df_trades["cum_net"] = df_trades["net_pnl_bps"].cumsum()

            fig_pnl = go.Figure()
            fig_pnl.add_trace(
                go.Scatter(
                    x=df_trades["exit_date"],
                    y=df_trades["cum_gross"],
                    name="Gross Paper PnL",
                    line=dict(color="#00FFAA", dash="dash"),
                )
            )
            fig_pnl.add_trace(
                go.Scatter(
                    x=df_trades["exit_date"],
                    y=df_trades["cum_net"],
                    name="Net Realized PnL (Post Slippage & Spread)",
                    line=dict(color="#FF4B4B" if df_trades["cum_net"].iloc[-1] < 0 else "#00FF66", width=2.5),
                )
            )
            fig_pnl.update_layout(
                title="Gross vs. Realized Return: The Friction Wedge",
                xaxis_title="Exit Date",
                yaxis_title="Cumulative Return (Bps)",
                template="plotly_dark",
                hovermode="x unified",
            )
            st.plotly_chart(fig_pnl, use_container_width=True)

            col_ex1, col_ex2 = st.columns([1, 2])
            with col_ex1:
                reasons = Counter(df_trades["exit_reason"])
                fig_ex = px.pie(
                    values=list(reasons.values()),
                    names=list(reasons.keys()),
                    title="Lifecycle Exit Reasons",
                    hole=0.45,
                    template="plotly_dark",
                )
                st.plotly_chart(fig_ex, use_container_width=True)
            with col_ex2:
                st.markdown("### Execution Characteristics")
                st.write(
                    "• **Reversion Target reached**: Spread collapsed back within ±0.5 Z.\n"
                    "• **Max Hold**: Forced exit after 20 holding days to prevent capital lockup.\n"
                    "• **Near Expiry**: Forced exit 5 days prior to first leg delivery to eliminate delivery risk."
                )
                st.dataframe(df_trades.tail(5), use_container_width=True)
        else:
            st.warning("No trade signals passed the executability, OI, and cost-hurdle filters.")

# =========================================================
# TAB 3: Execution Realism & Attribution
# =========================================================
with tab_attribution:
    st.subheader("Attribution & Cost Sensitivity Analysis")
    st.markdown(
        """
        A true relative value strategy must satisfy two criteria:
        1. **Zero Outright Exposure**: Returns must show near-zero correlation with underlying gold price movements.
        2. **Stress-Surviving Edge**: The edge must survive when assumed broker spreads are doubled.
        """
    )

    report_pair = None
    if 'all_trades' in locals() and pairs:
        for a, b in pairs:
            pair_trades = [t for t, aa, bb in all_trades if aa is a and bb is b and t.exit_date is not None]
            if len(pair_trades) >= 5:
                report_pair = (a, b, pair_trades)
                break

    if report_pair:
        a, b, p_trades = report_pair
        report = analyze(p_trades, a, b)

        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Strategy Verdict", report.verdict)
        a2.metric(
            "Gold Outright Correlation",
            f"{report.gold_correlation:.3f}" if report.gold_correlation is not None else "N/A",
            help="Correlation to outright gold move. A value near 0 confirms genuine market neutrality.",
        )
        a3.metric("Net Edge @ 0.5x Costs", f"{report.net_bps_at_half_cost:+.1f} bps")
        a4.metric("Net Edge @ 2.0x Costs", f"{report.net_bps_at_double_cost:+.1f} bps")

        base_net = stats.mean([t.net_pnl_bps for t in p_trades])
        stress_df = pd.DataFrame(
            {
                "Cost Scenario": ["Optimistic (0.5x Spread)", "Baseline Model (1.0x)", "Pessimistic Stress (2.0x)"],
                "Net Return (bps/trade)": [report.net_bps_at_half_cost, base_net, report.net_bps_at_double_cost],
            }
        )

        fig_stress = px.bar(
            stress_df,
            x="Cost Scenario",
            y="Net Return (bps/trade)",
            color="Net Return (bps/trade)",
            color_continuous_scale="RdYlGn",
            template="plotly_dark",
            title=f"Cost Fragility Analysis: {a.symbol} vs {b.symbol}",
        )
        st.plotly_chart(fig_stress, use_container_width=True)

        if report.verdict == "REJECT_COST":
            st.error(
                f"**Demonstration of the Null**: While gross alpha is detectable, "
                f"the spread between {a.symbol} and {b.symbol} is consumed by transaction costs. "
                f"This matches the hackathon requirement: *'A rigorous demonstration that no persistent edge survives costs is also a valid analytical outcome.'*"
            )
    else:
        st.info("Insufficient trade density across a single contract pair to render an attribution verdict (min 5 required).")

# =========================================================
# TAB 4: Auditable Alert System
# =========================================================
with tab_alerts:
    st.subheader("Auditable Alert Engine: Silence by Default")
    st.caption(
        "Standard dashboards emit noisy false alerts. This engine enforces an audit trail: "
        "if an alert does not fire, it logs the exact structural gatekeeper that suppressed it."
    )

    if pairs:
        a, b = pairs[-1]
        common = sorted(set(a.bars) & set(b.bars))
        tail = common[-100:] if len(common) > 100 else common

        alerts, suppressed = [], []
        for i, d in enumerate(tail):
            hist_start_idx = max(0, len(common) - len(tail) + i - 30)
            hist_dates = common[hist_start_idx : len(common) - len(tail) + i]
            res = evaluate_day(a, b, d, hist_dates)
            if isinstance(res, Alert):
                alerts.append(res)
            elif isinstance(res, SuppressedSignal):
                suppressed.append(res)

        k1, k2, k3 = st.columns(3)
        k1.metric("Trading Days Monitored", len(tail))
        k2.metric("Actionable Live Alerts", len(alerts))
        k3.metric("Suppressed (Saved Friction)", len(suppressed))

        if alerts:
            st.success(f"🟢 {len(alerts)} High-Probability Trading Signals Fired")
            alert_records = [
                {
                    "Date": getattr(al, "date", "N/A"),
                    "Z-Score": f"{getattr(al, 'z_score', 0.0):.2f}",
                    "Direction": getattr(al, "suggested_direction", getattr(al, "direction", "N/A")),
                    "Expected Edge": f"{getattr(al, 'expected_edge_bps', 0.0):.1f} bps",
                    "Rationale": getattr(al, "note", getattr(al, "detail", "")),
                }
                for al in alerts
            ]
            st.dataframe(pd.DataFrame(alert_records), use_container_width=True)

        st.markdown("### Gatekeeper Audit Log (Why We Stayed Silent)")
        if suppressed:
            reason_counts = Counter(getattr(s, "reason", "unknown") for s in suppressed)
            st.write("**Suppression Breakdown:**", dict(reason_counts))

            supp_records = []
            for s in suppressed[-15:]:
                z_val = getattr(s, "z_score", None)
                supp_records.append(
                    {
                        "Date": getattr(s, "date", "N/A"),
                        "Z-Score": f"{z_val:.2f}" if isinstance(z_val, (int, float)) else "N/A",
                        "Direction": getattr(s, "suggested_direction", getattr(s, "direction", "N/A")),
                        "Suppression Reason": getattr(s, "reason", "N/A"),
                        "Diagnostic Detail": getattr(s, "detail", getattr(s, "note", "")),
                    }
                )
            st.dataframe(pd.DataFrame(supp_records), use_container_width=True)
        else:
            st.info("Market is behaving within normal boundaries. No signals crossed threshold.")