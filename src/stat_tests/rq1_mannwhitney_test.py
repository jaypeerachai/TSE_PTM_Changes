"""
Mann-Whitney U test comparing PTM and Library cadence distributions (global level) for RQ1.
"""

import pandas as pd
from scipy.stats import mannwhitneyu
import numpy as np

dep_cadence_df = pd.read_excel('data_files/lib_cadence_df.xlsx')
ptm_cadence_df = pd.read_excel('data_files/ptm_cadence_df.xlsx')

def calculate_global_medians(df, prefix):
    stats = []
    # identify the cadence columns (added, removed, migration, any)
    cadence_cols = [c for c in df.columns if c.startswith('cadence_')]
    
    for col in cadence_cols:
        valid_vals = df[col].dropna()
        n_count = len(valid_vals)
        median_val = valid_vals.median()
        
        stats.append({
            'Category': col.replace('cadence_', '').replace('_ptm', '').replace('_dep', ''),
            'n_lines': n_count,
            'Median Cadence': round(median_val, 2)
        })
    
    return pd.DataFrame(stats)

# Calculate for PTMs
ptm_global_stats = calculate_global_medians(ptm_cadence_df, 'ptm')
print("=== PTM Global Medians (All lines with >=1 change) ===")
print(ptm_global_stats)

# calculate for Dependencies
dep_global_stats = calculate_global_medians(dep_cadence_df, 'dep')
print("\n=== Dependency Global Medians (All lines with >=1 change) ===")
print(dep_global_stats)

def cliffs_delta(lst1, lst2):
    """Calculates Cliff's Delta effect size."""
    m, n = len(lst1), len(lst2)
    matrix = np.zeros((m, n))
    for i in range(m):
        for j in range(n):
            if lst1[i] > lst2[j]: matrix[i, j] = 1
            elif lst1[i] < lst2[j]: matrix[i, j] = -1
    return np.sum(matrix) / (m * n)

categories = ['any', 'added', 'removed', 'migration']

print(f"{'Category':<12} | {'p-value':<10} | {'Cliff delta':<12}")
print("-" * 55)

for cat in categories:
    p_ptm = ptm_cadence_df["cadence" + "_" + cat].dropna().tolist()
    d_dep = dep_cadence_df["cadence" + "_" + cat].dropna().tolist()
    
    # Perform Mann-Whitney U
    stat, p_val = mannwhitneyu(p_ptm, d_dep, alternative='two-sided')
    
    # Calculate Effect Size
    delta = cliffs_delta(p_ptm, d_dep)
    
    abs_delta = abs(delta)
    
    print(f"{cat:<12} | {p_val:<10.4e} | {delta:<12.3f}")