"""
Sample library change events for the RQ2 documentation/trigger analysis.

This script draws a stratified sample from the validated library change
events table, keeps the target mix across change types, and writes the sampled
rows to a CSV file for annotation.
"""

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from utils.DBConfig import DatabaseConfig

EVENT_TABLE = 'release_line_library_change_events_after_validation'
SEED = 42
TARGET_TOTAL = 420
TARGET_BY_TYPE = {
    'added': 140,
    'updated': 140,
    'removed': 90,
    'migration': 50,
}

OUT_DIR = Path('<path_to_output_directory>')
OUT_DIR.mkdir(parents=True, exist_ok=True)
RUN_TS = datetime.now().strftime('%Y%m%d_%H%M%S')

print(f'Event table: {EVENT_TABLE}')
print(f'Target sample size: {TARGET_TOTAL}')
print(f'Target by type: {TARGET_BY_TYPE}')
print(f'Seed: {SEED}')

def get_table_columns(table_name: str):
    """Read the current column list for a table."""
    db = DatabaseConfig()
    _, cur = db.create_db_connection()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return [r['column_name'] for r in (cur.fetchall() or [])]

def pick_first_existing(candidates, existing_cols):
    """Pick the first column name that exists in the table."""
    for c in candidates:
        if c in existing_cols:
            return c
    return None

def normalize_change_type(x) -> str:
    """Normalize small change-type spelling variants into one label set."""
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode('utf-8', errors='ignore')
        except Exception:
            x = str(x)

    s = str(x or '').strip().lower()
    aliases = {
        'add': 'added',
        'added': 'added',
        'remove': 'removed',
        'removed': 'removed',
        'update': 'updated',
        'updated': 'updated',
        'migrate': 'migration',
        'migrated': 'migration',
        'migration': 'migration',
    }
    return aliases.get(s, s)

def compute_effective_targets(pop_by_type: dict, base_targets: dict, total_target: int) -> dict:
    """
    Adjust the requested sample targets to the actual population size.

    If one stratum has fewer rows than requested, the remaining quota is
    redistributed to other strata with spare capacity.
    """
    # Start from the requested targets, but do not exceed the available rows.
    effective = {t: min(int(base_targets.get(t, 0)), int(pop_by_type.get(t, 0))) for t in base_targets}
    assigned = sum(effective.values())
    remaining = int(total_target) - assigned

    if remaining <= 0:
        return effective

    # Reallocate the leftover quota to strata that still have spare rows.
    while remaining > 0:
        spare = {
            t: int(pop_by_type.get(t, 0)) - int(effective.get(t, 0))
            for t in effective
        }
        spare = {t: s for t, s in spare.items() if s > 0}
        if not spare:
            break

        total_spare = sum(spare.values())
        if total_spare <= 0:
            break

        allocated_this_round = 0
        for t, s in spare.items():
            add = int(np.floor(remaining * (s / total_spare)))
            if add > 0:
                add = min(add, s)
                effective[t] += add
                allocated_this_round += add

        if allocated_this_round == 0:
            # If rounding gives zero everywhere, fill one-by-one from the largest spare strata.
            for t, _ in sorted(spare.items(), key=lambda kv: kv[1], reverse=True):
                if remaining == 0:
                    break
                if effective[t] < pop_by_type.get(t, 0):
                    effective[t] += 1
                    allocated_this_round += 1
                    remaining -= 1
            continue

        remaining -= allocated_this_round

    return effective

def get_release_url_map(release_ids):
    """Build a small release-id to release-url lookup for the sampled rows."""
    db = DatabaseConfig()
    _, cur = db.create_db_connection()

    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'releases'
        """
    )
    rel_cols = {r['column_name'] for r in (cur.fetchall() or [])}

    url_col = None
    for c in ('html_url', 'url', 'release_url'):
        if c in rel_cols:
            url_col = c
            break

    ids = sorted({int(x) for x in release_ids if pd.notna(x)})

    placeholders = ','.join(['%s'] * len(ids))
    cur.execute(
        f"""
        SELECT id, {url_col} AS release_url
        FROM releases
        WHERE id IN ({placeholders})
        """,
        tuple(ids),
    )

    return {int(r['id']): (r.get('release_url') or '') for r in (cur.fetchall() or [])}

def main():
    """Load dependency change events, sample them, and save the output CSV."""
    db = DatabaseConfig()
    _, cur = db.create_db_connection()

    # Inspect the table first because some column names vary slightly across versions.
    cols = get_table_columns(EVENT_TABLE)
    print(f'Columns found in {EVENT_TABLE}: {len(cols)}')

    line_col = pick_first_existing(['release_line_id', 'relesae_line_id'], cols)
    id_col = pick_first_existing(['event_id', 'id'], cols)
    repo_col = pick_first_existing(['repo_id'], cols)
    pair_id_col = pick_first_existing(['id'], cols)

    required_cols = [
        pair_id_col, repo_col, line_col,
        'prev_release_id', 'curr_release_id',
        'change_type', 'changed_name', 'associated_version', 'to_version', 'to_name',
        'commit_sha', 'commit_url', 'changed_file_path', 'changed_file_type'
    ]
    required_cols = [c for c in required_cols if c and c in cols]
    required_cols = list(dict.fromkeys(required_cols))

    # Load the full event pool and keep only the fields needed for sampling and tracing.
    select_cols = ', '.join(required_cols)
    sql = f"""
    SELECT {select_cols}
    FROM {EVENT_TABLE}
    """

    cur.execute(sql)
    rows = cur.fetchall() or []
    df = pd.DataFrame(rows)
    df = df.loc[:, ~pd.Index(df.columns).duplicated(keep='first')].copy()

    raw_change_types = sorted({str(v).strip() for v in df['change_type'].dropna().unique().tolist()})
    print('Raw change_type values (sample):', raw_change_types[:20])

    # Normalize labels so the stratified targets use one consistent naming scheme.
    df['change_type'] = df['change_type'].map(normalize_change_type)
    print('Normalized change_type counts:')
    print(df['change_type'].value_counts(dropna=False).sort_index())

    df = df[df['change_type'].isin(TARGET_BY_TYPE.keys())].copy()

    # Build a stable row key so the sampled records can be traced back later.
    trace_cols = [
        c for c in [
            pair_id_col, repo_col, line_col,
            'prev_release_id', 'curr_release_id',
            'change_type', 'changed_name', 'associated_version',
            'to_version', 'to_name', 'commit_sha', 'changed_file_path'
        ]
        if c in df.columns
    ]
    trace_cols = list(dict.fromkeys(trace_cols))

    key_frame = df[trace_cols].copy()
    key_frame = key_frame.loc[:, ~pd.Index(key_frame.columns).duplicated(keep='first')].copy()
    key_frame = key_frame.fillna('').astype(str)

    df['sample_row_key'] = ['||'.join(row) for row in key_frame.to_numpy()]

    release_url_map = get_release_url_map(df['curr_release_id'].dropna().tolist())
    df['release_url'] = df['curr_release_id'].map(lambda x: release_url_map.get(int(x), '') if pd.notna(x) else '')

    print('events:', len(df))
    print(df['change_type'].value_counts().sort_index())

    # Work out how many rows to take from each change-type stratum.
    pop_by_type = df['change_type'].value_counts().to_dict()
    effective_targets = compute_effective_targets(pop_by_type, TARGET_BY_TYPE, TARGET_TOTAL)
    effective_total = int(sum(effective_targets.values()))

    print('Population by type:', pop_by_type)
    print('Effective targets:', effective_targets)
    print('Effective total:', effective_total)

    rng = np.random.default_rng(SEED)
    sample_frames = []
    for ctype, n_take in effective_targets.items():
        sub = df[df['change_type'] == ctype].copy()
        if n_take <= 0 or sub.empty:
            continue

        # The seed makes the sample reproducible for the replication package.
        sampled_idx = rng.choice(sub.index.to_numpy(), size=n_take, replace=False)
        sample_frames.append(sub.loc[sampled_idx].copy())

    sample_df = pd.concat(sample_frames, axis=0, ignore_index=True)
    sample_df = sample_df.sample(frac=1.0, random_state=SEED).reset_index(drop=True)

    print('Sampled rows:', len(sample_df))
    print(sample_df['change_type'].value_counts().sort_index())

    # Add simple weighting columns in case the sample is used for estimates later.
    sample_counts = sample_df['change_type'].value_counts().to_dict()
    pop_total = int(len(df))
    sample_total = int(len(sample_df))

    weight_map = {}
    for t in TARGET_BY_TYPE:
        Nh = int(pop_by_type.get(t, 0))
        nh = int(sample_counts.get(t, 0))
        weight_map[t] = (Nh / nh) if nh > 0 else np.nan

    sample_df['pop_stratum_n'] = sample_df['change_type'].map(lambda t: int(pop_by_type.get(t, 0)))
    sample_df['sample_stratum_n'] = sample_df['change_type'].map(lambda t: int(sample_counts.get(t, 0)))
    sample_df['weight_stratum'] = sample_df['change_type'].map(weight_map)
    sample_df['weight_norm'] = sample_df['weight_stratum'] * (sample_total / pop_total)

    out_path = OUT_DIR / f'sampled_events_{RUN_TS}.csv'
    sample_df.to_csv(out_path, index=False)
    print(f'Sampled data saved to: {out_path}')

    db.close_db_connection()


if __name__ == "__main__":
    main()
