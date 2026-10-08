# API/api_enum.py
"""
Enumeration of all available API endpoints.

This module defines the ApiName enum which contains all supported API operations.
Each enum value corresponds to a configuration entry in grl_api_config.json.
"""
from enum import Enum


class ApiName(Enum):
    """
    Enumeration of all available GRL API endpoints.

    Each enum value maps to a configuration in grl_api_config.json that defines
    the HTTP method, service, and endpoint for the API call.
    """
    # Connection and Setup APIs
    GET_LATEST_FIRMWARE_VERSION = "GetLatestFirmwareVersion"
    GET_LATEST_ELOAD_VERSION = "GetLatestEloadVersion"
    GET_LATEST_SHORT_FIXTURE_VERSION = "GetLatestShortFixtureVersion"
    CONNECT_TO_TEST_EQUIPMENT = "ConnectToTestEquipment"

    # Application APIs
    GET_SOFTWARE_VERSION = "GetSoftwareVersion"
    GET_MESSAGE_BOX = "GetMessageBox"
    GET_APP_STATE = "GetAppState"
    PUT_MESSAGE_BOX_RESPONSE = "PutMessageBoxResponse"
    GET_SELECTED_QI_SPEC_MODE_MPP = "GetSelectedQiSpecMode_MPP"
    PUT_SELECTED_QI_SPEC_MODE_MPP = "PutSelectedQiSpecMode_MPP"
    GET_WEBSOCKET_SUPPORT = "GetWebSocketSupport"

    # Test Configuration APIs
    GET_TEST_CASE_LIST = "GetTestCaseList"
    # Profile-scoped variant. C3-TPR (and the TPT BPP/EPP mode) return an EMPTY
    # list from the unparameterised route and only populate this one.
    GET_TEST_CASE_LIST_BY_PROFILE = "GetTestCaseListByProfile"
    PUT_PROJECT_FOLDER = "PutProjectFolder"
    PUT_VERIFY_ESDF_DATA = "PutVerifyEsdfData"
    # WPC-format (signed) ESDF: the app verifies the signature and flattens the
    # nested DutInfo tree into the flat field names the rest of the API uses.
    POST_VERIFY_ESDF_JSON_SIGN = "PostVerifyESDFJsonSign"
    GET_PROJECT_CONFIGURATION = "GetProjectConfiguration"
    PUT_OPTIMUM_COIL_VALUES = "PutOptimumCoilValues"
    GET_OPTIMUM_COIL_VALUES = "GetOptimumCoilValues"
    PUT_CERTIFICATION_FILTER_TOGGLE = "PutCertificationFilterToggle"

    # Test Execution APIs
    GET_TEST_STATUS = "GetTestStatus"
    POST_TEST_LIST_TO_EXECUTE = "PostTestListToExecute"
    POST_FORCE_STOP = "PostForceStop"

    # Plot and Capture APIs
    PUT_CLEAR_CAPTURE = "PutClearCapture"
