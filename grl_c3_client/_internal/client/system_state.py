# client/system_state.py
"""
System state tracking module for GRL API Client.

This module defines the SystemState dataclass which is used to track and maintain
the current state of the GRL application and test execution. It provides a structured
way to access information about the application state, connection state, and current test.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SystemState:
    """
    System state information including application state and test case details.

    This class maintains information about the application state, connection status,
    and the current test case being executed. It is updated continuously during
    test execution to reflect the real-time state of the system.

    Attributes:
        app_state: Current state of the application ('BUSY', 'READY', 'IDLE', 'ERROR', etc.)
        connection_state: Current connection status with the test equipment
                         ('CONNECTED', 'DISCONNECTED', 'ERROR', etc.)
        test_case_name: Name of the currently executing test case (None when no test is running)
        test_status: Status of the current test case ('Started', 'Completed', 'Failed', etc.)
    """
    app_state: str  # 'BUSY', 'IDLE', etc.
    connection_state: str  # 'CONNECTED', 'DISCONNECTED', etc.
    test_case_name: Optional[str] = None  # Optional as there might not always be a test running
    test_status: Optional[str] = None  # 'Started', 'Completed', 'Failed', etc.