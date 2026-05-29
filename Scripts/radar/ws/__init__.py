"""WebSocket lifecycle bindings — push callbacks + user-channel bridges.

Extracted from arb_server.py in audit-28b cont 12 (29.05.2026). Lives
in its own subpackage so the imports stay tidy — `radar/ws/callbacks.py`
holds the orderbook push callbacks; future modules can hold user-channel
fill bridges (Polymarket / Limitless / SX user WS).
"""
