# Docflow MCP Server

MCP Server for [Docflow](https://docs-docflow.textin.com/docflow/cn/00-overview/intro) — TextIn's document automation platform. Enables LLM agents to classify documents, extract fields/tables/stamps, and run rule-based compliance review through 41 MCP tools.

## Features

- **Document classification** — auto-classify PDFs, images, Word, and Excel files into user-defined categories
- **Field extraction** — extract structured fields, table rows, handwriting, and stamps using LLM/VLM models
- **Compliance review** — run AI-powered rule evaluation with risk levels and detailed reasoning
- **Two-layer tool design**:
  - 6 **workflow tools** — high-level composite operations for common pipelines (upload → extract → review)
  - 35 **resource tools** — one tool per API endpoint for fine-grained configuration and edge cases

## Installation

```bash
pip install git+https://github.com/your-org/docflow-mcp.git
```

Or install in editable mode for development:

```bash
git clone https://github.com/your-org/docflow-mcp.git
cd docflow-mcp
pip install -e .
```

## Configuration

Set environment variables before starting the server:

```bash
export DOCFLOW_APP_ID=your_app_id
export DOCFLOW_SECRET_CODE=your_secret_code

# Optional: override API host (default: https://docflow.textin.com)
export DOCFLOW_HOST=https://docflow.textin.com
```

Get your `APP_ID` and `SECRET_CODE` from the [Docflow console](https://docflow.textin.com).

## Running

### Stdio (for MCP clients)

```bash
# Via installed command
docflow-mcp

# Or via Python module
python -m docflow_mcp
```

### Claude Code Integration

Add to your Claude Code MCP settings (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "docflow": {
      "command": "docflow-mcp",
      "env": {
        "DOCFLOW_APP_ID": "your_app_id",
        "DOCFLOW_SECRET_CODE": "your_secret_code"
      }
    }
  }
}
```

Or if using `python -m`:

```json
{
  "mcpServers": {
    "docflow": {
      "command": "python",
      "args": ["-m", "docflow_mcp"],
      "env": {
        "DOCFLOW_APP_ID": "your_app_id",
        "DOCFLOW_SECRET_CODE": "your_secret_code"
      }
    }
  }
}
```

## Tool Reference

### Workflow Tools (6)

High-level tools that hide polling, multi-step logic, and upload routing.

| Tool | Description |
|------|-------------|
| `docflow_get_or_create_workspace` | Find workspace by name, or create if not found (idempotent) |
| `docflow_list_categories` | List all enabled categories with field configurations |
| `docflow_create_category` | Create category with fields and sample file |
| `docflow_upload_and_extract` | Upload files (list or directory) and wait for extraction results |
| `docflow_setup_review_rules` | Create review rule repo with groups and rules (idempotent) |
| `docflow_run_review` | Submit review task and wait for rule-by-rule results |

#### `docflow_upload_and_extract` — Upload Routing

Automatically selects upload mode based on file count:
- **≤ 3 files** → synchronous upload (results returned immediately)
- **> 3 files** → asynchronous upload + polling until all files complete

Accepts either:
- `file_paths`: explicit list of local file paths
- `directory`: auto-scans for supported files (PDF, JPG, PNG, DOC, DOCX, XLS, XLSX, OFD, TXT)

#### `docflow_setup_review_rules` — Rule Structure

```
Rule Repository
└── Rule Group (e.g. "金额审核")
    └── Rule (name, prompt, category_ids, risk_level)
        risk_level: 10=high, 20=medium, 30=low
```

---

### Resource Tools (35)

Thin wrappers covering every CRUD endpoint, organized by resource.

#### Files

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_fetch_files` | GET /file/fetch | Query processed files, filter by status/category |
| `docflow_update_file` | POST /file/update | Update file metadata or verification status |
| `docflow_delete_files` | POST /file/delete | Permanently delete files by task_id or batch_number |
| `docflow_extract_fields` | POST /file/extract_fields | Re-extract specific fields without re-uploading |
| `docflow_retry_files` | POST /file/retry | Retry processing for failed files |
| `docflow_amend_category` | POST /file/amend_category | Correct misclassified file and trigger re-extraction |

#### Workspaces

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_list_workspaces` | GET /workspace/list | List all accessible workspaces |
| `docflow_get_workspace` | GET /workspace/get | Get workspace details and statistics |
| `docflow_update_workspace` | POST /workspace/update | Update name, description, auth scope, or callback URL |
| `docflow_delete_workspace` | POST /workspace/delete | Delete workspace and all contents (permanent) |

#### Categories

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_update_category` | POST /category/update | Update category name, model, prompt, or enabled status |
| `docflow_delete_category` | POST /category/delete | Delete category (existing file data is preserved) |

#### Category Tables

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_list_category_tables` | GET /category/tables/list | List table configs (for extracting structured rows) |
| `docflow_add_category_table` | POST /category/tables/add | Add table field group (e.g. invoice line items) |
| `docflow_update_category_table` | POST /category/tables/update | Update table name or column definitions |
| `docflow_delete_category_tables` | POST /category/tables/delete | Delete table configs |

#### Category Fields

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_list_category_fields` | GET /category/fields/list | List fields with field_ids (needed for rule references) |
| `docflow_add_category_fields` | POST /category/fields/add | Add fields to existing category |
| `docflow_update_category_field` | POST /category/fields/update | Update field name, description, or extraction prompt |
| `docflow_delete_category_fields` | POST /category/fields/delete | Delete fields from category |

#### Category Samples

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_add_category_samples` | POST /category/sample/upload | Add sample files to improve classification (3–5 recommended) |
| `docflow_list_category_samples` | GET /category/sample/list | List all sample files for a category |
| `docflow_delete_category_samples` | POST /category/sample/delete | Remove low-quality samples |

#### Review Rule Repositories

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_list_review_repos` | GET /review/rule_repo/list | List all rule repositories in workspace |
| `docflow_get_review_repo` | GET /review/rule_repo/get | Get repo with all groups and rules |
| `docflow_update_review_repo` | POST /review/rule_repo/update | Rename rule repository |
| `docflow_delete_review_repo` | POST /review/rule_repo/delete | Delete repo and all its rules (permanent) |

#### Review Rule Groups

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_update_review_rule_group` | POST /review/rule_group/update | Rename rule group |
| `docflow_delete_review_rule_group` | POST /review/rule_group/delete | Delete group and all its rules |

#### Review Rules

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_update_review_rule` | POST /review/rule/update | Update rule prompt, categories, or risk level |
| `docflow_delete_review_rule` | POST /review/rule/delete | Delete a single rule |

#### Review Tasks

| Tool | Endpoint | Description |
|------|----------|-------------|
| `docflow_get_review_result` | POST /review/task/result | Get current review status without waiting |
| `docflow_delete_review_task` | POST /review/task/delete | Delete review tasks |
| `docflow_retry_review_task` | POST /review/task/retry | Retry entire review task |
| `docflow_retry_review_rule` | POST /review/task/rule/retry | Retry a single rule without re-running others |

---

## Typical Workflow

```
1. docflow_get_or_create_workspace  →  workspace_id
2. docflow_list_categories          →  check existing categories
3. docflow_create_category (×N)     →  category_id per document type
4. docflow_upload_and_extract       →  files[], task_ids[]
5. docflow_setup_review_rules       →  repo_id
6. docflow_run_review               →  status, groups[rule results]
```

## API Coverage

44 of 46 Docflow API endpoints are covered.

Intentionally omitted:
- `GET /auth/token` — generates short-lived token for iframe embedding, not agent use
- `GET /category/sample/download` — returns binary file content, not useful for agents

Internally called (no separate tool needed):
- `POST /file/upload`, `/file/upload/sync` → used inside `docflow_upload_and_extract`
- `POST /workspace/create` → used inside `docflow_get_or_create_workspace`
- `POST /review/rule_repo/create`, `/rule_group/create`, `/rule/create` → used inside `docflow_setup_review_rules`
- `POST /review/task/submit` → used inside `docflow_run_review`

## Requirements

- Python ≥ 3.10
- Network access to `docflow.textin.com`
- Docflow account with valid `APP_ID` and `SECRET_CODE`

## License

MIT
