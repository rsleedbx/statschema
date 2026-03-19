---
name: docs-writing-style
description: Writing style rules for Zerobus project documentation. Use when writing or editing any markdown in docs/, including FAQ files, implementation notes, and learnings. Enforces concise, fact-first documentation style.
---

# Docs Writing Style

## Core rules

### 1. State the fact — nothing more

Write what is true. Do not explain why it is convenient, label it a workaround, or note what it replaces.

**Good:**
> On Azure, the workspace ID is directly parseable from the hostname.

**Bad:**
> On Azure, the workspace ID is directly parseable from the hostname itself — no API call needed.

The second half adds nothing. If the ID is in the hostname, of course no API call is needed.

---

### 2. Do not restate the same fact in different words

If the first sentence already states the fact, do not add a clause or sentence that says the same thing differently.

**Bad:**
> The connector does not support writing to storage secured through a private endpoint. If your workspace storage is secured behind a private endpoint, Zerobus cannot be used until support is added.

The second sentence repeats the first with extra words.

**Good:**
> The connector does not support writing to storage secured through a private endpoint.

---

### 3. If you need X, here is how

For how-to content, frame it as a direct action. Do not explain why the user might not know something or apologise for the documentation.

**Bad:**
> The Databricks docs do not explicitly tell you how to look up the region. This is the most common source of ambiguity.

**Good:**
> To find the region, use one of the methods below.

---

### 3a. Explanatory context: descriptive, not imperative

When annotating an example — explaining what a value means, why a transformation exists, or what a field contains — use descriptive tone, not imperative. Imperative implies the reader must act; descriptive explains what the code already does.

This matters especially when the explanation sits between two examples: an imperative reads as an instruction to write code; a descriptive reads as context for code already shown.

**Bad — imperative implies the reader must do this themselves:**
> Strip the trailing letter from `default_zone`: `us-west-2a` → `us-west-2`

A reader seeing this before the "Construct the endpoint" step may write their own stripping code, not realising step 3 handles it.

**Good — descriptive explains what the commands above already did:**
> `default_zone` is an availability zone (`us-west-2a`); the region is the value without the trailing letter (`us-west-2`). The commands above handle this.

---

### 4. No editorialising about external systems

Do not describe limitations as "significant", call approaches "workarounds", or note that something is "not officially documented". State what works and how.

**Bad:**
> This is a significant constraint for customers in regulated industries.

**Good:**
> If your workspace storage is secured behind a private endpoint, Zerobus cannot be used with that workspace.

---

### 5. Separate cloud-specific content into per-cloud sections

When commands, URLs, or steps differ between clouds (AWS, Azure, GCP), give each cloud its own self-contained section. Do not mix cloud-specific content into a single section with conditional phrases like "on Azure, use…" or "except on GCP…".

A reader using one cloud should be able to copy-paste their section without reading or discarding content for other clouds.

**Bad — mixed content the reader must filter:**

```markdown
## Construct the endpoint

For AWS: `https://<id>.zerobus.<region>.cloud.databricks.com`
For Azure: `https://<id>.zerobus.<region>.azuredatabricks.net`
For GCP: `https://<id>.zerobus.<region>.gcp.databricks.com`

Note: on Azure the workspace ID is in the hostname; on AWS use ?o= from the browser URL;
on GCP use the CLI.
```

**Good — one section per cloud, each self-contained:**

```markdown
## Construct the endpoint

### AWS

Workspace ID: from the browser URL after `?o=`
```
ZEROBUS_ENDPOINT="https://${WORKSPACE_ID}.zerobus.${REGION}.cloud.databricks.com"
```

### Azure

Workspace ID: digits between `adb-` and the first `.` in the hostname
```
ZEROBUS_ENDPOINT="https://${WORKSPACE_ID}.zerobus.${REGION}.azuredatabricks.net"
```

### GCP

Workspace ID: from the browser URL after `?o=`
```
ZEROBUS_ENDPOINT="https://${WORKSPACE_ID}.zerobus.${REGION}.gcp.databricks.com"
```
```

Each section contains everything a reader on that cloud needs. Nothing to discard.

**Instructions that apply to all clouds belong above the cloud selector, not repeated inside each section.**

If a note, prerequisite, or caveat is identical for every cloud, place it once before the cloud sections. Do not copy it to the end of each cloud section.

**Bad — same note duplicated at the end of each cloud section:**

```markdown
### AWS
... steps ...
> To use a named profile: CLI adds `--profile <name>`; Python SDK uses `WorkspaceClient(profile="<name>")`.

### Azure
... steps ...
> To use a named profile: CLI adds `--profile <name>`; Python SDK uses `WorkspaceClient(profile="<name>")`.
```

**Good — stated once, above the cloud selector:**

```markdown
> To use a named profile in the examples below: CLI adds `--profile <name>`; Python SDK uses `WorkspaceClient(profile="<name>")`.

Select your cloud: [AWS](#aws) · [Azure](#azure)

### AWS
... steps ...

### Azure
... steps ...
```

---

### 6a. Always include `--profile DEFAULT` in CLI commands

Always include `--profile DEFAULT` (or `-p DEFAULT`) in every `databricks` CLI command. This makes the target workspace explicit and prevents the CLI from silently picking up a different host from `databricks.yml`, `DATABRICKS_HOST`, or other env vars.

Replace `DEFAULT` with the actual profile name when targeting a different workspace. Add this note once at the top of the doc:

> Replace `DEFAULT` with your profile name if using a named profile in `~/.databrickscfg`.

**Bad — no profile flag; silently uses whatever host the CLI resolves:**

```bash
databricks api get /api/2.0/clusters/list-zones | jq -r '.default_zone'
```

**Good — explicit profile; predictable target workspace:**

```bash
databricks api get /api/2.0/clusters/list-zones --profile DEFAULT | jq -r '.default_zone'
```

---

### 6. Multiple methods: label consistently and keep the same order throughout

When a task can be done in more than one way (e.g. CLI and Python SDK), show all methods, label each clearly, and use the **same labels in the same order** every time that choice appears in the doc.

**Standard order for Zerobus docs:**
1. CLI (`databricks` CLI)
2. Python SDK

Every section that offers both options must show them in that order. Do not show Python first in one section and CLI first in another.

**What qualifies as each method:**

| Label | Requires | Code must use |
|---|---|---|
| **CLI:** | `databricks` CLI, `jq` | `databricks` commands, shell pipes, `jq` |
| **Python SDK:** | `pip install databricks-sdk` | `WorkspaceClient` and its methods directly — no `subprocess`, no shell pipes |

**`subprocess.check_output(['databricks', 'api', 'get', ...])` is not "Python SDK".** It calls the CLI from Python and still requires the CLI installed. If you find yourself calling `subprocess` to invoke a `databricks` CLI command inside a "Python SDK" block, replace it with the equivalent SDK method:

| CLI call | SDK equivalent |
|---|---|
| `databricks api get /api/2.0/clusters/list-zones --profile DEFAULT` | `WorkspaceClient(profile="DEFAULT").clusters.list_zones()` |
| `databricks api get /api/2.0/preview/scim/v2/Me --profile DEFAULT` (for workspace ID) | `WorkspaceClient(profile="DEFAULT").get_workspace_id()` |

**Bad — subprocess inside a "Python SDK" block:**

```markdown
**Python SDK:**
```python
import subprocess, json
out = subprocess.check_output(['databricks', 'api', 'get', '/api/2.0/clusters/list-zones'])
region = json.loads(out)['default_zone'][:-1]
```
```

This still requires the CLI. The label is wrong.

**Good — pure SDK, no subprocess:**

```markdown
**Python SDK:**
```bash
python3 -c "from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile=\"DEFAULT\").clusters.list_zones().default_zone[:-1])"
```
```

**Bad — inconsistent labelling and order:**

```markdown
## Get workspace ID

Using Python:
python -c "...WorkspaceClient(profile="DEFAULT").get_workspace_id()..."

## Get region

Run this CLI command:
databricks api get /api/2.0/clusters/list-zones --profile DEFAULT
```

**Good — consistent label and order:**

```markdown
## Get workspace ID

**CLI:**
databricks ... --profile DEFAULT

**Python SDK:**
python3 -c "from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile=\"DEFAULT\").get_workspace_id())"

## Get region

**CLI:**
databricks api get /api/2.0/clusters/list-zones --profile DEFAULT | jq -r '.default_zone[:-1]'

**Python SDK:**
python3 -c "from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile=\"DEFAULT\").clusters.list_zones().default_zone[:-1])"
```

---

---

### 7. Test every cut-and-paste example before publishing

Every code example marked as runnable must be tested in a terminal before the doc is saved. "Runnable" means any block in a `bash` or `shell` fence, and any `python3 << 'EOF' ... EOF` heredoc.

**Testing methodology:**

1. **Run the exact block as written.** Copy the example verbatim from the markdown source and paste it into a terminal. Do not mentally simulate — execute it.
2. **Verify the output matches the comment.** If the block ends with `# → <expected output>`, confirm the actual output matches that form (values will differ per workspace; structure must match).
3. **Check each tool is available.** Before using a CLI tool (`jq`, `databricks`, `python3`, `sed`), confirm it is installed (`which <tool>`). If a tool is not universally available, note the prerequisite.
4. **Prefer purpose-built tools over ad-hoc parsing.**
   - Use `jq` for JSON field extraction, not `grep`, `sed`, or `python3 -c "import json..."` pipes.
   - Use `sed` for URL/string pattern matching where `jq` does not apply.
   - Use `python3` SDK calls for values only the SDK exposes (e.g. `get_workspace_id()`).
5. **Fix the doc, not the terminal.** If a command fails, update the example in the doc. Do not document a workaround in a comment.

**What counts as a test failure:**
- Non-zero exit code
- Command not found
- Output structure does not match the `# →` comment
- A parsing step silently returns empty string

---

## Quick checklist before saving a doc edit

- [ ] Does every sentence state a fact or an action?
- [ ] Is any fact stated twice in different words? Remove the second.
- [ ] Does any sentence explain why the reader might not know something? Remove it.
- [ ] Does any explanatory annotation between examples use imperative tone ("Strip…", "Convert…", "Remove…")? Rewrite as descriptive ("The commands above strip…", "The value is…").
- [ ] Does any sentence label something a "workaround", "constraint", or "not documented"? Rewrite as a plain fact.
- [ ] Does any section mix cloud-specific commands or steps? Split into per-cloud sections.
- [ ] Is any note or prerequisite repeated identically at the end of each cloud section? Move it once to above the cloud selector.
- [ ] Do any `databricks` CLI commands omit `--profile DEFAULT`? Add it.
- [ ] Do any `WorkspaceClient()` calls omit `profile="DEFAULT"`? Add it.
- [ ] Do any `python3 -c "...WorkspaceClient(profile=\"DEFAULT\")..."` one-liners use double quotes as the outer shell string? Change to single-quote outer: `python3 -c '...WorkspaceClient(profile="DEFAULT")...'`.
- [ ] When multiple methods are shown (CLI, Python SDK), are they labelled consistently and in the same order (CLI first, then Python SDK) throughout the doc?
- [ ] Do any "Python SDK" blocks call `subprocess` to invoke a `databricks` CLI command? Replace with the SDK equivalent method.
- [ ] Are multi-line Python examples wrapped in a `python3 << 'EOF' ... EOF` heredoc so they can be pasted directly into a terminal?
- [ ] Has every runnable code block been executed in a terminal and produced output matching its `# →` comment?
- [ ] Does every CLI example use `jq` for JSON field extraction rather than `grep`, `sed`, or inline Python?
