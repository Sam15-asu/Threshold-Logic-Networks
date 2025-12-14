#!/usr/bin/env python3
import os
import csv
import itertools
from functools import reduce

import torch
import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────────────────────
# 1) Export effective weights from .pth → model_effective_params_<dataset>.csv
# ──────────────────────────────────────────────────────────────────────────────

def export_effective_params_to_csv(model_weights_path, dataset_name, weights_dir="weights"):
    """
    Loads a saved state_dict (.pth), builds effective_weight = weight * mask
    for masked layers, and writes model_effective_params_<dataset>.csv
    with up to 6 nonzero weights per neuron + bias.
    """
    os.makedirs(weights_dir, exist_ok=True)

    state_dict = torch.load(model_weights_path, map_location='cpu')

    effective_state_dict = {}

    # build effective_state_dict: replace weight with weight * mask when mask exists
    for key, tensor in state_dict.items():
        if key.endswith("weight"):
            mask_key = key.replace("weight", "mask")
            if mask_key in state_dict:
                effective_weight = tensor * state_dict[mask_key]
                effective_state_dict[key.replace("weight", "effective_weight")] = effective_weight
            else:
                effective_state_dict[key] = tensor
        else:
            effective_state_dict[key] = tensor

    csv_filename = os.path.join(weights_dir, f"model_effective_params_{dataset_name}.csv")

    with open(csv_filename, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            'parameter', 'row_index', 'row_shape',
            'non_zero_indices', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6',
            'bias'
        ])

        for name, param in effective_state_dict.items():
            if '.s' in name or '.mask' in name:
                continue

            if not (name.endswith("weight") or name.endswith("effective_weight")):
                continue

            weight_np = param.detach().cpu().numpy()

            # infer bias key
            if name.endswith("effective_weight"):
                bias_key = name.replace("effective_weight", "bias")
            else:
                bias_key = name.replace("weight", "bias")

            bias_tensor = effective_state_dict.get(bias_key, None)
            bias_np = bias_tensor.detach().cpu().numpy() if bias_tensor is not None else None

            if weight_np.ndim == 1:
                row = weight_np
                nonzero_indices = np.nonzero(row)[0]
                nonzero_values = row[nonzero_indices]
                w_vals = [str(val) for val in nonzero_values]
                while len(w_vals) < 6:
                    w_vals.append("")
                bias_value = bias_np[0] if (bias_np is not None and bias_np.size > 0) else ""
                writer.writerow([
                    name,
                    0,
                    row.shape,
                    ','.join(map(str, nonzero_indices))
                ] + w_vals[:6] + [bias_value])
            else:
                for i, row in enumerate(weight_np):
                    if row.ndim > 1:
                        row = row.flatten()
                    nonzero_indices = np.nonzero(row)[0]
                    nonzero_values = row[nonzero_indices]
                    w_vals = [str(val) for val in nonzero_values]
                    while len(w_vals) < 6:
                        w_vals.append("")
                    bias_value = bias_np[i] if (bias_np is not None and i < bias_np.shape[0]) else ""
                    writer.writerow([
                        name,
                        i,
                        row.shape,
                        ','.join(map(str, nonzero_indices))
                    ] + w_vals[:6] + [bias_value])

    # optional: stats pass (kept, but not printed)
    df = pd.read_csv(csv_filename)
    grouped = df.groupby('parameter')
    for _, group in grouped:
        all_weight_values = []
        all_bias_values = []
        for _, row in group.iterrows():
            for col in ['w1', 'w2', 'w3', 'w4', 'w5', 'w6']:
                if pd.notna(row[col]) and row[col] != "":
                    try:
                        all_weight_values.append(float(row[col]))
                    except ValueError:
                        pass
            if pd.notna(row['bias']) and row['bias'] != "":
                try:
                    all_bias_values.append(float(row['bias']))
                except ValueError:
                    pass
        _ = np.array(all_weight_values)
        _ = np.array(all_bias_values)

    return csv_filename


# ──────────────────────────────────────────────────────────────────────────────
# 2) CSV post-processing: signs → bias, abs weights, reorder
# ──────────────────────────────────────────────────────────────────────────────

def process_input_csv(input_csv, output_csv):
    df = pd.read_csv(input_csv)

    df["parameter"] = df["parameter"].astype(str)
    df = df[~df["parameter"].str.contains(r"model\.2\.weight|model\.5\.weight", na=False)]

    if "non_zero_indices" not in df.columns:
        df["non_zero_indices"] = ""
    df["non_zero_indices"] = df["non_zero_indices"].fillna("").astype(str).astype("object")

    if "bias" not in df.columns:
        df["bias"] = 0.0
    df["bias"] = pd.to_numeric(df["bias"], errors="coerce").fillna(0.0)

    for i in range(1, 7):
        col = f"w{i}"
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # mark negative indices
    def add_negative_index(row):
        indices = [s.strip() for s in str(row["non_zero_indices"]).split(",") if s.strip()]
        negative_indices = []
        for i in range(1, 7):
            w = row.get(f"w{i}", 0.0)
            w = 0.0 if pd.isna(w) else float(w)
            if w < 0 and (i - 1) < len(indices):
                negative_indices.append(indices[i - 1])
        return ", ".join(negative_indices)

    df["negative index"] = df.apply(add_negative_index, axis=1).astype("object")

    # fold negatives into bias
    def update_bias(row):
        bias_val = float(row["bias"])
        for i in range(1, 7):
            w = row.get(f"w{i}", 0.0)
            w = 0.0 if pd.isna(w) else float(w)
            if w < 0:
                bias_val += -w
        return bias_val

    df["bias"] = df.apply(update_bias, axis=1)

    # absolute weights
    def convert_weights_to_abs(row):
        for i in range(1, 7):
            col = f"w{i}"
            w = row.get(col, None)
            if pd.isna(w):
                continue
            row[col] = abs(float(w))
        return row

    df = df.apply(convert_weights_to_abs, axis=1)

    df["non_zero_indices"] = df["non_zero_indices"].astype("object")
    df["negative index"] = df["negative index"].astype("object")

    def reorder_weights_indices_row(row):
        indices = [s.strip() for s in str(row["non_zero_indices"]).split(",") if s.strip()]
        originally_negative = {s.strip() for s in str(row["negative index"]).split(",") if s.strip()}

        pairs = []
        for i in range(1, 7):
            w = pd.to_numeric(row.get(f"w{i}", 0.0), errors="coerce")
            w = 0.0 if pd.isna(w) else float(w)
            idx = indices[i - 1] if i - 1 < len(indices) else ""
            was_neg = idx in originally_negative
            pairs.append((w, idx, was_neg))

        pairs.sort(key=lambda x: x[0], reverse=True)

        new_weights = [p[0] for p in pairs]
        new_nzi = ", ".join(p[1] for p in pairs if p[1])
        new_neg = ", ".join(p[1] for p in pairs if p[2] and p[1])

        out = {f"w{i+1}": new_weights[i] for i in range(6)}
        out["non_zero_indices_new"] = new_nzi
        out["negative index_new"] = new_neg
        return pd.Series(out)

    _out = df.apply(reorder_weights_indices_row, axis=1)

    for i in range(1, 7):
        df[f"w{i}"] = pd.to_numeric(_out[f"w{i}"], errors="coerce").fillna(0.0)

    df["non_zero_indices"] = _out["non_zero_indices_new"].astype("object")
    df["negative index"] = _out["negative index_new"].astype("object")

    df.to_csv(output_csv, index=False)
    return output_csv


# ──────────────────────────────────────────────────────────────────────────────
# 3) Truth-table generation from 6-weight neuron
# ──────────────────────────────────────────────────────────────────────────────

def generate_truth_tables(input_csv, output_truth_csv):
    def generate_truth_table_dict_normal(w1, w2, w3, w4, w5, w6, bias):
        truth = {}
        for combo in itertools.product([0, 1], repeat=6):
            x1, x2, x3, x4, x5, x6 = combo
            f = w1*x1 + w2*x2 + w3*x3 + w4*x4 + w5*x5 + w6*x6
            truth["".join(str(b) for b in combo)] = 1 if f >= bias else 0
        return truth

    df = pd.read_csv(input_csv)
    functions_dict = {}

    for _, row in df.iterrows():
        try:
            w1 = float(row["w1"]); w2 = float(row["w2"]); w3 = float(row["w3"])
            w4 = float(row["w4"]); w5 = float(row["w5"]); w6 = float(row["w6"])
            bias = float(row["bias"])
        except Exception:
            continue

        try:
            neuron = int(row["row_index"])
        except Exception:
            continue

        param = str(row.get("parameter", ""))

        if "model.1.effective_weight" in param:
            layer = 1
        elif ("model.3.effective_weight" in param) or ("model.4.effective_weight" in param):
            layer = 2
        else:
            continue

        truth_table = generate_truth_table_dict_normal(w1, w2, w3, w4, w5, w6, bias)
        functions_dict[f"row{neuron}_{layer}"] = truth_table

    if not functions_dict:
        pd.DataFrame(columns=["Function", "neuron number"]).to_csv(output_truth_csv, index=False)
        return output_truth_csv

    first_truth = next(iter(functions_dict.values()))
    minterms = sorted(list(first_truth.keys()))

    rows = []
    for idx, (func, truth) in enumerate(functions_dict.items()):
        row_out = {"Function": func, "neuron number": f"neuron{idx}"}
        row_out.update({m: truth[m] for m in minterms})
        rows.append(row_out)

    pd.DataFrame(rows, columns=["Function", "neuron number"] + minterms).to_csv(output_truth_csv, index=False)
    return output_truth_csv


# ──────────────────────────────────────────────────────────────────────────────
# 4) Quine–McCluskey and minimization
# ──────────────────────────────────────────────────────────────────────────────

def _int_to_bits(n, num_vars):
    return tuple((n >> i) & 1 for i in reversed(range(num_vars)))

def _count_ones(bits):
    return sum(bits)

def _combine(a, b):
    diff = 0
    out  = []
    for x, y in zip(a, b):
        if x == y:
            out.append(x)
        else:
            diff += 1
            out.append(None)
    return tuple(out) if diff == 1 else None

def quine_mccluskey(truth, dont_care=None, var_names=None):
    if dont_care is None:
        dont_care = []
    n = len(truth)
    m = n.bit_length() - 1
    if var_names is None:
        var_names = [f"x{i}" for i in range(m)]

    minterms  = {i for i, v in enumerate(truth) if v == 1}
    dc_terms  = set(dont_care)
    all_terms = minterms | dc_terms

    groups = {}
    for t in all_terms:
        bits = _int_to_bits(t, m)
        groups.setdefault(_count_ones(bits), []).append(bits)

    prime_implicants = set()
    while groups:
        new_groups = {}
        marked     = set()
        for k in sorted(groups):
            for a in groups[k]:
                for b in groups.get(k + 1, []):
                    c = _combine(a, b)
                    if c:
                        key = _count_ones([bit for bit in c if bit == 1])
                        new_groups.setdefault(key, []).append(c)
                        marked.add(a)
                        marked.add(b)
        for bucket in groups.values():
            for term in bucket:
                if term not in marked:
                    prime_implicants.add(term)
        groups = {k: list(set(v)) for k, v in new_groups.items()}

    chart = {}
    for pi in prime_implicants:
        covers = {
            t for t in minterms
            if all(b is None or b == tb for b, tb in zip(pi, _int_to_bits(t, m)))
        }
        if covers:
            chart[pi] = covers

    essential = []
    uncovered = set(minterms)
    for t in minterms:
        covering = [pi for pi, cov in chart.items() if t in cov]
        if len(covering) == 1 and covering[0] not in essential:
            essential.append(covering[0])
    for pi in essential:
        uncovered -= chart[pi]

    best = set()
    if uncovered:
        P = []
        for t in uncovered:
            pis = [pi for pi, cov in chart.items() if t in cov]
            P.append({frozenset([pi]) for pi in pis})

        def _mul(A, B): return {a | b for a in A for b in B}
        candidates = reduce(_mul, P)
        min_size   = min(len(sol) for sol in candidates)
        best       = next(sol for sol in candidates if len(sol) == min_size)

    final = set(essential) | set(best)

    def _term_to_str(term):
        parts = []
        for name, bit in zip(var_names, term):
            if bit == 1:
                parts.append(name)
            elif bit == 0:
                parts.append(f"~{name}")
        return "1" if not parts else "&".join(parts)

    return "|".join(_term_to_str(pi) for pi in final)


def minimize_truth_tables(dataset_name, weights_dir="weights", out_dir="out"):
    """
    Reads:
      - model_effective_params_<dataset_name>_changed.csv
      - truth_tables_generated_<dataset_name>.csv
    and writes:
      - minimized_expressions_<dataset_name>.txt
    """
    os.makedirs(out_dir, exist_ok=True)

    proc_csv  = os.path.join(weights_dir, f"model_effective_params_{dataset_name}_changed.csv")
    truth_csv = os.path.join(weights_dir, f"truth_tables_generated_{dataset_name}.csv")
    output    = os.path.join(out_dir,    f"minimized_expressions_{dataset_name}.txt")

    var_names = ['a', 'b', 'c', 'd', 'e', 'f']
    order     = {v: i for i, v in enumerate(var_names)}

    expr_ids = {}
    next_id  = 0

    df_proc = pd.read_csv(proc_csv)
    proc_map = {}
    for _, r in df_proc.iterrows():
        try:
            idx = int(r['row_index'])
        except Exception:
            continue
        param = str(r['parameter'])
        if "model.1.effective_weight" in param:
            layer = 1
        elif "model.3.effective_weight" in param or "model.4.effective_weight" in param:
            layer = 2
        else:
            continue
        key = f"row{idx}_{layer}"
        proc_map[key] = {
            "non_zero": r.get('non_zero_indices', ""),
            "negative": r.get('negative index', "")
        }

    df_truth = pd.read_csv(truth_csv)
    if df_truth.empty:
        print(f"[minimize_truth_tables] No rows in {truth_csv}, skipping minimization.")
        return

    truth_cols = [c for c in df_truth.columns if c not in ['Function', 'neuron number']]

    with open(output, "w") as out:
        for _, r in df_truth.iterrows():
            func   = r['Function']
            neuron = r['neuron number']
            truth  = [int(r[c]) for c in truth_cols]

            if not any(truth):
                expr = "zero"
            else:
                expr = quine_mccluskey(truth, var_names=var_names)
                if expr == "1":
                    expr = "one"
                terms = expr.split("|") if expr else []
                def key_term(t):
                    if t in ("one", "zero", ""):
                        return [len(var_names)]
                    lits = [lit.lstrip("~") for lit in t.split("&")]
                    return [order.get(l, len(var_names)) for l in lits]
                expr = "|".join(sorted(terms, key=key_term)) if terms else "zero"

            fid = None
            if expr not in ("zero", "one"):
                if expr not in expr_ids:
                    expr_ids[expr] = next_id
                    next_id += 1
                fid = expr_ids[expr]

            info = proc_map.get(func, {"non_zero": "", "negative": ""})
            nz   = info["non_zero"]
            neg  = info["negative"]

            line = f"{func} / {neuron} / {expr}"
            if fid is not None:
                line += f" / (Func: {fid})"
            line += f" / non_zero_indices: {nz} / negative index: {neg}\n"
            out.write(line)

    print(f"[minimize_truth_tables] Wrote {len(expr_ids)} unique non-constant functions to {output}")


# ──────────────────────────────────────────────────────────────────────────────
# 5) Single entrypoint
# ──────────────────────────────────────────────────────────────────────────────

def run_full_logic_pipeline(dataset_name, model_weights_path, weights_dir="weights", out_dir="out"):
    """
    One-shot pipeline:
      .pth → model_effective_params.csv
            → model_effective_params_changed.csv
            → truth_tables_generated.csv
            → minimized_expressions.txt
    """
    export_effective_params_to_csv(model_weights_path, dataset_name, weights_dir=weights_dir)

    original_csv    = os.path.join(weights_dir, f"model_effective_params_{dataset_name}.csv")
    processed_csv   = os.path.join(weights_dir, f"model_effective_params_{dataset_name}_changed.csv")
    truth_table_csv = os.path.join(weights_dir, f"truth_tables_generated_{dataset_name}.csv")

    process_input_csv(original_csv, processed_csv)
    generate_truth_tables(processed_csv, truth_table_csv)
    minimize_truth_tables(dataset_name, weights_dir=weights_dir, out_dir=out_dir)
