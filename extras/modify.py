import pandas as pd
import itertools

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
