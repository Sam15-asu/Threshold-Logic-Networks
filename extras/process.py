import pandas as pd
import itertools

# Define input and output file names (single file for final output)
input_csv = "weights/model_effective_params_adult.csv"
output_csv = "weights/model_effective_params_adult_changed.csv"

# Read the original CSV file
df = pd.read_csv(input_csv)

############################
# Step 2: Add "negative index" column
############################
def add_negative_index(row):
    # Get the non_zero_indices values as a list (assuming they are comma-separated)
    indices = [item.strip() for item in row["non_zero_indices"].split(",")]
    negative_indices = []
    # Loop through weight columns w1 to w5
    for i in range(1, 6):
        weight_col = f"w{i}"
        try:
            weight = float(row[weight_col])
        except Exception:
            weight = 0.0
        if weight < 0:
            # Append the corresponding index if available
            if i - 1 < len(indices):
                negative_indices.append(indices[i - 1])
    # Return a comma-separated string of negative indices (or an empty string)
    return ", ".join(negative_indices)

# Create new column "negative index" in the DataFrame
df["negative index"] = df.apply(add_negative_index, axis=1)
print("Step 2 complete: 'negative index' column added.")

############################
# Step 3: Update bias by adding the absolute value of negative weights
############################
def update_bias(row):
    try:
        bias_val = float(row["bias"])
    except Exception:
        bias_val = 0.0
    # For each weight column, if negative, add its absolute value to bias
    for i in range(1, 6):
        weight_col = f"w{i}"
        try:
            weight_val = float(row[weight_col])
        except Exception:
            weight_val = 0.0
        if weight_val < 0:
            bias_val += abs(weight_val)
    return bias_val

# Update the "bias" column in the DataFrame
df["bias"] = df.apply(update_bias, axis=1)
print("Step 3 complete: Bias updated with absolute values of negative weights.")

############################
# Step 4: Change all negative weight magnitudes to positive weight magnitudes
############################
def convert_weights_to_abs(row):
    for i in range(1, 6):
        weight_col = f"w{i}"
        try:
            weight_val = float(row[weight_col])
            row[weight_col] = abs(weight_val)
        except Exception:
            # If conversion fails, leave the value unchanged.
            pass
    return row

# Apply the conversion for all weight columns
df = df.apply(convert_weights_to_abs, axis=1)
print("Step 4 complete: All negative weight magnitudes converted to positive.")

############################
# Step 5: Reorder weights (w1 to w5) and corresponding non_zero_indices in decreasing order
############################
def reorder_weights_indices(row):
    # Get the list of indices from the non_zero_indices column (assumed comma-separated)
    indices = [item.strip() for item in row["non_zero_indices"].split(",")]
    pairs = []
    # Create list of (weight, corresponding index) pairs for w1 to w5
    for i in range(1, 6):
        weight_col = f"w{i}"
        try:
            weight_val = float(row[weight_col])
        except Exception:
            weight_val = 0.0
        # Get the corresponding index (if exists)
        idx_val = indices[i - 1] if i - 1 < len(indices) else ""
        pairs.append((weight_val, idx_val))
    # Sort pairs in descending order by weight magnitude
    sorted_pairs = sorted(pairs, key=lambda x: x[0], reverse=True)
    # Update weight columns (w1 to w5) with sorted weight values
    for i, (w, _) in enumerate(sorted_pairs, start=1):
        row[f"w{i}"] = w
    # Update non_zero_indices with the corresponding indices in the new order
    row["non_zero_indices"] = ", ".join(idx for (_, idx) in sorted_pairs)
    return row

# Apply the reordering to each row in the DataFrame
df = df.apply(reorder_weights_indices, axis=1)
print("Step 5 complete: Weights and non_zero_indices reordered in decreasing order.")

# Write the final DataFrame to the output CSV file
df.to_csv(output_csv, index=False)
print(f"Final file saved to '{output_csv}'")


#Generate truth table

def generate_truth_table_dict_normal(w1, w2, w3, w4, w5, bias):
    """
    Generates a dictionary representing the truth table for the function:
      f = w1*x1 + w2*x2 + w3*x3 + w4*x4 + w5*x5
    The output is 1 if f >= bias, otherwise 0.
    
    The keys are the original 5-bit minterms (as strings, e.g. "01011"),
    and the values are the corresponding outputs.
    """
    truth_dict = {}
    # Iterate over all 2^5 combinations of 5 binary inputs.
    for combo in itertools.product([0, 1], repeat=5):
        # Create a minterm string representing the original inputs.
        minterm = "".join(str(bit) for bit in combo)
        # Use the inputs directly without inversion.
        x1, x2, x3, x4, x5 = combo
        # Compute the weighted sum using raw weights.
        f = w1 * x1 + w2 * x2 + w3 * x3 + w4 * x4 + w5 * x5
        output = 1 if f >= bias else 0
        truth_dict[minterm] = output
    return truth_dict

# Define the input CSV file (with weights and bias)
input_csv = "weights/model_effective_params_adult_changed.csv"
# Define the output CSV file for the normal truth tables
output_truth_csv = "weights/truth_tables_generated.csv"

# Read the CSV file into a DataFrame.
df = pd.read_csv(input_csv)

functions_dict = {}
# Process each row: generate the truth table using the normal method.
for index, row in df.iterrows():
    try:
        w1 = float(row["w1"])
        w2 = float(row["w2"])
        w3 = float(row["w3"])
        w4 = float(row["w4"])
        w5 = float(row["w5"])
        bias = float(row["bias"])
    except Exception as e:
        print(f"Error in row {index+1}: {e}")
        continue
    truth_table = generate_truth_table_dict_normal(w1, w2, w3, w4, w5, bias)
    functions_dict[f"row{index+1}"] = truth_table

# Get a sorted list of minterm keys (e.g., "00000", "00001", ..., "11111").
minterms = sorted(list(next(iter(functions_dict.values())).keys()))

# Build a DataFrame where each row corresponds to one function's truth table.
rows = []
for func, truth in functions_dict.items():
    row_data = {"Function": func}
    row_data.update({m: truth[m] for m in minterms})
    rows.append(row_data)

truth_df = pd.DataFrame(rows, columns=["Function"] + minterms)

# Save the resulting truth tables to the output CSV file.
truth_df.to_csv(output_truth_csv, index=False)
print(f"Normal truth tables saved to {output_truth_csv}")

#Match the truth tables

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