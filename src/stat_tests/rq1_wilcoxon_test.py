"""
Wilcoxon signed-rank test comparing PTM and Dependency cadence distributions (release_line level) for RQ1.
"""

import pandas as pd

dep_cadence_df = pd.read_excel('data_files/dep_cadence_df.xlsx')
ptm_cadence_df = pd.read_excel('data_files/ptm_cadence_df.xlsx')

merged_df = pd.merge(
    ptm_cadence_df, 
    dep_cadence_df, 
    on=['repo_id', 'release_line_id'], 
    how='inner', 
    suffixes=('_ptm', '_dep')
)
print(merged_df.columns)

from scipy.stats import wilcoxon

# Define the pairs to test: (PTM column, Dep column, Label)
test_dimensions = [
    ('cadence_any_ptm', 'cadence_any_dep', 'Overall Cadence'),
    ('cadence_added_ptm', 'cadence_added_dep', 'Addition Cadence'),
    ('cadence_removed_ptm', 'cadence_removed_dep', 'Removal Cadence'),
    ('cadence_migration_ptm', 'cadence_migration_dep', 'Migration Cadence')
]

results = []

for ptm_col, dep_col, label in test_dimensions:
    temp_df = merged_df[[ptm_col, dep_col]].dropna()
    n = len(temp_df)
    
    if n < 5:
        print(f"Skipping {label}: Insufficient pairs (n={n})")
        continue
    
    # Wilcoxon requires at least one non-zero difference
    diff = temp_df[ptm_col] - temp_df[dep_col]
    if (diff == 0).all():
        print(f"Skipping {label}: All differences are zero.")
        continue
        
    stat, p_val = wilcoxon(temp_df[ptm_col], temp_df[dep_col])
    
    # Calculate Effect Size (Rank-Biserial Correlation r)
    # r = 1 - (2 * stat) / (n * (n + 1) / 2)
    r_val = 1 - (2 * stat) / (n * (n + 1) / 2)
    
    results.append({
        'Dimension': label,
        'n': n,
        'PTM Median': temp_df[ptm_col].median(),
        'Dep Median': temp_df[dep_col].median(),
        'p-value': p_val,
        'Rank-Biserial r': r_val
    })

results_df = pd.DataFrame(results)
print(results_df)