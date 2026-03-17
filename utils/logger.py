from __future__ import annotations  # Lets Python postpone evaluation of type hints.

import logging  # Imports Python's standard logging module.
from pathlib import Path  # Imports Path for filesystem path handling.


def setup_logger(name: str = "algo_trader", log_file: str = "algo_trader.log") -> logging.Logger:  # Configures and returns the shared application logger.
    """Configure and return a logger with console and file handlers."""
    logger = logging.getLogger(name)  # Gets or creates a logger with the provided name.
    if logger.handlers:  # Checks whether handlers were already attached earlier.
        return logger  # Reuses the existing logger to avoid duplicate log messages.

    logger.setLevel(logging.INFO)  # Sets the minimum logging level to INFO.
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")  # Defines the common log message format.

    stream_handler = logging.StreamHandler()  # Creates a handler for console output.
    stream_handler.setFormatter(formatter)  # Applies the common formatter to console logs.
    logger.addHandler(stream_handler)  # Attaches the console handler to the logger.

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)  # Ensures the target log directory exists before creating the file handler.
    file_handler = logging.FileHandler(log_file)  # Creates a handler that writes logs to a file.
    file_handler.setFormatter(formatter)  # Applies the same formatter to file logs.
    logger.addHandler(file_handler)  # Attaches the file handler to the logger.

    return logger  # Returns the configured logger instance.
