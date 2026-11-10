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
        return not ipaddress.ip_address(host.split("%")[0]).is_global
    except ValueError:
        return False
