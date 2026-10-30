from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("slackattic")
except PackageNotFoundError:
    # Running from a source tree that was never installed
    __version__ = "0.0.0"
