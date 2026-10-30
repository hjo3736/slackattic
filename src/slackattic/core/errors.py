from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    NOT_IMPLEMENTED = 2


class SlackatticError(Exception):
    """Expected failure shown to the user without a traceback."""

    exit_code: ExitCode = ExitCode.ERROR


class ConfigError(SlackatticError):
    pass


class StorageError(SlackatticError):
    pass
