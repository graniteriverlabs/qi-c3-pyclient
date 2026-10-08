# client/modules/project_management/project_config_manager.py
"""
Handles loading and validation of project configuration models.
Updated to use enum-based API calls.
"""
import json
import os
import traceback
from typing import Dict, Optional

from API import ApiName


class ProjectConfigManager:
    """
    Manages loading and validation of project configuration models
    (Project, Tester, Report configurations).
    Updated to use enum-based API calls.
    """

    def __init__(self, config_manager, logger):
        self.config_manager = config_manager
        self.logger = logger
        self.api_handler = None

    def set_api_handler(self, api_handler):
        """Set the API handler instance."""
        self.api_handler = api_handler

    def load_project_configuration(self, json_dir: str) -> Optional[Dict[str, dict]]:
        """
        Load Project, Tester, and Report configuration models.

        Args:
            json_dir: Directory containing the JSON configuration files

        Returns:
            Dictionary with loaded models if successful, else None
        """
        required_keys = {
            "ProjectConfigurationModel",
            "TesterConfigurationModel",
            "ReportConfigurationModel"
        }

        file_map = self.config_manager.file_map
        missing_keys = required_keys - file_map.keys()
        if missing_keys:
            self.logger.error(
                f"Missing required model keys in 'files' mapping for app "
                f"'{self.config_manager.app_name}': {missing_keys}"
            )
            return None

        models = {}
        for model_key in required_keys:
            filename = file_map[model_key]
            model = self._load_config_model(json_dir, filename, model_key)
            if not model:
                return None
            models[model_key] = model

        return models

    def _load_config_model(self, json_dir: str, filename: str, model_key: str) -> Optional[Dict]:
        """
        Helper method to load a configuration model from a JSON file.

        Args:
            json_dir: Directory containing the JSON files
            filename: JSON file name to load
            model_key: Key of the configuration model inside the file

        Returns:
            Loaded model dict if successful, None if error occurs
        """
        try:
            file_path = os.path.join(json_dir, filename)
            self.logger.debug(f"Loading {model_key} data from: {file_path}")

            if not os.path.exists(file_path):
                self.logger.error(f"JSON file not found: {file_path}")
                return None

            with open(file_path, 'r', encoding='utf-8') as json_file:
                data = json.load(json_file)

            model = data.get(model_key)
            if not model:
                self.logger.error(f"Key '{model_key}' not found in {filename}")
                return None

            return model

        except Exception as e:
            self.logger.error(
                f"Failed to load {model_key} from {filename}: {e}\n{traceback.format_exc()}"
            )
            return None

