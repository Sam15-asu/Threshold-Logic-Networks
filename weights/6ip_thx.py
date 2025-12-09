#!/usr/bin/env python3
import os
import sys

def generate_verilog_modules(input_txt, output_sv, start_idx=117):
    seen = {}  # expr -> module_idx
    idx  = start_idx

    with open(input_txt) as f, open(output_sv, "w") as out:
        for line in f:
            parts = [p.strip() for p in line.strip().split("/")]

            # ensure we have at least 3 "/" delimiters
            if len(parts) < 3:
                continue

            expr = parts[2]        # the boolean expression
            # normalize spacing around operators just in case
            expr = expr.replace(" ", "")

            # find which inputs appear
            vars_in_expr = set([c for c in expr if c in "abcdef"])
            # only if it uses all six inputs
            if vars_in_expr != set("abcdef"):
                continue

            # dedupe
            if expr not in seen:
                seen[expr] = idx
                idx += 1

                # write the module
                mod_idx = seen[expr]
                out.write(f"module function_{mod_idx}(a,b,c,d,e,f,y,clk,rst);\n")
                out.write("  input a;\n")
                out.write("  input b;\n")
                out.write("  input c;\n")
                out.write("  input d;\n")
                out.write("  input e;\n")
                out.write("  input f;\n")
                out.write("  input clk;\n")
                out.write("  input rst;\n")
                out.write("  output reg y;\n\n")
                out.write("  always @(posedge clk) begin\n")
                out.write("    if (rst) begin\n")
                out.write("      y <= 0;\n")
                out.write("    end else begin\n")
                out.write(f"      y <= ({expr});\n")
                out.write("    end\n")
                out.write("  end\n")
                out.write("endmodule\n\n")

    print(f"Wrote {len(seen)} modules to {output_sv}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python make_modules.py <dataset_name>")
        sys.exit(1)

    dataset   = sys.argv[1]
    base_dir  = "/home/sampad/lab/thx_conv3/weights"
    input_txt = os.path.join(base_dir, f"minimized_expressions_{dataset}.txt")
    output_sv = os.path.join(base_dir, f"functions_{dataset}.sv")

    generate_verilog_modules(input_txt, output_sv, start_idx=117)
