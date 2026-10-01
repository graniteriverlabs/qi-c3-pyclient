# client/modules/__init__.py
"""
Client Manager Modules Package
Contains all manager modules for the GRL API Client.

These modules provide specific functionality components:
- AppManager: Application lifecycle management
- ConnectionManager: Test equipment connection handling
- PopupManager: Dialog and popup management
- TestManager: Test execution and monitoring
- ProjectManager: Project configuration and setup coordination (from project_management package)

The project management functionality has been modularized into a separate package:
- project_management/: Contains all project-related managers

These modules are generally not used directly but through the GRLApiClient.
"""

# Import standalone managers
from .app_manager import AppManager
from .connection_manager import ConnectionManager
from .popup_manager import PopupManager
from .test_manager import TestManager

# Import project management package - only expose ProjectManager
from .project_management import ProjectManager

# Define what's available when using "from client.modules import *"
__all__ = [
    'AppManager',
    'ConnectionManager',
    'PopupManager',
    'TestManager',
    'ProjectManager'
]