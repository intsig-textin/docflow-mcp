"""
Docflow MCP Server

Exposes Docflow document automation capabilities as MCP tools.

Coverage: 44 of 46 API endpoints.
  Intentionally omitted:
    - GET /auth/token        — generates short-lived token for iframe embedding, not agent use.
    - GET /category/sample/download — returns binary file content, not useful for agents.
  Internally called (no separate tool needed):
    - POST /file/upload, /file/upload/sync  → docflow_upload_and_extract
    - POST /workspace/create               → docflow_get_or_create_workspace
    - POST /review/rule_repo/create        → docflow_setup_review_rules
    - POST /review/rule_group/create       → docflow_setup_review_rules
    - POST /review/rule/create             → docflow_setup_review_rules
    - POST /review/task/submit             → docflow_run_review

Two layers of tools:
  Workflow tools  (6)  — composite operations hiding polling and multi-step details.
  Resource tools  (35) — one tool per API endpoint, for configuration and edge cases.

Authentication (environment variables):
  DOCFLOW_APP_ID       — x-ti-app-id
  DOCFLOW_SECRET_CODE  — x-ti-secret-code
  DOCFLOW_HOST         — optional, defaults to https://docflow.textin.com
"""

import dataclasses
import os
import time
from typing import Any, Optional

from fastmcp import FastMCP
from docflow import DocflowClient
from docflow.exceptions import DocflowException

# ---------------------------------------------------------------------------
# Server and client setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "docflow",
    instructions=(
        "Docflow document automation platform. "
        "Use workflow tools for the standard extract/review pipeline. "
        "Use resource tools for CRUD operations on workspaces, categories, and review rules."
    ),
)

client = DocflowClient.from_env()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SUPPORTED_EXTS = {
    ".pdf", ".jpg", ".jpeg", ".png",
    ".doc", ".docx", ".xls", ".xlsx",
    ".ofd", ".txt",
}

_SYNC_THRESHOLD = 3  # files <= this use synchronous upload


def _collect_files(file_paths: Optional[list[str]], directory: Optional[str]) -> list[str]:
    """Resolve file list from explicit paths or directory scan."""
    if file_paths and directory:
        raise ValueError("Provide either file_paths or directory, not both.")
    if directory:
        if not os.path.isdir(directory):
            raise ValueError(f"Directory not found: {directory}")
        files = sorted(
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if os.path.splitext(f)[1].lower() in _SUPPORTED_EXTS
        )
        if not files:
            raise ValueError(
                f"No supported files found in {directory}. "
                f"Supported: {', '.join(sorted(_SUPPORTED_EXTS))}"
            )
        return files
    if file_paths:
        missing = [p for p in file_paths if not os.path.isfile(p)]
        if missing:
            raise ValueError(f"Files not found: {missing}")
        return file_paths
    raise ValueError("Provide either file_paths or directory.")


def _s(obj: Any) -> Any:
    """Recursively convert pydantic models / dataclasses to plain dicts/lists."""
    if hasattr(obj, "model_dump"):
        return _s(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _s(dataclasses.asdict(obj))
    if isinstance(obj, list):
        return [_s(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _s(v) for k, v in obj.items()}
    return obj


def _poll_files(workspace_id: str, batch_numbers: list[str], timeout: int) -> list[dict]:
    """Poll until all batches reach a terminal recognition_status (1=success, 2=failed)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        results: list[dict] = []
        all_done = True
        for bn in batch_numbers:
            resp = _s(client.file.fetch(workspace_id=workspace_id, batch_number=bn))
            for f in resp.get("files", []):
                if f.get("recognition_status") not in (1, 2):
                    all_done = False
                results.append(f)
        if all_done:
            return results
        time.sleep(3)
    raise TimeoutError(f"File processing timed out after {timeout}s.")


def _poll_review(workspace_id: str, task_id: str, timeout: int) -> dict:
    """Poll review task until it reaches a terminal status (not 3=reviewing)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _s(client.review.get_task_result(workspace_id=workspace_id, task_id=task_id))
        if result.get("status") != 3:
            return result
        time.sleep(5)
    raise TimeoutError(f"Review task timed out after {timeout}s.")


# ===========================================================================
# WORKFLOW TOOLS  (composite, high-level)
# ===========================================================================

@mcp.tool()
def docflow_get_or_create_workspace(
    name: str,
    enterprise_id: int = 0,
    description: str = "",
) -> dict:
    """
    Find a workspace by name, or create one if it does not exist.

    Returns:
        workspace_id (str): ID of the workspace.
        created (bool): True if a new workspace was created.

    Use this as Step 1 of every workflow to ensure a workspace is ready.
    """
    try:
        workspaces = _s(client.workspace.list(enterprise_id=enterprise_id, page=1, page_size=100))
        for ws in workspaces.get("workspaces", []):
            if ws.get("name") == name:
                return {"workspace_id": ws["workspace_id"], "created": False}
    except DocflowException:
        pass

    result = client.workspace.create(
        name=name, description=description,
        enterprise_id=enterprise_id, auth_scope=0,
    )
    return {"workspace_id": _s(result)["workspace_id"], "created": True}


@mcp.tool()
def docflow_list_categories(workspace_id: str) -> list[dict]:
    """
    List all enabled categories in a workspace, including their field configurations.

    Returns a list of category objects with name, category_id, and fields.
    Use this to check which categories are already configured before creating new ones.
    """
    resp = _s(client.category.list(workspace_id=workspace_id, page=1, page_size=100))
    return resp.get("categories", [])


@mcp.tool()
def docflow_create_category(
    workspace_id: str,
    name: str,
    fields: list[dict],
    sample_file_path: str,
    extract_model: str = "llm",
    category_prompt: str = "",
) -> dict:
    """
    Create a file category with field configuration and a sample file.

    Args:
        workspace_id: Target workspace.
        name: Category name (e.g. "增值税发票", "酒店水单").
        fields: Field definitions. Format: [{"name": "发票金额", "description": "价税合计"}]
        sample_file_path: Local path to a sample file (PDF/image). Required for classification.
        extract_model: "llm" for text-heavy docs, "vlm" for scanned/layout-complex docs.
        category_prompt: Optional classification hint (max 500 chars).

    Returns:
        category_id (str): ID of the created category.
    """
    return _s(client.category.create(
        workspace_id=workspace_id, name=name, fields=fields,
        sample_file_path=sample_file_path,
        extract_model=extract_model, category_prompt=category_prompt,
    ))


@mcp.tool()
def docflow_upload_and_extract(
    workspace_id: str,
    file_paths: Optional[list[str]] = None,
    directory: Optional[str] = None,
    category: Optional[str] = None,
    auto_verify_vat: bool = False,
    timeout: int = 120,
) -> dict:
    """
    Upload files and wait for classification and extraction to complete.

    Provide either file_paths (explicit list) or directory (auto-scans supported files).
    Supported formats: PDF, JPG, PNG, DOC, DOCX, XLS, XLSX, OFD, TXT.

    Automatically selects upload mode:
      <= 3 files  → synchronous (results returned immediately).
      >  3 files  → asynchronous upload then polls until complete.

    Args:
        workspace_id: Target workspace.
        file_paths: Explicit list of local file paths.
        directory: Directory path to scan for supported files.
        category: Force a specific category, skipping auto-classification.
        auto_verify_vat: Enable VAT invoice authenticity verification.
        timeout: Max seconds to wait for async processing (default 120).

    Returns:
        files: List of results, each containing:
               name, category, recognition_status, task_id,
               data.fields, data.items, data.stamps, data.handwritings.
        total (int), failed_count (int), failed_files (list).
    """
    files = _collect_files(file_paths, directory)
    kwargs = dict(workspace_id=workspace_id, category=category, auto_verify_vat=auto_verify_vat)

    if len(files) <= _SYNC_THRESHOLD:
        results = []
        for fp in files:
            resp = _s(client.file.upload_sync(file_path=fp, **kwargs))
            results.extend(resp.get("files", []))
    else:
        batch_numbers = []
        for fp in files:
            resp = _s(client.file.upload(file_path=fp, **kwargs))
            if bn := resp.get("batch_number"):
                batch_numbers.append(bn)
        results = _poll_files(workspace_id, batch_numbers, timeout)

    failed = [f for f in results if f.get("recognition_status") == 2]
    return {
        "files": results,
        "total": len(results),
        "failed_count": len(failed),
        "failed_files": [f.get("name") for f in failed],
    }


@mcp.tool()
def docflow_setup_review_rules(
    workspace_id: str,
    repo_name: str,
    rule_groups: list[dict],
) -> dict:
    """
    Create a review rule repository (idempotent — reuses existing repo if name matches).

    Args:
        workspace_id: Target workspace.
        repo_name: Name for the rule repository.
        rule_groups: List of rule group definitions:
            [{"name": "组名", "rules": [
                {"name": "规则名", "prompt": "审核判断提示词",
                 "category_ids": ["cat_id"], "risk_level": 10}
            ]}]
            risk_level: 10=high, 20=medium, 30=low.

    Returns:
        repo_id (str), created (bool).
    """
    try:
        repos = _s(client.review.list_repos(workspace_id=workspace_id)).get("items", [])
        for repo in repos:
            if repo.get("name") == repo_name:
                return {"repo_id": repo["repo_id"], "created": False}
    except DocflowException:
        pass

    repo_id = _s(client.review.create_repo(workspace_id=workspace_id, name=repo_name))["repo_id"]

    for group_def in rule_groups:
        group_id = _s(client.review.create_group(
            workspace_id=workspace_id, repo_id=repo_id, name=group_def["name"],
        ))["group_id"]
        for rule_def in group_def.get("rules", []):
            client.review.create_rule(
                workspace_id=workspace_id, repo_id=repo_id, group_id=group_id,
                name=rule_def["name"], prompt=rule_def["prompt"],
                category_ids=rule_def.get("category_ids", []),
                risk_level=rule_def.get("risk_level", 20),
                referenced_fields=rule_def.get("referenced_fields"),
            )

    return {"repo_id": repo_id, "created": True}


@mcp.tool()
def docflow_run_review(
    workspace_id: str,
    repo_id: str,
    task_ids: list[str],
    name: str = "审核任务",
    timeout: int = 300,
) -> dict:
    """
    Submit a review task and wait for results.

    Args:
        workspace_id: Target workspace.
        repo_id: Review rule repository ID (from docflow_setup_review_rules).
        task_ids: Extraction task IDs to review (task_id from docflow_upload_and_extract).
        name: Display name for this review task.
        timeout: Max seconds to wait (default 300).

    Returns:
        status: 1=pass, 2=failed, 4=not_pass, 7=recognition_failed.
        statistics: {pass_count, failure_count}.
        groups: Rule-by-rule results with AI reasoning.
    """
    submit = _s(client.review.submit_task(
        workspace_id=workspace_id, name=name,
        repo_id=repo_id, extract_task_ids=task_ids,
    ))
    return _poll_review(workspace_id, submit["task_id"], timeout)


# ===========================================================================
# RESOURCE TOOLS — Files  (endpoints 3–8)
# ===========================================================================

@mcp.tool()
def docflow_fetch_files(
    workspace_id: str,
    batch_number: Optional[str] = None,
    file_id: Optional[str] = None,
    category: Optional[str] = None,
    recognition_status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """
    Query processed files in a workspace (GET /file/fetch).

    Useful for checking status of previously uploaded files or retrieving results later.
    recognition_status: "0"=pending, "1"=success, "2"=failed, "3"=processing.
    """
    return _s(client.file.fetch(
        workspace_id=workspace_id, batch_number=batch_number,
        file_id=file_id, category=category,
        recognition_status=recognition_status, page=page, page_size=page_size,
    ))


@mcp.tool()
def docflow_update_file(
    workspace_id: str,
    file_id: str,
    data: dict,
) -> dict:
    """
    Update metadata or verification status of a processed file (POST /file/update).

    Args:
        workspace_id: Target workspace.
        file_id: ID of the file to update.
        data: Fields to update (e.g. {"verification_status": 1}).
    """
    return _s(client.file.update(workspace_id=workspace_id, file_id=file_id, data=data))


@mcp.tool()
def docflow_delete_files(
    workspace_id: str,
    task_ids: Optional[list[str]] = None,
    batch_numbers: Optional[list[str]] = None,
) -> dict:
    """
    Delete processed files from a workspace (POST /file/delete).
    Provide either task_ids or batch_numbers. Deletion is permanent.
    """
    if not task_ids and not batch_numbers:
        raise ValueError("Provide either task_ids or batch_numbers.")
    client.file.delete(
        workspace_id=workspace_id,
        task_id=task_ids, batch_number=batch_numbers,
    )
    return {"deleted_count": len(task_ids or batch_numbers or [])}


@mcp.tool()
def docflow_extract_fields(
    workspace_id: str,
    task_id: str,
    fields: Optional[list[dict]] = None,
    tables: Optional[list[dict]] = None,
) -> dict:
    """
    Re-extract specific fields from an already-processed file (POST /file/extract_fields).

    Use when you need to extract additional fields without re-uploading the file,
    or when the original extraction missed certain fields.

    Args:
        workspace_id: Target workspace.
        task_id: Task ID of the already-processed file.
        fields: Fields to extract. Format: [{"name": "字段名", "prompt": "抽取提示"}]
        tables: Table fields to extract. Format: [{"name": "表名", "fields": [...]}]
    """
    return _s(client.file.extract_fields(
        workspace_id=workspace_id, task_id=task_id,
        fields=fields, tables=tables,
    ))


@mcp.tool()
def docflow_retry_files(workspace_id: str, task_ids: list[str]) -> dict:
    """
    Retry processing for files that failed (POST /file/retry).
    Use when recognition_status=2 (failed).
    """
    for task_id in task_ids:
        client.file.retry(workspace_id=workspace_id, task_id=task_id)
    return {"retried_count": len(task_ids)}


@mcp.tool()
def docflow_amend_category(
    workspace_id: str,
    task_id: str,
    category: str,
) -> dict:
    """
    Correct a misclassified file's category and trigger re-extraction (POST /file/amend_category).

    Use when a file was classified into the wrong category.
    The file will be re-processed using the correct category's field configuration.
    """
    client.file.amend_category(workspace_id=workspace_id, task_id=task_id, category=category)
    return {"task_id": task_id, "new_category": category}


# ===========================================================================
# RESOURCE TOOLS — Workspace  (endpoints 11–14)
# ===========================================================================

@mcp.tool()
def docflow_list_workspaces(enterprise_id: int = 0) -> list[dict]:
    """
    List all workspaces accessible to the current account (GET /workspace/list).
    Returns workspace_id, name, description, and auth_scope for each.
    """
    resp = _s(client.workspace.list(enterprise_id=enterprise_id, page=1, page_size=100))
    return resp.get("workspaces", [])


@mcp.tool()
def docflow_get_workspace(workspace_id: str) -> dict:
    """
    Get detailed information about a specific workspace (GET /workspace/get).
    Returns workspace metadata including name, description, auth_scope, and statistics.
    """
    return _s(client.workspace.get(workspace_id=workspace_id))


@mcp.tool()
def docflow_update_workspace(
    workspace_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    auth_scope: Optional[int] = None,
    callback_url: Optional[str] = None,
) -> dict:
    """
    Update workspace settings (POST /workspace/update).

    Args:
        workspace_id: Workspace to update.
        name: New display name.
        description: New description.
        auth_scope: 0=private (self only), 1=public (enterprise members).
        callback_url: Webhook URL for processing completion notifications.
    """
    return _s(client.workspace.update(
        workspace_id=workspace_id, name=name,
        description=description, auth_scope=auth_scope,
        callback_url=callback_url,
    ))


@mcp.tool()
def docflow_delete_workspace(workspace_id: str) -> dict:
    """
    Delete a workspace and all its contents (POST /workspace/delete).
    WARNING: This permanently deletes all files, categories, and review rules in the workspace.
    """
    client.workspace.delete(workspace_ids=[workspace_id])
    return {"deleted_workspace_id": workspace_id}


# ===========================================================================
# RESOURCE TOOLS — Category  (endpoints 16–18)
# ===========================================================================

@mcp.tool()
def docflow_update_category(
    workspace_id: str,
    category_id: str,
    name: Optional[str] = None,
    extract_model: Optional[str] = None,
    category_prompt: Optional[str] = None,
    enabled: Optional[int] = None,
) -> dict:
    """
    Update an existing file category's settings (POST /category/update).

    Args:
        extract_model: "llm" or "vlm".
        category_prompt: Classification hint text.
        enabled: 1=enable, 0=disable.
    """
    return _s(client.category.update(
        workspace_id=workspace_id, category_id=category_id,
        name=name, extract_model=extract_model,
        category_prompt=category_prompt, enabled=enabled,
    ))


@mcp.tool()
def docflow_delete_category(workspace_id: str, category_id: str) -> dict:
    """
    Delete a file category and its field configuration (POST /category/delete).
    WARNING: Existing files classified under this category retain their extracted data,
    but future uploads will not match this category.
    """
    client.category.delete(workspace_id=workspace_id, category_ids=[category_id])
    return {"deleted_category_id": category_id}


# ===========================================================================
# RESOURCE TOOLS — Category Tables  (endpoints 19–22)
# ===========================================================================

@mcp.tool()
def docflow_list_category_tables(workspace_id: str, category_id: str) -> list[dict]:
    """
    List all table configurations in a category (GET /category/tables/list).

    Returns table_id, name, and fields for each table.
    Use this to get table_id before adding table fields or building review rules.
    """
    resp = _s(client.category.tables.list(workspace_id=workspace_id, category_id=category_id))
    return resp.get("tables", [])


@mcp.tool()
def docflow_add_category_table(
    workspace_id: str,
    category_id: str,
    name: str,
    fields: list[dict],
) -> dict:
    """
    Add a table field group to a category (POST /category/tables/add).

    Use for extracting structured table data (e.g. invoice line items, hotel charges).

    Args:
        name: Table name (e.g. "货物明细", "消费明细").
        fields: Column definitions. Format: [{"name": "列名", "description": "描述"}]

    Returns:
        table_id (str): ID of the created table.
    """
    return _s(client.category.tables.add(
        workspace_id=workspace_id, category_id=category_id,
        name=name, fields=fields,
    ))


@mcp.tool()
def docflow_update_category_table(
    workspace_id: str,
    category_id: str,
    table_id: str,
    name: Optional[str] = None,
    fields: Optional[list[dict]] = None,
) -> dict:
    """
    Update a table configuration in a category (POST /category/tables/update).
    """
    return _s(client.category.tables.update(
        workspace_id=workspace_id, category_id=category_id,
        table_id=table_id, name=name, fields=fields,
    ))


@mcp.tool()
def docflow_delete_category_tables(
    workspace_id: str,
    category_id: str,
    table_ids: list[str],
) -> dict:
    """
    Delete table configurations from a category (POST /category/tables/delete).
    """
    client.category.tables.delete(
        workspace_id=workspace_id, category_id=category_id, table_ids=table_ids,
    )
    return {"deleted_count": len(table_ids)}


# ===========================================================================
# RESOURCE TOOLS — Category Fields  (endpoints 23, 25–26)
# ===========================================================================

@mcp.tool()
def docflow_list_category_fields(workspace_id: str, category_id: str) -> list[dict]:
    """
    List all fields configured in a category (GET /category/fields/list).

    Returns field_id, name, description, and configuration for each field.
    Use this to get field_ids needed when building review rules with referenced_fields.
    """
    resp = _s(client.category.fields.list(workspace_id=workspace_id, category_id=category_id))
    return resp.get("fields", [])


@mcp.tool()
def docflow_add_category_fields(
    workspace_id: str,
    category_id: str,
    fields: list[dict],
) -> dict:
    """
    Add fields to an existing category (POST /category/fields/add).

    Args:
        fields: Fields to add. Format: [{"name": "字段名", "description": "可选描述"}]

    Returns:
        added_count (int), field_ids (list).
    """
    field_ids = []
    for field in fields:
        resp = _s(client.category.fields.add(
            workspace_id=workspace_id, category_id=category_id,
            name=field["name"], description=field.get("description", ""),
        ))
        field_ids.append(resp.get("field_id"))
    return {"added_count": len(field_ids), "field_ids": field_ids}


@mcp.tool()
def docflow_update_category_field(
    workspace_id: str,
    category_id: str,
    field_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    prompt: Optional[str] = None,
) -> dict:
    """
    Update a field's name, description, or extraction prompt (POST /category/fields/update).

    Updating the prompt can improve extraction accuracy for specific field types.
    """
    return _s(client.category.fields.update(
        workspace_id=workspace_id, category_id=category_id,
        field_id=field_id, name=name, description=description, prompt=prompt,
    ))


@mcp.tool()
def docflow_delete_category_fields(
    workspace_id: str,
    category_id: str,
    field_ids: list[str],
) -> dict:
    """
    Delete fields from a category (POST /category/fields/delete).
    """
    client.category.fields.delete(
        workspace_id=workspace_id, category_id=category_id, field_ids=field_ids,
    )
    return {"deleted_count": len(field_ids)}


# ===========================================================================
# RESOURCE TOOLS — Category Samples  (endpoints 27–28, 30)
# ===========================================================================

@mcp.tool()
def docflow_add_category_samples(
    workspace_id: str,
    category_id: str,
    sample_file_paths: list[str],
) -> dict:
    """
    Add sample files to a category to improve classification accuracy (POST /category/sample/upload).

    More samples (3–5 recommended, max 10 total) improve classification reliability.
    """
    added = 0
    for path in sample_file_paths:
        client.category.samples.upload(
            workspace_id=workspace_id, category_id=category_id, sample_file_path=path,
        )
        added += 1
    return {"added_count": added}


@mcp.tool()
def docflow_list_category_samples(workspace_id: str, category_id: str) -> list[dict]:
    """
    List all sample files configured for a category (GET /category/sample/list).
    Returns sample_id, file name, and upload time for each sample.
    """
    resp = _s(client.category.samples.list(workspace_id=workspace_id, category_id=category_id))
    return resp.get("samples", [])


@mcp.tool()
def docflow_delete_category_samples(
    workspace_id: str,
    category_id: str,
    sample_ids: list[str],
) -> dict:
    """
    Delete sample files from a category (POST /category/sample/delete).
    Use to remove low-quality or irrelevant samples that hurt classification accuracy.
    """
    client.category.samples.delete(
        workspace_id=workspace_id, category_id=category_id, sample_ids=sample_ids,
    )
    return {"deleted_count": len(sample_ids)}


# ===========================================================================
# RESOURCE TOOLS — Review Rule Repo  (endpoints 32–35)
# ===========================================================================

@mcp.tool()
def docflow_list_review_repos(workspace_id: str) -> list[dict]:
    """
    List all review rule repositories in a workspace (GET /review/rule_repo/list).
    Returns repo_id, name, and creation time for each.
    """
    resp = _s(client.review.list_repos(workspace_id=workspace_id))
    return resp.get("items", [])


@mcp.tool()
def docflow_get_review_repo(workspace_id: str, repo_id: str) -> dict:
    """
    Get a review rule repository with all its groups and rules (GET /review/rule_repo/get).
    Use this to inspect existing rule configurations before making changes.
    """
    return _s(client.review.get_repo(workspace_id=workspace_id, repo_id=repo_id))


@mcp.tool()
def docflow_update_review_repo(
    workspace_id: str,
    repo_id: str,
    name: str,
) -> dict:
    """
    Rename a review rule repository (POST /review/rule_repo/update).
    """
    client.review.update_repo(workspace_id=workspace_id, repo_id=repo_id, name=name)
    return {"repo_id": repo_id, "new_name": name}


@mcp.tool()
def docflow_delete_review_repo(workspace_id: str, repo_id: str) -> dict:
    """
    Delete a review rule repository and all its groups and rules (POST /review/rule_repo/delete).
    WARNING: This is permanent. Existing review task results are not affected.
    """
    client.review.delete_repo(workspace_id=workspace_id, repo_ids=[repo_id])
    return {"deleted_repo_id": repo_id}


# ===========================================================================
# RESOURCE TOOLS — Review Rule Groups  (endpoints 37–38)
# ===========================================================================

@mcp.tool()
def docflow_update_review_rule_group(
    workspace_id: str,
    group_id: str,
    name: str,
) -> dict:
    """
    Rename a review rule group (POST /review/rule_group/update).
    """
    client.review.update_group(workspace_id=workspace_id, group_id=group_id, name=name)
    return {"group_id": group_id, "new_name": name}


@mcp.tool()
def docflow_delete_review_rule_group(workspace_id: str, group_id: str) -> dict:
    """
    Delete a review rule group and all rules within it (POST /review/rule_group/delete).
    """
    client.review.delete_group(workspace_id=workspace_id, group_id=group_id)
    return {"deleted_group_id": group_id}


# ===========================================================================
# RESOURCE TOOLS — Review Rules  (endpoints 40–41)
# ===========================================================================

@mcp.tool()
def docflow_update_review_rule(
    workspace_id: str,
    rule_id: str,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    category_ids: Optional[list[str]] = None,
    risk_level: Optional[int] = None,
    referenced_fields: Optional[list[dict]] = None,
) -> dict:
    """
    Update an existing review rule's prompt or configuration (POST /review/rule/update).

    Use to refine rule prompts based on review results, or adjust risk levels.
    risk_level: 10=high, 20=medium, 30=low.
    """
    client.review.update_rule(
        workspace_id=workspace_id, rule_id=rule_id,
        name=name, prompt=prompt, category_ids=category_ids,
        risk_level=risk_level, referenced_fields=referenced_fields,
    )
    return {"updated_rule_id": rule_id}


@mcp.tool()
def docflow_delete_review_rule(workspace_id: str, rule_id: str) -> dict:
    """
    Delete a review rule (POST /review/rule/delete).
    """
    client.review.delete_rule(workspace_id=workspace_id, rule_id=rule_id)
    return {"deleted_rule_id": rule_id}


# ===========================================================================
# RESOURCE TOOLS — Review Tasks  (endpoints 43–46)
# ===========================================================================

@mcp.tool()
def docflow_get_review_result(workspace_id: str, task_id: str) -> dict:
    """
    Get the current result of a review task without waiting (POST /review/task/result).

    Use to check status of a previously submitted review, or for manual polling.
    status: 0=pending, 1=pass, 2=failed, 3=reviewing, 4=not_pass, 7=recognition_failed.
    """
    return _s(client.review.get_task_result(workspace_id=workspace_id, task_id=task_id))


@mcp.tool()
def docflow_delete_review_task(workspace_id: str, task_ids: list[str]) -> dict:
    """
    Delete review tasks (POST /review/task/delete).
    """
    client.review.delete_task(workspace_id=workspace_id, task_ids=task_ids)
    return {"deleted_count": len(task_ids)}


@mcp.tool()
def docflow_retry_review_task(workspace_id: str, task_id: str) -> dict:
    """
    Retry an entire review task that failed or produced unexpected results (POST /review/task/retry).
    All rules in the task will be re-evaluated.
    """
    client.review.retry_task(workspace_id=workspace_id, task_id=task_id)
    return {"retried_task_id": task_id}


@mcp.tool()
def docflow_retry_review_rule(
    workspace_id: str,
    task_id: str,
    rule_id: str,
) -> dict:
    """
    Retry evaluation of a single rule within a review task (POST /review/task/rule/retry).

    Use when one specific rule failed or needs re-evaluation after updating its prompt,
    without re-running all other rules in the task.
    """
    client.review.retry_task_rule(workspace_id=workspace_id, task_id=task_id, rule_id=rule_id)
    return {"retried_task_id": task_id, "retried_rule_id": rule_id}


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
