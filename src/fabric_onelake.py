"""
Microsoft Fabric OneLake File Operations

Provides file-level operations against OneLake (the unified data lake for Fabric):
- List files/folders in a Lakehouse's Files or Tables section
- Read files (CSV, Parquet, JSON, text)
- Write / upload files
- Delete files
- Get file metadata (size, last modified, etc.)

Authentication uses workload identity via DefaultAzureCredential.
All operations go through the OneLake DFS (Data Lake Storage Gen2) endpoint.
"""

import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from azure.identity import DefaultAzureCredential
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ONELAKE_DFS_ENDPOINT = os.getenv(
    "FABRIC_ONELAKE_DFS_ENDPOINT", "https://onelake.dfs.fabric.microsoft.com"
)
FABRIC_WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID", "")
ONELAKE_SCOPE = "https://storage.azure.com/.default"

# Max download size to prevent out-of-memory (50 MB)
MAX_DOWNLOAD_BYTES = int(os.getenv("ONELAKE_MAX_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# OneLake Client
# ---------------------------------------------------------------------------

class OneLakeClient:
    """
    Client for OneLake DFS operations.

    OneLake paths follow the pattern:
        /<workspace-id>/<item-id>/Files/<path>
        /<workspace-id>/<item-id>/Tables/<table-name>/<partition>

    The DFS API is compatible with Azure Data Lake Storage Gen2.
    """

    def __init__(
        self,
        workspace_id: str = "",
        dfs_endpoint: str = "",
    ):
        self.workspace_id = workspace_id or FABRIC_WORKSPACE_ID
        self.dfs_endpoint = (dfs_endpoint or ONELAKE_DFS_ENDPOINT).rstrip("/")
        self.credential = DefaultAzureCredential()
        self._token_cache: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    def _get_token(self) -> str:
        """Get an access token for OneLake DFS with caching."""
        from datetime import timedelta

        if self._token_cache and self._token_expiry:
            if datetime.now(timezone.utc) < self._token_expiry:
                return self._token_cache

        token = self.credential.get_token(ONELAKE_SCOPE)
        self._token_cache = token.token
        self._token_expiry = datetime.fromtimestamp(
            token.expires_on, tz=timezone.utc
        ) - timedelta(minutes=5)
        return token.token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "x-ms-version": "2021-08-06",
        }

    def _build_url(self, lakehouse_id: str, path: str, section: str = "Files") -> str:
        """
        Build a DFS URL for the given lakehouse item and path.

        Args:
            lakehouse_id: The Fabric lakehouse item ID
            path: Relative path within the section (e.g., "raw/sales.csv")
            section: "Files" or "Tables"
        """
        clean_path = path.strip("/")
        return (
            f"{self.dfs_endpoint}/{self.workspace_id}/{lakehouse_id}"
            f"/{section}/{clean_path}"
        )

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_files(
        self,
        lakehouse_id: str,
        path: str = "",
        section: str = "Files",
        recursive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List files and directories in a OneLake path.

        Args:
            lakehouse_id: Lakehouse item ID
            path: Relative directory path
            section: "Files" or "Tables"
            recursive: Whether to list recursively

        Returns:
            List of dicts with name, path, isDirectory, contentLength, lastModified
        """
        url = self._build_url(lakehouse_id, path, section)
        params: Dict[str, str] = {
            "resource": "filesystem",
            "recursive": str(recursive).lower(),
        }
        if path:
            params["directory"] = f"{section}/{path.strip('/')}"

        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        entries: List[Dict[str, Any]] = []
        for item in data.get("paths", []):
            entries.append({
                "name": item.get("name", "").split("/")[-1],
                "path": item.get("name", ""),
                "isDirectory": item.get("isDirectory", "false") == "true",
                "contentLength": int(item.get("contentLength", 0)),
                "lastModified": item.get("lastModified", ""),
            })

        return entries

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_file(
        self,
        lakehouse_id: str,
        path: str,
        section: str = "Files",
        encoding: str = "utf-8",
    ) -> Dict[str, Any]:
        """
        Read a file from OneLake.

        Args:
            lakehouse_id: Lakehouse item ID
            path: Relative file path
            section: "Files" or "Tables"
            encoding: Text encoding (for text files)

        Returns:
            Dict with content, size, contentType, path
        """
        url = self._build_url(lakehouse_id, path, section)

        # HEAD first to check size
        head_resp = requests.head(url, headers=self._headers(), timeout=15)
        head_resp.raise_for_status()
        size = int(head_resp.headers.get("Content-Length", 0))

        if size > MAX_DOWNLOAD_BYTES:
            return {
                "success": False,
                "error": f"File too large ({size} bytes). Max: {MAX_DOWNLOAD_BYTES}.",
                "path": path,
                "size": size,
            }

        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        is_text = any(
            t in content_type
            for t in ["text/", "json", "csv", "xml"]
        ) or path.endswith((".csv", ".json", ".txt", ".md", ".sql", ".xml", ".yaml", ".yml"))

        content: Any
        if is_text:
            content = resp.content.decode(encoding, errors="replace")
        else:
            # Return base64 for binary files (or just metadata)
            import base64
            content = base64.b64encode(resp.content).decode("ascii")

        return {
            "success": True,
            "path": path,
            "size": size,
            "contentType": content_type,
            "isText": is_text,
            "content": content,
        }

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write_file(
        self,
        lakehouse_id: str,
        path: str,
        content: str,
        section: str = "Files",
        overwrite: bool = True,
    ) -> Dict[str, Any]:
        """
        Write / upload a file to OneLake.

        Uses the DFS 'create then append then flush' three-step pattern.

        Args:
            lakehouse_id: Lakehouse item ID
            path: Relative file path
            content: File content as string
            section: "Files" or "Tables"
            overwrite: Whether to overwrite if exists

        Returns:
            Dict with success, path, size
        """
        url = self._build_url(lakehouse_id, path, section)
        headers = self._headers()

        data_bytes = content.encode("utf-8")
        data_len = len(data_bytes)

        # Step 1: Create
        create_params = {"resource": "file"}
        if overwrite:
            create_params["mode"] = "overwrite"
        create_resp = requests.put(
            url, headers=headers, params=create_params, timeout=30
        )
        create_resp.raise_for_status()

        # Step 2: Append
        append_params = {"action": "append", "position": "0"}
        headers_upload = {**headers, "Content-Length": str(data_len)}
        append_resp = requests.patch(
            url,
            headers=headers_upload,
            params=append_params,
            data=data_bytes,
            timeout=60,
        )
        append_resp.raise_for_status()

        # Step 3: Flush
        flush_params = {"action": "flush", "position": str(data_len)}
        flush_resp = requests.patch(
            url, headers=headers, params=flush_params, timeout=30
        )
        flush_resp.raise_for_status()

        logger.info(f"Wrote {data_len} bytes to OneLake: {path}")

        return {
            "success": True,
            "path": path,
            "size": data_len,
            "section": section,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_file(
        self,
        lakehouse_id: str,
        path: str,
        section: str = "Files",
        recursive: bool = False,
    ) -> Dict[str, Any]:
        """
        Delete a file or directory from OneLake.

        Args:
            lakehouse_id: Lakehouse item ID
            path: Relative path
            section: "Files" or "Tables"
            recursive: If True, delete directory recursively
        """
        url = self._build_url(lakehouse_id, path, section)
        params: Dict[str, str] = {}
        if recursive:
            params["recursive"] = "true"

        resp = requests.delete(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()

        logger.info(f"Deleted from OneLake: {path}")

        return {
            "success": True,
            "path": path,
            "deleted": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def get_file_properties(
        self,
        lakehouse_id: str,
        path: str,
        section: str = "Files",
    ) -> Dict[str, Any]:
        """Get metadata (size, last modified, content type) for a file."""
        url = self._build_url(lakehouse_id, path, section)
        resp = requests.head(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()

        return {
            "success": True,
            "path": path,
            "size": int(resp.headers.get("Content-Length", 0)),
            "contentType": resp.headers.get("Content-Type", ""),
            "lastModified": resp.headers.get("Last-Modified", ""),
            "eTag": resp.headers.get("ETag", ""),
        }


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_onelake_client: Optional[OneLakeClient] = None


def get_onelake_client() -> OneLakeClient:
    """Get or create the global OneLake client."""
    global _onelake_client
    if _onelake_client is None:
        _onelake_client = OneLakeClient()
        logger.info("OneLake client initialized")
    return _onelake_client


# ---------------------------------------------------------------------------
# MCP Tool wrappers (JSON-in, JSON-out)
# ---------------------------------------------------------------------------

def onelake_list_files_tool(
    lakehouse_id: str,
    path: str = "",
    section: str = "Files",
    recursive: bool = False,
) -> str:
    """
    List files and directories in a OneLake Lakehouse path.

    Args:
        lakehouse_id: ID of the lakehouse
        path: Directory path within Files or Tables section
        section: "Files" or "Tables"
        recursive: Whether to list recursively

    Returns:
        JSON string with file listing
    """
    try:
        client = get_onelake_client()
        entries = client.list_files(lakehouse_id, path, section, recursive)
        return json.dumps({
            "success": True,
            "lakehouse_id": lakehouse_id,
            "path": path,
            "section": section,
            "files": entries,
            "count": len(entries),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Error listing OneLake files: {e}")
        return json.dumps({"success": False, "error": str(e)})


def onelake_read_file_tool(
    lakehouse_id: str,
    path: str,
    section: str = "Files",
) -> str:
    """
    Read a file from a OneLake Lakehouse.

    Args:
        lakehouse_id: ID of the lakehouse
        path: File path within the section
        section: "Files" or "Tables"

    Returns:
        JSON string with file content and metadata
    """
    try:
        client = get_onelake_client()
        result = client.read_file(lakehouse_id, path, section)
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error reading OneLake file: {e}")
        return json.dumps({"success": False, "error": str(e)})


def onelake_write_file_tool(
    lakehouse_id: str,
    path: str,
    content: str,
    section: str = "Files",
    overwrite: bool = True,
) -> str:
    """
    Write a file to a OneLake Lakehouse.

    Args:
        lakehouse_id: ID of the lakehouse
        path: Destination file path
        content: File content as string
        section: "Files" or "Tables"
        overwrite: Whether to overwrite existing file

    Returns:
        JSON string with write confirmation
    """
    try:
        client = get_onelake_client()
        result = client.write_file(lakehouse_id, path, content, section, overwrite)
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error writing OneLake file: {e}")
        return json.dumps({"success": False, "error": str(e)})


def onelake_delete_file_tool(
    lakehouse_id: str,
    path: str,
    section: str = "Files",
    recursive: bool = False,
) -> str:
    """
    Delete a file or directory from a OneLake Lakehouse.

    Args:
        lakehouse_id: ID of the lakehouse
        path: Path to delete
        section: "Files" or "Tables"
        recursive: Whether to delete directories recursively

    Returns:
        JSON string with deletion confirmation
    """
    try:
        client = get_onelake_client()
        result = client.delete_file(lakehouse_id, path, section, recursive)
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error deleting OneLake file: {e}")
        return json.dumps({"success": False, "error": str(e)})


def onelake_get_file_properties_tool(
    lakehouse_id: str,
    path: str,
    section: str = "Files",
) -> str:
    """
    Get metadata for a file in OneLake.

    Args:
        lakehouse_id: ID of the lakehouse
        path: File path
        section: "Files" or "Tables"

    Returns:
        JSON string with file properties
    """
    try:
        client = get_onelake_client()
        result = client.get_file_properties(lakehouse_id, path, section)
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error getting OneLake file properties: {e}")
        return json.dumps({"success": False, "error": str(e)})
