"""
Validate library migrations from the candidate pairs list.

This follows the library release-line workflow after change detection.
It validates migration events, rebuilds the after-validation tables, and
then fills update evidence from the legacy commit-level table.

Migration candidates come from add/remove pairs that appear in the same
file and commit (same as our PTM change detection). We validate them in two steps: first with a curated list
of analogous library pairs from Islam et al. (2023,24), and then with a GPT-based
prompt for the remaining candidates Islam et al. (2024). If a candidate is not validated, we
turn it back into separate added and removed events.
"""

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Set, Tuple

from utils.DBConfig import DatabaseConfig

EVENT_TABLE = "release_line_library_change_events"
OVERVIEW_TABLE = "release_line_library_change_overview"
EVENT_AFTER_TABLE = "release_line_library_change_events_after_validation"
OVERVIEW_AFTER_TABLE = "release_line_library_change_overview_after_validation"
LEGACY_EVENT_TABLE = "library_change_events"
LIBPAIR_CSV = Path("libpair/analogous_pairs.csv")
VALIDATION_COLUMN = "is_valid_migration"
CLEAR_AFTER_TABLES_FIRST = True

db = None
conn = None
cur = None


def _normalize_lib_name(name: str) -> str:
    """
    Normalize library names so matching stays consistent.
    """
    return (name or "").strip().lower().replace("_", "-")


def _canon_undirected_pair(a: str, b: str) -> Tuple[str, str]:
    """
    Build one stable key for a library pair regardless of direction.
    """
    a = _normalize_lib_name(a)
    b = _normalize_lib_name(b)
    return tuple(sorted((a, b)))


def _assert_valid_sql_identifier(name: str):
    """
    Allow only safe SQL identifier names before formatting them into SQL.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(f"Invalid SQL identifier: {name}")


def _ensure_open_db_connection():
    """
    Reuse an open DB connection or create a new one if needed.
    """
    global db, conn, cur
    try:
        if cur is not None:
            cur.execute("SELECT 1")
            return
    except Exception:
        pass

    try:
        if db is not None:
            db.close_db_connection()
    except Exception:
        pass

    db = DatabaseConfig()
    conn, cur = db.create_db_connection()


def load_analogous_pairs(csv_path: Path = LIBPAIR_CSV) -> Set[Tuple[str, str]]:
    """
    Load valid migration pairs from the library pair CSV file.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    pairs: Set[Tuple[str, str]] = set()
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        headers = {h.lower(): h for h in (reader.fieldnames or [])}
        s_col = headers.get("source")
        t_col = headers.get("target")
        if not s_col or not t_col:
            raise ValueError(f"analogous_pairs.csv missing source/target columns: {reader.fieldnames}")

        for row in reader:
            s = _normalize_lib_name(row.get(s_col, ""))
            t = _normalize_lib_name(row.get(t_col, ""))
            if s and t:
                pairs.add(_canon_undirected_pair(s, t))
    return pairs


def validate_snapshot_migrations(validation_column: str = VALIDATION_COLUMN):
    """
    Mark migration events as valid or invalid using the known library pairs.
    """
    _assert_valid_sql_identifier(validation_column)
    mappings = load_analogous_pairs()

    db_v = DatabaseConfig()
    conn_v, cur_v = db_v.create_db_connection()
    try:
        # Make sure the validation column exists before updating rows.
        cur_v.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = %s
              AND column_name = %s
            """,
            (EVENT_TABLE, validation_column),
        )
        n_col = int((cur_v.fetchone() or {}).get("n") or 0)
        if n_col == 0:
            raise ValueError(f"Column '{validation_column}' not found in {EVENT_TABLE}")

        # Reset the validation flag for all migration rows first.
        cur_v.execute(
            f"UPDATE {EVENT_TABLE} SET `{validation_column}` = 0 WHERE change_type = 'migration'"
        )

        # Read all migration rows and check whether the pair is in the mapping list.
        cur_v.execute(
            f"""
            SELECT
                id, repo_id, release_line_id, prev_release_id, curr_release_id,
                changed_name, to_name, commit_sha, changed_file_path
            FROM {EVENT_TABLE}
            WHERE change_type = 'migration'
            """
        )
        rows = cur_v.fetchall() or []

        updates = []
        for r in rows:
            src = _normalize_lib_name(r.get("changed_name") or "")
            tgt = _normalize_lib_name(r.get("to_name") or "")
            is_valid = 1 if _canon_undirected_pair(src, tgt) in mappings else 0
            updates.append(
                (
                    is_valid,
                    int(r["id"]),
                    int(r["repo_id"]),
                    int(r["release_line_id"]),
                    int(r["prev_release_id"]),
                    int(r["curr_release_id"]),
                    r.get("commit_sha"),
                    r.get("changed_name"),
                    r.get("to_name"),
                    r.get("changed_file_path"),
                )
            )

        if updates:
            cur_v.executemany(
                f"""
                UPDATE {EVENT_TABLE}
                SET `{validation_column}` = %s
                WHERE id = %s
                  AND repo_id = %s
                  AND release_line_id = %s
                  AND prev_release_id = %s
                  AND curr_release_id = %s
                  AND commit_sha <=> %s
                  AND changed_name = %s
                  AND to_name <=> %s
                  AND changed_file_path <=> %s
                  AND change_type = 'migration'
                """,
                updates,
            )

        conn_v.commit()

        # Print a small summary for the validation step.
        cur_v.execute(
            f"""
            SELECT
                COUNT(*) AS n_migration_events,
                COALESCE(SUM(CASE WHEN `{validation_column}` = 1 THEN 1 ELSE 0 END), 0) AS n_valid_migrations
            FROM {EVENT_TABLE}
            WHERE change_type = 'migration'
            """
        )
        summary = cur_v.fetchone() or {}
        total = int(summary.get("n_migration_events") or 0)
        valid = int(summary.get("n_valid_migrations") or 0)

        print("=== Snapshot Migration Validation Summary ===")
        print(f"Analogous pairs loaded: {len(mappings)}")
        print(f"Migration events checked: {total}")
        print(f"Valid migrations (mapped): {valid}")
        print(f"Invalid migrations (unmapped): {total - valid}")
        pct = 0.0 if total == 0 else (100.0 * valid / total)
        print(f"Valid ratio: {pct:.2f}%")

    finally:
        db_v.close_db_connection()


def _pair_key(r: dict):
    """
    Build the release-pair key used for summary counts.
    """
    return (
        int(r["id"]),
        int(r["repo_id"]),
        int(r["release_line_id"]),
        int(r["prev_release_id"]),
        int(r["curr_release_id"]),
    )


def _group_key(r: dict):
    """
    Group rows by release pair, commit, and file path.
    """
    return (
        int(r["id"]),
        int(r["repo_id"]),
        int(r["release_line_id"]),
        int(r["prev_release_id"]),
        int(r["curr_release_id"]),
        (r.get("commit_sha") or ""),
        (r.get("changed_file_path") or ""),
    )


def _assert_validation_column_exists(col_name: str):
    """
    Check that the validation flag column exists in the source event table.
    """
    cur.execute(
        """
        SELECT COUNT(*) AS n
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND column_name = %s
        """,
        (EVENT_TABLE, col_name),
    )
    n = int((cur.fetchone() or {}).get("n") or 0)
    if n == 0:
        raise ValueError(f"Validation column '{col_name}' not found in {EVENT_TABLE}")


def _reconstruct_group_events(group_rows: list):
    """
    Rebuild one group of events after migration validation.
    """
    adds = [r for r in group_rows if r["change_type"] == "added"]
    removes = [r for r in group_rows if r["change_type"] == "removed"]
    updates = [r for r in group_rows if r["change_type"] == "updated"]
    valid_migs = [
        r for r in group_rows
        if r["change_type"] == "migration" and int(r.get(VALIDATION_COLUMN) or 0) == 1
    ]

    add_used = [False] * len(adds)
    rem_used = [False] * len(removes)
    finalized = []

    # Keep update rows as they are.
    for u in updates:
        out = dict(u)
        out[VALIDATION_COLUMN] = 0
        finalized.append(out)

    # Consume one remove and one add for each valid migration.
    for m in valid_migs:
        old_name = _normalize_lib_name(m.get("changed_name") or "")
        new_name = _normalize_lib_name(m.get("to_name") or "")

        rem_idx = None
        for i, r in enumerate(removes):
            if rem_used[i]:
                continue
            if _normalize_lib_name(r.get("changed_name") or "") == old_name:
                rem_idx = i
                break

        add_idx = None
        for i, a in enumerate(adds):
            if add_used[i]:
                continue
            if _normalize_lib_name(a.get("changed_name") or "") == new_name:
                add_idx = i
                break

        if rem_idx is not None and add_idx is not None:
            rem_used[rem_idx] = True
            add_used[add_idx] = True
            out = dict(m)
            out[VALIDATION_COLUMN] = 1
            finalized.append(out)

    # Keep leftover adds and removes as standalone events.
    for i, a in enumerate(adds):
        if not add_used[i]:
            out = dict(a)
            out[VALIDATION_COLUMN] = 0
            finalized.append(out)

    for i, r in enumerate(removes):
        if not rem_used[i]:
            out = dict(r)
            out[VALIDATION_COLUMN] = 0
            finalized.append(out)

    return finalized


def reconstruct_after_validation(validation_column: str = VALIDATION_COLUMN):
    """
    Rebuild the after-validation event and overview tables.
    """
    _ensure_open_db_connection()
    _assert_valid_sql_identifier(validation_column)
    _assert_validation_column_exists(validation_column)

    # Pull all source events with the validation flag.
    cur.execute(
        f"""
        SELECT
            id, repo_id, release_line_id, prev_release_id, curr_release_id,
            change_type, changed_name, associated_version, to_version, to_name,
            commit_sha, commit_url, changed_file_path, changed_file_type,
            COALESCE(`{validation_column}`, 0) AS `{validation_column}`
        FROM {EVENT_TABLE}
        ORDER BY id, repo_id, release_line_id, prev_release_id, curr_release_id,
                 commit_sha, changed_file_path, change_type
        """
    )
    raw_events = cur.fetchall() or []

    # Group rows in the same scope where migration matching is defined.
    grouped = defaultdict(list)
    for r in raw_events:
        grouped[_group_key(r)].append(r)

    finalized_events = []
    for rows in grouped.values():
        finalized_events.extend(_reconstruct_group_events(rows))

    # Read the base overview so migration_candidates stay unchanged.
    cur.execute(
        f"""
        SELECT id, repo_id, release_line_id, prev_release_id, curr_release_id, migration_candidates
        FROM {OVERVIEW_TABLE}
        ORDER BY id
        """
    )
    base_overview = cur.fetchall() or []

    # Recompute event counts per release pair.
    counts = defaultdict(
        lambda: {
            "num_changes": 0,
            "num_additions": 0,
            "num_removals": 0,
            "num_updates": 0,
            "num_migrations": 0,
        }
    )

    for e in finalized_events:
        k = _pair_key(e)
        c = counts[k]
        c["num_changes"] += 1
        t = (e.get("change_type") or "").strip().lower()
        if t == "added":
            c["num_additions"] += 1
        elif t == "removed":
            c["num_removals"] += 1
        elif t == "updated":
            c["num_updates"] += 1
        elif t == "migration":
            c["num_migrations"] += 1

    if CLEAR_AFTER_TABLES_FIRST:
        cur.execute(f"DELETE FROM {EVENT_AFTER_TABLE}")
        cur.execute(f"DELETE FROM {OVERVIEW_AFTER_TABLE}")

    # Insert the finalized event rows into the after-validation table.
    if finalized_events:
        vals = []
        for e in finalized_events:
            vals.append(
                (
                    int(e["id"]),
                    int(e["repo_id"]),
                    int(e["release_line_id"]),
                    int(e["prev_release_id"]),
                    int(e["curr_release_id"]),
                    e.get("change_type"),
                    e.get("changed_name"),
                    e.get("associated_version"),
                    e.get("to_version"),
                    e.get("to_name"),
                    e.get("commit_sha"),
                    e.get("commit_url"),
                    None,
                    None,
                    e.get("changed_file_path"),
                    e.get("changed_file_type"),
                    int(e.get(validation_column) or 0),
                )
            )

        cur.executemany(
            f"""
            INSERT INTO {EVENT_AFTER_TABLE} (
                id, repo_id, release_line_id, prev_release_id, curr_release_id,
                change_type, changed_name, associated_version, to_version, to_name,
                commit_sha, commit_url, evidence_commit_shas_json, evidence_commit_urls_json,
                changed_file_path, changed_file_type, is_valid_migration
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            vals,
        )

    # Upsert the recomputed overview rows into the after table.
    ov_vals = []
    for r in base_overview:
        k = (
            int(r["id"]),
            int(r["repo_id"]),
            int(r["release_line_id"]),
            int(r["prev_release_id"]),
            int(r["curr_release_id"]),
        )
        c = counts.get(
            k,
            {
                "num_changes": 0,
                "num_additions": 0,
                "num_removals": 0,
                "num_updates": 0,
                "num_migrations": 0,
            },
        )
        ov_vals.append(
            (
                k[0],
                k[1],
                k[2],
                k[3],
                k[4],
                c["num_changes"],
                c["num_additions"],
                c["num_removals"],
                c["num_updates"],
                c["num_migrations"],
                r.get("migration_candidates"),
            )
        )

    if ov_vals:
        cur.executemany(
            f"""
            INSERT INTO {OVERVIEW_AFTER_TABLE} (
                id, repo_id, release_line_id, prev_release_id, curr_release_id,
                num_changes, num_additions, num_removals, num_updates, num_migrations, migration_candidates
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                repo_id = VALUES(repo_id),
                release_line_id = VALUES(release_line_id),
                prev_release_id = VALUES(prev_release_id),
                curr_release_id = VALUES(curr_release_id),
                num_changes = VALUES(num_changes),
                num_additions = VALUES(num_additions),
                num_removals = VALUES(num_removals),
                num_updates = VALUES(num_updates),
                num_migrations = VALUES(num_migrations),
                migration_candidates = VALUES(migration_candidates)
            """,
            ov_vals,
        )

    conn.commit()

    print("=== Reconstruct After Validation (Snapshot-based) ===")
    print(f"Source events: {len(raw_events)}")
    print(f"Finalized events: {len(finalized_events)}")
    print(f"Overview rows upserted: {len(ov_vals)}")


def fill_update_commit_evidence_from_legacy():
    """
    Fill update rows with the full commit evidence sequence from the legacy table.
    """
    _ensure_open_db_connection()

    # Build an evidence index from legacy updated rows.
    cur.execute(
        f"""
        SELECT
            repo_id, release_line_id, prev_release_id, curr_release_id,
            changed_name, changed_file_path, commit_sha, commit_url
        FROM {LEGACY_EVENT_TABLE}
        WHERE change_type = 'updated'
          AND commit_sha IS NOT NULL
        ORDER BY repo_id, release_line_id, prev_release_id, curr_release_id,
                 changed_file_path, changed_name, commit_sha
        """
    )
    legacy_rows = cur.fetchall() or []

    evidence = defaultdict(lambda: {"shas": [], "urls": [], "seen_shas": set()})

    for r in legacy_rows:
        key = (
            int(r["repo_id"]),
            int(r["release_line_id"]),
            int(r["prev_release_id"]),
            int(r["curr_release_id"]),
            _normalize_lib_name(r.get("changed_name") or ""),
            (r.get("changed_file_path") or ""),
        )
        sha = (r.get("commit_sha") or "").strip()
        url = (r.get("commit_url") or "").strip()
        if not sha or sha in evidence[key]["seen_shas"]:
            continue
        evidence[key]["seen_shas"].add(sha)
        evidence[key]["shas"].append(sha)
        evidence[key]["urls"].append(url)

    # Read update rows from the after-validation table.
    cur.execute(
        f"""
        SELECT
            id, repo_id, release_line_id, prev_release_id, curr_release_id,
            changed_name, changed_file_path, commit_sha, commit_url
        FROM {EVENT_AFTER_TABLE}
        WHERE change_type = 'updated'
        """
    )
    updates = cur.fetchall() or []

    update_sql_values = []
    filled = 0

    for r in updates:
        key = (
            int(r["repo_id"]),
            int(r["release_line_id"]),
            int(r["prev_release_id"]),
            int(r["curr_release_id"]),
            _normalize_lib_name(r.get("changed_name") or ""),
            (r.get("changed_file_path") or ""),
        )
        ev = evidence.get(key)

        shas = ev["shas"] if ev else []
        urls = ev["urls"] if ev else []

        # Keep the representative commit if it already exists.
        rep_sha = r.get("commit_sha")
        rep_url = r.get("commit_url")
        if (not rep_sha) and shas:
            rep_sha = shas[-1]
            rep_url = urls[-1] if urls else None

        if shas:
            filled += 1

        update_sql_values.append(
            (
                rep_sha,
                rep_url,
                json.dumps(shas),
                json.dumps(urls),
                int(r["id"]),
                int(r["repo_id"]),
                int(r["release_line_id"]),
                int(r["prev_release_id"]),
                int(r["curr_release_id"]),
                r.get("changed_name"),
                r.get("changed_file_path"),
            )
        )

    if update_sql_values:
        cur.executemany(
            f"""
            UPDATE {EVENT_AFTER_TABLE}
            SET
                commit_sha = %s,
                commit_url = %s,
                evidence_commit_shas_json = %s,
                evidence_commit_urls_json = %s
            WHERE id = %s
              AND repo_id = %s
              AND release_line_id = %s
              AND prev_release_id = %s
              AND curr_release_id = %s
              AND changed_name = %s
              AND changed_file_path <=> %s
              AND change_type = 'updated'
            """,
            update_sql_values,
        )

    conn.commit()

    print("=== Update Evidence Fill (Snapshot-based) ===")
    print(f"Legacy updated rows scanned: {len(legacy_rows)}")
    print(f"After-validation updated rows: {len(updates)}")
    print(f"Updated rows with non-empty evidence list: {filled}")


def run_migration_validation():
    """
    Run validation, reconstruction, and evidence filling in one script flow.
    """
    validate_snapshot_migrations()
    reconstruct_after_validation()
    fill_update_commit_evidence_from_legacy()

    if db is not None:
        db.close_db_connection()


if __name__ == "__main__":

    run_migration_validation()
    print("\nMigration validation process completed.")