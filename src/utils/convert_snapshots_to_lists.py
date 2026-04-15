"""
Utility code to convert raw snapshot records into grouped lists for more efficient storage and querying in DB.
"""
from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF

import pandas as pd
import json as _json

# Try a faster JSON encoder; fall back to stdlib if unavailable.
try:
    import orjson
    def jdump(obj):  # returns str
        return orjson.dumps(obj).decode("utf-8")
except Exception:
    def jdump(obj):
        return _json.dumps(obj, separators=(",", ":"))  # compact

CHUNK_SIZE = 5000  # safe default for executemany

def _safe_enum(name, default):
    try:
        return getattr(DBT, name).value
    except Exception:
        return default



if __name__ == "__main__":
    db = DatabaseConfig()
    db.create_db_connection()

    MODE = "os"
    SOURCE_TABLE = _safe_enum("FILE_MODEL_SNAPSHOTS", "reused_files_model_snapshots")
    TARGET_TABLE = _safe_enum("FILE_MODEL_SNAPSHOTS_LISTS", "reused_files_model_snapshots_lists")

    # Load snapshots
    snapshots = db.select_from_db(
        SOURCE_TABLE,
        columns="*"
    )
    print(f"[{MODE.upper()}] Found {len(snapshots)} snapshots in table: {SOURCE_TABLE}")

    if not snapshots:
        raise SystemExit("No snapshots found. Exiting.")

    # dataFrame
    df = pd.DataFrame(snapshots)


    # ensure dtypes
    for col in ("repo_id", "file_id", "commit_id", "commit_file_id", "model_count", "parse_status"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], downcast="integer")

    # Base aggregated columns (lists)
    agg_cols = [
        "model_id", "model_name", "call_line_number", "end_call_line_number",
        "param_line_number", "end_param_line_number", "import_origin",
        "call_signature", "signature_id"
    ]

    include_model_source = "model_source" in df.columns
    if include_model_source:
        agg_cols.append("model_source")

    # group & aggregate
    df_grouped = (
        df.groupby(["repo_id", "file_id", "commit_id", "commit_file_id"], sort=False, as_index=False)
          .agg({"model_count": "first", **{c: list for c in agg_cols}})
    )

    print(df_grouped.head())
    print(f"Grouped DataFrame shape: {df_grouped.shape}")

    # prepare dynamic insert (add model_source column only when present)
    # Build the column list in the same order as rows
    insert_columns = [
        DBF.FileModelSnapshotsLists.REPO_ID,
        DBF.FileModelSnapshotsLists.FILE_ID,
        DBF.FileModelSnapshotsLists.COMMIT_ID,
        DBF.FileModelSnapshotsLists.COMMIT_FILE_ID,
        DBF.FileModelSnapshotsLists.MODEL_COUNT,
        DBF.FileModelSnapshotsLists.MODEL_ID,
        DBF.FileModelSnapshotsLists.MODEL_NAME,
        DBF.FileModelSnapshotsLists.CALL_LINE_NUMBER,
        DBF.FileModelSnapshotsLists.END_CALL_LINE_NUMBER,
        DBF.FileModelSnapshotsLists.PARAM_LINE_NUMBER,
        DBF.FileModelSnapshotsLists.END_PARAM_LINE_NUMBER,
        DBF.FileModelSnapshotsLists.IMPORT_ORIGIN,
        DBF.FileModelSnapshotsLists.CALL_SIGNATURE,
        DBF.FileModelSnapshotsLists.SIGNATURE_ID,
        DBF.FileModelSnapshotsLists.PARSE_STATUS,
        DBF.FileModelSnapshotsLists.PARSE_ERROR,
    ]

    placeholders = ",".join(["%s"] * len(insert_columns))
    columns_sql = ",".join(insert_columns)

    insert_sql = f"""
        INSERT INTO {TARGET_TABLE} (
            {columns_sql}
        ) VALUES ({placeholders})
    """

    # Build rows once; JSON-encode list fields
    rows = []
    it = df_grouped.itertuples(index=False)
    for rec in it:
        base_vals = [
            int(rec.repo_id),
            int(rec.file_id),
            int(rec.commit_id),
            int(rec.commit_file_id),
            int(rec.model_count),
            jdump(rec.model_id),
            jdump(rec.model_name),
            jdump(rec.call_line_number),
            jdump(rec.end_call_line_number),
            jdump(rec.param_line_number),
            jdump(rec.end_param_line_number),
            jdump(rec.import_origin),
            jdump(rec.call_signature),
            jdump(rec.signature_id),
            int(rec.parse_status),
            rec.parse_error if rec.parse_error is None else str(rec.parse_error)
        ]
        if include_model_source:
            base_vals.append(jdump(rec.model_source))
        rows.append(tuple(base_vals))

    conn = db.connection
    cur = conn.cursor()
    try:
        conn.autocommit = False
        for i in range(0, len(rows), CHUNK_SIZE):
            batch = rows[i:i+CHUNK_SIZE]
            cur.executemany(insert_sql, batch)
        conn.commit()
        print(f"[{MODE.upper()}] Inserted {len(rows)} grouped records into {TARGET_TABLE}.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
