#!/usr/bin/env python3
"""
Detect PTM changes across adjacent releases within each release line.

It first aggregates file-level PTM snapshots into one release-level multiset, then
compares adjacent releases in first-parent order inside each release line.

The multiset part matters here: if the same PTM appears in multiple reused
files, or appears multiple times across those files, we keep those counts
instead of collapsing them into a simple set.

For each release pair, we record additions, removals, and the paired baseline
used later for migration validation. We also mark the first PTM-adoption point
so downstream analysis can use the same t1 starting point across release lines.
"""

from __future__ import annotations

from typing import Dict, List, Tuple, Any, Optional
from collections import Counter, defaultdict
import json
import sys

from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF

INPUT_TABLE_FILES_RELEASES = DBT.FILE_MODEL_SNAPSHOTS_LISTS_RELEASES  # (repo_id, file_id, release_id)
RELEASES_T = DBT.RELEASES
RELEASE_LINES_T = DBT.RELEASE_LINES
OUTPUT_TABLE_RELEASE_LINE = DBT.RELEASE_LINE_MODEL_CHANGES


# ---------- parsing helpers ----------

def _safe_parse_list(raw: Any) -> List:
    """
    safely parse a list field safely and fall back to an empty list
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if not s:
        return []
    try:
        return json.loads(s)
    except Exception:
        try:
            return json.loads(json.loads(s))
        except Exception:
            return []


def _coerce_model_ids(ids: List[Any]) -> List[int]:
    """keep only model ids that can be converted to integers"""
    out: List[int] = []
    for v in ids:
        try:
            out.append(int(v))
        except Exception:
            continue
    return out


def _normalize_names(names: List[Any], n: int) -> List[str]:
    """Align model-name lists with model-id lists."""
    if names is None:
        names = []
    names = [("" if x is None else str(x)) for x in names]
    if len(names) < n:
        names = names + [""] * (n - len(names))
    return names[:n]


def _expand_instances(counter: Counter[int]) -> List[int]:
    """
    expand a multiset counter into a sorted instance list.
    """
    out: List[int] = []
    for mid in sorted(counter.keys()):
        out.extend([mid] * counter[mid])
    return out


def _build_name_map_from_snapshots(samples: List[Tuple[int, str]]) -> Dict[int, str]:
    """
    Build a stable id-to-name map from repeated snapshot samples
    """
    name_map: Dict[int, str] = {}
    for mid, nm in samples:
        if nm:
            name_map[mid] = nm
    return name_map


# ---------- core diff ----------

def _multiset_diff_with_lists(prev_ids: List[int], curr_ids: List[int],
                              prev_names: List[str], curr_names: List[str]) -> Dict[str, Any]:
    """
    Compare two release-level PTM multisets and summarize the change

    The counts follow the paper definition:
    - A: total added PTM instances
    - R: total removed PTM instances
    - U = min(A, R): paired add/remove baseline for possible migrations cadidates
    """
    prev = Counter(prev_ids)
    nxt  = Counter(curr_ids)

    # Count the per-model delta between the two adjacent releases.
    all_ids = set(prev) | set(nxt)
    deltas = {m: nxt.get(m, 0) - prev.get(m, 0) for m in all_ids}

    A = sum(max(d, 0) for d in deltas.values())
    R = sum(max(-d, 0) for d in deltas.values())
    U = min(A, R)

    # Keep the full multiset counts before we split paired and unpaired changes.
    add_unpaired = Counter({m: max(d, 0) for m, d in deltas.items()})
    rem_unpaired = Counter({m: max(-d, 0) for m, d in deltas.items()})

    added_all_ids   = _expand_instances(add_unpaired)
    removed_all_ids = _expand_instances(rem_unpaired)

    # Pair as many additions and removals as possible as the migration baseline.
    migrated_from_ids = removed_all_ids[:U]
    migrated_to_ids   = added_all_ids[:U]
    added_residual_ids    = added_all_ids[U:]
    removed_residual_ids  = removed_all_ids[U:]

    # Build a simple id-to-name map, preferring current-release names.
    id_to_name: Dict[int, str] = {}
    for mid, nm in zip(curr_ids, curr_names):
        if nm:
            id_to_name[mid] = nm
    for mid, nm in zip(prev_ids, prev_names):
        if nm and mid not in id_to_name:
            id_to_name[mid] = nm

    def names_for(ids: List[int]) -> List[str]:
        return [id_to_name.get(i, "") for i in ids]

    return {
        # Scalar summaries for the release pair.
        "prev_model_count":             int(sum(prev.values())),
        "curr_model_count":             int(sum(nxt.values())),
        "prev_model_ids":              json.dumps(list(prev_ids)),
        "prev_model_names":            json.dumps(list(prev_names)),
        "curr_model_ids":              json.dumps(list(curr_ids)),
        "curr_model_names":            json.dumps(list(curr_names)),
        "migrated_count":               int(U),
        "added_count":                  int(len(added_residual_ids)),
        "removed_count":                int(len(removed_residual_ids)),

        "added_model_ids":              json.dumps(added_residual_ids),
        "added_model_names":            json.dumps(names_for(added_residual_ids)),
        "removed_model_ids":            json.dumps(removed_residual_ids),
        "removed_model_names":          json.dumps(names_for(removed_residual_ids)),
        "migrated_from_model_ids":      json.dumps(migrated_from_ids),
        "migrated_from_model_names":    json.dumps(names_for(migrated_from_ids)),
        "migrated_to_model_ids":        json.dumps(migrated_to_ids),
        "migrated_to_model_names":      json.dumps(names_for(migrated_to_ids)),
    }


# ---------- release-line helpers ----------

def _load_release_lines_for_repo(db: DatabaseConfig, repo_id: int) -> Tuple[Dict[int, List[int]], List[int]]:
    """
    load release lines for one repo.

    The release ids stay ordered by first-parent position, which is the order we
    later use for adjacent release comparisons inside each line.
    """
    rows = db.select_from_db(
        table_name=RELEASE_LINES_T.value,
        columns="release_line_id, release_id, fp_index",
        where="repo_id = %s",
        params=(repo_id,),
        order_by="release_line_id ASC, fp_index ASC, release_id ASC",
        fetch_one=False,
    ) or []

    if not rows:
        return {}, []

    per_line: Dict[int, List[int]] = defaultdict(list)
    all_release_ids: List[int] = []

    for r in rows:
        rl_id = int(r["release_line_id"])
        rid   = int(r["release_id"])
        per_line[rl_id].append(rid)
        all_release_ids.append(rid)

    all_release_ids = sorted(set(all_release_ids))
    return per_line, all_release_ids


def _load_aggregated_release_snapshots_for_ids(
    db: DatabaseConfig,
    repo_id: int,
    release_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """
    Produce per-release snapshots for the given release_ids within a repo.

    Output dict keyed by release_id:
      rid -> {
        "release_id": int,
        "rel_time":   datetime,
        "model_ids":  List[int],   # expanded multiset
        "model_names":List[str],   # aligned with model_ids
      }

    All requested releases are included. If a release has no file-level PTM
    snapshot, it is treated as an empty multiset.
    """
    if not release_ids:
        return {}

    # Load release metadata first so we can keep the release-line order stable.
    placeholders = ",".join(["%s"] * len(release_ids))
    rows = db.select_from_db(
        table_name=RELEASES_T.value,
        columns=f"id AS release_id, COALESCE(published_at, created_at) AS rel_time",
        where=f"repo_id = %s AND id IN ({placeholders})",
        params=(repo_id, *release_ids),
        order_by="rel_time ASC, id ASC",
        fetch_one=False,
    ) or []

    if not rows:
        print(f"(agg/REPO) repo {repo_id}: no release metadata for requested release_ids → empty snapshots")
        return {}

    # Keep only releases that still have matching metadata rows.
    ordered_release_ids: List[int] = [int(r["release_id"]) for r in rows]
    rel_time_map = {int(r["release_id"]): r["rel_time"] for r in rows}

    # Load file-level snapshots and aggregate them into one release-level multiset.
    rows_snap = db.select_from_db(
        table_name=f"{INPUT_TABLE_FILES_RELEASES.value} s "
                   f"JOIN {RELEASES_T.value} r ON r.id = s.release_id",
        columns=("s.release_id, s.model_id, s.model_name, "
                 "COALESCE(r.published_at, r.created_at) AS rel_time"),
        where=f"s.repo_id = %s AND s.release_id IN ({placeholders})",
        params=(repo_id, *release_ids),
        order_by="rel_time ASC, s.release_id ASC, s.commit_id ASC, s.commit_file_id ASC",
        fetch_one=False,
    ) or []

    print(f"(agg/REPO) repo {repo_id}: loaded {len(rows_snap)} file-level snapshots (pre-aggregation)")

    per_release_counts: Dict[int, Counter[int]] = defaultdict(Counter)
    per_release_name_samples: Dict[int, List[Tuple[int, str]]] = defaultdict(list)

    for r in rows_snap:
        rid = int(r["release_id"])
        if rid not in rel_time_map:
            continue

        mids_raw = _safe_parse_list(r.get("model_id"))
        mnames   = _safe_parse_list(r.get("model_name"))

        mids     = _coerce_model_ids(mids_raw)
        mnames   = _normalize_names(mnames, len(mids))

        for mid, nm in zip(mids, mnames):
            per_release_counts[rid][mid] += 1
            per_release_name_samples[rid].append((mid, nm))

    # Build one aggregated snapshot per release, including empty releases.
    snapshots: Dict[int, Dict[str, Any]] = {}
    for rid in ordered_release_ids:
        cnt = per_release_counts.get(rid, Counter())
        if not cnt:
            snapshots[rid] = {
                "release_id":  rid,
                "rel_time":    rel_time_map[rid],
                "model_ids":   [],
                "model_names": [],
            }
            continue

        name_map = _build_name_map_from_snapshots(per_release_name_samples[rid])
        ids_expanded = _expand_instances(cnt)
        names_expanded = [name_map.get(mid, "") for mid in ids_expanded]

        snapshots[rid] = {
            "release_id":  rid,
            "rel_time":    rel_time_map[rid],
            "model_ids":   ids_expanded,
            "model_names": names_expanded,
        }

    return snapshots


# ---------- per-repo computation over release lines ----------

def compute_and_store_model_changes_for_repo_by_release_line(db: DatabaseConfig, repo_id: int) -> int:
    """
    Compute release-line PTM changes for one repo and store the result.

    This keeps the comparison at the release-line level, but each release
    snapshot is still a multiset aggregated from reused files underneath it.
    """
    per_line, all_release_ids = _load_release_lines_for_repo(db, repo_id)
    if not per_line:
        db.execute_manual_sql(f"DELETE FROM {OUTPUT_TABLE_RELEASE_LINE.value} WHERE repo_id=%s", params=(repo_id,))
        print(f"(diff/RL) repo {repo_id}: no release lines → cleared diffs")
        return 0

    snapshots_by_id = _load_aggregated_release_snapshots_for_ids(db, repo_id, all_release_ids)
    if not snapshots_by_id:
        db.execute_manual_sql(f"DELETE FROM {OUTPUT_TABLE_RELEASE_LINE.value} WHERE repo_id=%s", params=(repo_id,))
        print(f"(diff/RL) repo {repo_id}: no snapshots for release-line releases → cleared diffs")
        return 0

    # Clear old rows first so the script can be rerun safely for one repo.
    db.execute_manual_sql(f"DELETE FROM {OUTPUT_TABLE_RELEASE_LINE.value} WHERE repo_id=%s", params=(repo_id,))

    # First pass: compare each adjacent pair inside each release line.
    # Each item: (prev_time, line_id, prev_release_id, curr_release_id, diff_dict, prev_cnt, curr_cnt)
    pairs: List[Tuple[Any, int, int, int, Dict[str, Any], int, int]] = []

    for rl_id, seq in per_line.items():
        # Reverse the first-parent order so we walk oldest to newest.
        seq_ids = [rid for rid in reversed(seq) if rid in snapshots_by_id]
        if len(seq_ids) < 2:
            continue

        for i in range(len(seq_ids) - 1):
            prev_id = seq_ids[i]
            curr_id = seq_ids[i + 1]

            prev_snap = snapshots_by_id[prev_id]
            curr_snap = snapshots_by_id[curr_id]

            d = _multiset_diff_with_lists(
                prev_snap["model_ids"], curr_snap["model_ids"],
                prev_snap["model_names"], curr_snap["model_names"],
            )

            prev_cnt = d["prev_model_count"]
            curr_cnt = d["curr_model_count"]
            prev_time = prev_snap["rel_time"]

            pairs.append((prev_time, rl_id, prev_id, curr_id, d, prev_cnt, curr_cnt))

    if not pairs:
        print(f"(diff/RL) repo {repo_id}: no adjacent release pairs within lines → wrote 0 rows")
        return 0

    # ---------- Per-repo first adoption ----------
    # This marks the first transition where PTMs appear for the repo.
    pairs_sorted = sorted(pairs, key=lambda x: (x[0], x[1], x[2], x[3]))

    first_adoption_marked = False
    step_index = 0
    # key: (line_id, prev_id, curr_id) -> 0/1
    repo_first_map: Dict[Tuple[int, int, int], int] = {}

    for prev_time, rl_id, prev_id, curr_id, d, prev_cnt, curr_cnt in pairs_sorted:
        is_first = 0
        if not first_adoption_marked:
            if prev_cnt == 0 and curr_cnt > 0:
                is_first = 1
            elif step_index == 0 and prev_cnt > 0:
                # If the history starts with PTMs already present, mark the earliest pair.
                is_first = 1

        if is_first:
            first_adoption_marked = True

        repo_first_map[(rl_id, prev_id, curr_id)] = is_first
        step_index += 1

    # ---------- Per-line first adoption ----------
    # This is the line-level version of the same t1 idea.
    per_line_pairs: Dict[int, List[Tuple[Any, int, int, int, Dict[str, Any], int, int]]] = defaultdict(list)
    for entry in pairs:
        _, rl_id, _, _, _, _, _ = entry
        per_line_pairs[rl_id].append(entry)

    line_first_map: Dict[Tuple[int, int, int], int] = {}

    for rl_id, pair_list in per_line_pairs.items():
        line_first_marked = False
        for j, entry in enumerate(pair_list):
            _, _, prev_id, curr_id, d, prev_cnt, curr_cnt = entry
            is_first_line = 0

            if not line_first_marked:
                if prev_cnt == 0 and curr_cnt > 0:
                    is_first_line = 1
                elif j == 0 and prev_cnt > 0:
                    # Some lines start after PTM adoption already happened.
                    is_first_line = 1

            if is_first_line:
                line_first_marked = True

            line_first_map[(rl_id, prev_id, curr_id)] = is_first_line

    # ---------- Insert rows ----------
    # keep both scalar summaries and instance-level metadata for each release pair
    wrote = 0
    for prev_time, rl_id, prev_id, curr_id, d, prev_cnt, curr_cnt in pairs:
        key = (rl_id, prev_id, curr_id)
        is_repo_adopt = repo_first_map.get(key, 0)
        is_line_adopt = line_first_map.get(key, 0)

        row = {
            "repo_id":                 repo_id,
            "release_line_id":         rl_id,
            "prev_release_id":         prev_id,
            "curr_release_id":         curr_id,
            **d,
            "is_first_adoption":       int(is_repo_adopt),
            "is_first_adoption_line":  int(is_line_adopt),
        }
        db.insert_to_db(OUTPUT_TABLE_RELEASE_LINE.value, row)
        wrote += 1

    print(
        f"(diff/RL) repo {repo_id}: wrote {wrote} release-line-change rows "
        f"(first adoption per repo + per line flagged)"
    )
    return wrote



# ---------- worklist selection ----------

def select_repos_with_release_lines(db: DatabaseConfig) -> List[int]:
    """
    Find repos that already have at least one extracted release line.
    """
    rows = db.select_from_db(
        table_name=RELEASE_LINES_T.value,
        columns="DISTINCT repo_id",
        where=None,
        params=None,
        order_by="repo_id ASC",
        fetch_one=False,
    ) or []
    repos = [int(r["repo_id"]) for r in rows]
    print(f"(scan/RL) discovered {len(repos)} repos with release lines")
    return repos


# ---------- main ----------

def main(repo_id: Optional[int] = None):
    """
    run the release-line PTM change detector for one repo or all repos
    if repo_id is provided, only that repo is processed. Otherwise, all repos with release lines are processed.
    """
    print("Initializing RELEASE-LINE-level model-change diff (aggregate files → release lines)…\n")
    db = DatabaseConfig()
    db.create_db_connection()

    try:
        targets = [repo_id] if repo_id is not None else select_repos_with_release_lines(db)

        total_changes = 0
        for rid in targets:
            try:
                total_changes += compute_and_store_model_changes_for_repo_by_release_line(db, rid)
            except Exception as e:
                print(f"(ERR/RL) repo {rid}: {e}")
                try:
                    db.log_error_to_db(f"release-line model-change repo={rid}: {e}")
                except Exception:
                    pass

        print(f"\nDone. Wrote {total_changes} release-line-change rows across {len(targets)} repos.")
    finally:
        db.close_db_connection()


if __name__ == "__main__":
    arg = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    main(arg)
