# client/core/client_config.py
"""
Client configuration and initialization logic.
"""
import contextlib
import os
from datetime import datetime
from typing import Optional

from utils.log_manager import LogManager
from utils.config_manager import GRLConfigManager
from ..system_state import SystemState


def _per_run_log_filename(configured: Optional[str]) -> str:
    """Turn the configured log name into a per-run file so runs don't pile into one stale,
    misnamed file. `foo.log` -> `foo_<YYYYMMDD-HHMMSS>.log`."""
    base = configured or "grl_api_debug.log"
    root, ext = os.path.splitext(base)
    return f"{root}_{datetime.now().strftime('%Y%m%d-%H%M%S')}{ext or '.log'}"


class ClientConfig:
    """
    Handles client configuration and initialization.
    """

    def __init__(self, config_file_path: str = "grl_config.json"):
        """Initialize client configuration."""
        # A missing config file used to be tolerated: every setting stayed None and the first
        # symptom was an unrelated failure deep inside another manager, which tells a new user
        # nothing. It is also the mistake they are most likely to make, because the default path
        # is relative and resolves against whatever directory they happen to be in. Say so here,
        # naming the path we actually looked at and where we looked from.
        if not os.path.isfile(config_file_path):
            raise FileNotFoundError(
                "No GRL configuration file at:\n"
                "  {0}\n\n"
                "That path is relative to the current directory ({1}). Pass an absolute path, "
                "or run from the directory that holds your grl_config.json.".format(
                    os.path.abspath(config_file_path), os.getcwd()))

        # Step 1: Initialize configuration
        self.config_manager = GRLConfigManager(config_file_path)

        # Step 2: Initialize logging — per-run file, level from config (INFO by default).
        log_filename = _per_run_log_filename(self.config_manager.log_filename)
        self.log_manager = LogManager(
            log_filename=log_filename,
            logger_name="GRLApiClient",
            log_level=getattr(self.config_manager, "log_level", "INFO"),
        )
        self.logger = self.log_manager.get_logger()
        self.config_manager.set_logger(self.logger)
        self.log_manager.log_run_start()

        self.logger.info("Initializing GRL API Client with unified enum-based interface")
        self.logger.info(f"Log level: {self.config_manager.log_level} | log file: {log_filename}")

        # Step 3: Initialize system state
        self.system_state_data = SystemState(app_state='UNKNOWN', connection_state='UNKNOWN')

        self._log_configuration()

    def _log_configuration(self) -> None:
        """Log the current configuration settings."""
        self.logger.info("GRL API Client Configuration:")
        self.logger.info(f"  Application Name: {self.config_manager.app_name}")
        self.logger.info(f"  Application Path: {self.config_manager.app_path}")
        self.logger.info(f"  Known Port: {self.config_manager.known_port}")
        self.logger.info(f"  Initial Wait: {self.config_manager.initial_wait} seconds")
        self.logger.info(f"  Log Filename: {self.config_manager.log_filename}")

