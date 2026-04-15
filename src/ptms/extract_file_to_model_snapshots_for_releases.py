"""
Extract PTM model snapshots for reused files at release versions.
reuse the same static analysis as we did before
"""

import traceback
import json
from typing import Optional, List, Dict, Any

from static_analysis_fp_mapping import NamespaceResolver
from utilities.HFConfig import HuggingFaceConfig
from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utilities.RawFileConfig import RawFileConfig
from raw_snapshot_cache import RawSnapshotCache
from utils.model_snapshot_utils import (
    build_model_name_to_id,
    collect_frameworks_from_analyzer,
    extract_snapshot_occurrences_with_analyzer,
    load_signatures_for_imports,
)


def select_repos(db: DatabaseConfig | None = None) -> List[dict]:
    """
    Select repos that are ready for release-level snapshot extraction.
    """
    print("\nSelecting repos to process...")
    rows = db.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND EXISTS (
                SELECT 1
                FROM {DBT.FILES} rf
                JOIN {DBT.FILE_TO_MODEL} rfm
                    ON rf.{DBF.Files.ID} = rfm.{DBF.FileToModel.FILE_ID}
                WHERE rf.{DBF.Files.REPO_ID} =
                        {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
            )
            AND EXISTS (
                SELECT 1
                FROM {DBT.RELEASES} re
                WHERE re.{DBF.Releases.REPO_ID} =
                        {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
            )
        """,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC",
        fetch_one=False
    ) or []

    print(f"Selected {len(rows)} repos to process.\n")
    return rows

def select_file_versions(
    db: DatabaseConfig,
    repo_id: int,
) -> List[dict]:
    """
    Load file-release versions that should be analyzed for one repo.
    """
    rows = db.select_from_db(
        DBT.REUSED_FILES_TO_RELEASES.value,
        columns="*",
        where=f"""
            {DBF.ReusedFilesToReleases.REPO_ID} = {repo_id}
        """,
        # order by file id and release id to keep things deterministic
        order_by=f"{DBF.ReusedFilesToReleases.FILE_ID} ASC, {DBF.ReusedFilesToReleases.RELEASE_ID} ASC",
        fetch_one=False
    ) or []
    return rows

_LIST_FIELDS = [
    "model_id",
    "model_name",
    "call_line_number",
    "end_call_line_number",
    "param_line_number",
    "end_param_line_number",
    "import_origin",
    "call_signature",
    "signature_id",
]

def _encode_lists_as_json(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    JSON-encode the list-style fields before inserting them into MySQL.
    """
    out = dict(data)
    for f in _LIST_FIELDS:
        if f in out:
            v = out[f]
            if isinstance(v, str):
                continue
            out[f] = json.dumps(v)
    return out

def _safe_insert(db_config, table: str, data_dict: Dict[str, Any]) -> bool:
    """
    Insert one row and return a simple success flag.
    """
    try:
        db_config.insert_to_db(table, data_dict=data_dict)
        return True
    except Exception as e:
        print(f"[ERR ] insert into {table} failed: {e}")
        return False

def insert_snapshot_rows(
    db_config,
    repo_id: int,
    file_id: int,
    commit_id: Optional[int],
    commit_file_id: Optional[int],
    release_id: int,
    occs: Optional[List[dict]],
    parse_status: int,
    parse_error: Optional[str] = None,
    # existing_row: Optional[dict],
) -> None:
    """
    Insert one release-level snapshot row into the list-based output table.
    """

    if occs is not None:
        filtered = [oc for oc in occs if oc.get("model_id") is not None]

        # Keep the row order stable across runs.
        def _key(oc: dict):
            return (
                oc.get("call_line_number", 10**9),
                oc.get("param_line_number", 10**9),
                str(oc.get("model_name") or ""),
                int(oc.get("signature_id")) if oc.get("signature_id") is not None else 10**9,
            )
        filtered.sort(key=_key)

        model_count = len(filtered)

        if model_count == 0:
            payload = {
                "repo_id": repo_id,
                "file_id": file_id,
                "release_id": release_id,
                "commit_id": commit_id,
                "commit_file_id": commit_file_id,
                "model_count": 0,
                "model_id": [None],
                "model_name": [None],
                "call_line_number": [None],
                "end_call_line_number": [None],
                "param_line_number": [None],
                "end_param_line_number": [None],
                "import_origin": [None],
                "call_signature": [None],
                "signature_id": [None],
                "parse_status": parse_status,
                "parse_error": parse_error,
            }
            payload = _encode_lists_as_json(payload)
            ok = _safe_insert(db_config, DBT.FILE_MODEL_SNAPSHOTS_LISTS_RELEASES.value, payload)
            if ok:
                print(f"(snap) repo {repo_id} file {file_id} commit_file {commit_file_id}: inserted sentinel (0 models)")
            return

        # Store aligned lists so one row still keeps all occurrences for the file version.
        payload = {
            "repo_id": repo_id,
            "file_id": file_id,
            "release_id": release_id,
            "commit_id": commit_id,
            "commit_file_id": commit_file_id,
            "model_count": model_count,
            "model_id": [oc.get("model_id") for oc in filtered],
            "model_name": [oc.get("model_name") for oc in filtered],
            "call_line_number": [oc.get("call_line_number") for oc in filtered],
            "end_call_line_number": [oc.get("end_call_line_number") for oc in filtered],
            "param_line_number": [oc.get("param_line_number") for oc in filtered],
            "end_param_line_number": [oc.get("end_param_line_number") for oc in filtered],
            "import_origin": [oc.get("import_origin") for oc in filtered],
            "call_signature": [oc.get("call_signature") for oc in filtered],
            "signature_id": [oc.get("signature_id") for oc in filtered],
            "parse_status": parse_status,
            "parse_error": parse_error,
        }

        L = model_count
        for k in _LIST_FIELDS:
            if len(payload[k]) != L:
                raise ValueError(f"Length mismatch for '{k}': expected {L}, got {len(payload[k])}")

        payload = _encode_lists_as_json(payload)
        ok = _safe_insert(db_config, DBT.FILE_MODEL_SNAPSHOTS_LISTS_RELEASES.value, payload)
        if ok:
            print(f"(snap) repo {repo_id} file {file_id} commit_file {commit_file_id}: inserted {model_count} models")
        return

    payload = {
        "repo_id": repo_id,
        "file_id": file_id,
        "release_id": release_id,
        "commit_id": commit_id,
        "commit_file_id": commit_file_id,
        "model_count": 0,
        "model_id": [None],
        "model_name": [None],
        "call_line_number": [None],
        "end_call_line_number": [None],
        "param_line_number": [None],
        "end_param_line_number": [None],
        "import_origin": [None],
        "call_signature": [None],
        "signature_id": [None],
    }
    payload = _encode_lists_as_json(payload)
    ok = _safe_insert(db_config, DBT.FILE_MODEL_SNAPSHOTS_LISTS_RELEASES.value, payload)
    if ok:
        print(f"(snap) repo {repo_id} file {file_id} commit_file {commit_file_id}: inserted sentinel (0 models) [fallback]")

if __name__ == "__main__":
    print("Initializing snapshot pipeline...\n")
    db = DatabaseConfig()
    connection, cursor = db.create_db_connection()

    hf = HuggingFaceConfig()
    hf.init_huggingface_access()
    resolver = NamespaceResolver(hf._token)
    model_name_to_id: Dict[str, int] = build_model_name_to_id(db)

    raw_conf = RawFileConfig(db="model_update")
    cache = RawSnapshotCache(raw_conf)

    repos = select_repos(db)

    for repo in repos:
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        file_versions = select_file_versions(db, repo_id)
        print(f"Processing repo ID {repo_id} with {len(file_versions)} file versions...")

        for fv in file_versions:
            release_id = fv[DBF.ReusedFilesToReleases.RELEASE_ID]
            file_id = fv[DBF.ReusedFilesToReleases.FILE_ID]
            parse_status = 0
            parse_error = None

            download_url = fv.get(DBF.ReusedFilesToReleases.DOWNLOAD_URL)
            commit_id_int = fv.get(DBF.ReusedFilesToReleases.EXACT_TAG_COMMIT_ID)
            select_commit_file_query = f"""
            SELECT id
            FROM commit_files
            WHERE commit_id = %s
                AND file_name = %s
            """
            cursor.execute(select_commit_file_query, (commit_id_int, fv.get(DBF.ReusedFilesToReleases.PATH)))
            commit_file_row = cursor.fetchone()
            if not commit_file_row:
                commit_file_id = None
            else:
                commit_file_id = commit_file_row['id']

            try:
                analyzer, _raw_path, _ast_path = cache.ensure_and_build_analyzer(
                    download_url=download_url,
                    repo_id=repo_id,
                    file_id=file_id,
                    commit_file_id=commit_file_id,
                )

                frameworks = collect_frameworks_from_analyzer(analyzer)
                print(f"(file) {file_id} commit_file {commit_file_id}: imports={sorted(list(frameworks))}")
                sigs = load_signatures_for_imports(db, frameworks)
                print(f"(file) {file_id} commit_file {commit_file_id}: signatures loaded={len(sigs)}")

                occs = extract_snapshot_occurrences_with_analyzer(
                    analyzer=analyzer,
                    file_id=file_id,
                    signature_rows=sigs,
                    model_name_to_id=model_name_to_id,
                    resolver=resolver,
                )
                print(f"(file) {file_id} commit_file {commit_file_id}: occurrences found={len(occs)}")
                # Keep the AST dump as cached evidence for debugging.
                cache.dump_ast(analyzer, _ast_path)
                parse_status = 1
            except Exception as e:
                print(f"(ERR ) analyze repo {repo_id} file {file_id} commit_file {commit_file_id}: {e}")
                occs = []
                parse_status = -1
                parse_error = str(e) + "\n" + traceback.format_exc()

            insert_snapshot_rows(
                db_config=db,
                repo_id=repo_id,
                file_id=file_id,
                commit_id=commit_id_int,
                commit_file_id=commit_file_id,
                release_id=release_id,
                occs=occs,
                parse_status=parse_status,
                parse_error=parse_error,
            )
    print("\nSnapshot pipeline completed.")

    db.close_db_connection()
