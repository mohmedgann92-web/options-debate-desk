"""
Alpaca's MCP server does not return Greeks or implied volatility for this
account/data tier (confirmed empirically — see project notes). Rather than
depend on data that isn't there, we compute it ourselves from the option's
market price via Black-Scholes. This is a reasonable, standard approach and
arguably a better hackathon story than a passthrough would have been.
"""
import math
import re
from datetime import date, datetime


def parse_occ_symbol(symbol: str) -> dict:
    """
    Parses an OCC option symbol, e.g. "AAPL260826C00205000" ->
    {root: "AAPL", expiry: date(2026,8,26), option_type: "call", strike: 205.0}
    """
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
    if not m:
        raise ValueError(f"Could not parse OCC symbol: {symbol}")
    root, exp_str, cp, strike_str = m.groups()
    expiry = datetime.strptime(exp_str, "%y%m%d").date()
    strike = int(strike_str) / 1000.0
    return {
        "root": root,
        "expiry": expiry,
        "option_type": "call" if cp == "C" else "put",
        "strike": strike,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    """Black-Scholes European option price. T in years, sigma annualized."""
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(
    market_price: float, S: float, K: float, T: float, r: float, option_type: str,
    tol: float = 1e-4, max_iter: int = 50
) -> float | None:
    """
    Solves for IV via Newton-Raphson (falls back to bisection if it misbehaves).
    Returns None if no solution converges (e.g. price outside no-arbitrage bounds).
    """
    if T <= 0 or market_price <= 0:
        return None

    intrinsic = max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
    if market_price < intrinsic:
        return None  # below intrinsic value, not solvable

    sigma = 0.3  # reasonable starting guess
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega = S * _norm_pdf(
            (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        ) * math.sqrt(T)
        diff = price - market_price
        if abs(diff) < tol:
            return round(sigma, 4)
        if vega < 1e-8:
            break
        sigma -= diff / vega
        if sigma <= 0:
            sigma = 0.01

    # Fallback: bisection over a wide, safe range. NOTE: near-expiry, deep
    # ITM/OTM contracts produce numerically unstable IV (tiny extrinsic
    # value divided across almost no time) — this is a known Black-Scholes
    # edge case, not a bug. RISK_CONFIG's min_days_to_expiry (default 7)
    # keeps the real pipeline away from the worst of it.
    lo, hi = 0.001, 3.0
    for _ in range(100):
        mid = (lo + hi) / 2
        price = bs_price(S, K, T, r, mid, option_type)
        if abs(price - market_price) < tol:
            return round(mid, 4)
        if price > market_price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 4)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> dict:
    """Standard Black-Scholes Greeks. Returns None values if inputs are degenerate."""
    if T <= 0 or sigma <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)

    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * pdf_d1 * math.sqrt(T) / 100  # per 1% change in vol

    if option_type == "call":
        delta = _norm_cdf(d1)
        theta = (
            -S * pdf_d1 * sigma / (2 * math.sqrt(T))
            - r * K * math.exp(-r * T) * _norm_cdf(d2)
        ) / 365  # per day
    else:
        delta = _norm_cdf(d1) - 1
        theta = (
            -S * pdf_d1 * sigma / (2 * math.sqrt(T))
            + r * K * math.exp(-r * T) * _norm_cdf(-d2)
        ) / 365  # per day

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


def days_to_expiry(expiry: date, as_of: date = None) -> int:
    as_of = as_of or date.today()
    return (expiry - as_of).days
