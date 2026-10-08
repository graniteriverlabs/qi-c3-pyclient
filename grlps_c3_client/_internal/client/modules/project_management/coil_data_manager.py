# client/modules/project_management/coil_data_manager.py
"""
Handles optimum coil data operations for specific applications.
Updated to use enum-based API calls.
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, Any

from API import ApiName
from client.core.app_response import ci_get as _ci_get

# The app keeps coil positions as strings and matches them with a plain string compare
# against its own CoilPosition table ("(0.0,0.0)", "(0.0,2.0)", "(2.0,3.5)", "(0.0,1.26)"...).
# "(0,0)" is the same position to a human and a different string to the app, so any other
# spelling silently fails the lookup and the case is reported NOT_RUN. This is the app's
# own default position (AppController.defaultOptimumCoilPosition).
_DEFAULT_COIL_POSITION = "(0.0,0.0)"
_POS_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

# The app now exports optimum coil values as JSON; older setups hold the same data as XML.
# Both are accepted and produce the identical PutOptimumCoilValues payload, so the file map
# may name either. "OptimumCoilValue" is the format-neutral key; "OptimumCoilValueXml" is
# kept because existing grl_config.json files use it (its value may still be a .json file).
_COIL_FILE_KEYS = ("OptimumCoilValue", "OptimumCoilValueXml")


class CoilDataManager:
    """
    Manages optimum coil data operations including:
    - Coil file parsing, JSON (the app's own export) or XML (legacy)
    - Conversion to the app's OptimumCoilValuesModel
    - API communication for coil values
    Updated to use enum-based API calls.
    """

    def __init__(self, config_manager, logger):
        """
        Initialize the CoilDataManager.

        Args:
            config_manager: GRLConfigManager instance with project config
            logger: Logger instance for debug and error logging
        """
        self.config_manager = config_manager
        self.logger = logger
        self.api_handler = None

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    def process_coil_data(self, json_dir: str, run_time_app_json_dir: str) -> bool:
        """
        Process optimum coil data for the application.

        Args:
            json_dir: Directory containing input JSON files
            run_time_app_json_dir: Directory for runtime JSON files

        Returns:
            True if processing was successful, False otherwise
        """
        try:
            file_map = self.config_manager.file_map
            key = next((k for k in _COIL_FILE_KEYS if k in file_map), None)

            if key is None:
                self.logger.error(
                    f"None of {list(_COIL_FILE_KEYS)} found in file_map"
                )
                return False

            coil_filename = file_map[key]
            coil_file_path = os.path.join(json_dir, coil_filename)

            return self._put_optimum_coil_data(coil_file_path, run_time_app_json_dir)

        except Exception as e:
            self.logger.error(f"Exception during coil data processing: {str(e)}")
            return False

    def _put_optimum_coil_data(self, coil_file_path: str, run_time_app_json_dir: str) -> bool:
        """
        Parse the coil file (JSON or XML) into the API model and push it.

        Args:
            coil_file_path: Path to the file containing coil data, .json or .xml
            run_time_app_json_dir: Directory to save the processed JSON

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self._load_coil_file(coil_file_path)
            if result is None:
                return False

            coil_entries = result.get("CoilOptData", [])
            self.logger.info(f"Successfully parsed optimum coil data ({len(coil_entries)} entries)")
            self.logger.debug(f"Optimum coil data: {result}")

            # Save to JSON file
            path_coil_json_file = os.path.join(run_time_app_json_dir, "Optimum_coil_data.json")
            with open(path_coil_json_file, "w") as f:
                json.dump(result, f, indent=4)

            # Send to API using enum
            response = self.api_handler.call_api(ApiName.PUT_OPTIMUM_COIL_VALUES, data=result)
            if response['response']['success']:
                self.logger.info("optimum_coil_values are updated successfully.")
                self._verify_optimum_coil_data(result)
            else:
                error_msg = response['response'].get('data', 'Unknown error')
                self.logger.error(f"Failed to update optimum coil values: {error_msg}")
                return False

            return True

        except Exception as e:
            self.logger.error(f"Failed to process optimum coil data from '{coil_file_path}': {str(e)}")
            return False

    def _canonical_position(self, raw: str) -> str:
        """
        Normalise a coil position to the spelling the app matches against.

        The app compares our stored `position` to its own CoilPosition values with a
        plain string compare, so "(0,0)" never matches "(0.0,0.0)" and every test case
        at the centre position comes back NOT_RUN. Re-emit each coordinate with at
        least one decimal, preserving finer values: "(0,0)" -> "(0.0,0.0)",
        "(2,2)" -> "(2.0,2.0)", "(0.0,3.5)" and "(0.0,1.26)" unchanged.

        Args:
            raw: The <Coil_Position> text from the coil XML

        Returns:
            The position in the app's format; the app default if it cannot be parsed
        """
        numbers = _POS_NUMBER_RE.findall(raw or "")
        if len(numbers) != 2:
            if (raw or "").strip():
                self.logger.warning(
                    f"Coil_Position {raw!r} is not an (x,y) pair; using the app default "
                    f"{_DEFAULT_COIL_POSITION}"
                )
            return _DEFAULT_COIL_POSITION

        parts = []
        for number in numbers:
            text = f"{float(number):g}"
            if "." not in text:
                text += ".0"
            parts.append(text)
        return f"({parts[0]},{parts[1]})"

    def _verify_optimum_coil_data(self, sent: Dict[str, Any]) -> None:
        """
        Read the coil data back and confirm the app stored what we sent.

        PutOptimumCoilValues returns 200 with an empty body even when the payload does
        not bind, so the PUT on its own proves nothing — that is why a rejected payload
        went unnoticed across two runs. Reporting only: never raises, never fails a run.

        Args:
            sent: The payload we just pushed
        """
        if not self.api_handler:
            return

        try:
            response = self.api_handler.call_api(ApiName.GET_OPTIMUM_COIL_VALUES)
            data = response.get("response", {}).get("data") or {}
            # We send PascalCase (the app binds case-insensitively) but it answers in
            # camelCase, so the read-back has to match the key without regard to case.
            stored = _ci_get(data, "CoilOptData") or []
        except Exception as e:
            self.logger.warning(f"Could not read optimum coil values back to verify: {e}")
            return

        self.logger.debug(f"Optimum coil data read back from the app: {stored}")
        sent_entries = sent.get("CoilOptData", [])
        if len(stored) != len(sent_entries):
            self.logger.warning(
                f"Optimum coil read-back: sent {len(sent_entries)} entries, app stored {len(stored)}"
            )

        stored_positions = {str(_ci_get(item, "position", "")) for item in stored if isinstance(item, dict)}
        # An empty stored position is not an error: in Qi-specification mode the app
        # deliberately blanks it, and a blank position matches every test position.
        if "" in stored_positions:
            return
        for entry in sent_entries:
            if entry["position"] not in stored_positions:
                self.logger.warning(
                    f"Optimum coil read-back: position {entry['position']} for "
                    f"{entry['coilType']} was not stored by the app (app has "
                    f"{sorted(stored_positions)}). Coil-enabled test cases at that "
                    f"position will report NOT_RUN."
                )

    def _load_coil_file(self, coil_file_path: str) -> Dict[str, Any]:
        """
        Read the coil file in whichever format it is and return the API payload.

        Both formats carry the same information and produce the identical
        OptimumCoilValuesModel; only the container differs.

        Args:
            coil_file_path: Path to the coil file, .json or .xml

        Returns:
            Dictionary with coil data, or an empty structure if it cannot be read
        """
        if coil_file_path.lower().endswith(".json"):
            return self._convert_json_to_json_dict(coil_file_path)
        return self._convert_xml_to_json_dict(coil_file_path)

    def _convert_json_to_json_dict(self, json_file_path: str) -> Dict[str, Any]:
        """
        Read the app's exported optimum coil JSON and convert it to the API payload.

        Shape (as exported by the app):

            {"Optimum": {"SSCheck": bool, "Coil_Type": str, "Coil_Position": "(x,y)",
                         "Coil_Values": [{"coilType", "value", "position",
                                          "max_SS_obtained_position"}, ...]}}

        Richer than the XML: every coil carries its own position and the
        signal-strength-optimal position, instead of one position for the whole file.
        `SSCheck` and `Coil_Type` describe how the values were captured and have no field
        in the API model, so they are logged and not sent.

        Args:
            json_file_path: Path to the coil JSON file

        Returns:
            Dictionary with coil data, or empty structure if it cannot be read
        """
        if not os.path.exists(json_file_path):
            self.logger.error(f"Coil JSON file not found: {json_file_path}")
            return {"CoilOptData": []}

        try:
            self.logger.debug(f"Parsing coil JSON file: {json_file_path}")
            with open(json_file_path, "r", encoding="utf-8") as f:
                root = json.load(f)

            if not isinstance(root, dict):
                self.logger.error(f"Invalid coil JSON structure in: {json_file_path}")
                return {"CoilOptData": []}

            # Tolerate the payload with or without the "Optimum" wrapper.
            optimum = root.get("Optimum") if isinstance(root.get("Optimum"), dict) else root
            file_position = self._canonical_position(optimum.get("Coil_Position"))
            self.logger.debug(
                f"Coil file context: Coil_Type={optimum.get('Coil_Type')!r}, "
                f"SSCheck={optimum.get('SSCheck')!r} (neither is sent — no API field)"
            )

            coil_opt_data = []
            for coil in optimum.get("Coil_Values") or []:
                if not isinstance(coil, dict):
                    self.logger.warning(f"Skipping non-object entry in Coil_Values: {coil!r}")
                    continue
                # "key"/"value" is the XML spelling, accepted here too.
                key = str(coil.get("coilType") or coil.get("key") or "").strip()
                raw = coil.get("value", coil.get("Value"))
                if not key or raw is None or str(raw).strip() == "":
                    self.logger.warning(f"Missing coilType or value in Coil_Values entry: {coil!r}")
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = raw

                # Per-coil position when the file gives one, else the file-level position.
                own_position = str(coil.get("position") or "").strip()
                position = self._canonical_position(own_position) if own_position else file_position

                coil_opt_data.append({
                    "coilType": key,                    # CoilType enum member, e.g. "TPT_MPP1"
                    "value": value,                     # coil Q / max signal-strength value
                    "position": position,               # optimum coil position, app spelling
                    "max_SS_obtained_position": str(coil.get("max_SS_obtained_position") or ""),
                })
                self.logger.debug(
                    f"Coil opt data: coilType={key}, value={value}, position={position}, "
                    f"max_SS_obtained_position={coil_opt_data[-1]['max_SS_obtained_position']!r}"
                )

            return {"CoilOptData": coil_opt_data}

        except Exception as e:
            self.logger.error(f"Failed to parse coil JSON file '{json_file_path}': {str(e)}")
            return {"CoilOptData": []}

    def _convert_xml_to_json_dict(self, xml_file_path: str) -> Dict[str, Any]:
        """
        Read XML file and convert it into a dictionary matching the expected JSON format.

        Args:
            xml_file_path: Path to the XML file

        Returns:
            Dictionary with coil data, or empty structure if file not found
        """
        # The app's PutOptimumCoilValues expects OptimumCoilValuesModel:
        #   { "CoilOptData": [ { coilType, value, position, max_SS_obtained_position }, ... ] }
        # where coilType is a CoilType enum member name (e.g. "TPT_MPP1"), value is the coil Q
        # (double), position is the optimum coil position, and max_SS_obtained_position is the
        # signal-strength-optimal position (used when SSCheck is on). The previous shape
        # ({coilValue,sSCheck}) did NOT bind to this model, so the optimum coil never loaded and
        # coil-enabled test cases came back NOT_RUN ("optimum coil position ... not loaded").
        if not os.path.exists(xml_file_path):
            self.logger.error(f"XML file not found: {xml_file_path}")
            return {"CoilOptData": []}

        try:
            self.logger.debug(f"Parsing XML file: {xml_file_path}")
            tree = ET.parse(xml_file_path)
            root = tree.getroot()

            position = self._canonical_position(root.findtext('Coil_Position'))

            coil_opt_data = []
            for coil in root.findall('Coil_Values'):
                key = (coil.findtext('key') or "").strip()
                raw = (coil.findtext('value') or "").strip()
                if not key or not raw:
                    self.logger.warning("Missing <key> or <value> in Coil_Values element")
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
                coil_opt_data.append({
                    "coilType": key,                    # CoilType enum member, e.g. "TPT_MPP1"
                    "value": value,                     # coil Q / max signal-strength value
                    "position": position,               # optimum coil position, app spelling
                    # Nothing in the app's execution gate reads this; both its own UI flow
                    # and its project-file converter write an empty string here.
                    "max_SS_obtained_position": "",
                })
                self.logger.debug(f"Coil opt data: coilType={key}, value={value}, position={position}")

            return {"CoilOptData": coil_opt_data}

        except Exception as e:
            self.logger.error(f"Failed to parse XML file '{xml_file_path}': {str(e)}")
            return {"CoilOptData": []}
