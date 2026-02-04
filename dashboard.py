import streamlit as st
import pandas as pd
import time
import plotly.graph_objects as go

# Import our system modules
from system_senses_stream import fetch_stock_data
from senses_macro import fetch_macro_data, analyze_market_regime
from system_strategy_evaluator import calculate_score

# --- PAGE CONFIG ---
st.set_page_config(page_title="Trading Bot Command Center", layout="wide", page_icon="📈")

# --- HEADER ---
st.title("🤖 Algo-Trading Command Center")
st.markdown("Live market analysis using **Momentum**, **Trend**, and **Macro** sensors.")

# --- SIDEBAR CONTROLS ---
st.sidebar.header("Configuration")
watchlist_input = st.sidebar.text_area("Watchlist", "NVDA, AAPL, EURUSD=X, BTC-USD")
refresh_rate = st.sidebar.slider("Refresh Rate (seconds)", 30, 300, 60)

if st.sidebar.button("Run Manual Scan"):
    st.cache_data.clear() 

# --- MACRO SENSORS ---
st.subheader("🌍 Global Market Regime")
col1, col2, col3 = st.columns(3)

macro_data = fetch_macro_data()
if macro_data:
    regime = analyze_market_regime(macro_data)
    if regime:
        col1.metric("VIX (Fear Index)", f"{macro_data.get('vix', 0):.2f}")
        
        tnx_val = macro_data.get('tnx_yield', 0)
        tnx_delta = macro_data.get('tnx_change_pct', 0)
        col2.metric("10-Year Yield", f"{tnx_val:.2f}%", f"{tnx_delta:.2f}%", delta_color="inverse")
        
        score = regime.get('score_modifier', 0)
        status_color = "green" if score >= 0 else "red"
        reasons = regime.get('regime_reasons', ["Unknown"])
        col3.markdown(f"**Market Status:** :{status_color}[{reasons[0]}]")
else:
    col3.warning("Macro Data Unavailable")

st.divider()

# --- MAIN ASSET SCANNER ---
st.subheader("🔍 Asset Analysis")

tickers = [t.strip() for t in watchlist_input.split(',')]

for ticker in tickers:
    with st.expander(f"Analysis: {ticker}", expanded=True):
        # 1. Fetch Data
        data = fetch_stock_data(ticker)
        
        if data is not None:
            # 2. Extract History for Charting (if available)
            history_df = data.get('history', pd.DataFrame())
            
            # 3. Get Brain Decision
            # The Brain expects the 'data' dict we just fetched
            result = calculate_score(data, ticker)
            decision = result.get('decision', 'WAIT')
            score = result.get('final_score', 0)
            breakdown = result.get('breakdown', [])
            
            # 4. Layout
            c1, c2 = st.columns([1, 2])
            
            with c1:
                # Use 'current_price' safely
                curr_price = data.get('current_price')
                if curr_price:
                    st.metric("Price", f"${curr_price:,.2f}")
                else:
                    st.metric("Price", "N/A")
                
                st.metric("Technical Score", f"{score}/10")
                
                if decision == "BUY":
                    st.success(f"**ACTION: {decision}**")
                elif decision == "SELL":
                    st.error(f"**ACTION: {decision}**")
                else:
                    st.warning(f"**ACTION: {decision}**")
                
                st.write("**Logic Breakdown:**")
                for reason in breakdown:
                    st.caption(f"• {reason}")
            
            with c2:
                # Plotly Candle Chart
                if not history_df.empty:
                    # We only plot the last 60 days to keep it readable
                    plot_data = history_df.tail(60)
                    
                    fig = go.Figure(data=[go.Candlestick(
                        x=plot_data.index,
                        open=plot_data['Open'], high=plot_data['High'],
                        low=plot_data['Low'], close=plot_data['Close']
                    )])
                    
                    # Add SMA lines if they exist
                    if 'sma_50' in plot_data.columns:
                        fig.add_trace(go.Scatter(x=plot_data.index, y=plot_data['sma_50'], line=dict(color='orange', width=1), name='SMA 50'))
                    
                    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=300, xaxis_rangeslider_visible=False)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Chart data unavailable (No history returned)")
        else:
            st.warning(f"Could not fetch data for {ticker}")

# --- AUTO REFRESH ---
if st.checkbox("Enable Auto-Refresh", value=False):
    time.sleep(refresh_rate)
    st.rerun()