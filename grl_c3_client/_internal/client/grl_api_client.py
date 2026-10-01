# client/grl_api_client.py
"""
GRL API Client with dynamic method access - no wrapper functions needed.
Uses __getattr__ magic method to automatically delegate legacy method calls.
"""
import contextlib
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

from API import ApiName
from .system_state import SystemState
from .core.client_config import ClientConfig
from .core.api_interface import ApiInterface
from .core.legacy_managers import LegacyManagers
from .modules.app_manager import AppManager
from .transport import LiveTransport, create_live_transport


class GRLApiClient:
    """
    GRL API Client with dynamic method delegation.

    This client provides two interfaces:
    1. Primary: Enum-based API calls via call_api()
    2. Legacy: Automatic delegation to legacy managers (no wrappers needed)

    Usage Examples:
        # Primary: Enum-based API calls (RECOMMENDED)
        response = client.call_api(ApiName.GET_SOFTWARE_VERSION)
        response = client.call_api(ApiName.PUT_OPTIMUM_COIL_VALUES, data=coil_data)

        # Legacy: Direct method access (auto-delegates to legacy managers)
        status = client.set_project("MyProject")          # Auto-delegates to legacy
        result = client.submit_test_list(test_list)       # Auto-delegates to legacy
        connection = client.connect(ip_address)           # Auto-delegates to legacy

        # Any new legacy method automatically works - NO WRAPPER FUNCTIONS NEEDED!
        client.any_new_legacy_method()                    # Auto-delegates automatically
    """

    def __init__(self, config_file_path: str = "grl_config.json",
                 app: Optional[str] = None,
                 ip_address: Optional[str] = None) -> None:
        """
        Initialize the GRL API client with configuration.

        Args:
            config_file_path: Path to the configuration JSON file
            app: Which application under "applications" to drive, overriding ``Selected_app``
                for this client only. One configuration file describes all of them, so a script
                or a command line can pick one per run without editing the file. The choice is
                applied before the managers are built, because they read the application's name
                and paths as they are created.
            ip_address: The tester to talk to, overriding this application's ``ip_address``.
                Useful when one script drives several benches.
        """
        # Initialize configuration
        self.config = ClientConfig(config_file_path)

        # Initialize logger following the consistent pattern
        self.logger = self.config.logger

        if app:
            if not self.config.config_manager.select_app(app):
                available = list((self.config.config_manager.config.get("applications") or {}))
                raise ValueError(
                    "Unknown application {0!r}. Configured applications: {1}".format(
                        app, ", ".join(available) or "(none)"))
            self.logger.info(f"Application selected for this run: {app}")

        if ip_address:
            # Set on the same field the configuration fills, so every later step - connect,
            # diagnostics, logging - sees one address and there is no second source of truth.
            self.config.config_manager.ip_address = ip_address
            self.logger.info(f"Tester address for this run: {ip_address}")

        # Initialize direct managers
        self.app_manager = AppManager(self.config.config_manager, self.logger)
        self.api_interface = ApiInterface(self.logger)

        # Initialize legacy managers for backward compatibility
        self.legacy = LegacyManagers(self.config.config_manager, self.logger, self)

        # Live-data transport (REST polling or WebSocket). Chosen at launch, once the
        # app is up and can tell us which build it is.
        self.live_transport: Optional[LiveTransport] = None

        # disconnect() is called both explicitly (sample_run.py) and again from __del__ during
        # garbage collection; this guard makes teardown run exactly once.
        self._disconnected = False

        # Update managers if logger changed
        if hasattr(self.config, 'logger'):
            self.legacy.update_all_managers_logger(self.logger)

    def __del__(self) -> None:
        """Destructor to ensure resources are cleaned up safely."""
        with contextlib.suppress(Exception):
            self.disconnect()

    def __getattr__(self, name):
        """
        Dynamic method delegation - automatically forwards unknown methods to legacy managers.

        This magic method is called when an attribute/method is not found on the client.
        It automatically looks for the method in legacy managers and delegates the call.

        This means:
        - Your existing code like client.set_project() continues to work
        - Any new methods added to legacy managers automatically work on client
        - NO WRAPPER FUNCTIONS EVER NEEDED!

        Example:
            client.set_project()        -> auto-delegates to legacy.set_project()
            client.submit_test_list()   -> auto-delegates to legacy.submit_test_list()
            client.connect()            -> auto-delegates to legacy.connect()
            client.any_new_method()     -> auto-delegates to legacy.any_new_method()

        Args:
            name: The name of the method/attribute being accessed

        Returns:
            The method from the legacy manager

        Raises:
            AttributeError: If the method is not found in any manager
        """
        # List of managers to check for methods (in priority order)
        managers_to_check = [
            ('legacy', self.legacy),
            ('project_manager', getattr(self.legacy, 'project_manager', None)),
            ('test_manager', getattr(self.legacy, 'test_manager', None)),
            ('connection_manager', getattr(self.legacy, 'connection_manager', None)),
            ('popup_manager', getattr(self.legacy, 'popup_manager', None)),
            ('app_manager', getattr(self.legacy, 'app_manager', None)),
        ]

        # Look for the method in managers
        for manager_name, manager in managers_to_check:
            if manager and hasattr(manager, name):
                method = getattr(manager, name)
                if callable(method):
                    self.logger.debug(f"Auto-delegating '{name}' to {manager_name}")
                    return method

        # If method not found anywhere, raise AttributeError
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # ---------------------------------------------------------------------------
    # SECTION: Application Lifecycle Management
    # ---------------------------------------------------------------------------

    def launch_app(self) -> bool:
        """
        Launch the GRL application using the single handler approach.
        """
        self.logger.info("Launching GRL application...")

        # Step 1: Launch using app_manager (creates the ONLY handler)
        if self.app_manager.launch_app():
            self.logger.info(f"Application launched successfully. Base URL: {self.app_manager.base_url}")

            # Step 2: Get the existing handler (no duplication)
            api_handler = self.app_manager.get_api_handler()

            if api_handler:
                # Optional raw HTTP trace (off by default). Hook the shared session so every
                # call — including the capture manager's GetCCLinePackets/PostCurrentTestReport,
                # which reuse this session — is timestamped for debugging/RCA.
                if getattr(self.config_manager, "trace_http", False):
                    try:
                        from .core.http_trace import install_http_trace
                        trace_path = os.path.join(
                            "logs", f"grl_api_trace_{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
                        )
                        install_http_trace(getattr(api_handler, "session", None), trace_path, self.logger)
                    except Exception as e:
                        self.logger.warning(f"Could not enable HTTP trace (continuing): {e}")

                # Step 3: Initialize API interface with existing handler
                if self.api_interface.initialize_api_handler(api_handler):
                    self.logger.info("✅ GRL API Client launched successfully")

                    # Step 4: Set API handler for ALL legacy managers manually
                    if hasattr(self.legacy, 'connection_manager') and self.legacy.connection_manager:
                        self.legacy.connection_manager.set_api_handler(api_handler)
                        self.logger.debug("✅ Set API handler for legacy connection_manager")

                    if hasattr(self.legacy, 'test_manager') and self.legacy.test_manager:
                        self.legacy.test_manager.set_api_handler(api_handler)
                        self.logger.debug("✅ Set API handler for legacy test_manager")

                    if hasattr(self.legacy, 'popup_manager') and self.legacy.popup_manager:
                        self.legacy.popup_manager.set_api_handler(api_handler)
                        self.logger.debug("✅ Set API handler for legacy popup_manager")

                    # Step 5: Set API handler for project managers
                    if hasattr(self.legacy, 'project_manager') and self.legacy.project_manager:
                        self.legacy.project_manager.set_api_handler(api_handler)
                        self.logger.debug("✅ Set API handler for legacy project_manager")

                    # Data-capture manager (per-case measurement capture) needs the handler too.
                    if hasattr(self.legacy, 'capture_manager') and self.legacy.capture_manager:
                        self.legacy.capture_manager.set_api_handler(api_handler)
                        self.logger.debug("✅ Set API handler for capture_manager")

                    # Exerciser manager (license-gated manual mode) — handler only; it is inert
                    # unless run_mode == "exerciser".
                    if hasattr(self.legacy, 'exerciser_manager') and self.legacy.exerciser_manager:
                        self.legacy.exerciser_manager.set_api_handler(api_handler)
                        self.logger.debug("✅ Set API handler for exerciser_manager")

                    # Call the setup method if it exists
                    if hasattr(self.legacy, 'setup_legacy_api_handlers'):
                        self.legacy.setup_legacy_api_handlers()

                    self.logger.info("✅ All legacy managers configured with API handler")

                    # Step 6: Choose how we receive live data from THIS app build.
                    # Asks the app whether it serves a WebSocket; falls back to REST polling.
                    self._setup_live_transport(api_handler)
                    return True
                else:
                    self.logger.error("❌ Failed to initialize API interface")
                    return False
            else:
                self.logger.error("❌ No API handler available from app_manager")
                return False
        else:
            self.logger.error("❌ Failed to launch application")
            return False

    def _setup_live_transport(self, api_handler) -> None:
        """
        Decide how live data (pop-ups, test progress, measurements) reaches us.

        The C3 app ships either as a WebSocket build or a REST-polling build, never both.
        We ask the app which it is and wire the matching transport into the managers, so
        the rest of the client is identical either way.
        """
        self.live_transport = create_live_transport(
            api_handler=api_handler,
            logger=self.logger,
            system_state=self.system_state,
            popup_manager=getattr(self.legacy, 'popup_manager', None),
            base_url=self.app_manager.base_url,
            websocket_config=getattr(self.config_manager, 'websocket_config', {}),
        )

        for manager_name in ('connection_manager', 'test_manager'):
            manager = getattr(self.legacy, manager_name, None)
            if manager and hasattr(manager, 'set_live_transport'):
                manager.set_live_transport(self.live_transport)

        self.logger.info(f"✅ Live data transport: {self.live_transport.name}")

    def run_exerciser(self, sequence_file: Optional[str] = None,
                      dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """
        Run a license-gated exerciser session (the `run_mode == "exerciser"` path).

        Args:
            sequence_file: the exported sequence to use, relative to this application's input
                folder. None -> `files.ExerciserSequenceModel` from the configuration.
            dry_run: compose every request and send none. None -> `common.exerciser.dry_run`.


        One input file, mirroring how a compliance run takes an ESDF: the app's **exported
        sequence file** (`ExerciserSequenceModel`) supplies the packet sequence, the controller
        settings and the phase timings — nothing is hand-typed. How the session runs (steps,
        measurement channels, `dry_run`, verify policy) comes from `common.exerciser` in
        `grl_config.json`, next to `run_mode`.

        Returns the session result, or a clear blocked result if the controller is unlicensed,
        the app has no exerciser support, or the sequence file is unusable.
        """
        ex = self.exerciser
        if ex is None:
            self.logger.error("Exerciser manager not available")
            return {"success": False, "error": "no exerciser manager"}

        # License from the connect the client already made (no redundant connect).
        conn_mgr = getattr(self.legacy, "connection_manager", None)
        ex.apply_license(getattr(conn_mgr, "last_connection_info", None))
        if not ex.licensed:
            self.logger.error(f"Exerciser mode blocked: {ex.license_reason}")
            return {"success": False, "error": ex.license_reason}

        from client.modules import exerciser_sequence as seq
        from utils.project_root import project_root
        root = project_root()
        app = self.config_manager.app_name
        file_map = getattr(self.config_manager, "file_map", {}) or {}
        user_dir = os.path.join(root, "JSON_User_input", app)

        # Run controls live in grl_config.json (common.exerciser), beside run_mode.
        session = dict(getattr(self.config_manager, "exerciser", {}) or {})

        if dry_run is not None:
            session["dry_run"] = bool(dry_run)

        # A caller-supplied sequence wins over the configured one, for this run only.
        seq_name = sequence_file or file_map.get("ExerciserSequenceModel")
        if not seq_name:
            self.logger.error(f"No 'ExerciserSequenceModel' configured for {app} — the exported "
                              f"sequence file is what supplies the packets and settings")
            return {"success": False, "error": "no exerciser sequence file configured"}
        seq_path = os.path.join(user_dir, seq_name)
        try:
            doc = seq.load_sequence_file(seq_path)
            steps, problems = seq.build_steps(doc, ex.variant, session.get("steps"))
        except seq.SequenceError as e:
            self.logger.error(str(e))
            return {"success": False, "error": str(e)}

        info = seq.describe(doc)
        self.logger.info(f"Exerciser sequence {os.path.basename(seq_path)}: spec={info['qi_spec']} "
                         f"profile={info['power_profile']} packets={info['packets']}")
        for problem in problems:
            self.logger.warning(f"[exerciser] {problem}")

        # NOTE: `spec_mode` is NOT the power profile. `PutSelectedQiSpecMode` selects a legacy
        # spec/technology ("MPP"/"BPP"/"EPP" on TPT, "1.3"/"2.1"/... on MPP-TPR) and its C# switch
        # has no default, so a power profile like "MPP25" falls through and does nothing behind an
        # HTTP 200. Defaulting it from the file's PowerProfile was a category error that broke the
        # 2026-08-17 bench run. Exerciser mode is entered by `start` itself, so leaving spec_mode
        # unset is correct; the user may still set it explicitly in common.exerciser.
        if session.get("spec_mode"):
            self.logger.info(f"[exerciser] spec_mode {session['spec_mode']!r} set explicitly in "
                             f"config (this selects a legacy spec/technology, not a power profile)")

        ex.capture_dir = self._exerciser_capture_dir(root, app)
        self.logger.info(f"Running exerciser session ({len(steps)} steps, "
                         f"verify={session.get('verify', 'strict')}, "
                         f"dry_run={bool(session.get('dry_run'))})")
        return ex.run_session(session, steps)

    def _exerciser_capture_dir(self, root: str, app: str) -> str:
        """Per-session evidence folder, alongside the compliance run captures."""
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return os.path.join(root, "Runtime_Capture", app, f"exerciser_{stamp}")

    def get_telemetry(self) -> Dict[str, Any]:
        """
        Live measurement data captured during the run (power chart, signals, packets).

        Only available on WebSocket builds; returns an empty dict on REST builds, which
        do not expose live measurements.
        """
        if not self.live_transport:
            return {}
        return self.live_transport.get_telemetry()

    def disconnect(self) -> None:
        """
        Safely disconnect from the GRL application and clean up resources.

        Idempotent: safe to call more than once (explicitly and again from __del__).
        """
        if getattr(self, "_disconnected", False):
            return
        self._disconnected = True
        self.logger.info("Disconnecting from GRL application...")

        # Stop receiving live data first, so no thread outlives the session.
        if self.live_transport:
            try:
                self.live_transport.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping live transport: {e}")
            self.live_transport = None

        # Close enum-based API handler
        self.api_interface.disconnect()

        # Disconnect using legacy managers
        self.legacy.disconnect_app()

        self.logger.info("Disconnected successfully")

    # ---------------------------------------------------------------------------
    # SECTION: MAIN API INTERFACE - Single Method for All Enum APIs
    # ---------------------------------------------------------------------------

    def call_api(self, api_name: ApiName, data: Optional[Dict] = None,
                 params: Optional[Dict] = None, endpoint_params: Optional[Dict] = None,
                 endpoint_override: Optional[str] = None) -> Dict[str, Any]:
        """
        Universal method to call any API using enum values.
        This is the PRIMARY method for making API calls with the modern interface.

        Args:
            api_name: ApiName enum value (e.g., ApiName.GET_SOFTWARE_VERSION)
            data: Optional request payload for POST/PUT requests
            params: Optional query parameters
            endpoint_params: Optional parameters for URL template substitution
            endpoint_override: Optional override for the entire endpoint path

        Returns:
            Dictionary with consistent response format:
            {
                "request": {
                    "method": "GET/POST/PUT/DELETE",
                    "url": "full_url",
                    "data": {...},
                    "params": {...}
                },
                "response": {
                    "success": True/False,
                    "status_code": 200,
                    "data": {...},
                    "error": "error_message" (if applicable)
                }
            }

        Examples:
            # Simple GET request
            response = client.call_api(ApiName.GET_SOFTWARE_VERSION)
            if response["response"]["success"]:
                version = response["response"]["data"]

            # PUT with data
            coil_data = {"coilValue": [...], "sSCheck": True}
            response = client.call_api(ApiName.PUT_OPTIMUM_COIL_VALUES, data=coil_data)

            # GET with endpoint parameters
            response = client.call_api(
                ApiName.CONNECT_TO_TEST_EQUIPMENT,
                endpoint_params={"ip_address": "192.0.2.50"}
            )

            # PUT with endpoint override (dynamic endpoint)
            mode = True  # BPP/EPP mode
            endpoint = f"PutCertificationFilterToggle/{str(mode).lower()}"
            response = client.call_api(
                ApiName.PUT_CERTIFICATION_FILTER_TOGGLE,
                endpoint_override=endpoint
            )

            # POST with test list
            test_list = ["MP_TST_001", "MP_TST_002"]
            response = client.call_api(ApiName.POST_TEST_LIST_TO_EXECUTE, data=test_list)
        """
        return self.api_interface.call_api(api_name, data, params, endpoint_params, endpoint_override)

    # ---------------------------------------------------------------------------
    # SECTION: Utility Properties and Methods
    # ---------------------------------------------------------------------------

    @property
    def system_state(self) -> SystemState:
        """
        Access to system state data.

        Returns:
            SystemState: Current system state tracking object
        """
        return self.config.system_state_data

    @property
    def logs(self):
        """
        Access to logging system for compatibility with legacy code.

        Returns:
            LogManager: The log manager instance
        """
        return self.config.log_manager

    @property
    def config_manager(self):
        """
        Access to configuration manager.

        Returns:
            GRLConfigManager: The configuration manager instance
        """
        return self.config.config_manager

    @property
    def exerciser(self):
        """
        The license-gated exerciser manager (manual controller mode).

        Exposed as a property because the manager object is not callable, so the client's
        __getattr__ delegation (which only forwards callables) would not surface it. Use in an
        exerciser run: `client.exerciser.enter(spec)`, `client.exerciser.start()`, etc.
        """
        return getattr(self.legacy, "exerciser_manager", None)

    @property
    def base_url(self) -> Optional[str]:
        """
        Access to the base URL of the GRL application.

        Returns:
            str or None: Base URL if application is launched, None otherwise
        """
        return getattr(self.api_interface, 'base_url', None)

    def is_api_ready(self) -> bool:
        """
        Check if the API handler is ready for use.

        Returns:
            bool: True if API handler is initialized and ready, False otherwise
        """
        return self.api_interface.is_api_ready()

    def get_available_apis(self) -> List[str]:
        """
        Get list of all available API endpoints.

        Returns:
            List[str]: List of API endpoint names from the enum
        """
        return self.api_interface.get_available_apis()

    def check_api_health(self) -> Dict[str, Any]:
        """
        Quick health check of API endpoints.

        Returns:
            Dict[str, Any]: Dictionary with health status of various endpoints
        """
        return self.api_interface.check_api_health()

    def get_api_info(self) -> Dict[str, Any]:
        """
        Get comprehensive information about the API client state.

        Returns:
            Dict containing client state information
        """
        return {
            "api_ready": self.is_api_ready(),
            "base_url": self.base_url,
            "available_apis": len(self.get_available_apis()),
            "system_state": {
                "app_state": self.system_state.app_state,
                "connection_state": self.system_state.connection_state
            },
            "config": {
                "app_name": self.config_manager.app_name,
                "app_path": self.config_manager.app_path,
                "known_port": self.config_manager.known_port
            }
        }

    # ---------------------------------------------------------------------------
    # SECTION: Context Manager Support
    # ---------------------------------------------------------------------------

    def __enter__(self):
        """
        Context manager entry.

        Returns:
            GRLApiClient: Self for use in with statement
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit with automatic cleanup.

        Args:
            exc_type: Exception type if an exception occurred
            exc_val: Exception value if an exception occurred
            exc_tb: Exception traceback if an exception occurred
        """
        self.disconnect()

    # ---------------------------------------------------------------------------
    # SECTION: String Representation
    # ---------------------------------------------------------------------------

    def __repr__(self) -> str:
        """
        String representation of the client.

        Returns:
            str: String representation showing client state
        """
        state = "Ready" if self.is_api_ready() else "Not Ready"
        app_name = getattr(self.config_manager, 'app_name', 'Unknown')
        return f"GRLApiClient(app='{app_name}', state='{state}')"

    def __str__(self) -> str:
        """
        Human-readable string representation.

        Returns:
            str: Human-readable representation
        """
        return self.__repr__()