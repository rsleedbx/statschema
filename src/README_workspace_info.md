# Workspace Region Info Module

This module provides functionality to retrieve region and configuration information from Databricks workspaces.

## Features

- Get AWS/Azure/GCP region information
- Retrieve workspace ID and zones
- Generate zerobus endpoint URLs
- Get Unity Catalog metastore information
- Determine cloud provider automatically

## Installation

The required dependencies are already in the project's `requirements.txt`:
```bash
pip install databricks-sdk>=0.20.0
```

## Usage

### Basic Usage

```python
from databricks.sdk import WorkspaceClient
from workspace_region_info import get_workspace_info
import json

# Initialize workspace client
w = WorkspaceClient(
    host="https://e2-dogfood.staging.cloud.databricks.com",
    profile="e2-dogfood"
)

# Get workspace information
info = get_workspace_info("https://e2-dogfood.staging.cloud.databricks.com", w)

# Print the results
print(json.dumps(info, indent=2))
```

### Using the Class Directly

```python
from databricks.sdk import WorkspaceClient
from workspace_region_info import WorkspaceInfoRetriever

# Initialize client and retriever
w = WorkspaceClient(host="your-workspace-url", profile="your-profile")
retriever = WorkspaceInfoRetriever(w)

# Get comprehensive workspace info
info = retriever.get_workspace_region_info("your-workspace-url")
```

### Save to File

```python
from workspace_region_info import save_workspace_info_to_file

# Save workspace info to JSON file
info = save_workspace_info_to_file(
    "https://e2-dogfood.staging.cloud.databricks.com",
    w,
    "workspace_info.json"
)
```

## Output Format

The function returns a dictionary with the following structure:

```json
{
  "workspace_url": "https://e2-dogfood.staging.cloud.databricks.com",
  "workspace_host": "e2-dogfood.staging.cloud.databricks.com",
  "region": "us-west-2",
  "cloud_provider": "AWS",
  "workspace_id": 6051921418418893,
  "default_zone": "us-west-2a",
  "available_zones": ["us-west-2a", "us-west-2b"],
  "metastore_info": {
    "metastore_id": "19a85dee-54bc-43a2-87ab-023d0ec16013",
    "default_catalog": "main",
    "workspace_id": 6051921418418893
  },
  "zerobus_endpoints": {
    "internal": "zerobus-internal-6051921418418893.dogfood.staging.cloud.databricks.com",
    "external": "zerobus-external-6051921418418893.dogfood.staging.cloud.databricks.com",
    "port": 443
  },
  "errors": []
}
```

## Testing

Run the test script to verify functionality:

```bash
cd /Users/robert.lee/github/zerobus/src
python3 test_workspace_info.py
```

## API Endpoints Used

The module uses the following Databricks API endpoints:
- `/api/2.0/clusters/list-zones` - Get availability zones and region
- `/api/2.1/unity-catalog/current-metastore-assignment` - Get metastore info
- `/api/2.0/workspace-conf` - Get workspace configuration

## Error Handling

The module handles API errors gracefully and includes them in the `errors` field of the returned dictionary. If certain information cannot be retrieved, those fields will be `None` or empty.

## Cloud Provider Detection

The module automatically detects the cloud provider based on:
- AWS: Regions starting with us-, eu-, ap-, etc.
- Azure: Hostname containing "azuredatabricks.net"
- GCP: Hostname containing "gcp.databricks.com"