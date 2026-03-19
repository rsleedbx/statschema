#!/usr/bin/env python3
"""
Test script for workspace_region_info module.

This script demonstrates how to use the workspace_region_info module
to retrieve region and configuration information from Databricks workspaces.
"""

import json
import sys
from databricks.sdk import WorkspaceClient
from src.zbhelper.workspace_region_info import get_workspace_info, save_workspace_info_to_file


def main():
    """Main function to test workspace info retrieval."""

    # Define test workspaces
    test_workspaces = [
        {
            "url": "https://e2-dogfood.staging.cloud.databricks.com",
            "profile": "e2-dogfood"
        },
        {
            "url": "https://dogfood.staging.databricks.com",
            "profile": "dogfood-staging"
        }
    ]

    print("=" * 70)
    print("Databricks Workspace Region Information Test")
    print("=" * 70)

    for workspace in test_workspaces:
        print(f"\nTesting workspace: {workspace['url']}")
        print("-" * 50)

        try:
            # Initialize workspace client
            w = WorkspaceClient(
                host=workspace["url"],
                profile=workspace["profile"]
            )

            # Get workspace information
            info = get_workspace_info(workspace["url"], w)

            # Print key information
            print(f"✓ Region: {info.get('region', 'Unknown')}")
            print(f"✓ Cloud Provider: {info.get('cloud_provider', 'Unknown')}")
            print(f"✓ Workspace ID: {info.get('workspace_id', 'Unknown')}")
            print(f"✓ Default Zone: {info.get('default_zone', 'Unknown')}")
            print(f"✓ Available Zones: {', '.join(info.get('available_zones', []))}")

            if info.get('zerobus_endpoints'):
                print(f"✓ Zerobus Internal: {info['zerobus_endpoints'].get('internal', 'N/A')}")
                print(f"✓ Zerobus External: {info['zerobus_endpoints'].get('external', 'N/A')}")

            if info.get('metastore_info'):
                print(f"✓ Metastore ID: {info['metastore_info'].get('metastore_id', 'N/A')}")

            # Save to file (optional)
            # output_file = f"workspace_info_{workspace['profile']}.json"
            # save_workspace_info_to_file(workspace["url"], w, output_file)

            # Print full JSON (optional - comment out for cleaner output)
            # print("\nFull JSON output:")
            # print(json.dumps(info, indent=2))

        except Exception as e:
            print(f"✗ Error: {str(e)}")
            print("  Make sure you're authenticated to this workspace")

    print("\n" + "=" * 70)
    print("Test complete!")


if __name__ == "__main__":
    main()