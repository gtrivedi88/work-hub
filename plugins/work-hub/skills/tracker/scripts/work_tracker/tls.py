"""TLS verification for Work Hub's HTTPS clients."""

import os
import ssl
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def https_context():
    context = ssl.create_default_context()
    # Some macOS Python installations have no bundled CA file. Use the
    # operating system's bundle when no explicit certificate override is set.
    bundle = Path("/etc/ssl/cert.pem")
    if (
        sys.platform == "darwin"
        and not (os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"))
        and context.cert_store_stats()["x509_ca"] == 0
        and bundle.is_file()
    ):
        context.load_verify_locations(cafile=str(bundle))
    return context


def certificate_failure(error):
    return isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError)
