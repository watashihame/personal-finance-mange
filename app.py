import logging
import os
from datetime import datetime, timezone, timedelta

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import select

from models import init_db, get_session, Holding, PriceCache, ExchangeRate
from price_fetcher import (
    refresh_all_prices,
    set_manual_price,
    clear_manual_override,
    fetch_exchange_rates,
    CHART_COLORS,
)

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")

init_db()

MARKETS = ["CN", "US", "JP", "CRYPTO", "OTHER"]
ASSET_TYPES = ["stock", "etf", "fund", "bond", "crypto", "other"]
CURRENCIES = ["CNY", "USD", "JPY", "HKD", "EUR", "GBP"]

MARKET_CURRENCY_DEFAULT = {
    "CN": "CNY",
    "US": "USD",
    "JP": "JPY",
    "CRYPTO": "USD",
    "OTHER": "CNY",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_portfolio(holdings, prices: dict, rates: dict) -> tuple[list[dict], float, float]:
    """
    Compute per-holding rows and totals.
    Returns (rows, total_value_cny, total_cost_cny).
    """
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
            "market_value_cny": market_value_cny,
            "cost_cny": cost_cny,
            "pnl_cny": pnl_cny,
            "pnl_pct": pnl_pct,
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


def _load_portfolio_data():
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

        rows, total_value, total_cost = _compute_portfolio(holdings, price_map, rates)
        return rows, total_value, total_cost
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    rows, total_value, total_cost = _load_portfolio_data()
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    # Show top 10 by value on dashboard
    top_rows = sorted(rows, key=lambda r: r["market_value_cny"], reverse=True)[:10]
    return render_template(
        "index.html",
        rows=top_rows,
        total_value=total_value,
        total_cost=total_cost,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        holding_count=len(rows),
    )


@app.route("/holdings")
def holdings_list():
    sort_by = request.args.get("sort", "market_value_cny")
    sort_dir = request.args.get("dir", "desc")
    tag_filter = request.args.get("tag", "").strip()

    rows, total_value, total_cost = _load_portfolio_data()

    # Collect all distinct tags across all holdings
    all_tags = sorted({tag for r in rows for tag in r["tags"]})

    # Apply tag filter
    if tag_filter:
        rows = [r for r in rows if tag_filter in r["tags"]]

    total_pnl = sum(r["pnl_cny"] for r in rows)
    filtered_value = sum(r["market_value_cny"] for r in rows)
    filtered_cost = sum(r["cost_cny"] for r in rows)

    reverse = sort_dir == "desc"
    valid_sorts = {"name", "symbol", "market", "asset_type", "quantity",
                   "cost_price", "current_price", "market_value_cny", "pnl_pct"}
    if sort_by not in valid_sorts:
        sort_by = "market_value_cny"

    rows = sorted(rows, key=lambda r: (r[sort_by] or 0), reverse=reverse)

    return render_template(
        "holdings.html",
        rows=rows,
        total_value=filtered_value,
        total_cost=filtered_cost,
        total_pnl=total_pnl,
        sort_by=sort_by,
        sort_dir=sort_dir,
        all_tags=all_tags,
        tag_filter=tag_filter,
    )


@app.route("/holdings/add", methods=["GET", "POST"])
def holding_add():
    if request.method == "POST":
        try:
            raw_tags = request.form.get("tags", "")
            tags = ",".join(t.strip() for t in raw_tags.split(",") if t.strip())
            h = Holding(
                name=request.form["name"].strip(),
                symbol=request.form["symbol"].strip().upper(),
                market=request.form["market"],
                asset_type=request.form["asset_type"],
                currency=request.form["currency"],
                quantity=float(request.form["quantity"]),
                cost_price=float(request.form["cost_price"]),
                tags=tags,
                notes=request.form.get("notes", "").strip(),
            )
            session = get_session()
            try:
                session.add(h)
                session.commit()
                flash(f"已添加持仓：{h.name} ({h.symbol})", "success")
            finally:
                session.close()
        except (ValueError, KeyError) as exc:
            flash(f"输入有误：{exc}", "danger")
        return redirect(url_for("holdings_list"))

    return render_template(
        "holding_form.html",
        action="add",
        holding=None,
        markets=MARKETS,
        asset_types=ASSET_TYPES,
        currencies=CURRENCIES,
        market_currency_default=MARKET_CURRENCY_DEFAULT,
    )


@app.route("/holdings/<int:holding_id>/edit", methods=["GET", "POST"])
def holding_edit(holding_id: int):
    session = get_session()
    try:
        h = session.get(Holding, holding_id)
        if h is None:
            flash("持仓不存在", "warning")
            return redirect(url_for("holdings_list"))

        if request.method == "POST":
            try:
                raw_tags = request.form.get("tags", "")
                h.name = request.form["name"].strip()
                h.symbol = request.form["symbol"].strip().upper()
                h.market = request.form["market"]
                h.asset_type = request.form["asset_type"]
                h.currency = request.form["currency"]
                h.quantity = float(request.form["quantity"])
                h.cost_price = float(request.form["cost_price"])
                h.tags = ",".join(t.strip() for t in raw_tags.split(",") if t.strip())
                h.notes = request.form.get("notes", "").strip()
                h.updated_at = datetime.now(timezone.utc)
                session.commit()
                flash(f"已更新：{h.name} ({h.symbol})", "success")
            except (ValueError, KeyError) as exc:
                flash(f"输入有误：{exc}", "danger")
            return redirect(url_for("holdings_list"))

        return render_template(
            "holding_form.html",
            action="edit",
            holding=h,
            markets=MARKETS,
            asset_types=ASSET_TYPES,
            currencies=CURRENCIES,
            market_currency_default=MARKET_CURRENCY_DEFAULT,
        )
    finally:
        session.close()


@app.route("/holdings/<int:holding_id>/delete", methods=["POST"])
def holding_delete(holding_id: int):
    session = get_session()
    try:
        h = session.get(Holding, holding_id)
        if h:
            name = f"{h.name} ({h.symbol})"
            session.delete(h)
            session.commit()
            flash(f"已删除：{name}", "info")
        else:
            flash("持仓不存在", "warning")
    finally:
        session.close()
    return redirect(url_for("holdings_list"))


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/refresh-prices", methods=["POST"])
def api_refresh_prices():
    session = get_session()
    try:
        holdings = session.execute(select(Holding)).scalars().all()
        if not holdings:
            return jsonify({"updated": 0, "failed": 0, "errors": [], "timestamp": ""})
        # Also refresh exchange rates
        fetch_exchange_rates()
        result = refresh_all_prices(holdings)
        return jsonify(result)
    finally:
        session.close()


@app.route("/api/override-price", methods=["POST"])
def api_override_price():
    data = request.get_json(force=True)
    symbol = str(data.get("symbol", "")).strip().upper()
    try:
        price = float(data["price"])
        currency = str(data.get("currency", "CNY")).upper()
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    if not symbol or price <= 0:
        return jsonify({"error": "无效的 symbol 或 price"}), 400

    set_manual_price(symbol, price, currency)
    return jsonify({"ok": True, "symbol": symbol, "price": price})


@app.route("/api/clear-override", methods=["POST"])
def api_clear_override():
    data = request.get_json(force=True)
    symbol = str(data.get("symbol", "")).strip().upper()
    if not symbol:
        return jsonify({"error": "缺少 symbol"}), 400
    clear_manual_override(symbol)
    return jsonify({"ok": True, "symbol": symbol})


@app.route("/api/portfolio-data")
def api_portfolio_data():
    rows, _, _ = _load_portfolio_data()
    rows_sorted = sorted(rows, key=lambda r: r["market_value_cny"], reverse=True)
    labels = [f"{r['name']} ({r['symbol']})" for r in rows_sorted]
    values = [round(r["market_value_cny"], 2) for r in rows_sorted]
    colors = [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(rows_sorted))]
    return jsonify({"labels": labels, "values": values, "colors": colors})


@app.route("/api/holdings/search")
def api_holdings_search():
    q = request.args.get("q", "").strip().lower()
    session = get_session()
    try:
        holdings = session.execute(select(Holding)).scalars().all()
        results = []
        for h in holdings:
            if not q or q in h.name.lower() or q in h.symbol.lower():
                tag_list = [t.strip() for t in (h.tags or "").split(",") if t.strip()]
                results.append({
                    "id": h.id,
                    "name": h.name,
                    "symbol": h.symbol,
                    "market": h.market,
                    "quantity": h.quantity,
                    "tags": tag_list,
                })
        return jsonify(results)
    finally:
        session.close()


@app.route("/api/holdings", methods=["POST"])
def api_holding_add():
    data = request.get_json(force=True)
    required = ["name", "symbol", "market", "asset_type", "currency", "quantity", "cost_price"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"缺少必填字段: {field}"}), 400
    try:
        quantity = float(data["quantity"])
        cost_price = float(data["cost_price"])
    except (TypeError, ValueError):
        return jsonify({"error": "quantity 和 cost_price 必须为数字"}), 400
    if quantity <= 0 or cost_price <= 0:
        return jsonify({"error": "quantity 和 cost_price 必须大于 0"}), 400

    market = str(data["market"]).upper()
    asset_type = str(data["asset_type"]).lower()
    currency = str(data["currency"]).upper()
    if market not in MARKETS:
        return jsonify({"error": f"market 无效，可选值: {MARKETS}"}), 400
    if asset_type not in ASSET_TYPES:
        return jsonify({"error": f"asset_type 无效，可选值: {ASSET_TYPES}"}), 400
    if currency not in CURRENCIES:
        return jsonify({"error": f"currency 无效，可选值: {CURRENCIES}"}), 400

    raw_tags = data.get("tags", "")
    if isinstance(raw_tags, list):
        raw_tags = ",".join(raw_tags)
    tags = ",".join(t.strip() for t in raw_tags.split(",") if t.strip())

    h = Holding(
        name=str(data["name"]).strip(),
        symbol=str(data["symbol"]).strip().upper(),
        market=market,
        asset_type=asset_type,
        currency=currency,
        quantity=quantity,
        cost_price=cost_price,
        tags=tags,
        notes=str(data.get("notes", "")).strip(),
    )
    session = get_session()
    try:
        session.add(h)
        session.commit()
        return jsonify({"ok": True, "id": h.id, "name": h.name, "symbol": h.symbol})
    finally:
        session.close()


@app.route("/api/holdings/<int:holding_id>/quantity", methods=["PATCH"])
def api_holding_quantity(holding_id: int):
    data = request.get_json(force=True)
    session = get_session()
    try:
        h = session.get(Holding, holding_id)
        if h is None:
            return jsonify({"error": "持仓不存在"}), 404

        if "quantity" in data:
            try:
                new_qty = float(data["quantity"])
            except (TypeError, ValueError):
                return jsonify({"error": "quantity 必须为数字"}), 400
        elif "delta" in data:
            try:
                new_qty = h.quantity + float(data["delta"])
            except (TypeError, ValueError):
                return jsonify({"error": "delta 必须为数字"}), 400
        else:
            return jsonify({"error": "请提供 quantity 或 delta"}), 400

        if new_qty <= 0:
            return jsonify({"error": "修改后的持有量必须大于 0"}), 400

        h.quantity = new_qty
        h.updated_at = datetime.now(timezone.utc)
        session.commit()
        return jsonify({"ok": True, "id": h.id, "symbol": h.symbol, "quantity": h.quantity})
    finally:
        session.close()


@app.route("/api/holdings/<int:holding_id>/tags", methods=["PATCH"])
def api_holding_tags(holding_id: int):
    data = request.get_json(force=True)
    if "tags" not in data:
        return jsonify({"error": "缺少 tags 字段"}), 400

    raw_tags = data["tags"]
    if isinstance(raw_tags, list):
        raw_tags = ",".join(str(t) for t in raw_tags)
    elif not isinstance(raw_tags, str):
        return jsonify({"error": "tags 必须为字符串或数组"}), 400

    tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]

    session = get_session()
    try:
        h = session.get(Holding, holding_id)
        if h is None:
            return jsonify({"error": "持仓不存在"}), 404
        h.tags = ",".join(tag_list)
        h.updated_at = datetime.now(timezone.utc)
        session.commit()
        return jsonify({"ok": True, "id": h.id, "symbol": h.symbol, "tags": tag_list})
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter("fmt_num")
def fmt_num(value, decimals=2):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


@app.template_filter("fmt_pct")
def fmt_pct(value):
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


@app.template_filter("pnl_class")
def pnl_class(value):
    if value is None:
        return ""
    return "pnl-positive" if value >= 0 else "pnl-negative"


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
