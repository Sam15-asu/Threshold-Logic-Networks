#!/usr/bin/env python3
import os
import re
import sys

"""
Purpose
    Generate a Verilog wrapper that wires up functions into TWO layers where:
      - Layer 1 (formerly layer 7) reads from input_layer[...] and drives Y[255:0]
      - Layer 3 (formerly layer 9) reads from Y[...] and drives Z[255:0]
    Lines in the input file labeled with 7/9 are remapped to 1/3 automatically.

Key changes from your original
    - LAYERS_TO_KEEP = {"1","3"}
    - LAYER_REMAP_IN = {"7":"1", "9":"3"} (so old files still work)
    - All conditionals and comments now refer to layers 1 and 3
"""

# keep only these (target) layers
TARGET_LAYERS = {"1", "3"}

# if your input still uses old labels (7,9), remap them to (1,3)
LAYER_REMAP_IN = {"7": "1", "9": "3"}

def extract_expr(line):
    """Return the 3rd slash-delimited field (the Boolean expression)."""
    parts = [p.strip() for p in line.split("/") if p.strip()]
    return parts[2] if len(parts) >= 3 else None

def find_ordered_vars(expr):
    """
    Scan left-to-right and collect each of a–f exactly once.
    E.g. 'a|b|c|d&e' -> ['a','b','c','d','e']
    """
    out = []
    for c in expr:
        if c in "abcdef" and c not in out:
            out.append(c)
    return out

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 generate_wrapper.py <dataset_name>")
        sys.exit(1)
    dataset = sys.argv[1]

    # input + output paths
    infile_dir = r"/home/sampad/lab/difflogic_thx2/out"
    infile     = os.path.join(infile_dir, f"minimized_expressions_{dataset}.txt")
    output_dir = r"/home/sampad/lab/difflogic_thx2/out"
    os.makedirs(output_dir, exist_ok=True)
    outfile    = os.path.join(output_dir, f"{dataset}_6.v")

    zero_neurons = []
    one_neurons  = []
    used_Y       = set()  # layer 1 outputs
    used_Z       = set()  # layer 3 outputs
    zero_count   = 0
    one_count    = 0
    inst_count   = 0

    with open(infile, "r") as inp, open(outfile, "w") as out:
        # header
        out.write("`timescale 1ns / 1ps\n")
        out.write(f"`include \"all_cmos_tf_rst_{dataset}.v\"\n")
        out.write("`include \"all_cmos_tf_rst.v\"\n\n")
        out.write(f"module {dataset}_neural_network(\n")
        out.write("    input wire clk,\n")
        out.write("    input wire rst,\n")
        # NOTE: width here must cover the largest index seen for LAYER 1 (formerly 7).
        out.write("    input wire [115:0] input_layer, // TODO: widen if needed\n")
        out.write("    output wire [255:0] Z\n")
        out.write(");\n\n")
        out.write("  // Internal signals\n")
        out.write("  wire [255:0] Y; // layer 1 outputs feed layer 3\n\n")

        for line in inp:
            line = line.strip()
            if not line:
                continue

            tokens = [t.strip() for t in line.split("/")]
            if len(tokens) < 3:
                continue

            # parse row/layer from the first token (e.g., "row12_7")
            m1 = re.match(r'row(\d+)_(\d+)', tokens[0])
            if not m1:
                continue
            neuron_num, original_layer = m1.group(1), m1.group(2)

            # remap 7->1 and 9->3, then filter
            layer = LAYER_REMAP_IN.get(original_layer, original_layer)
            if layer not in TARGET_LAYERS:
                continue

            # expression (3rd field)
            expr = extract_expr(line)
            if not expr:
                continue
            expr_l = expr.lower().strip()

            # constants
            if expr_l in ("zero", "one"):
                val = "1'b0" if expr_l == "zero" else "1'b1"
                if layer == "1":
                    out.write(f"  assign Y[{neuron_num}] = {val};\n\n")
                    used_Y.add(int(neuron_num))
                    (zero_neurons if expr_l == "zero" else one_neurons).append(neuron_num)
                    zero_count += expr_l == "zero"
                    one_count  += expr_l == "one"
                else:  # layer == "3"
                    out.write(f"  assign Z[{neuron_num}] = {val};\n\n")
                    used_Z.add(int(neuron_num))
                    zero_count += expr_l == "zero"
                    one_count  += expr_l == "one"
                continue

            # non-constant: get Func#
            m2 = re.search(r'\(Func:\s*(\d+)\)', line)
            if not m2:
                continue
            func_id = m2.group(1)
            inst_count += 1

            # parse indices
            nz, neg = [], []
            for t in tokens:
                if t.startswith("non_zero_indices:"):
                    nz = [x.strip() for x in t[len("non_zero_indices:"):].split(",") if x.strip() != ""]
                if t.startswith("negative index:"):
                    neg = [x.strip() for x in t[len("negative index:"):].split(",") if x.strip() != ""]

            # derive ports from expression itself (a..f), up to len(nz)
            ports = find_ordered_vars(expr)[:len(nz)]

            # build port lines
            port_lines = []
            for i, p in enumerate(ports):
                idx = nz[i]
                # layer 1 reads input_layer, layer 3 reads Y
                sig = f"input_layer[{idx}]" if layer == "1" else f"Y[{idx}]"
                if idx in neg:
                    sig = f"~{sig}"
                port_lines.append(f"      .{p}({sig}),\n")

            # outputs
            if layer == "1":
                out_sig = f"Y[{neuron_num}]"
                used_Y.add(int(neuron_num))
            else:  # layer == "3"
                out_sig = f"Z[{neuron_num}]"
                used_Z.add(int(neuron_num))

            # instantiate
            inst_name = f"neuron{neuron_num}_{layer}"
            out.write(f"  // instantiate function_{func_id} for neuron{neuron_num}_{layer}\n")
            out.write(f"  function_{func_id} {inst_name} (\n")
            for pl in port_lines:
                out.write(pl)
            out.write(f"      .y({out_sig}),\n")
            out.write("      .clk(clk),\n")
            out.write("      .rst(rst)\n")
            out.write("  );\n\n")

        # summary & warnings
        out.write(f"// total zero consts: {zero_count}\n")
        out.write(f"// total one  consts: {one_count}\n")
        out.write(f"// total instantiations: {inst_count}\n\n")
        for i in range(256):
            if i not in used_Y:
                out.write(f"  // Warning: Y[{i}] not driven (layer 1)\n")
        for i in range(256):
            if i not in used_Z:
                out.write(f"  // Warning: Z[{i}] not driven (layer 3)\n")
        out.write("\nendmodule\n")

    print(f"Wrote Verilog wrapper to {outfile}")

if __name__ == "__main__":
    main()
