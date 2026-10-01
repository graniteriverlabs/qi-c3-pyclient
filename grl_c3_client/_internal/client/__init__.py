# client/__init__.py
"""
GRL Client Package
Contains the main GRL API client and all related modules.

This package provides a modular interface for interacting with GRL applications,
including application lifecycle management, test equipment connections, 
test execution, project configuration, and popup handling.

Typical usage:
    from client.grl_api_client import GRLApiClient
    
    client = GRLApiClient("grl_config.json")
    client.launch_app()
    client.connect("192.0.2.50")
    # ... etc
"""

# Import main components for easy access
from .grl_api_client import GRLApiClient
from .system_state import SystemState

# Import all module classes for internal access
from .modules import (
    AppManager,
    ConnectionManager,
    PopupManager,
    ProjectManager,
    TestManager
)

# Define what's available when using "from client import *"
__all__ = [
    'GRLApiClient',  # Main client class
    'SystemState',   # System state tracking
]

# Version information
__version__ = '1.0.0'
__author__ = 'GRL Team'
