"""A single shared SSL context, reused by every httpx.AsyncClient this
backend creates.

Root cause of the full-server freezes reported repeatedly ("multiple
sections not loading at all"): every price/data-fetch helper built a brand
new httpx.AsyncClient() per call (often several per stock, times dozens of
stocks per basket-profile load), and httpx builds a fresh ssl.SSLContext()
for each one unless given an existing one via `verify=`. Confirmed live via
py-spy thread dump on the hung process -- the single asyncio event-loop
thread was stuck synchronously inside ssl.create_default_context(), which
blocks the entire server (not just the one request) since nothing else can
run on that thread until it returns. After enough repeated SSLContext
construction over a long-running process, one of these calls stalls
indefinitely on Windows.

Fix: build the SSL context ONCE at import time and pass it as `verify=` to
every httpx.AsyncClient(...) call across the backend, so no code path ever
calls ssl.create_default_context() again after startup.
"""
import ssl

SHARED_SSL_CONTEXT = ssl.create_default_context()
