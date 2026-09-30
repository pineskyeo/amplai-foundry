"""Errors. The command line turns every ``StockroomError`` into exit code 1."""


class StockroomError(Exception):
    """Base class of the errors this package raises on bad input."""


class ParseError(StockroomError):
    """Text that is not a valid amount, date, record or file."""


class StockError(StockroomError):
    """A stock movement that the current levels do not allow."""


class ConfigError(StockroomError):
    """An unknown setting, a bad value or an unreadable settings file."""


class TransitionError(StockroomError):
    """An order status change that the order workflow does not allow."""
