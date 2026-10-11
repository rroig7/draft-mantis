"""HTTPS with a certificate fallback.

Some Python installs cannot verify certificates out of the box (python.org builds on macOS before
"Install Certificates.command" is run, corporate proxies, old CA bundles) and every fetch fails with
CERTIFICATE_VERIFY_FAILED. On that error Mantis retries with other CA bundles (certifi, the system's),
and only if none of them work does it fall back to an unverified connection, with a warning.

  MANTIS_SSL=strict    never fall back: fail on a certificate error
  MANTIS_SSL=off       skip verification from the start
"""

from __future__ import annotations

import os
import ssl
import sys
import urllib.error
import urllib.request

SYSTEM_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")

_ctx: ssl.SSLContext | None = None   # the context that last worked; reused for every later request
_insecure = False


def _candidates():
    yield "default", ssl.create_default_context()
    try:
        import certifi
        yield "certifi", ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for path in SYSTEM_BUNDLES:
        if os.path.exists(path):
            try:
                yield path, ssl.create_default_context(cafile=path)
            except Exception:
                pass


def _unverified() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _cert_error(e: BaseException) -> bool:
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _warn():
    print("Draft Mantis: could not verify SSL certificates, continuing WITHOUT certificate checks.\n"
          "  Fix your Python certificates to turn checks back on (macOS python.org builds: run\n"
          "  'Install Certificates.command' in /Applications/Python 3.x, or `pip install certifi`).\n"
          "  Set MANTIS_SSL=strict to refuse unverified connections.", file=sys.stderr)


def urlopen(req: urllib.request.Request, timeout: float):
    """urllib.request.urlopen that survives a broken local certificate store (see module doc)."""
    global _ctx, _insecure
    mode = os.environ.get("MANTIS_SSL", "").lower()
    if mode == "off" and not _insecure:
        _ctx, _insecure = _unverified(), True
    if _ctx is not None:
        return urllib.request.urlopen(req, timeout=timeout, context=_ctx)
    last = None
    for _name, ctx in _candidates():
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        except (urllib.error.URLError, ssl.SSLError) as e:
            if not _cert_error(e):
                raise
            last = e
            continue
        _ctx = ctx
        return resp
    if mode == "strict":
        raise last
    _ctx, _insecure = _unverified(), True
    _warn()
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx)


def verified() -> bool:
    return not _insecure
