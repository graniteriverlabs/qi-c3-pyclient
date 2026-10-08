# utils/log_manager.py
"""
Logging manager for GRL API.
Handles log file creation, configuration, and rotation.
"""

import os
import logging
import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Optional, Union, List, Dict, Any


class _FsyncOnEmit:
    """
    Mixin: after each record is written, flush AND os.fsync the file.

    logging's default flush() only pushes the bytes to the OS buffer; on Windows the file's
    change notification / mtime often isn't updated until the OS flushes metadata, so editors
    (e.g. VS Code) watching the open log don't auto-refresh until you close/reopen it. fsync
    forces the metadata update, so the file appears to grow in real time while a run is live.
    """

    def emit(self, record):
        super().emit(record)
        try:
            stream = getattr(self, "stream", None)
            if stream is not None:
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError):
            # stream mid-rotation or closed — safe to skip this fsync.
            pass


class FlushingRotatingFileHandler(_FsyncOnEmit, RotatingFileHandler):
    """Size-rotating file handler that fsyncs each record (live-tailable in editors)."""


class FlushingTimedRotatingFileHandler(_FsyncOnEmit, TimedRotatingFileHandler):
    """Time-rotating file handler that fsyncs each record (live-tailable in editors)."""


class LogManager:
    """
    Manager for logging functionality across the application.
    Handles log file creation, configuration, and customization.
    """

    # Default format strings for different scenarios
    DEFAULT_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] [%(threadName)s] - %(message)s'
    SIMPLE_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
    DETAILED_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] [%(threadName)s] [%(funcName)s] - %(message)s'

    def __init__(self,
                 log_filename: str = "grl_api_debug.log",
                 logger_name: str = "GRLApi",
                 log_level=logging.INFO,
                 log_to_console: bool = True,
                 max_log_size_mb: int = 20,
                 backup_count: int = 5,
                 log_mode: str = 'a',
                 rotation_type: str = 'size',
                 log_format: str = None):
        """
        Initialize the log manager with specified configuration.

        Args:
            log_filename (str): Name of the log file
            logger_name (str): Name of the logger
            log_level (int): Logging level (e.g., logging.DEBUG)
            log_to_console (bool): Whether to also log to console
            max_log_size_mb (int): Maximum log file size in MB before rotation
            backup_count (int): Number of backup log files to keep
            log_mode (str): 'a' for append, 'w' for overwrite
            rotation_type (str): 'size' for size-based rotation, 'time' for time-based rotation
            log_format (str): Optional custom log format string
        """
        self.log_filename = log_filename
        self.logger_name = logger_name
        # Accept an int (logging.INFO) or a name ("INFO"/"DEBUG"/…); default INFO on anything odd.
        if isinstance(log_level, str):
            self.log_level = logging.getLevelName(log_level.strip().upper())
            if not isinstance(self.log_level, int):
                self.log_level = logging.INFO
        else:
            self.log_level = log_level
        self.log_to_console = log_to_console
        self.max_log_size_bytes = max_log_size_mb * 1024 * 1024
        self.backup_count = backup_count
        self.log_mode = log_mode
        self.rotation_type = rotation_type

        # Use the specified format or the default
        self.format_string = log_format or self.DEFAULT_FORMAT

        # Create the logger
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        """Set up and configure the logger instance."""
        # Create logger
        logger = logging.getLogger(self.logger_name)
        logger.setLevel(self.log_level)
        logger.handlers = []  # Clear any existing handlers

        # Create formatter
        self.formatter = logging.Formatter(self.format_string)

        # Create directory for log file if it doesn't exist
        log_dir = os.path.dirname(self.log_filename)
        if log_dir:
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError as e:
                print(f"Error creating log directory: {e}")
                # Fall back to current directory
                self.log_filename = os.path.basename(self.log_filename)

        try:
            # Add appropriate handler based on rotation type
            if self.rotation_type.lower() == 'time':
                file_handler = FlushingTimedRotatingFileHandler(
                    self.log_filename,
                    when='midnight',
                    interval=1,
                    backupCount=self.backup_count,
                    encoding='utf-8'
                )
            else:  # Default to size-based rotation
                file_handler = FlushingRotatingFileHandler(
                    self.log_filename,
                    mode=self.log_mode,
                    maxBytes=self.max_log_size_bytes,
                    backupCount=self.backup_count,
                    encoding='utf-8'
                )

            file_handler.setFormatter(self.formatter)
            logger.addHandler(file_handler)
            self.file_handler = file_handler

            # Optionally add console handler
            if self.log_to_console:
                console_handler = logging.StreamHandler()
                console_handler.setFormatter(self.formatter)
                logger.addHandler(console_handler)
                self.console_handler = console_handler

        except Exception as e:
            # If file logging setup fails, fall back to console-only logging
            print(f"Error setting up file logging: {e}")
            logger.handlers = []  # Clear any partially setup handlers

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(self.formatter)
            logger.addHandler(console_handler)
            self.console_handler = console_handler

            # Log the error
            logger.error(f"Failed to set up file logging to {self.log_filename}: {e}")

        return logger

    def get_logger(self) -> logging.Logger:
        """
        Get the configured logger instance.

        Returns:
            logging.Logger: The configured logger
        """
        return self.logger

    def set_log_level(self, level: int) -> None:
        """
        Set the log level.

        Args:
            level (int): Logging level (e.g., logging.DEBUG)
        """
        self.log_level = level
        self.logger.setLevel(level)
        for handler in self.logger.handlers:
            handler.setLevel(level)

        self.logger.debug(f"Log level set to {logging.getLevelName(level)}")

    def log_run_start(self, include_system_info: bool = False) -> None:
        """
        Log a divider and a "new run started" message.
        Useful for clearly marking the start of a new session in logs.

        Args:
            include_system_info (bool): Whether to include system information
        """
        divider = "=" * 80
        self.logger.info(divider)
        self.logger.info(f"NEW RUN STARTED AT {datetime.datetime.now().isoformat()}")
        self.logger.info("-" * 80)
        self.logger.info(f"Logging to file: {os.path.abspath(self.log_filename)}")

        if include_system_info:
            try:
                import platform
                import sys

                self.logger.info(f"Python version: {sys.version}")
                self.logger.info(f"Platform: {platform.platform()}")
                self.logger.info(f"Machine: {platform.machine()}")
                self.logger.info(f"Processor: {platform.processor()}")
            except Exception as e:
                self.logger.warning(f"Could not gather system information: {e}")

    def set_log_formatter(self, format_string: str) -> None:
        """
        Set a custom log formatter.

        Args:
            format_string (str): Format string for the formatter
        """
        try:
            self.format_string = format_string
            new_formatter = logging.Formatter(format_string)

            for handler in self.logger.handlers:
                handler.setFormatter(new_formatter)

            self.formatter = new_formatter
            self.logger.debug("Log formatter updated")
        except Exception as e:
            self.logger.error(f"Failed to set log formatter: {e}")

    def use_predefined_format(self, format_type: str) -> None:
        """
        Use a predefined log format.

        Args:
            format_type (str): Type of format - 'default', 'simple', or 'detailed'
        """
        if format_type.lower() == 'simple':
            self.set_log_formatter(self.SIMPLE_FORMAT)
        elif format_type.lower() == 'detailed':
            self.set_log_formatter(self.DETAILED_FORMAT)
        else:
            self.set_log_formatter(self.DEFAULT_FORMAT)

    def add_handler(self, handler: logging.Handler) -> None:
        """
        Add a custom handler to the logger.

        Args:
            handler (logging.Handler): Handler to add
        """
        try:
            handler.setFormatter(self.formatter)
            self.logger.addHandler(handler)
            self.logger.debug(f"Added new handler: {type(handler).__name__}")
        except Exception as e:
            self.logger.error(f"Failed to add handler: {e}")

    def enable_console_logging(self, enable: bool = True) -> None:
        """
        Enable or disable console logging.

        Args:
            enable (bool): Whether to enable console logging
        """
        # Check if we already have a console handler
        has_console_handler = any(isinstance(h, logging.StreamHandler) and not
        isinstance(h, logging.FileHandler) for h in self.logger.handlers)

        if enable and not has_console_handler:
            # Add console handler
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(self.formatter)
            self.logger.addHandler(console_handler)
            self.console_handler = console_handler
            self.log_to_console = True
            self.logger.debug("Console logging enabled")
        elif not enable and has_console_handler:
            # Remove console handlers
            for handler in list(self.logger.handlers):
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                    self.logger.removeHandler(handler)

            self.log_to_console = False
            self.logger.debug("Console logging disabled")

    def rotate_log(self) -> None:
        """
        Force log rotation regardless of size.
        """
        for handler in self.logger.handlers:
            if isinstance(handler, RotatingFileHandler):
                self.logger.debug("Forcing log rotation")
                handler.doRollover()
                self.logger.info(f"Log rotated: {self.log_filename}")

    def close(self) -> None:
        """
        Close the logger and its handlers.
        """
        self.logger.info("Closing logger")
        for handler in self.logger.handlers:
            try:
                handler.close()
                self.logger.removeHandler(handler)
            except Exception as e:
                print(f"Error closing handler: {e}")