"""solbot - An automated Pump.fun sniper/trader built on Solana.

The package is split into focused, self-contained modules:

- ``config``     : typed settings loaded from ``.env``
- ``data_stream``: PumpDev WebSocket feed for new-token launches
- ``dex_screener``: DexScreener REST client for pair liquidity + price data
- ``prices``      : Jupiter ``/price/v3`` fallback price oracle
- ``rugcheck``    : in-house rug-pull heuristic detector
- ``scoring``     : buy-signal scoring (0-100)
- ``scanner``     : candidate selection pipeline (filter + qualify)
- ``jupiter``     : Jupiter Swap API V2 (``/order`` + ``/execute``)
- ``trader``      : position lifecycle, risk, exit & compounding
- ``monitoring``  : logging, crash resistance & trade persistence
- ``reporter``    : Telegram alerting with elegant markdown cards
"""

__version__ = "0.1.0"