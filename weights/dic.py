import csv

# Dictionary to hold the mapping {Func: Boolean Expression}
func_expr_dict = {}

# Open and read the CSV file.
# Replace 'input.csv' with your CSV file name.
with open('/home/sampad/lab/difflogic_thx/weights/thx_func.csv', 'r') as csv_file:
    reader = csv.DictReader(csv_file)
    for row in reader:
        # Convert the Func value to an integer (if desired) and strip whitespace from the Boolean expression.
        key = int(row["Func"])
        value = row["Boolean Expression"].strip()
        func_expr_dict[key] = value

# Write the dictionary to a text file.
# The output will be a string representation of the dictionary, e.g., {0: 'a', 1: 'a&b', ... }
with open('output.txt', 'w') as txt_file:
    txt_file.write(str(func_expr_dict))

print("Dictionary created and written to output.txt successfully!")
