# 🤖 AI-Enhanced Algorithmic Trading Bot

A multi-asset automated trading system built in Python, featuring a real-time monitoring dashboard, macro-economic sentiment filtering, and a secure, encrypted trading journal.



## 🌟 Overview
This system is designed to analyze **Stocks**, **Forex**, and **Cryptocurrency** using asset-specific technical strategies. It incorporates a "Macro Sensor" that monitors global volatility and bond yields to adjust trading aggression dynamically.

### 🚀 Key Features
* **Specialized Brains:** Separate logic modules for Stocks (Volume-based), Forex (Mean Reversion), and Crypto (Momentum-based).
* **Macro Sentinel:** Monitors the **VIX** (Fear Index) and **10-Year Treasury Yields** to determine market regimes.
* **Live Dashboard:** A Streamlit-based GUI featuring interactive Plotly charts and real-time technical scoring.
* **Encrypted Journaling:** All trade signals and logic breakdowns are AES-encrypted and synced to a secure "Memory Bank" on Google Drive.
* **Safety First:** Built-in "Paper Trading" simulation mode to test strategies without financial risk.

---

## 🏗️ System Architecture

The bot is divided into four distinct layers:

1.  **The Senses (`system_senses_stream.py`, `senses_macro.py`):** Fetches live price data and global macro indicators via Yahoo Finance.
2.  **The Brain (`system_strategy_evaluator.py`):** Routes data to asset-specific algorithms (`algo_stocks.py`, etc.) for technical analysis.
3.  **The Hands (`system_execution_client.py`):** Interfaces with the **Charles Schwab API** for portfolio syncing and order execution.
4.  **The Black Box (`journal.py`):** Handles secure storage of all trading activity for future Machine Learning analysis.



---

## 🛠️ Installation & Setup

### 1. Prerequisites
* Python 3.10+
* Charles Schwab Developer Account (App Key & Secret)
* Google Cloud Console Project (for Journal sync)

### 2. Install Dependencies
```bash
pip install pandas yfinance streamlit plotly cryptography google-api-python-client schwab-py