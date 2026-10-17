"""Values shared by the server, the plugin entry point and the configurator.

Stdlib-only and free of ``qgis`` imports, like :mod:`wire`, :mod:`errors` and
:mod:`registry`. Keeping them here is what lets ``configurator`` read the
settings prefix without importing ``plugin``, which imports it back.
"""

import errno
import os

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

# The port spin box's range, and so the range QGIS_MCP_PORT may name.
MIN_PORT = 1024
MAX_PORT = 65535


def env_port():
    """The port QGIS_MCP_PORT asks for, None when unset.

    The saved port lives in QgsSettings, which is per profile, so two windows on
    one profile would both start on it. The environment is per launch, and the
    same variable the MCP server reads, so one value configures both halves.
    Raises ValueError for a value the spin box could not hold.
    """
    raw = os.environ.get("QGIS_MCP_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"QGIS_MCP_PORT={raw!r} is not a port between {MIN_PORT} and {MAX_PORT}")
    return port


# The plugin package directory - where metadata.txt and icons/ live. Anchored
# here rather than on each module's own ``__file__`` so a module inside a
# subpackage (``handlers/``) still resolves them correctly.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# QgsSettings key prefix for every setting the plugin persists.
SETTINGS_PREFIX = "qgis_mcp"

# Cap what is accepted as a peer's version string: it arrives over the socket
# and ends up in front of the user. Mirrors MAX_VERSION_LENGTH in
# src/qgis_mcp/protocol.py, which the client applies before sending.
MAX_VERSION_LENGTH = 32

# Same treatment for the checkout path a peer announces. Mirrors
# MAX_PATH_LENGTH in src/qgis_mcp/protocol.py.
MAX_PATH_LENGTH = 160

_plugin_version = None


def plugin_version():
    """This plugin's version, from metadata.txt - the file QGIS itself reads.

    Cached: both ``diagnose`` and the version-drift warning ask for it, and it
    cannot change without a plugin reload.
    """
    global _plugin_version
    if _plugin_version is None:
        import configparser

        config = configparser.ConfigParser()
        config.read(os.path.join(PLUGIN_DIR, "metadata.txt"))
        _plugin_version = config.get("general", "version", fallback="unknown")
    return _plugin_version


# "someone else already holds this port". EADDRINUSE is the usual answer, and is
# what a second QGIS window gets since both sides set SO_EXCLUSIVEADDRUSE. Windows
# answers WSAEACCES instead when the other holder used plain SO_REUSEADDR, which
# means the same thing here - the spin box floor is 1024, so EACCES cannot be the
# "privileged port" case.
ADDR_IN_USE = frozenset({errno.EADDRINUSE, errno.EACCES, 10048, 10013})
