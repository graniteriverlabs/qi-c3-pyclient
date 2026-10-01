# client/modules/project_management/esdf_manager.py
"""
Handles ESDF (Electronic System Description File) operations.
Updated to use enum-based API calls.
"""
import glob
import hashlib
import json
import os
import traceback
from typing import Dict, Optional, List, Any

from API import ApiName
from client.core.app_response import ci_get as _ci_get

# Two input shapes are accepted, told apart by the presence of a "DutInfo" object:
#
#   WPC (SchemaVersion "3.0") - the signed file the app now exports. Nested
#       {"DutInfo": {..., "PTx"/"PRx": {...}}, "DigitalSignatureInfoESDF": {...}}.
#       We do NOT parse it: the app owns the format. POST the raw file text to
#       PostVerifyESDFJsonSign and it verifies the WPC signature and hands back the
#       fields already flattened onto the same names used below.
#
#   Legacy (SchemaVersion "2.0") - one flat object of field/value pairs, parsed here.
#       Still used by the BPP/EPP and WP-TPR inputs. Schema 1.0 is not supported.
ESDF_SCHEMA_VERSION = "2.0"
WPC_ESDF_SCHEMA_VERSION = "3.0"

# Which DUT each build tests, from IsLoadedESDFValidForCurrentApplication in the app:
# a TPR build emulates the receiver, so the DUT it tests is a PTx, and vice versa.
# A file with the wrong DutType is a hard 400 from the app, so it is caught here first.
_DUT_TYPE_BY_ROLE = {"TPR": "PTx", "TPT": "PRx"}


class EsdfManager:
    """
    Manages ESDF configuration loading and processing.
    Handles both single and multiple ESDF file scenarios.
    Accepts the WPC signed ESDF (SchemaVersion "3.0") and the legacy flat file
    (SchemaVersion "2.0"); legacy 1.0 is rejected.
    """

    def __init__(self, config_manager, logger):
        self.config_manager = config_manager
        self.logger = logger
        self.api_handler = None
        # Flattened fields per ESDF content, so one file is verified by the app once per run.
        # A run reads the same ESDF more than once (project creation, the spec-mode sync's
        # PowerProfile lookup, verify_esdf), and each read would otherwise be another POST.
        # Keyed by content hash, so editing the file re-verifies it without a stale hit.
        self._wpc_fields_cache: Dict[str, Dict[str, Any]] = {}

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    def load_esdf_model(self, json_dir: str, file_path: str = None) -> Optional[Dict]:
        """
        Load EsdfConfigurationModel from its corresponding JSON file.

        Args:
            json_dir: Directory containing the JSON configuration files
            file_path: Specific ESDF file path to load

        Returns:
            Loaded ESDF model dict if successful, else None
        """
        model_key = "EsdfConfigurationModel"

        try:
            if not getattr(self.config_manager, 'is_multiple_esdf_files', False):
                file_map = self.config_manager.file_map
                if model_key not in file_map:
                    self.logger.error(f"Missing '{model_key}' in file map for app '{self.config_manager.app_name}'")
                    return None
                filename = file_map[model_key]
                file_path = os.path.join(json_dir, filename)
            else:
                if not file_path:
                    self.logger.error("Multiple ESDF is set to True, but no file_path was provided.")
                    return None

            self.logger.debug(f"Loading ESDF configuration from: {file_path}")
            return self._load_esdf_config_model(file_path)

        except Exception as e:
            self.logger.error(f"Exception while loading ESDF model: {str(e)}")
            return None

    def process_multiple_esdf_files(self, project_name: str, json_dir_app: str,
                                    create_project_func, client) -> List[str]:
        """
        Process multiple ESDF files from a directory.

        Args:
            project_name: Base project name
            json_dir_app: Application-specific JSON directory
            create_project_func: Function to create project
            client: Client instance for test submission

        Returns:
            List indicating overall status
        """
        self.logger.info("Multiple ESDF files processing is enabled in configuration.")

        esdf_dir_name = getattr(self.config_manager, 'get_esdf_folder_name', lambda x: 'esdf_files')(
            app_name=self.config_manager.app_name
        )
        esdf_dir = os.path.join(json_dir_app, esdf_dir_name)

        if not os.path.isdir(esdf_dir):
            error_msg = f"ESDF directory not found: {esdf_dir}"
            self.logger.error(error_msg)
            return [error_msg]

        esdf_files = glob.glob(os.path.join(esdf_dir, "*.json"))
        if not esdf_files:
            warning_msg = f"No ESDF JSON files found in: {esdf_dir}"
            self.logger.warning(warning_msg)
            return [warning_msg]

        self.logger.info(f"Found {len(esdf_files)} ESDF files to process.")

        encountered_issues = False

        for index, file_path in enumerate(sorted(esdf_files), start=1):
            try:
                self.logger.info(f"Processing ESDF file {index}/{len(esdf_files)}: {file_path}")

                esdf_base = os.path.splitext(os.path.basename(file_path))[0]
                test_cases_list = create_project_func(
                    project_name=project_name,
                    esdf_file_path=file_path,
                    esdf_file_name=esdf_base
                )

                if client:
                    result = client.submit_test_list(test_cases_list)

                    if result.get("success"):
                        if "warning" in result:
                            self.logger.warning(f"Processed {file_path} with warning: {result['warning']}")
                            encountered_issues = True
                        else:
                            self.logger.info(
                                f"Successfully processed {file_path}, Total: {len(test_cases_list)} test cases.")
                    else:
                        error_msg = result.get("error", "Unknown error")
                        self.logger.error(f"Failed to process {file_path}: {error_msg}")
                        encountered_issues = True
                else:
                    self.logger.warning("Client not provided, skipping test submission.")
                    encountered_issues = True

            except Exception as e:
                self.logger.error(f"Error processing ESDF file {file_path}: {str(e)}")
                encountered_issues = True

        final_msg = "Test Execution completed with warnings/errors" if encountered_issues else "Test Execution completed"
        self.logger.info(final_msg)
        return [final_msg]

    def put_verify_esdf_data(self, file_path: str, is_from_edit: bool = False) -> Optional[Dict[str, Any]]:
        """
        Call PutVerifyEsdfData (same as UI ESDF upload/verify).

        Request body matches the browser: {"Esdf_Elements": [...], "isFromEdit": bool}.
        Boolean field values are sent as JSON true/false (see HAR capture).

        Args:
            file_path: Path to an ESDF JSON file (WPC signed or legacy flat)
            is_from_edit: Passed as isFromEdit in the JSON body

        Returns:
            API handler result dict, or None if the file could not be loaded
        """
        if not self.api_handler:
            self.logger.error("API handler not set; cannot call PutVerifyEsdfData")
            return None

        model = self._load_esdf_config_model(file_path)
        if not model:
            return None

        payload = {
            "Esdf_Elements": model["Esdf_Elements"],
            "isFromEdit": is_from_edit,
        }
        return self.api_handler.call_api(ApiName.PUT_VERIFY_ESDF_DATA, data=payload)

    def _build_esdf_elements(self, raw_sdf: Dict[str, Any], native_booleans: bool) -> List[Dict[str, Any]]:
        unit_map = {
            "PotentialLoadPower": "W",
            "PotentialLoadPowerEP": "W",
            "GuaranteedLoadPower": "W",
        }
        esdf_elements: List[Dict[str, Any]] = []
        for key, value in raw_sdf.items():
            v: Any = value
            if not native_booleans and isinstance(v, bool):
                v = str(v).lower()
            elif key == "CloakRetryCount" and type(v) is int:
                # HAR: Value is the string "1", not a JSON number
                v = str(v)
            elif key == "CloakRetryCount" and isinstance(v, float) and v.is_integer():
                v = str(int(v))
            esdf_elements.append({
                "Field": key,
                "Value": v,
                "unit": unit_map.get(key, ""),
            })
        return esdf_elements

    def _expected_dut_type(self) -> Optional[str]:
        """
        The DutType this build's ESDF must declare, or None if it cannot be told.

        Returns:
            "PTx" for a TPR build, "PRx" for a TPT build, None if the app name matches
            neither role (or, defensively, both)
        """
        app_name = (getattr(self.config_manager, "app_name", "") or "").upper()
        matches = {dut for role, dut in _DUT_TYPE_BY_ROLE.items() if role in app_name}
        if len(matches) != 1:
            self.logger.warning(
                f"Cannot tell the DUT type for app {app_name!r}; skipping the ESDF DutType check"
            )
            return None
        return matches.pop()

    def _load_wpc_esdf(
        self, file_path: str, raw_text: str, parsed: Dict[str, Any], native_booleans: bool
    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """
        Verify a WPC signed ESDF with the app and take the flattened fields back from it.

        The signature covers the exact file bytes, so the file text is posted verbatim as
        text/plain; re-serialising it would invalidate the signature.

        Args:
            file_path: Path to the ESDF file (for messages only)
            raw_text: The file's exact text
            parsed: The same text already parsed, for the local DutType pre-check
            native_booleans: Passed through to element building

        Returns:
            Dictionary with formatted ESDF elements, or None if the app rejected the file
        """
        dut_type = (parsed.get("DutInfo") or {}).get("DutType")
        expected = self._expected_dut_type()
        if expected and dut_type != expected:
            self.logger.error(
                f"ESDF {os.path.basename(file_path)} describes a {dut_type!r} DUT but "
                f"{self.config_manager.app_name} tests a {expected!r} DUT. The app rejects "
                f"this file; load the ESDF exported for this application."
            )
            return None

        cache_key = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        cached = self._wpc_fields_cache.get(cache_key)
        if cached is not None:
            self.logger.debug(
                f"ESDF {os.path.basename(file_path)} already verified this run "
                f"({len(cached)} fields); reusing it"
            )
            return {"Esdf_Elements": self._build_esdf_elements(cached, native_booleans)}

        if not self.api_handler:
            self.logger.error(
                "API handler not set; a WPC ESDF can only be read by the app itself"
            )
            return None

        response = self.api_handler.call_api(
            ApiName.POST_VERIFY_ESDF_JSON_SIGN,
            data=raw_text.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8", "Accept": "application/json"},
        )
        inner = response.get("response", {}) if isinstance(response, dict) else {}
        if not inner.get("success"):
            # A 400 carries the app's own reason as plain text (unsupported schema, not an
            # ESDF, missing signature block, WPC public key not found).
            self.logger.error(
                f"App rejected ESDF {os.path.basename(file_path)} "
                f"(HTTP {inner.get('status_code')}): {inner.get('data') or inner.get('error')}"
            )
            return None

        data = inner.get("data") or {}
        fields = _ci_get(data, "ESDFFields") or {}
        if not isinstance(fields, dict) or not fields:
            self.logger.error(
                f"App returned no ESDF fields for {os.path.basename(file_path)}; "
                f"response was {data!r}"
            )
            return None

        # Signature state is reported, never fatal: the app accepts an unverified file and
        # simply stamps the report with a remark saying so.
        message = _ci_get(data, "Message") or ""
        if _ci_get(data, "IsSignatureValid") is True:
            self.logger.info(f"ESDF signature verified ({len(fields)} fields). {message}")
        else:
            self.logger.warning(
                f"ESDF signature NOT verified — the report will carry a remark. {message}"
            )

        self._wpc_fields_cache[cache_key] = fields
        return {"Esdf_Elements": self._build_esdf_elements(fields, native_booleans)}

    def _load_esdf_config_model(
        self, file_path: str, native_booleans: bool = True
    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """
        Helper method to load ESDF configuration model from a JSON file and format it with units.

        Accepts both input shapes (see the module notes): a WPC signed file is handed to the
        app to verify and flatten, a legacy flat file is parsed here. Either way the result is
        the same {"Esdf_Elements": [...]} the app's PutProjectFolder expects.

        Default serialization matches the C3 browser HAR for PutProjectFolder / PutVerifyEsdfData:
        boolean Values are JSON true/false (not the strings \"true\"/\"false\").

        Args:
            file_path: Path to the ESDF JSON file
            native_booleans: If True (default), boolean Values match the UI/HAR. If False, booleans
                become lower-case strings (only for exceptional backward compatibility).

        Returns:
            Dictionary with formatted ESDF elements if successful, None if error occurs
        """
        try:
            if not os.path.exists(file_path):
                self.logger.error(f"JSON file not found: {file_path}")
                return None

            with open(file_path, 'r', encoding='utf-8') as json_file:
                raw_text = json_file.read()
            raw_sdf = json.loads(raw_text)

            if not isinstance(raw_sdf, dict):
                self.logger.error(f"Invalid JSON structure in file: {file_path}")
                return None

            if isinstance(raw_sdf.get("DutInfo"), dict):
                self.logger.debug(
                    f"WPC ESDF (SchemaVersion {raw_sdf.get('SchemaVersion')!r}): {file_path}"
                )
                return self._load_wpc_esdf(file_path, raw_text, raw_sdf, native_booleans)

            sv = raw_sdf.get("SchemaVersion")
            if sv != ESDF_SCHEMA_VERSION:
                self.logger.error(
                    f"Invalid ESDF file {file_path}: SchemaVersion must be "
                    f'"{ESDF_SCHEMA_VERSION}" for a flat file or "{WPC_ESDF_SCHEMA_VERSION}" '
                    f"with a DutInfo object for a WPC file (got {sv!r})."
                )
                return None

            esdf_elements = self._build_esdf_elements(raw_sdf, native_booleans)
            return {"Esdf_Elements": esdf_elements}

        except Exception as e:
            self.logger.error(
                f"Failed to load ESDF config from {file_path}: {e}\n{traceback.format_exc()}"
            )
            return None
