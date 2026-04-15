"""
Filter repositories and releases based on release-management quality.

This script applies the release-based filtering criteria used in the paper:
remove draft/prerelease tags, require at least two releases, filter irregular
release timing, drop inactive repositories, and keep only repositories with
mostly SemVer-like tags.
"""

import pandas as pd

from utilities.DBConfig import DatabaseConfig

db_config  = DatabaseConfig()
connection, cursor = db_config.create_db_connection()

query = """
SELECT * FROM releases
"""
cursor.execute(query)
# fetch results and convert to DataFrame
releases = cursor.fetchall()
releases_df = pd.DataFrame(releases)

# Criterion 1: remove draft and prerelease tags because they are not stable releases.
releases_df = releases_df[(releases_df.draft == 0) & (releases_df.prerelease == 0)]
releases_df = releases_df[['id', 'repo_id', 'tag_name', 'published_at']]

repo_group = releases_df.groupby('repo_id').size().reset_index(name='release_count')
repo_group.sort_values(by='release_count', ascending=False, inplace=True)

# Criterion 2: keep only repos with at least two releases so evolution can be observed.
repo_group = repo_group[repo_group.release_count > 1]
releases_df = releases_df.merge(repo_group, on='repo_id', how='inner')

releases_df['published_at'] = pd.to_datetime(releases_df['published_at'])

# Sort by repo_id and date
releases_df = releases_df.sort_values(['repo_id', 'published_at'])

# Compute the time difference between consecutive releases within each repo
releases_df['days_since_prev'] = (
    releases_df.groupby('repo_id')['published_at']
    .diff()
    .dt.days
)

# min/mean/med/max time between releases per repo
stat_release_intervals = (
    releases_df.groupby('repo_id')['days_since_prev']
    .agg(['size','min', 'mean', 'median', 'max'])
    .reset_index()
)
stat_release_intervals.sort_values(by='mean', ascending=True, inplace=True)

# Criterion 3a: remove repos with unusually short or long median release intervals.
filtered_stat_release_intervals = stat_release_intervals[(stat_release_intervals['median'] >= 7) & (stat_release_intervals['median'] <= 365)]

# Count total releases per repo
release_counts = releases_df.groupby('repo_id')['id'].count().reset_index(name='total_releases')

# Find the time span (in years) between first and last release
repo_span = (
    releases_df.groupby('repo_id')
    .agg(first_release=('published_at', 'min'), last_release=('published_at', 'max'))
    .reset_index()
)
repo_span['years_active'] = (repo_span['last_release'] - repo_span['first_release']).dt.days / 365

# Merge and compute frequency
frequency_df = release_counts.merge(repo_span, on='repo_id')
frequency_df['releases_per_year'] = frequency_df['total_releases'] / frequency_df['years_active']

# releases_per_week
frequency_df['releases_per_week'] = frequency_df['releases_per_year'] / 52

# releases_per_day
frequency_df['releases_per_day'] = frequency_df['releases_per_year'] / 365

# Criterion 3b: remove repos with more than one release per day.
high_freq_repos_day = frequency_df[frequency_df['releases_per_day'] > 1]
# remove high_freq_repos_day from filtered_stat_release_intervals
high_freq_repo_ids_day = high_freq_repos_day['repo_id'].tolist()
final_filtered_stat_release_intervals = filtered_stat_release_intervals[~filtered_stat_release_intervals['repo_id'].isin(high_freq_repo_ids_day)]

# Criterion 3c: remove repos with fewer than one release per year.
low_freq_repos = frequency_df[frequency_df['releases_per_year'] < 1]

# remove low_freq_repos from filtered_stat_release_intervals
low_freq_repo_ids = low_freq_repos['repo_id'].tolist()
final_filtered_stat_release_intervals = final_filtered_stat_release_intervals[~final_filtered_stat_release_intervals['repo_id'].isin(low_freq_repo_ids)]

print(f"Filtered repos: {final_filtered_stat_release_intervals.shape[0]}")
print(f"# of releases left: {final_filtered_stat_release_intervals['size'].sum()}")

# Criterion 4: remove repos that have not released anything in the recent period.
assert pd.api.types.is_datetime64_ns_dtype(frequency_df['last_release'])
cutoff = pd.Timestamp('2024-01-01')
mask = frequency_df['last_release'] < cutoff
print("True count:", mask.sum())
print("Min/Max last_release:", frequency_df['last_release'].min(), frequency_df['last_release'].max())
not_recent_releases = frequency_df.loc[mask].copy()

# remove not_recent_releases from filtered_stat_release_intervals
not_recent_repo_ids = not_recent_releases['repo_id'].tolist()
final_filtered_stat_release_intervals = final_filtered_stat_release_intervals[~final_filtered_stat_release_intervals['repo_id'].isin(not_recent_repo_ids)]

# merge every information together
final_repos = final_filtered_stat_release_intervals.merge(frequency_df, on='repo_id', how='inner')

# get every release info for the final repos
final_repo_ids = final_repos['repo_id'].tolist()
final_releases = releases_df[releases_df['repo_id'].isin(final_repo_ids)]


import re
import pandas as pd

# These rules implement the manual tag-style categories from the paper.

# Strict SemVer: standard MAJOR.MINOR.PATCH with optional prerelease/build info.
SEMVER_FULL = re.compile(
    r'^[Vv]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'    # -pre
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*) )?$',  # +build
)

# Lenient case: a full SemVer token appears inside a larger tag string.
SEMVER_TOKEN_ANYWHERE = re.compile(
    r'([Vv]?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
    r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)'
)

# Lenient case: 3-part version but prerelease markers are glued on.
MISUSED_RC_BETA_3PART = re.compile(r'^[Vv]?\d+\.\d+\.\d+(?:rc|RC|b|a)\d+$')  # v2.0.0rc1

# Lenient case: underscores used in place of dots.
MISUSED_UNDERSCORE_2 = re.compile(r'^\d+_\d+$')          # 1_7
MISUSED_UNDERSCORE_3 = re.compile(r'^\d+_\d+_\d+$')      # 1_7_5

# -------- NEW: two-part lenient detectors (for your Unknowns) --------
# Pure two-part numeric (allow leading V/v)
TWO_PART_NUM = re.compile(r'^[Vv]?\d+\.\d+$')            # V1.0, 3.5

# Two-part with rc/b/a stuck on the end: v0.1rc, 1.2b3
MISUSED_RC_BETA_2PART = re.compile(r'^[Vv]?\d+\.\d+(?:rc|RC|b|a)\d*$', re.IGNORECASE)

# Two-part with trailing label via - or _ or . : v1.1_FINAL, v0.1-iw, v2.99-R3.1, v.0.11
TWO_PART_WITH_LABEL = re.compile(r'^(?:[Vv]\.)?\d+\.\d+(?:[-_.](?:[A-Za-z][A-Za-z0-9.-]*))+$')

# Two-part numeric token appearing inside a longer string (prefix/suffix/path)
CONTAINS_TWO_PART = re.compile(r'[Vv]?\d+\.\d+(?:[-_.][A-Za-z0-9.-]+)?')

# Violated case: timestamps, nightly tags, or build-stamp style names.
NIGHTLY = re.compile(r'^nightly[-_].*', re.IGNORECASE)
LONG_DIGITS = re.compile(r'^(?:19|20)\d{12,}$')              # 20230307081844
HAS_LONG_DIGIT_SUFFIX = re.compile(r'.*(?:19|20)\d{6,}.*')    # release-20250309141326

# Violated case: calendar-style or year-based versioning.
YEAR_DOT = re.compile(r'^(?:19|20)\d{2}(?:\.\d+){1,3}$')     # 2024.1.0.0
YEAR_MONTH = re.compile(r'^(?:19|20)\d{2}[-_.]?(0[1-9]|1[0-2])(?:[-_.]v?\d+)?$')  # 2025-02, 2025-02-v2
YEAR_COMPACT = re.compile(r'^[Vv]?(?:19|20)\d{2}(0[1-9]|1[0-2])(?:[.\-_].*)?$')   # v202411.p1
MONTH_NAME_YEAR = re.compile(
    r'^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Q[1-4])[-_ ]?(?:19|20)\d{2}$',
    re.IGNORECASE
)

# Violated case: year plus some custom suffix.
YEAR_THEN_TRAIL = re.compile(r'^(?:19|20)\d{2}[A-Za-z].*')

# Violated case: short custom numeric tags without proper SemVer structure.
V_DOT_MINOR = re.compile(r'^[Vv]\.\d+$')          # e.g., v.045, v.02

# Violated case: arbitrary labels with no usable version token.
CUSTOM_WORDY = re.compile(r'^[A-Za-z][A-Za-z0-9_+&\- ]+$')

def _normalize_v_prefix(token: str) -> str:
    return token[1:] if token and token[0] in ('v','V') else token

def classify_tag(tag: str):
    s = (str(tag) if tag is not None else '').strip()

    # 1) Strict SemVer (3-part with proper -/+)
    if SEMVER_FULL.fullmatch(s):
        return ("Strict Semantic Versioning", "fullmatch", _normalize_v_prefix(s),
                "Strict SemVer (MAJOR.MINOR.PATCH[ -pre ][ +build ])")

    # 2) Lenient — 3-part misused prerelease glued
    if MISUSED_RC_BETA_3PART.fullmatch(s):
        g = re.match(r'^([Vv]?)(\d+\.\d+\.\d+)(rc|RC|b|a)(\d+)$', s)
        normalized = None
        if g:
            base = _normalize_v_prefix(g.group(2))
            label = g.group(3).lower()
            label = {'rc':'rc','b':'beta','a':'alpha'}.get(label, label)
            normalized = f"{base}-{label}.{g.group(4)}"
        return ("Lenient Semantic Versioning", "misused_sign_rcbeta_3part", normalized,
                "3-part rc/beta/alpha attached without hyphen")

    # 3) Lenient — underscores instead of dots
    if MISUSED_UNDERSCORE_2.fullmatch(s) or MISUSED_UNDERSCORE_3.fullmatch(s):
        repl = s.replace('_','.')
        normalized = repl if SEMVER_FULL.fullmatch(repl) else None
        return ("Lenient Semantic Versioning", "misused_sign_underscore", normalized,
                "underscores used instead of dots")

    # 4) Lenient — contains a proper 3-part token somewhere
    tok = SEMVER_TOKEN_ANYWHERE.search(s)
    if tok:
        return ("Lenient Semantic Versioning", "name_prefix_or_suffix",
                _normalize_v_prefix(tok.group(1)), "contains valid SemVer token")

    # -------- NEW: handle two-part variants you listed --------
    # 5) Lenient — pure two-part numeric (V1.0, 3.5)
    if TWO_PART_NUM.fullmatch(s):
        return ("Lenient Semantic Versioning", "two_part_numeric", None, "only TWO numeric parts")

    # 6) Lenient — two-part with rc/b/a glued (v0.1rc, 1.2b3)
    if MISUSED_RC_BETA_2PART.fullmatch(s):
        g = re.match(r'^([Vv]?)(\d+\.\d+)(rc|RC|b|a)(\d*)$', s)
        normalized = None
        if g:
            base = _normalize_v_prefix(g.group(2)) + ".0"   # assume missing PATCH → .0
            label = g.group(3).lower()
            label = {'rc':'rc','b':'beta','a':'alpha'}.get(label, label)
            suffix = f".{g.group(4)}" if g.group(4) else ""  # rc / rc.1
            normalized = f"{base}-{label}{suffix}"
        return ("Lenient Semantic Versioning", "two_part_rcbeta", normalized,
                "two-part rc/beta/alpha without hyphen or patch")

    # 7) Lenient — two-part with trailing label / weird dot after V (v1.1_FINAL, v0.1-iw, v2.99-R3.1, v.0.11)
    if TWO_PART_WITH_LABEL.fullmatch(s):
        # Try to suggest normalization: promote to PATCH=0 and keep label as prerelease
        m = re.search(r'(\d+\.\d+)(?:[-_.]([A-Za-z][A-Za-z0-9.-]*))+$', s.replace('V.','').replace('v.',''))
        normalized = None
        if m:
            base = m.group(1) + ".0"
            # keep everything after the first separator as a prerelease id (sanitize underscores)
            tail = s.split(m.group(1),1)[1].lstrip('-_.')
            tail = tail.replace('_','-')
            normalized = f"{base}-{tail}"
        return ("Lenient Semantic Versioning", "two_part_with_label", normalized,
                "two-part with trailing label / v. prefix")

    # 8) Lenient — contains a two-part token somewhere (e.g., Flux-V1.0, Release-v3.3, adapters1.0)
    if CONTAINS_TWO_PART.search(s):
        return ("Lenient Semantic Versioning", "two_part_token_in_name", None,
                "contains two-part version token in name")

    # 9) Violated — timestamp/nightly/build-stamp
    if NIGHTLY.fullmatch(s) or LONG_DIGITS.fullmatch(s) or HAS_LONG_DIGIT_SUFFIX.fullmatch(s):
        return ("Violated Semantic Versioning", "timestamp_or_buildstamp", None, "timestamp/build stamp or nightly tag")

    # 10) Violated — calendar/year-based (including compact and '2025v1')
    if YEAR_DOT.fullmatch(s) or YEAR_MONTH.fullmatch(s) or YEAR_COMPACT.fullmatch(s) or YEAR_THEN_TRAIL.fullmatch(s) or MONTH_NAME_YEAR.fullmatch(s):
        return ("Violated Semantic Versioning", "calendar_based", None, "calendar/quarter/month-based tag")

    # 11) Violated — short-form "v.<digits>" (no proper MAJOR.MINOR[.PATCH])
    if V_DOT_MINOR.fullmatch(s):
        return ("Violated Semantic Versioning", "shortform_v_dot_minor", None,
                "short-form tag like 'v.<digits>' (no MINOR/PATCH)")

    # 12) Violated — custom words (no version token)
    if CUSTOM_WORDY.fullmatch(s):
        return ("Violated Semantic Versioning", "custom_id", None, "custom/labeled tag without SemVer token")

    # Fallback
    return ("Violated Semantic Versioning", "unclassified", None, "no rule matched")


def classify_tags(series_or_list):
    ser = pd.Series(series_or_list, dtype="string")
    out = ser.apply(lambda x: pd.Series(classify_tag(x),
                                        index=['category','subcategory','normalized','reason']))
    out.insert(0, 'tag_name', ser)
    return out


# Apply the SemVer rules to the filtered release set.
sv_df = final_releases.copy()
sv_df['tag_name'] = sv_df['tag_name'].astype("string")

tag_cls = classify_tags(sv_df['tag_name']).drop(columns=['tag_name'])

# Keep alignment by row order so each tag stays attached to its release row.
sv_cls = pd.concat(
    [sv_df.reset_index(drop=True), tag_cls.reset_index(drop=True)],
    axis=1
)

assert len(sv_cls) == len(sv_df)

# This helps summarize whether a tag still carries a recognizable SemVer token.
sv_cls['has_semver_token'] = sv_cls['subcategory'].eq('name_prefix_or_suffix') | \
                                   sv_cls['category'].eq('Strict Semantic Versioning')

# Aggregate tag quality at the repository level.
agg = (
    sv_cls.groupby('repo_id')
    .agg(
        n=('id','count'),
        strict_ratio=('category', lambda s: (s=='Strict Semantic Versioning').mean()),
        token_ratio=('has_semver_token','mean'),
        lenient_ratio=('category', lambda s: (s=='Lenient Semantic Versioning').mean()),
        violated_ratio=('category', lambda s: (s=='Violated Semantic Versioning').mean()),
        last_release=('published_at','max'),
    )
    .reset_index()
)

# Criterion 5a: remove repos whose strict+lenient SemVer ratio is below 0.8.
violated_df = agg[agg['strict_ratio'] + agg['lenient_ratio'] < 0.8]
print(f"Number of repos where strict + lenient < 0.8: {len(violated_df)}")

violated_ids = violated_df['repo_id'].tolist()
# not in violated_ids
filtered_sv_cls = sv_cls[~sv_cls['repo_id'].isin(violated_ids)]
print(f"Number of releases after filtering: {len(filtered_sv_cls)}")
filtered_agg = agg[~agg['repo_id'].isin(violated_ids)]
print(f"Number of repos after filtering: {len(filtered_agg)}")

have_violated_df = filtered_agg[filtered_agg['violated_ratio'] > 0]
have_violated_ids = have_violated_df['repo_id'].tolist()
# Criterion 5b: keep the repo, but drop releases that are individually violated tags.
violated_release_df = filtered_sv_cls[filtered_sv_cls['repo_id'].isin(have_violated_ids) & filtered_sv_cls['category'].eq('Violated Semantic Versioning')]
violated_release_ids = violated_release_df['id'].tolist()
filtered_sv_cls = filtered_sv_cls[~filtered_sv_cls['id'].isin(violated_release_ids)]

# merge information together between final_repos and filtered_agg (drop duplicate columns)
final_repos = final_repos.merge(filtered_agg, on='repo_id', how='inner', suffixes=('', '_agg'))
print(final_repos.shape)

# merge every information together between final_releases and filtered_sv_cls (drop duplicate columns)
final_releases = final_releases.merge(filtered_sv_cls.drop(columns=['repo_id','tag_name','published_at','release_count','days_since_prev','category','subcategory','normalized','reason','has_semver_token']), on='id', how='inner', suffixes=('', '_sv'))
print(final_releases.shape)