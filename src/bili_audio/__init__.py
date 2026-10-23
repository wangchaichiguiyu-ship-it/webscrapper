"""BiliSonic — best-available Bilibili audio downloader.

The package is intentionally split into small layers (URL parsing, extraction,
download, conversion, queue, HTTP API) so that upstream changes can be fixed in
one place. See MAINTENANCE.md for the operating playbook.
"""

__version__ = "0.1.0"
APP_NAME = "BiliSonic"
