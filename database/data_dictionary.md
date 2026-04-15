# 🗄️ SQL Database Snapshot (Zenodo)

This README documents the relational schema released with the replication package of:

**When AI Models Become Dependencies: Studying the Evolution of Pre-Trained Model Reuse in Downstream Software Systems**.

The database integrates:

- downstream GitHub repository metadata,
- development history (commits/issues/pull requests/discussions/releases/tags),
- PTM static-analysis evidence,
- file/release PTM snapshots,
- and release-line PTM change outcomes.

## 📋 Data Dictionary

| table_name | granularity | description |
|---|---|---|
| `models` | model | Canonical PTM records (HF identity, author, popularity, timestamps, pipeline/library metadata). |
| `owners` | GitHub owner | Repository owner entities (user/org), unique by `owner_id`. |
| `downstream_repos` | repository | Core set of downstream repositories analyzed for PTM reuse. |
| `branches` | repository-branch | Branch metadata and tip-commit linkage used for release-line inference. |
| `commits` | repository-commit | Commit history with message, author/committer, verification, and diff stats. |
| `commit_comments` | commit-comment | Review/discussion comments attached to commits with file/line context. |
| `discussions` | repository-discussion | GitHub Discussions metadata, content, answer status, and reactions. |
| `downstream_repo_info` | repository snapshot | Repository-level metadata (language, stars, forks, visibility, license, etc.). |
| `issues` | repository-issue | Issues (and issue-like PR entries) with lifecycle, labels, assignees, and reactions. |
| `issue_comments` | issue-comment | Comment-level issue discussion data with author/timestamp/reactions. |
| `pull_requests` | repository-PR | Pull request lifecycle and merge/diff/review statistics. |
| `pull_requests_to_commits` | PR-commit link | Many-to-many mapping between pull requests and commits. |
| `releases` | repository-release | Release metadata (tag, timestamps, body, author, assets, links). |
| `release_line_model_changes` | release-pair-in-line | PTM change outcomes between adjacent releases in each release line (added/removed/migrated + adoption flags). |
| `release_lines` | release-in-line | Assignment of releases to inferred release lines with first-parent order (`fp_index`). |
| `reused_files` | repository-file | Candidate downstream files containing PTM reuse evidence. |
| `reused_files_model_snapshots` | file-release snapshot | PTM snapshot extracted per file per release (model IDs/names + call-site evidence). |
| `reused_files_to_releases` | file-release link | Mapping of reused files to release snapshots with path/SHA/size/download fields. |
| `signatures` | signature pattern | PTM usage signatures (import/call patterns) used in downstream repository collection by GitHub Search Code and static matching. |
| `reused_files_signature_matches` | file-signature match | JSON-backed static-analysis evidence for signature hits in files during data collection at recent version of repositories. |
| `reused_files_to_models` | file-model usage | Normalized file-to-model linkage with matched signature and location fields during data collection at recent version of repositories. |
| `signatures_to_reused_files` | signature-file link | Mapping between signatures and reused files during data collection at recent version of repositories. |
| `tags` | repository-tag | Tag metadata and linkage to commits/releases. |
