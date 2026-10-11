"""Certificate fallback in mantis.net (no network).

Run: python3 -m unittest discover tests
"""

import io
import os
import ssl
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock

from mantis import net


def fake_urlopen(trusted):
    """Stands in for urllib's urlopen: only contexts named in `trusted` (or unverified ones) pass the handshake."""
    def opener(req, timeout, context):
        if context.verify_mode == ssl.CERT_NONE or id(context) in trusted:
            return io.BytesIO(b"ok")
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    return opener


class NetTests(unittest.TestCase):
    def setUp(self):
        net._ctx, net._insecure = None, False
        self.req = urllib.request.Request("https://example.invalid/")

    def tearDown(self):
        net._ctx, net._insecure = None, False

    def run_with(self, ctxs, trusted, env=""):
        with mock.patch.object(net, "_candidates", return_value=iter(ctxs)), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen(trusted)), \
             mock.patch.dict(os.environ, {"MANTIS_SSL": env}), redirect_stderr(io.StringIO()) as err:
            body = net.urlopen(self.req, timeout=5).read()
        return body, err.getvalue()

    def test_second_bundle_used_when_default_fails(self):
        a, b = ssl.create_default_context(), ssl.create_default_context()
        body, err = self.run_with([("default", a), ("certifi", b)], {id(b)})
        self.assertEqual(body, b"ok")
        self.assertIs(net._ctx, b)
        self.assertTrue(net.verified())
        self.assertEqual(err, "")

    def test_falls_back_to_unverified_with_warning(self):
        body, err = self.run_with([("default", ssl.create_default_context())], set())
        self.assertEqual(body, b"ok")
        self.assertFalse(net.verified())
        self.assertIn("WITHOUT certificate checks", err)

    def test_strict_mode_refuses(self):
        with self.assertRaises(urllib.error.URLError):
            self.run_with([("default", ssl.create_default_context())], set(), env="strict")
        self.assertTrue(net.verified())

    def test_other_errors_are_not_swallowed(self):
        def boom(req, timeout, context):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        with mock.patch.object(urllib.request, "urlopen", side_effect=boom):
            with self.assertRaises(urllib.error.URLError):
                net.urlopen(self.req, timeout=5)
        self.assertTrue(net.verified())


if __name__ == "__main__":
    unittest.main()
