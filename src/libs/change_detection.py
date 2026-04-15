"""
Detect library changes between release snapshots and save the results.
This mirrors the release-line framework we used for PTMs.
Dependency detection stays at file level, like PTMs.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from utils.DBConfig import DatabaseConfig

OVERVIEW_TABLE = "release_line_library_change_overview_after_validation"
EVENT_TABLE = "release_line_library_change_events_after_validation"
LEGACY_EVENT_TABLE = "library_change_events"

# to resume from a smaller range
START_ID = None
END_ID = None
LIMIT = None
COMMIT_EVERY = 50


def normalize_pkg_name(name: str) -> str:
    """
    Normalize package names so matching is more consistent
    """
    return (name or "").strip().lower().replace("_", "-")


def _parse_json_field(v, default):
    """
    Read a JSON field that may already be parsed or stored as text.
    """
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default
    return default

def load_release_pairs(start_id: Optional[int], end_id: Optional[int], limit: Optional[int]) -> List[dict]:
    """
    Load the release pairs that should be processed
    """
    where = []
    params: List[int] = []
    if start_id is not None:
        where.append("id >= %s")
        params.append(int(start_id))
    if end_id is not None:
        where.append("id <= %s")
        params.append(int(end_id))

    sql = """
        SELECT id, repo_id, release_line_id, prev_release_id, curr_release_id
        FROM final_repo_release_pairs
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(int(limit))

    cur.execute(sql, tuple(params))
    return cur.fetchall() or []


def load_snapshot_by_file(repo_id: int, release_line_id: int, release_id: int) -> Dict[str, Dict[str, str]]:
    """
    Load one release snapshot as {file_path: {dep_name: dep_version}}.
    """
    cur.execute(
        """
        SELECT file_path, dep_names, dep_versions
        FROM library_snapshots
        WHERE repo_id = %s
          AND release_line_id = %s
          AND release_id = %s
        """,
        (repo_id, release_line_id, release_id),
    )
    rows = cur.fetchall() or []

    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        fp = r.get("file_path")
        if not fp:
            continue

        dep_names = _parse_json_field(r.get("dep_names"), [])
        dep_versions = _parse_json_field(r.get("dep_versions"), {})

        deps: Dict[str, str] = {}
        if isinstance(dep_versions, dict) and dep_versions:
            for k, v in dep_versions.items():
                deps[normalize_pkg_name(str(k))] = "" if v is None else str(v)
        elif isinstance(dep_names, list):
            for k in dep_names:
                deps[normalize_pkg_name(str(k))] = ""

        out[fp] = deps

    return out

@dataclass
class ChangeEvent:
    """
    Store one dependency change event for a release pair.
    """
    change_type: str # added | removed | updated | migration
    changed_name: str
    associated_version: str
    to_version: Optional[str]
    to_name: Optional[str]
    commit_sha: Optional[str]
    commit_url: Optional[str]
    changed_file_path: Optional[str]
    changed_file_type: Optional[str]
    is_valid_migration: int = 0


def diff_release_snapshots(prev_by_file: Dict[str, Dict[str, str]], curr_by_file: Dict[str, Dict[str, str]]) -> List[ChangeEvent]:
    """
    Compare two snapshot views and build file-level change events.
    """
    events: List[ChangeEvent] = []

    all_files = sorted(set(prev_by_file.keys()) | set(curr_by_file.keys()))
    for fp in all_files:
        before = prev_by_file.get(fp, {})
        after = curr_by_file.get(fp, {})

        if fp not in prev_by_file:
            file_type = "added"
        elif fp not in curr_by_file:
            file_type = "removed"
        else:
            file_type = "modified"

        before_names = set(before.keys())
        after_names = set(after.keys())

        for name in sorted(after_names - before_names):
            events.append(ChangeEvent(
                change_type="added",
                changed_name=name,
                associated_version=after.get(name, "") or "",
                to_version=None,
                to_name=None,
                commit_sha=None,
                commit_url=None,
                changed_file_path=fp,
                changed_file_type=file_type,
            ))

        for name in sorted(before_names - after_names):
            events.append(ChangeEvent(
                change_type="removed",
                changed_name=name,
                associated_version=before.get(name, "") or "",
                to_version=None,
                to_name=None,
                commit_sha=None,
                commit_url=None,
                changed_file_path=fp,
                changed_file_type=file_type,
            ))

        for name in sorted(before_names & after_names):
            bv = before.get(name, "") or ""
            av = after.get(name, "") or ""
            if bv != av:
                events.append(ChangeEvent(
                    change_type="updated",
                    changed_name=name,
                    associated_version=bv,
                    to_version=av,
                    to_name=None,
                    commit_sha=None,
                    commit_url=None,
                    changed_file_path=fp,
                    changed_file_type="modified",
                ))

    return events

def load_legacy_events_for_pair(repo_id: int, release_line_id: int, prev_release_id: int, curr_release_id: int) -> List[dict]:
    """
    Load older dependency events so we can reuse commit metadata.
    """
    cur.execute(
        f"""
        SELECT
            change_type,
            changed_name,
            associated_version,
            to_version,
            to_name,
            commit_sha,
            commit_url,
            changed_file_path,
            changed_file_type
        FROM {LEGACY_EVENT_TABLE}
        WHERE repo_id = %s
          AND release_line_id = %s
          AND prev_release_id = %s
          AND curr_release_id = %s
          AND change_type IN ('added', 'removed', 'updated')
        """,
        (repo_id, release_line_id, prev_release_id, curr_release_id),
    )
    return cur.fetchall() or []


def index_legacy_events(rows: List[dict]):
    """
    Build lookup indexes for matching new events to legacy commit data
    """
    exact = defaultdict(list)
    relaxed = defaultdict(list)

    for r in rows:
        ctype = (r.get("change_type") or "").strip().lower()
        name = normalize_pkg_name(r.get("changed_name") or "")
        fp = r.get("changed_file_path")
        av = "" if r.get("associated_version") is None else str(r.get("associated_version"))
        tv = None if r.get("to_version") is None else str(r.get("to_version"))

        rec = {
            "sha": r.get("commit_sha"),
            "url": r.get("commit_url"),
            "file_type": r.get("changed_file_type"),
            "associated_version": av,
            "to_version": tv,
        }

        exact[(ctype, name, fp, av, tv)].append(rec)
        relaxed[(ctype, name, fp)].append(rec)

    return exact, relaxed


def attach_commit_candidates(events: List[ChangeEvent], exact_idx, relaxed_idx):
    """
    Attach the best commit match to each event and keep all candidates.
    """
    out = []
    for ev in events:
        key_exact = (
            ev.change_type,
            normalize_pkg_name(ev.changed_name),
            ev.changed_file_path,
            ev.associated_version or "",
            ev.to_version,
        )
        key_relaxed = (
            ev.change_type,
            normalize_pkg_name(ev.changed_name),
            ev.changed_file_path,
        )

        cands = list(exact_idx.get(key_exact, []))
        if not cands:
            cands = list(relaxed_idx.get(key_relaxed, []))

        # Keep the order stable so repeated runs behave the same way.
        cands = sorted(cands, key=lambda x: ((x.get("sha") or ""), (x.get("url") or "")))
        out.append(cands)

        # Save the first candidate directly on the event.
        if cands:
            ev.commit_sha = cands[0].get("sha")
            ev.commit_url = cands[0].get("url")
            if not ev.changed_file_type:
                ev.changed_file_type = cands[0].get("file_type")

    return out

def derive_migration_events(base_events: List[ChangeEvent], commit_candidates: List[List[dict]]):
    """
    Build migration candidates and confirmed migration events from base changes.
    """
    by_file_removed: Dict[str, List[Tuple[ChangeEvent, List[dict]]]] = defaultdict(list)
    by_file_added: Dict[str, List[Tuple[ChangeEvent, List[dict]]]] = defaultdict(list)

    for ev, cands in zip(base_events, commit_candidates):
        fp = ev.changed_file_path or ""
        if ev.change_type == "removed":
            by_file_removed[fp].append((ev, cands))
        elif ev.change_type == "added":
            by_file_added[fp].append((ev, cands))

    migration_candidates: List[Tuple[str, str]] = []
    migration_events: List[ChangeEvent] = []

    for fp in sorted(set(by_file_removed.keys()) | set(by_file_added.keys())):
        rems = by_file_removed.get(fp, [])
        adds = by_file_added.get(fp, [])

        # Keep all same-file removed/added pairs as migration candidates.
        for rem_ev, _ in rems:
            for add_ev, _ in adds:
                migration_candidates.append((rem_ev.changed_name, add_ev.changed_name))

        # Keep only removed/added pairs that share at least one commit.
        edges = []
        for ri, (rem_ev, rem_cands) in enumerate(rems):
            rem_sha_map = {c.get("sha"): c for c in rem_cands if c.get("sha")}
            if not rem_sha_map:
                continue

            for ai, (add_ev, add_cands) in enumerate(adds):
                add_sha_map = {c.get("sha"): c for c in add_cands if c.get("sha")}
                if not add_sha_map:
                    continue

                shared = sorted(set(rem_sha_map.keys()) & set(add_sha_map.keys()))
                if not shared:
                    continue

                # Pick one shared commit for this possible migration pair.
                chosen_sha = shared[0]
                chosen_rec = add_sha_map.get(chosen_sha) or rem_sha_map.get(chosen_sha) or {}

                edges.append((
                    chosen_sha,
                    normalize_pkg_name(rem_ev.changed_name),
                    normalize_pkg_name(add_ev.changed_name),
                    ri,
                    ai,
                    chosen_rec,
                ))

        # Match one removed event to at most one added event in the same file.
        edges.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        used_rem = set()
        used_add = set()

        for chosen_sha, _, _, ri, ai, chosen_rec in edges:
            if ri in used_rem or ai in used_add:
                continue

            used_rem.add(ri)
            used_add.add(ai)

            rem_ev, _ = rems[ri]
            add_ev, _ = adds[ai]

            migration_events.append(
                ChangeEvent(
                    change_type="migration",
                    changed_name=rem_ev.changed_name,
                    associated_version=rem_ev.associated_version or "",
                    to_version=None,
                    to_name=add_ev.changed_name,
                    commit_sha=chosen_sha,
                    commit_url=chosen_rec.get("url"),
                    changed_file_path=fp,
                    changed_file_type=(add_ev.changed_file_type or rem_ev.changed_file_type or "modified"),
                )
            )

    # Remove duplicate migration events that describe the same change.
    seen = set()
    deduped: List[ChangeEvent] = []
    for ev in migration_events:
        k = (
            ev.change_type,
            normalize_pkg_name(ev.changed_name),
            normalize_pkg_name(ev.to_name or ""),
            ev.commit_sha,
            ev.changed_file_path,
        )
        if k in seen:
            continue
        seen.add(k)
        deduped.append(ev)

    # Remove duplicate migration candidates for the same pair of names.
    cand_seen = set()
    cand_out = []
    for a, b in migration_candidates:
        k = (normalize_pkg_name(a), normalize_pkg_name(b))
        if k in cand_seen:
            continue
        cand_seen.add(k)
        cand_out.append((a, b))

    return deduped, cand_out

def build_release_pair_change_row(rp: dict) -> dict:
    """
    Build the full change summary for one release pair.
    """
    rid = int(rp["id"])
    repo_id = int(rp["repo_id"])
    line_id = int(rp["release_line_id"])
    prev_id = int(rp["prev_release_id"])
    curr_id = int(rp["curr_release_id"])

    prev_by_file = load_snapshot_by_file(repo_id, line_id, prev_id)
    curr_by_file = load_snapshot_by_file(repo_id, line_id, curr_id)

    base_events = diff_release_snapshots(prev_by_file, curr_by_file)

    legacy_rows = load_legacy_events_for_pair(repo_id, line_id, prev_id, curr_id)
    exact_idx, relaxed_idx = index_legacy_events(legacy_rows)
    commit_candidates = attach_commit_candidates(base_events, exact_idx, relaxed_idx)

    migration_events, migration_candidates = derive_migration_events(base_events, commit_candidates)

    all_events = base_events + migration_events

    n_added = sum(1 for e in all_events if e.change_type == "added")
    n_removed = sum(1 for e in all_events if e.change_type == "removed")
    n_updated = sum(1 for e in all_events if e.change_type == "updated")
    n_migration = sum(1 for e in all_events if e.change_type == "migration")

    return {
        "id": rid,
        "repo_id": repo_id,
        "release_line_id": line_id,
        "prev_release_id": prev_id,
        "curr_release_id": curr_id,
        "num_changes": len(all_events),
        "num_additions": n_added,
        "num_removals": n_removed,
        "num_updates": n_updated,
        "num_migrations": n_migration,
        "migration_candidates": json.dumps(
            [{"from": a, "to": b} for a, b in migration_candidates],
            ensure_ascii=False,
        ),
        "events": all_events,
    }

def upsert_overview(row: dict):
    """
    Insert or update the overview row for one release pair.
    """
    cur.execute(
        f"""
        INSERT INTO {OVERVIEW_TABLE} (
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
        (
            row["id"],
            row["repo_id"],
            row["release_line_id"],
            row["prev_release_id"],
            row["curr_release_id"],
            row["num_changes"],
            row["num_additions"],
            row["num_removals"],
            row["num_updates"],
            row["num_migrations"],
            row["migration_candidates"],
        ),
    )


def replace_events(row: dict):
    """
    Replace the detailed event rows for one release pair.
    """
    cur.execute(
        f"""
        DELETE FROM {EVENT_TABLE}
        WHERE id = %s
          AND repo_id = %s
          AND release_line_id = %s
          AND prev_release_id = %s
          AND curr_release_id = %s
        """,
        (
            row["id"],
            row["repo_id"],
            row["release_line_id"],
            row["prev_release_id"],
            row["curr_release_id"],
        ),
    )

    values = []
    for ev in row["events"]:
        values.append((
            row["id"],
            row["repo_id"],
            row["release_line_id"],
            row["prev_release_id"],
            row["curr_release_id"],
            ev.change_type,
            ev.changed_name,
            ev.associated_version,
            ev.to_version,
            ev.to_name,
            ev.commit_sha,
            ev.commit_url,
            ev.changed_file_path,
            ev.changed_file_type,
            ev.is_valid_migration,
        ))

    if values:
        cur.executemany(
            f"""
            INSERT INTO {EVENT_TABLE} (
                id, repo_id, release_line_id, prev_release_id, curr_release_id,
                change_type, changed_name, associated_version, to_version, to_name,
                commit_sha, commit_url, changed_file_path, changed_file_type, is_valid_migration
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            values,
        )

def run_change_detection():
    """
    run the change detection pipeline from release pairs to final tables.
    """
    global db, conn, cur

    db = DatabaseConfig()
    conn, cur = db.create_db_connection()

    pairs = load_release_pairs(START_ID, END_ID, LIMIT)
    print(f"Total release pairs: {len(pairs)}")

    processed = 0
    failed = 0

    for rp in pairs:
        try:
            row = build_release_pair_change_row(rp)
            upsert_overview(row)
            replace_events(row)
            processed += 1

            if processed % max(COMMIT_EVERY, 1) == 0:
                conn.commit()
                print(f"Committed {processed}/{len(pairs)} pairs (last id={row['id']})")
        except Exception as e:
            failed += 1
            conn.rollback()
            print(
                f"[FAIL] id={rp.get('id')} repo={rp.get('repo_id')} "
                f"line={rp.get('release_line_id')} {type(e).__name__}: {e}"
            )

    conn.commit()
    print(f"Done. processed={processed}, failed={failed}")

    # Print a small summary after writing all rows.
    cur.execute(f"SELECT COUNT(*) AS n FROM {OVERVIEW_TABLE}")
    overview_n = int((cur.fetchone() or {}).get("n") or 0)

    cur.execute(f"SELECT COUNT(*) AS n FROM {EVENT_TABLE}")
    event_n = int((cur.fetchone() or {}).get("n") or 0)

    cur.execute(
        f"""
        SELECT change_type, COUNT(*) AS n
        FROM {EVENT_TABLE}
        GROUP BY change_type
        ORDER BY n DESC
        """
    )
    by_type = cur.fetchall() or []

    print("=== Snapshot-based Release-Line Dependency Changes ===")
    print(f"Overview rows: {overview_n}")
    print(f"Event rows: {event_n}")
    print("By change_type:")
    for r in by_type:
        print(f"- {r['change_type']}: {int(r['n'])}")

    db.close_db_connection()


if __name__ == "__main__":
    # Run the full change detection flow with the current local settings
    run_change_detection()
