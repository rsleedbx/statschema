"""
Databricks Workspace Region Information Retriever

This module provides functionality to retrieve region and related information
from a Databricks workspace using the workspace URL and client instance.
"""

import json
import logging
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse
import re

# Configure logging
logger = logging.getLogger(__name__)


class WorkspaceInfoRetriever:
    """Class to retrieve workspace region and related information."""

    def __init__(self, workspace_client):
        """
        Initialize the WorkspaceInfoRetriever.

        Args:
            workspace_client: Databricks WorkspaceClient instance
        """
        self.w = workspace_client

    def get_workspace_region_info(self, workspace_url: str) -> Dict[str, Any]:
        """
        Retrieve comprehensive workspace region and configuration information.

        Args:
            workspace_url: The Databricks workspace URL (e.g., https://e2-dogfood.staging.cloud.databricks.com)

        Returns:
            Dict containing workspace region info, zones, workspace ID, and other metadata
        """
        info = {
            "workspace_url": workspace_url,
            "workspace_host": self._extract_host(workspace_url),
            "region": None,
            "cloud_provider": None,
            "workspace_id": None,
            "default_zone": None,
            "available_zones": [],
            "metastore_info": {},
            "zerobus_endpoints": {},
            "errors": []
        }

        # Get workspace ID from current user or metastore assignment
        info["workspace_id"] = self._get_workspace_id()

        # Get region and zones information
        region_data = self._get_region_and_zones()
        if region_data:
            info.update(region_data)

        # Get metastore information
        info["metastore_info"] = self._get_metastore_info()

        # Determine cloud provider
        info["cloud_provider"] = self._determine_cloud_provider(info)

        # Generate zerobus endpoints if workspace_id is available
        if info["workspace_id"]:
            info["zerobus_endpoints"] = self._generate_zerobus_endpoints(
                info["workspace_id"],
                info["workspace_host"]
            )

        return info

    def _extract_host(self, workspace_url: str) -> str:
        """Extract hostname from workspace URL."""
        parsed = urlparse(workspace_url)
        return parsed.netloc or parsed.path.split('/')[0]

    def _get_workspace_id(self) -> Optional[int]:
        """Get workspace ID from Unity Catalog metastore assignment or other sources."""
        try:
            # Try to get from metastore assignment
            metastore = self.w.metastores.current()
            if hasattr(metastore, 'workspace_id'):
                return metastore.workspace_id
        except Exception as e:
            logger.debug(f"Could not get workspace ID from metastore: {e}")

        try:
            # Try to get from workspace configuration
            config = self.w.api_client.do("GET", "/api/2.0/workspace-conf")
            if config and "workspace_id" in config:
                return int(config["workspace_id"])
        except Exception as e:
            logger.debug(f"Could not get workspace ID from workspace-conf: {e}")

        return None

    def _get_region_and_zones(self) -> Dict[str, Any]:
        """Get region and availability zones information."""
        result = {}

        try:
            # Get zones using clusters API
            zones_response = self.w.api_client.do("GET", "/api/2.0/clusters/list-zones")

            if zones_response:
                # Extract region from zone (e.g., "us-west-2a" -> "us-west-2")
                default_zone = zones_response.get("default_zone", "")
                zones = zones_response.get("zones", [])

                if default_zone:
                    # Remove the AZ suffix (last character) to get region
                    region = re.sub(r'[a-z]$', '', default_zone)
                    result["region"] = region

                result["default_zone"] = default_zone
                result["available_zones"] = zones

        except Exception as e:
            logger.error(f"Error getting zones information: {e}")
            result["errors"] = [f"Failed to get zones: {str(e)}"]

        return result

    def _get_metastore_info(self) -> Dict[str, Any]:
        """Get Unity Catalog metastore information."""
        metastore_info = {}

        try:
            # Get current metastore assignment
            assignment = self.w.api_client.do("GET", "/api/2.1/unity-catalog/current-metastore-assignment")
            if assignment:
                metastore_info = {
                    "metastore_id": assignment.get("metastore_id"),
                    "default_catalog": assignment.get("default_catalog_name"),
                    "workspace_id": assignment.get("workspace_id")
                }
        except Exception as e:
            logger.debug(f"Could not get metastore info: {e}")

        return metastore_info

    def _determine_cloud_provider(self, info: Dict[str, Any]) -> str:
        """Determine cloud provider based on region and hostname patterns."""
        region = info.get("region", "")
        host = info.get("workspace_host", "")

        # AWS regions pattern
        if re.match(r'^(us|eu|ap|sa|ca|me|af)-', region):
            return "AWS"

        # Azure hostname pattern
        if "azuredatabricks.net" in host:
            return "Azure"

        # GCP hostname pattern
        if "gcp.databricks.com" in host:
            return "GCP"

        # Default to AWS for cloud.databricks.com
        if "cloud.databricks.com" in host:
            return "AWS"

        return "Unknown"

    def _generate_zerobus_endpoints(self, workspace_id: int, workspace_host: str) -> Dict[str, str]:
        """Generate zerobus endpoint URLs based on workspace ID and host."""
        # Extract the base domain (e.g., dogfood.staging.cloud.databricks.com from e2-dogfood.staging.cloud.databricks.com)
        host_parts = workspace_host.split('.')
        if host_parts[0].startswith('e2-'):
            # Remove the 'e2-' prefix for zerobus endpoints
            base_domain = '.'.join([host_parts[0][3:]] + host_parts[1:])
        else:
            base_domain = workspace_host

        return {
            "internal": f"zerobus-internal-{workspace_id}.{base_domain}",
            "external": f"zerobus-external-{workspace_id}.{base_domain}",
            "port": 443
        }


def get_workspace_info(workspace_url: str, w) -> Dict[str, Any]:
    """
    Convenience function to get workspace region information.

    Args:
        workspace_url: The Databricks workspace URL
        w: Databricks WorkspaceClient instance

    Returns:
        Dict containing workspace region info as JSON-serializable data

    Example:
        >>> from databricks.sdk import WorkspaceClient
        >>> w = WorkspaceClient(host="https://e2-dogfood.staging.cloud.databricks.com", profile="e2-dogfood")
        >>> info = get_workspace_info("https://e2-dogfood.staging.cloud.databricks.com", w)
        >>> print(json.dumps(info, indent=2))
    """
    retriever = WorkspaceInfoRetriever(w)
    return retriever.get_workspace_region_info(workspace_url)


def save_workspace_info_to_file(workspace_url: str, w, output_file: str = "workspace_info.json") -> Dict[str, Any]:
    """
    Get workspace info and save it to a JSON file.

    Args:
        workspace_url: The Databricks workspace URL
        w: Databricks WorkspaceClient instance
        output_file: Path to save the JSON output

    Returns:
        Dict containing workspace region info
    """
    info = get_workspace_info(workspace_url, w)

    with open(output_file, 'w') as f:
        json.dump(info, f, indent=2)

    print(f"Workspace info saved to {output_file}")
    return info


# Example usage
if __name__ == "__main__":
    # Example of how to use this module
    from databricks.sdk import WorkspaceClient

    # Example workspace URLs
    example_urls = [
        "https://e2-dogfood.staging.cloud.databricks.com",
        "https://dogfood.staging.databricks.com"
    ]

    print("Databricks Workspace Region Info Retriever")
    print("-" * 50)
    print("\nExample usage:")
    print("```python")
    print("from databricks.sdk import WorkspaceClient")
    print("from workspace_region_info import get_workspace_info")
    print("")
    print("# Initialize workspace client")
    print('w = WorkspaceClient(host="https://e2-dogfood.staging.cloud.databricks.com", profile="e2-dogfood")')
    print("")
    print("# Get workspace information")
    print('info = get_workspace_info("https://e2-dogfood.staging.cloud.databricks.com", w)')
    print("print(json.dumps(info, indent=2))")
    print("```")