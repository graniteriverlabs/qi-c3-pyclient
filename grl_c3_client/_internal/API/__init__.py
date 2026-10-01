# API/__init__.py
"""
API handler module for GRL application APIs.

This module provides the main entry point for interacting with GRL application
APIs. It exports the GRLApiHandler class as the primary interface for making
API calls to the GRL application using enum-based configuration.

Usage:
    from API import GRLApiHandler, ApiName

    api_handler = GRLApiHandler(base_url)
    response = api_handler.call_api(ApiName.GET_SOFTWARE_VERSION)
"""
from .grl_api_handler import GRLApiHandler
from .api_enum import ApiName

# Export primary interfaces
__all__ = ['GRLApiHandler', 'ApiName']