#!/usr/bin/env python3
import os
import re

# ---------------- Config ----------------
INFILE  = os.path.join("/home/sampad/lab/threshold_convolution/out", "minimized_expressions_mnist.txt")
OUTFILE = os.path.join("/home/sampad/lab/threshold_convolution/out", "out.txt")

# ----- Layer-1 (single-channel 5x5 over 28x28) -----
H1, W1 = 28, 28
K1     = 5
S1     = 1
P1     = 0
HOUT1  = (H1 + 2*P1 - K1) // S1 + 1  # 24
WOUT1  = (W1 + 2*P1 - K1) // S1 + 1  # 24

# ----- Layer-2 (MaxPool 2x2, stride 2) over 16x24x24 -----
C2, H2, W2 = 16, 24, 24
K2, S2, P2 = 2, 2, 0
HOUT2      = (H2 + 2*P2 - K2) // S2 + 1  # 12
WOUT2      = (W2 + 2*P2 - K2) // S2 + 1  # 12

# ----- Layer-3 (Conv 3x3, s=1, p=1) over 16x12x12 (padded index space 14x14) -----
C3, H3, W3 = 16, 12, 12
K3, S3, P3 = 3, 1, 1
HOUT3      = (H3 + 2*P3 - K3) // S3 + 1  # 12
WOUT3      = (W3 + 2*P3 - K3) // S3 + 1  # 12

# ----- Layer-4 (MaxPool 2x2, stride 2) over 48x12x12 -----
C4, H4, W4 = 48, 12, 12
K4, S4, P4 = 2, 2, 0
HOUT4      = (H4 + 2*P4 - K4) // S4 + 1  # 6
WOUT4      = (W4 + 2*P4 - K4) // S4 + 1  # 6

# ----- Layer-5 (Conv 3x3, s=1, p=1) over 48x6x6 (padded index space 8x8) -----
C5, H5, W5 = 48, 6, 6
K5, S5, P5 = 3, 1, 1
HOUT5      = (H5 + 2*P5 - K5) // S5 + 1  # 6
WOUT5      = (W5 + 2*P5 - K5) // S5 + 1  # 6

# ----- Layer-6 (MaxPool 2x2, stride 2) over 144x6x6 -----
C6, H6, W6 = 144, 6, 6
K6, S6, P6 = 2, 2, 0
HOUT6      = (H6 + 2*P6 - K6) // S6 + 1  # 3
WOUT6      = (W6 + 2*P6 - K6) // S6 + 1  # 3

# -------- row tag matchers --------
def is_row1(line: str) -> bool: return re.match(r'^\s*row\d+_1\b', line) is not None
def is_row2(line: str) -> bool: return re.match(r'^\s*row\d+_2\b', line) is not None
def is_row3(line: str) -> bool: return re.match(r'^\s*row\d+_3\b', line) is not None
def is_row4(line: str) -> bool: return re.match(r'^\s*row\d+_4\b', line) is not None
def is_row5(line: str) -> bool: return re.match(r'^\s*row\d+_5\b', line) is not None
def is_row6(line: str) -> bool: return re.match(r'^\s*row\d+_6\b', line) is not None

# -------- helpers --------
def parse_index_list(line: str, field: str):
    m = re.search(rf'{re.escape(field)}:\s*([0-9,\s]+)', line)
    if not m:
        return False, None
    nums = [x.strip() for x in m.group(1).split(",") if x.strip() != ""]
    return True, [int(x) for x in nums]

def replace_field_values(line: str, field: str, new_values):
    new_val = ", ".join(str(x) for x in new_values)
    pattern = rf'({re.escape(field)}:\s*)([0-9,\s]+)'
    return re.sub(pattern, lambda m: m.group(1) + new_val, line, count=1)

def replace_row_tag_with_op(line: str, layer_suffix: int, op_number: int) -> str:
    return re.sub(
        rf'^(\s*row\d+)_({layer_suffix})\b',
        lambda m: f"{m.group(1)}_{op_number}_{m.group(2)}",
        line,
        count=1
    )

# -------- generic padded conv mapper --------
def map_local_list_to_global_padded(local_list, oy, ox, *, C, H, W, K, P):
    Hp, Wp = H + 2*P, W + 2*P
    KK = K * K
    HWp = Hp * Wp
    out = []
    for i in local_list:
        within = i % KK
        c      = i // KK
        ky, kx = divmod(within, K)
        row_p  = oy + ky
        col_p  = ox + kx
        out.append(c * HWp + row_p * Wp + col_p)
    return out

def map_local_list_to_global_layer1(local_list, oy, ox):
    return map_local_list_to_global_padded(local_list, oy, ox, C=1, H=H1, W=W1, K=K1, P=P1)

# ---------------- main ----------------
def main():
    if not os.path.exists(INFILE):
        raise FileNotFoundError(f"Input file not found: {INFILE}")
    os.makedirs(os.path.dirname(OUTFILE), exist_ok=True)

    with open(INFILE, "r") as fin, open(OUTFILE, "w") as fout:
        for raw_line in fin:
            line = raw_line.rstrip("\n")

            if not line.strip():
                fout.write(raw_line)
                continue

            # ---------- LAYER 1 ----------
            if is_row1(line):
                found_nz, local_nz = parse_index_list(line, "non_zero_indices")
                if not found_nz or not local_nz:
                    fout.write(raw_line); continue
                found_neg, local_neg = parse_index_list(line, "negative index")
                if not found_neg or local_neg is None:
                    local_neg = []
                for i in local_nz + local_neg:
                    if not (0 <= i < K1*K1):
                        raise ValueError(f"[L1] Local index {i} out of range [0,{K1*K1-1}] in: {line.strip()}")
                op_number = 1
                for oy in range(HOUT1):
                    for ox in range(WOUT1):
                        globals_nz = map_local_list_to_global_layer1(local_nz, oy, ox)
                        new_line   = replace_field_values(line, "non_zero_indices", globals_nz)
                        if local_neg:
                            globals_neg = map_local_list_to_global_layer1(local_neg, oy, ox)
                            new_line    = replace_field_values(new_line, "negative index", globals_neg)
                        new_line = replace_row_tag_with_op(new_line, layer_suffix=1, op_number=op_number)
                        fout.write(new_line + "\n")
                        op_number += 1
                continue

            # ---------- LAYER 2 (MaxPool 16x24x24 → 16x12x12) ----------
            if is_row2(line):
                op_number = 1
                HW = H2 * W2
                for c in range(C2):
                    base_c = c * HW
                    for oy in range(HOUT2):
                        r0 = oy * S2
                        for ox in range(WOUT2):
                            c0 = ox * S2
                            g0 = base_c + (r0    ) * W2 + (c0    )
                            g1 = base_c + (r0    ) * W2 + (c0 + 1)
                            g2 = base_c + (r0 + 1) * W2 + (c0    )
                            g3 = base_c + (r0 + 1) * W2 + (c0 + 1)
                            new_line = replace_field_values(line, "non_zero_indices", [g0, g1, g2, g3])
                            new_line = re.sub(r'(/\s*)neuron15(\s*/)', r'\1or\2', new_line, count=1)
                            new_line = replace_row_tag_with_op(new_line, layer_suffix=2, op_number=op_number)
                            fout.write(new_line + "\n")
                            op_number += 1
                continue

            # ---------- LAYER 3 (Conv 3x3, s=1, p=1) over 16x12x12, padded 14x14 ----------
            if is_row3(line):
                found_nz, local_nz = parse_index_list(line, "non_zero_indices")
                if not found_nz or not local_nz:
                    fout.write(raw_line); continue
                found_neg, local_neg = parse_index_list(line, "negative index")
                if not found_neg or local_neg is None:
                    local_neg = []
                max_i = C3 * K3 * K3  # 16 * 9 = 144
                for i in local_nz + local_neg:
                    if not (0 <= i < max_i):
                        raise ValueError(f"[L3] Local index {i} out of range [0,{max_i-1}] in: {line.strip()}")
                op_number = 1
                for oy in range(HOUT3):      # 12
                    for ox in range(WOUT3):  # 12
                        globals_nz  = map_local_list_to_global_padded(local_nz,  oy, ox, C=C3, H=H3, W=W3, K=K3, P=P3)
                        new_line    = replace_field_values(line, "non_zero_indices", globals_nz)
                        if local_neg:
                            globals_neg = map_local_list_to_global_padded(local_neg, oy, ox, C=C3, H=H3, W=W3, K=K3, P=P3)
                            new_line    = replace_field_values(new_line, "negative index", globals_neg)
                        new_line = replace_row_tag_with_op(new_line, layer_suffix=3, op_number=op_number)
                        fout.write(new_line + "\n")
                        op_number += 1
                continue

            # ---------- LAYER 4 (MaxPool 48x12x12 → 48x6x6) ----------
            if is_row4(line):
                op_number = 1
                HW = H4 * W4
                for c in range(C4):
                    base_c = c * HW
                    for oy in range(HOUT4):
                        r0 = oy * S4
                        for ox in range(WOUT4):
                            c0 = ox * S4
                            g0 = base_c + (r0    ) * W4 + (c0    )
                            g1 = base_c + (r0    ) * W4 + (c0 + 1)
                            g2 = base_c + (r0 + 1) * W4 + (c0    )
                            g3 = base_c + (r0 + 1) * W4 + (c0 + 1)
                            new_line = replace_field_values(line, "non_zero_indices", [g0, g1, g2, g3])
                            new_line = re.sub(r'(/\s*)neuron15(\s*/)', r'\1or\2', new_line, count=1)
                            new_line = replace_row_tag_with_op(new_line, layer_suffix=4, op_number=op_number)
                            fout.write(new_line + "\n")
                            op_number += 1
                continue

            # ---------- LAYER 5 (Conv 3x3, s=1, p=1) over 48x6x6, padded 8x8) ----------
            if is_row5(line):
                found_nz, local_nz = parse_index_list(line, "non_zero_indices")
                if not found_nz or not local_nz:
                    fout.write(raw_line); continue
                found_neg, local_neg = parse_index_list(line, "negative index")
                if not found_neg or local_neg is None:
                    local_neg = []
                max_i = C5 * K5 * K5  # 48 * 9 = 432
                for i in local_nz + local_neg:
                    if not (0 <= i < max_i):
                        raise ValueError(f"[L5] Local index {i} out of range [0,{max_i-1}] in: {line.strip()}")
                op_number = 1
                for oy in range(HOUT5):      # 6
                    for ox in range(WOUT5):  # 6
                        # start from original line each op
                        line0 = line
                        globals_nz  = map_local_list_to_global_padded(local_nz,  oy, ox, C=C5, H=H5, W=W5, K=K5, P=P5)
                        new_line    = replace_field_values(line0, "non_zero_indices", globals_nz)
                        if local_neg:
                            globals_neg = map_local_list_to_global_padded(local_neg, oy, ox, C=C5, H=H5, W=W5, K=K5, P=P5)
                            new_line    = replace_field_values(new_line, "negative index", globals_neg)
                        new_line = replace_row_tag_with_op(new_line, layer_suffix=5, op_number=op_number)
                        fout.write(new_line + "\n")
                        op_number += 1
                continue

            # ---------- LAYER 6 (MaxPool 144x6x6 → 144x3x3) ----------
            if is_row6(line):
                op_number = 1
                HW = H6 * W6
                for c in range(C6):             # 144 channels
                    base_c = c * HW
                    for oy in range(HOUT6):     # 3
                        r0 = oy * S6
                        for ox in range(WOUT6): # 3
                            c0 = ox * S6
                            g0 = base_c + (r0    ) * W6 + (c0    )
                            g1 = base_c + (r0    ) * W6 + (c0 + 1)
                            g2 = base_c + (r0 + 1) * W6 + (c0    )
                            g3 = base_c + (r0 + 1) * W6 + (c0 + 1)
                            new_line = replace_field_values(line, "non_zero_indices", [g0, g1, g2, g3])
                            new_line = re.sub(r'(/\s*)neuron15(\s*/)', r'\1or\2', new_line, count=1)
                            new_line = replace_row_tag_with_op(new_line, layer_suffix=6, op_number=op_number)
                            fout.write(new_line + "\n")
                            op_number += 1
                continue

            # ---------- Everything else ----------
            fout.write(raw_line)

    print(f"Wrote output to: {OUTFILE}")

if __name__ == "__main__":
    main()
