# client/modules/project_management/__init__.py
"""
Project Management Module

This module provides comprehensive project management functionality for the GRL API Client.
It includes specialized managers for different aspects of project operations:

- ProjectManager: Main orchestrator for project operations
- ProjectConfigManager: Handles project configuration loading and validation
- EsdfManager: Manages ESDF (Electronic System Description File) operations
- TestCaseManager: Handles test case operations and processing
- CoilDataManager: Manages optimum coil data operations

All managers have been updated to use enum-based API calls for better type safety,
consistency, and maintainability.

Usage:
    from client.modules.project_management import ProjectManager

    project_manager = ProjectManager(config_manager, logger)
    project_manager.set_api_handler(api_handler)
    test_cases = project_manager.create_project("MyProject")
"""

from .project_manager import ProjectManager
from .project_config_manager import ProjectConfigManager
from .esdf_manager import EsdfManager
from .test_case_manager import TestCaseManager
from .coil_data_manager import CoilDataManager

__all__ = [
    'ProjectManager',
    'ProjectConfigManager',
    'EsdfManager',
    'TestCaseManager',
    'CoilDataManager'
]

# Version information
__version__ = "2.0.0"
__author__ = "GRL Development Team"
__description__ = "Project Management Module with Enum-based API Support"
