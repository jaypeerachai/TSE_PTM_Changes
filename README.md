# Replication Package

## 🧠📦 When AI Models Become Dependencies: Studying the Evolution of Pre-Trained Model Reuse in Downstream Software Systems

This repository contains the replication package for a study submitted to IEEE Transactions on Software Engineering (TSE) on how pre-trained models (PTMs) evolve as dependencies in downstream software projects.

This package provides a concise overview of our appendix for dataset construction, artifacts, scripts, and data needed to support replication of the study.

> [!TIP]
> If you are new to this repository, start from the **Repository structure** and **Reproducing the study** sections below.

---

## 📝 Abstract (short version)

Software systems increasingly depend on pre-trained models (PTMs), not just traditional code libraries. However, it remains unclear how these PTM dependencies evolve over time and whether they follow the same patterns as third-party libraries. In this work, we conduct a large-scale empirical study of PTM evolution and systematically compare it with traditional library evolution.

Using 4,988 releases from 323 GitHub repositories, we find that PTM change behavior differs fundamentally from library change behavior in terms of change frequency and documented rationale.

---

## 🗂️ What is in this package?

This package combines:

- **Online Appendix**
- **Data analysis evidences**
- **Data artifacts**
- **Database snapshots**
- **Source codes**

---

## 🧭 Repository structure

```text
.
├── README.md
├── online_appendix-dataset_construction.pdf
├── 📊 data_analysis/
│   ├── result_figures/
│   ├── rq1_migration_manual_validation_ptm.xlsx
│   ├── rq2_annotation_lib.xlsx
│   ├── rq2_annotation_ptm.xlsx
│   └── ...
├── 📁 data_files/
│   ├── final_repo_release_pairs.csv
│   ├── library_snapshots.csv
│   ├── release_line_library_change_events_after_validation.csv
│   ├── release_line_library_change_overview_after_validation.csv
│   └── ...
├── 🗄️ database/
│   ├── README.md              # Full schema documentation (Zenodo)
│   └── data_dictionary.md     # Table descriptions
└── 🔧 src/
    ├── requirements.txt
    ├── 📚 libs/               # Library collection & change detection
    ├── 🤖 ptms/               # PTM collection & change detection
    ├── 📈 stat_tests/         # Statistical tests
    └── ⚙️ utils/              # Shared utilities
```

### 📁 Folder guide

- **[online_appendix-dataset_construction.pdf](online_appendix-dataset_construction.pdf)**: detailed documentation of the dataset construction process, including data collection, filtering, and validation steps.
- **[data_analysis/](data_analysis/)**: spreadsheets used for manual validation and qualitative coding.
- **[data_files/](data_files/)**: exported CSV artifacts used in the final analysis.
- **[database/](database/)**: 🗄️ schema documentation and Zenodo archival database reference (large SQL dumps hosted on Zenodo at DOI: https://doi.org/10.5281/zenodo.19312247).
- **[src/ptms/](src/ptms/)**: PTM-focused data collection and release-line PTM change detection.
- **[src/libs/](src/libs/)**: baseline library collection and library change detection.
- **[src/stat_tests/](src/stat_tests/)**: scripts for non-parametric statistical tests.
- **[src/utils/](src/utils/)**: shared utilities for DB access, schema names, API setup, and helpers.

---

## ⚙️ Environment setup

🛠️ Setup is lightweight and should take only a few minutes.

### 🐍 1) Python

We recommend Python **3.10+**.

### 📥 2) Install dependencies

From the repository root:

```bash
cd src
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 🔐 3) Database and API configuration

Most scripts read credentials from a `config.json` file in `src/` (or from a path set with `MODEL_UPDATE_DB_CONFIG` for DB settings).

A minimal template is:

```json
{
	"db": {
		"host": "localhost",
		"port": 3306,
		"user": "<your_user>",
		"password": "<your_password>",
		"database": "model_changes",
		"folder": "raw_data"
	},
	"gh": {
		"api": ["<github_token_1>", "<github_token_2>"]
	},
	"hf": {
		"api": ["<huggingface_token_1>"]
	}
}
```

#### Obtaining API tokens

- **GitHub Token**: Visit [github.com/settings/tokens](https://github.com/settings/tokens), create a new personal access token (classic) with `repo` and `read:org` scopes. (Note: we recommend multiple tokens to avoid rate limits.)
- **Hugging Face Token**: Visit [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), create a new read-only API token.

> Note: some collection scripts are interactive and may ask you to pick a token at runtime.

> [!WARNING]
> API data may change over time (deleted repositories, updated metadata, rate limits). For paper-level replication, please use the released artifacts in [data_files/](data_files/).

---

## 🔁 Reproducing the study

You can reproduce the study by combining released artifacts with executable scripts in this repository.

If you want a quick start, use files in `data_files/` directly (e.g., `final_repo_release_pairs.csv`, `library_snapshots.csv`, `release_line_library_change_events_after_validation.csv`).

For end-to-end reproduction, follow this high-level pipeline:

1. **Collect downstream repositories reusing PTM with metadata and development history (PTM pipeline)** using scripts in `src/ptms/collect_*.py`.
	- Example collectors: `collect_releases.py`, `collect_tags.py`, `collect_commits.py`, `collect_issue_comments.py`, `collect_pull_requests.py`.
	- This stage also includes repository/release filtering and static analysis support (e.g., `preliminary_filter_repos.py`, `release_filtering.py`, `static_analysis_fp_mapping.py`) to refine valid candidates before downstream extraction.
	- These scripts populate core database tables used by later extraction and change-detection stages.

2. **Extract release lines and release/file mappings** with scripts such as `extract_release_lines.py`, `extract_file_to_releases.py`, and `extract_reached_branches.py`.
	- This stage organizes releases into release lines and maps reused files to those releases.

3. **Build PTM snapshots across files and releases** using `extract_files_to_model_snapshots.py` and `extract_file_to_model_snapshots_for_releases.py`.
	- Output snapshots capture PTM identifiers/names per reused file and per release, which become inputs for PTM change detection.

4. **Detect PTM changes per release line** with `detect_release_line_model_changes.py`.
	- This script computes added/removed/migrated PTM instances between adjacent releases and stores release-line-level PTM change records.

5. **Construct the library baseline and detect library changes** with `src/libs/data_collection.py` and `src/libs/change_detection.py`.
	- `data_collection.py` parses dependency files (e.g., `requirements.txt`, `Pipfile`, `pyproject.toml`, `environment.yml`) from release snapshots.
	- `change_detection.py` compares adjacent release snapshots and produces events such as added/removed/updated dependencies.

6. **Run statistical tests** in `src/stat_tests/`.
	- Example scripts: `rq1_wilcoxon_test.py` and `rq1_mannwhitney_test.py`.
	- These scripts reproduce the non-parametric comparisons reported in the paper.

Because data collection is API- and DB-dependent, we recommend running scripts in small batches and validating intermediate tables before moving to the next step.

---

## 📌 Notes on reproducibility

- API snapshots can drift over time (deleted/private repos, updated metadata, rate limits).
- Re-running collection today may not produce byte-identical raw data.
- The provided released artifacts should be treated as the fixed reference for paper-level replication.

---

## 👥 Authors

- Peerachai Banyongrakkul  
- Mansooreh Zahedi 
- Christoph Treude 
- Haoyu Gao 
- Patanamon Thongtanunam

---

## 📚 Citation

If you use this package, please cite our paper:

```text

```

---

