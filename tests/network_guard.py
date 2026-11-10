"""What the tests' socket guard (``no_internet`` in ``conftest.py``) lets through."""

import ipaddress


def stays_local(address: object) -> bool:
    """Whether a socket address stays on this machine or a private network (Postgres, Docker), never the internet."""
    if not isinstance(address, tuple):
        return True  # a Unix socket
    host = str(address[0])
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    # Private (loopback included), not merely "not global": the carrier-grade NAT range 100.64.0.0/10 is neither
    # and is refused; link-local (cloud metadata) counts as private and is refused explicitly.
    return ip.is_private and not ip.is_link_local
