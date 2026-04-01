"""
MCP Server for the personal finance portfolio tracker.

Exposes portfolio data and operations to AI assistants (Claude Desktop / Claude Code
and remote agents) via the Model Context Protocol.

Transport modes
---------------
stdio (default) — local process, for Claude Code / Claude Desktop:
    python3 mcp_server.py

streamable-http — HTTP server, for remote agents:
    TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000 python3 mcp_server.py
    Endpoint: http://<host>:<port>/mcp

Environment variables
---------------------
DATABASE_URL   SQLAlchemy URL (default: sqlite:///portfolio.db)
TUSHARE_TOKEN  Tushare Pro API token for A-share price fetching
TRANSPORT      'stdio' (default) or 'streamable-http'
MCP_HOST       Bind host for HTTP transport (default: 0.0.0.0)
MCP_PORT       Bind port for HTTP transport (default: 8000)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Literal

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from models import init_db, get_session, Holding, PriceCache, ExchangeRate
from price_fetcher import (
    refresh_all_prices,
    set_manual_price,
    clear_manual_override,
    fetch_exchange_rates,
)

logging.basicConfig(level=logging.WARNING)

# Initialise DB tables (creates them if they don't exist, same DATABASE_URL as Flask)
init_db()

MARKETS = ["CN", "US", "JP", "CRYPTO", "OTHER"]
ASSET_TYPES = ["stock", "etf", "fund", "bond", "crypto", "other"]
CURRENCIES = ["CNY", "USD", "JPY", "HKD", "EUR", "GBP"]

mcp = FastMCP(
    name="portfolio-tracker",
    instructions=(
        "Personal investment portfolio tracker. Tracks holdings across A-shares (CN), "
        "US stocks, Japanese stocks (JP), and cryptocurrencies (CRYPTO). "
        "All monetary totals are in CNY (Chinese Yuan) unless a currency field says otherwise. "
        "Call search_holdings first to find holding IDs before calling update/delete tools."
    ),
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)


# ---------------------------------------------------------------------------
# Shared helpers (copied from app.py — no Flask dependency)
# ---------------------------------------------------------------------------

def _compute_portfolio(holdings, prices: dict, rates: dict) -> tuple[list[dict], float, float]:
    """Compute per-holding rows and totals. Returns (rows, total_value_cny, total_cost_cny)."""
    rows = []
    total_value = 0.0
    total_cost = 0.0

    for h in holdings:
        pc = prices.get(h.symbol)
        current_price = pc.price if pc else None
        fx = rates.get(h.currency, 1.0)

        if current_price is not None:
            market_value_cny = h.quantity * current_price * fx
        else:
            market_value_cny = h.quantity * h.cost_price * fx  # fallback to cost

        cost_cny = h.quantity * h.cost_price * fx
        pnl_cny = market_value_cny - cost_cny
        pnl_pct = (pnl_cny / cost_cny * 100) if cost_cny else 0.0

        raw_tags = h.tags or ""
        tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]

        rows.append({
            "id": h.id,
            "name": h.name,
            "symbol": h.symbol,
            "market": h.market,
            "asset_type": h.asset_type,
            "currency": h.currency,
            "quantity": h.quantity,
            "cost_price": h.cost_price,
            "current_price": current_price,
            "market_value_cny": round(market_value_cny, 2),
            "cost_cny": round(cost_cny, 2),
            "pnl_cny": round(pnl_cny, 2),
            "pnl_pct": round(pnl_pct, 2),
            "tags": tag_list,
            "is_manual": pc.is_manual if pc else False,
            "price_stale": (
                pc is not None and
                (datetime.now(timezone.utc) - pc.fetched_at.replace(tzinfo=timezone.utc))
                > timedelta(minutes=15)
            ) if pc else True,
        })

        total_value += market_value_cny
        total_cost += cost_cny

    return rows, total_value, total_cost


def _load_portfolio_data() -> tuple[list[dict], float, float]:
    session = get_session()
    try:
        holdings = session.execute(select(Holding)).scalars().all()
        price_map = {
            r.symbol: r
            for r in session.execute(select(PriceCache)).scalars().all()
        }
        rates = {
            r.from_currency: r.rate
            for r in session.execute(select(ExchangeRate)).scalars().all()
        }
        rates["CNY"] = 1.0
        return _compute_portfolio(holdings, price_map, rates)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Resources (read-only, proactively surfaced to Claude)
# ---------------------------------------------------------------------------

@mcp.resource("portfolio://summary")
def resource_portfolio_summary() -> str:
    """Current portfolio totals: total market value (CNY), cost, P&L, and per-holding breakdown."""
    rows, total_value, total_cost = _load_portfolio_data()
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    return json.dumps({
        "total_value_cny": round(total_value, 2),
        "total_cost_cny": round(total_cost, 2),
        "total_pnl_cny": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "holding_count": len(rows),
        "holdings": rows,
    }, ensure_ascii=False, indent=2)


@mcp.resource("portfolio://holdings")
def resource_holdings_list() -> str:
    """Complete list of all holdings with current prices, market values (CNY), P&L, and tags."""
    rows, _, _ = _load_portfolio_data()
    return json.dumps(rows, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tools — Read
# ---------------------------------------------------------------------------

@mcp.tool()
def get_portfolio_summary() -> str:
    """
    Get the full portfolio summary: total market value (CNY), total cost, overall P&L,
    and a complete per-holding breakdown sorted by market value descending.
    """
    try:
        rows, total_value, total_cost = _load_portfolio_data()
        rows_sorted = sorted(rows, key=lambda r: r["market_value_cny"], reverse=True)
        total_pnl = total_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
        return json.dumps({
            "total_value_cny": round(total_value, 2),
            "total_cost_cny": round(total_cost, 2),
            "total_pnl_cny": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "holding_count": len(rows_sorted),
            "holdings": rows_sorted,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def search_holdings(q: str = "") -> str:
    """
    Search holdings by name or symbol (case-insensitive substring match).
    Pass an empty string to list all holdings.
    Returns id, name, symbol, market, asset_type, currency, quantity, cost_price, tags.
    """
    try:
        session = get_session()
        try:
            holdings = session.execute(select(Holding)).scalars().all()
            q_lower = q.strip().lower()
            results = [
                {
                    "id": h.id,
                    "name": h.name,
                    "symbol": h.symbol,
                    "market": h.market,
                    "asset_type": h.asset_type,
                    "currency": h.currency,
                    "quantity": h.quantity,
                    "cost_price": h.cost_price,
                    "tags": [t.strip() for t in (h.tags or "").split(",") if t.strip()],
                    "notes": h.notes or "",
                }
                for h in holdings
                if not q_lower or q_lower in h.name.lower() or q_lower in h.symbol.lower()
            ]
            return json.dumps(results, ensure_ascii=False, indent=2)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def get_exchange_rates() -> str:
    """
    Get current CNY exchange rates for all tracked currencies (USD, JPY, HKD, EUR, GBP).
    Returns cached rates with their timestamps.
    """
    try:
        session = get_session()
        try:
            rate_rows = session.execute(select(ExchangeRate)).scalars().all()
            rates = {}
            timestamps = {}
            for r in rate_rows:
                rates[r.from_currency] = r.rate
                timestamps[r.from_currency] = r.fetched_at.isoformat()
            return json.dumps({
                "rates_to_cny": rates,
                "fetched_at": timestamps,
            }, ensure_ascii=False, indent=2)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tools — Write (holdings)
# ---------------------------------------------------------------------------

@mcp.tool()
def add_holding(
    name: str,
    symbol: str,
    market: Literal["CN", "US", "JP", "CRYPTO", "OTHER"],
    asset_type: Literal["stock", "etf", "fund", "bond", "crypto", "other"],
    currency: Literal["CNY", "USD", "JPY", "HKD", "EUR", "GBP"],
    quantity: float,
    cost_price: float,
    tags: str = "",
    notes: str = "",
) -> str:
    """
    Add a new investment holding to the portfolio.
    Symbol format: A-shares '600519.SH'/'000001.SZ', US stocks 'AAPL', JP stocks '7203.T', crypto 'BTC-USD'.
    tags: comma-separated string, e.g. '科技,长期持有'.
    """
    try:
        name = name.strip()
        symbol = symbol.strip().upper()
        if not name:
            return json.dumps({"ok": False, "error": "name is required"}, ensure_ascii=False)
        if not symbol:
            return json.dumps({"ok": False, "error": "symbol is required"}, ensure_ascii=False)
        if quantity <= 0:
            return json.dumps({"ok": False, "error": "quantity must be > 0"}, ensure_ascii=False)
        if cost_price <= 0:
            return json.dumps({"ok": False, "error": "cost_price must be > 0"}, ensure_ascii=False)

        session = get_session()
        try:
            h = Holding(
                name=name,
                symbol=symbol,
                market=market,
                asset_type=asset_type,
                currency=currency,
                quantity=quantity,
                cost_price=cost_price,
                tags=tags.strip(),
                notes=notes.strip(),
            )
            session.add(h)
            session.commit()
            session.refresh(h)
            return json.dumps({"ok": True, "id": h.id, "name": h.name, "symbol": h.symbol}, ensure_ascii=False)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def update_holding_quantity(
    holding_id: int,
    quantity: float | None = None,
    delta: float | None = None,
) -> str:
    """
    Update the quantity of an existing holding.
    Provide either 'quantity' (new absolute value) or 'delta' (amount to add/subtract, negative = sell).
    Use search_holdings first to find the holding ID.
    """
    try:
        if quantity is None and delta is None:
            return json.dumps({"ok": False, "error": "Provide either 'quantity' or 'delta'"}, ensure_ascii=False)
        if quantity is not None and delta is not None:
            return json.dumps({"ok": False, "error": "Provide only one of 'quantity' or 'delta', not both"}, ensure_ascii=False)

        session = get_session()
        try:
            h = session.get(Holding, holding_id)
            if not h:
                return json.dumps({"ok": False, "error": f"Holding {holding_id} not found"}, ensure_ascii=False)

            if quantity is not None:
                if quantity <= 0:
                    return json.dumps({"ok": False, "error": "quantity must be > 0"}, ensure_ascii=False)
                h.quantity = quantity
            else:
                new_qty = h.quantity + delta
                if new_qty <= 0:
                    return json.dumps({"ok": False, "error": f"Resulting quantity {new_qty} must be > 0"}, ensure_ascii=False)
                h.quantity = new_qty

            session.commit()
            return json.dumps({"ok": True, "id": h.id, "symbol": h.symbol, "quantity": h.quantity}, ensure_ascii=False)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def update_holding_tags(holding_id: int, tags: list[str]) -> str:
    """
    Replace the tags on a holding with a new list. Pass an empty list to clear all tags.
    Use search_holdings first to find the holding ID.
    """
    try:
        session = get_session()
        try:
            h = session.get(Holding, holding_id)
            if not h:
                return json.dumps({"ok": False, "error": f"Holding {holding_id} not found"}, ensure_ascii=False)

            cleaned = [t.strip() for t in tags if t.strip()]
            h.tags = ",".join(cleaned)
            session.commit()
            return json.dumps({"ok": True, "id": h.id, "symbol": h.symbol, "tags": cleaned}, ensure_ascii=False)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def delete_holding(holding_id: int, confirm: bool) -> str:
    """
    Permanently delete a holding from the portfolio. This cannot be undone.
    You MUST pass confirm=True to confirm deletion. Use search_holdings first to verify the correct ID.
    """
    try:
        if not confirm:
            return json.dumps({"ok": False, "error": "Pass confirm=True to confirm deletion"}, ensure_ascii=False)

        session = get_session()
        try:
            h = session.get(Holding, holding_id)
            if not h:
                return json.dumps({"ok": False, "error": f"Holding {holding_id} not found"}, ensure_ascii=False)

            deleted_info = {"id": h.id, "name": h.name, "symbol": h.symbol}
            session.delete(h)
            session.commit()
            return json.dumps({"ok": True, "deleted": deleted_info}, ensure_ascii=False)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tools — Price
# ---------------------------------------------------------------------------

@mcp.tool()
def refresh_prices() -> str:
    """
    Trigger a live price refresh for all holdings from market data sources
    (Tushare for A-shares, yfinance for US/JP/crypto). Also refreshes exchange rates.
    Holdings with manual price overrides are skipped. This may take 10-30 seconds.
    """
    try:
        fetch_exchange_rates()
        session = get_session()
        try:
            holdings = session.execute(select(Holding)).scalars().all()
            result = refresh_all_prices(holdings)
            return json.dumps(result, ensure_ascii=False, indent=2)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def set_price_override(
    symbol: str,
    price: float,
    currency: Literal["CNY", "USD", "JPY", "HKD", "EUR", "GBP"] = "CNY",
) -> str:
    """
    Manually set the price for a symbol, bypassing automatic fetching.
    Useful for illiquid assets or when the data source is unreliable.
    The override persists until cleared with clear_price_override.
    """
    try:
        symbol = symbol.strip().upper()
        if not symbol:
            return json.dumps({"ok": False, "error": "symbol is required"}, ensure_ascii=False)
        if price <= 0:
            return json.dumps({"ok": False, "error": "price must be > 0"}, ensure_ascii=False)

        set_manual_price(symbol, price, currency)
        return json.dumps({"ok": True, "symbol": symbol, "price": price, "currency": currency}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def clear_price_override(symbol: str) -> str:
    """
    Remove the manual price override for a symbol, allowing automatic price fetching to resume
    on the next refresh_prices call.
    """
    try:
        symbol = symbol.strip().upper()
        if not symbol:
            return json.dumps({"ok": False, "error": "symbol is required"}, ensure_ascii=False)

        clear_manual_override(symbol)
        return json.dumps({"ok": True, "symbol": symbol}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio")
    if transport == "streamable-http":
        logging.getLogger().setLevel(logging.INFO)
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
