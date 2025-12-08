#!/usr/bin/env python3
import os
import re
import sys

def extract_expr(line):
    """Return the 3rd slash-delimited field (the Boolean expression)."""
    parts = [p.strip() for p in line.split("/") if p.strip()]
    return parts[2] if len(parts) >= 3 else None

def find_vars(expr):
    """Return sorted list of distinct vars a–f appearing in expr."""
    return sorted(set(re.findall(r"[abcdef]", expr)))

def make_module(expr, idx):
    """
    Return a string containing the Verilog module for this expr as function_idx.
    Ports = any variables it uses, then y, clk, rst.
    """
    vars_ = find_vars(expr)
    ports = vars_ + ["y","clk","rst"]
    lines = []
    lines.append(f"module function_{idx}({','.join(ports)});\n")
    for v in vars_:
        lines.append(f"  input {v};\n")
    lines.append("  input clk;\n")
    lines.append("  input rst;\n")
    lines.append("  output reg y;\n\n")
    lines.append("  always @(posedge clk) begin\n")
    lines.append("    if (rst) begin\n")
    lines.append("      y <= 0;\n")
    lines.append("    end else begin\n")
    lines.append(f"      y <= ({expr});\n")
    lines.append("    end\n")
    lines.append("  end\n")
    lines.append("endmodule\n\n")
    return "".join(lines)

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 make_cmos_modules.py <dataset_name>")
        sys.exit(1)

    dataset = sys.argv[1]
    base     = "/home/sampad/lab/difflogic_thx2/out"
    base2    = "/home/sampad/lab/difflogic_thx2/reset_files"
    infile   = os.path.join(base, f"minimized_expressions_{dataset}.txt")
    outfile  = os.path.join(base, f"all_cmos_tf_rst_{dataset}.v")

    seen = {}  # expr -> module index
    idx  = 0

    # Collect unique non-constant expressions (skip "zero" and "one")
    with open(infile) as f:
        for line in f:
            expr = extract_expr(line)
            if not expr:
                continue
            expr = expr.replace(" ", "")
            if expr in ("zero", "one"):
                continue
            if expr not in seen:
                seen[expr] = idx
                idx += 1

    # Write Verilog file
    with open(outfile, "w") as out:
        out.write(f"// Generated CMOS modules for dataset: {dataset}\n\n")
        for expr, i in sorted(seen.items(), key=lambda kv: kv[1]):
            out.write(make_module(expr, i))

    print(f"Wrote {len(seen)} modules to {outfile}")

if __name__ == "__main__":
    main()
