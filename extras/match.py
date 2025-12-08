import pandas as pd

# Read the CSV files.
df_exp1 = pd.read_csv("weights/thx_truth_table.csv")
df_exp2 = pd.read_csv("weights/truth_tables_generated.csv")

# Identify the minterm columns.
# In thx_truth_table.csv, assume that "Function" and "Boolean Expression" are not minterm outputs.
minterm_cols_exp1 = [col for col in df_exp1.columns if col not in ["Function", "Boolean Expression"]]
# In truth_tables_generated.csv, assume that "Function" is not a minterm.
minterm_cols_exp2 = [col for col in df_exp2.columns if col != "Function"]

# Create a string signature for the minterm outputs in each row.
# This assumes the minterm columns are in the same order in both files.
df_exp1["minterm_str"] = df_exp1[minterm_cols_exp1].astype(str).agg("".join, axis=1)
df_exp2["minterm_str"] = df_exp2[minterm_cols_exp2].astype(str).agg("".join, axis=1)

# Initialize lists to store the result strings and unmatched function names.
results = []
unmatched_functions = []

# For each row in exp2, check all rows in exp1 for an exact match of minterm outputs.
for _, row_exp2 in df_exp2.iterrows():
    function_exp2 = row_exp2["Function"]
    exp2_signature = row_exp2["minterm_str"]
    
    # Find all rows in exp1 that have the same minterm signature.
    matching_rows = df_exp1[df_exp1["minterm_str"] == exp2_signature]
    
    if not matching_rows.empty:
        # For each match found, record the function from exp2 with the matching Boolean Expression from exp1.
        for _, row_exp1 in matching_rows.iterrows():
            boolean_expr = row_exp1["Boolean Expression"]
            results.append(f"{function_exp2}: {boolean_expr} (from thx_truth_table.csv)")
    else:
        # Check if all outputs in exp2 for this row are all zeros or all ones.
        if exp2_signature == "0" * len(exp2_signature):
            results.append(f"{function_exp2}: zero")
        elif exp2_signature == "1" * len(exp2_signature):
            results.append(f"{function_exp2}: one")
        else:
            results.append(f"{function_exp2}: no match found for this row.")
            unmatched_functions.append(function_exp2)

# Write the matching results to out.txt.
with open("out.txt", "w") as f:
    for line in results:
        f.write(line + "\n")

print("Matching results written to out.txt")

# If there are any functions with no match (after checking zeros and ones), print a summary.
if unmatched_functions:
    print("No match found corresponding to the following rows:")
    for fn in unmatched_functions:
        print(fn)
