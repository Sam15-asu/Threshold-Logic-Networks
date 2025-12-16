# pipeline.py
import os, re, csv
import numpy as np
import pandas as pd
import torch
from functools import reduce

# ===================== Fan-in config =====================
NUM_INPUTS = 8  # global representation width (conv uses up to 8; dense usually <=6)

# ===================== Layer mapping helper =====================
LAYER_MAP = {0: 1, 2: 3, 4: 5, 6: 7, 8: 9, 7: 11, 9: 13}

def get_layer_tag(param: str):
    if not isinstance(param, str):
        return None
    m = re.search(r'model\.(\d+)\.(?:gatebank(?:\.w)?|(?:effective_)?weight)\b', param)
    if not m:
        return None
    model_idx = int(m.group(1))
    return LAYER_MAP.get(model_idx)

# ===================== Exporter =====================

def build_effective_state_dict(state_dict: dict) -> dict:
    eff = {}
    for k, t in state_dict.items():
        if k.endswith("weight"):
            mkey = k.replace("weight", "mask")
            if mkey in state_dict:
                eff[k.replace("weight", "effective_weight")] = t * state_dict[mkey]
            else:
                eff[k] = t
        else:
            eff[k] = t
    return eff

def write_effective_params_csv(state_dict: dict, csv_filename: str):
    eff = build_effective_state_dict(state_dict)
    with open(csv_filename, 'w', newline='') as f:
        wtr = csv.writer(f)
        header = ['parameter','row_index','row_shape','non_zero_indices'] + \
                 [f"w{i}" for i in range(1, NUM_INPUTS + 1)] + ['bias']
        wtr.writerow(header)

        for name, param in eff.items():
            if any(tag in name for tag in ('.s', '.s_raw', '.mask')):
                continue

            if name.endswith(".gatebank.w"):
                base = name[:-2]
                theta_key = base + ".theta"
                idx_key   = base + ".idx"
                if theta_key not in eff or idx_key not in eff:
                    continue

                W   = param.detach().cpu().numpy()
                TH  = eff[theta_key].detach().cpu().numpy()
                IDX = eff[idx_key].detach().cpu().numpy()
                out_dim, fan_in_actual = W.shape

                for j in range(out_dim):
                    triples = [(abs(float(wv)), float(wv), int(ix)) for wv, ix in zip(W[j], IDX[j])]
                    if fan_in_actual > NUM_INPUTS:
                        triples.sort(key=lambda t: t[0], reverse=True)
                        triples = triples[:NUM_INPUTS]
                    weights = [t[1] for t in triples]
                    indices = [t[2] for t in triples]
                    while len(weights) < NUM_INPUTS:
                        weights.append("")

                    wtr.writerow([
                        name,
                        j,
                        (len(indices),),
                        ",".join(map(str, indices)),
                        *map(str, weights[:NUM_INPUTS]),
                        TH[j],
                    ])
                continue

            if name.endswith("weight") or name.endswith("effective_weight"):
                W = param.detach().cpu().numpy()
                bkey = name.replace("effective_weight", "bias") if name.endswith("effective_weight") else name.replace("weight", "bias")
                B = eff.get(bkey, None)
                Bnp = B.detach().cpu().numpy() if B is not None else None

                if W.ndim == 1:
                    row = W
                    nz_idx = np.nonzero(row)[0]
                    nz_val = row[nz_idx]
                    w_vals = [str(v) for v in nz_val]
                    while len(w_vals) < NUM_INPUTS:
                        w_vals.append("")
                    bias_val = Bnp[0] if (Bnp is not None and Bnp.size > 0) else ""
                    wtr.writerow(
                        [name, 0, row.shape, ",".join(map(str, nz_idx))]
                        + w_vals[:NUM_INPUTS]
                        + [bias_val]
                    )
                else:
                    for i, row in enumerate(W):
                        if row.ndim > 1:
                            row = row.flatten()
                        nz_idx = np.nonzero(row)[0]
                        nz_val = row[nz_idx]
                        w_vals = [str(v) for v in nz_val]
                        while len(w_vals) < NUM_INPUTS:
                            w_vals.append("")
                        bias_val = Bnp[i] if (Bnp is not None and i < (Bnp.shape[0] if hasattr(Bnp, 'shape') else 0)) else ""
                        wtr.writerow(
                            [name, i, row.shape, ",".join(map(str, nz_idx))]
                            + w_vals[:NUM_INPUTS]
                            + [bias_val]
                        )

# ===================== CSV processing =====================

def process_input_csv(input_csv, output_csv):
    df = pd.read_csv(input_csv)
    df = df[df["parameter"].apply(lambda p: get_layer_tag(str(p)) is not None)]

    def add_negative_index(row):
        indices = [item.strip() for item in str(row["non_zero_indices"]).split(",")]
        neg = []
        for i in range(1, NUM_INPUTS + 1):
            try:
                w = float(row[f"w{i}"])
            except Exception:
                w = 0.0
            if w < 0 and i-1 < len(indices):
                neg.append(indices[i-1])
        return ", ".join(neg)
    df["negative index"] = df.apply(add_negative_index, axis=1)

    def update_bias(row):
        try:
            b = float(row["bias"])
        except Exception:
            b = 0.0
        for i in range(1, NUM_INPUTS + 1):
            try:
                w = float(row[f"w{i}"])
            except Exception:
                w = 0.0
            if w < 0:
                b += abs(w)
        return b
    df["bias"] = df.apply(update_bias, axis=1)

    def to_abs(row):
        for i in range(1, NUM_INPUTS + 1):
            try:
                row[f"w{i}"] = abs(float(row[f"w{i}"]))
            except Exception:
                pass
        return row
    df = df.apply(to_abs, axis=1)

    def reorder(row):
        indices = [item.strip() for item in str(row["non_zero_indices"]).split(",")]
        neg_set = set([s.strip() for s in str(row["negative index"]).split(",") if s.strip()])
        pairs = []
        for i in range(1, NUM_INPUTS + 1):
            try:
                w = float(row[f"w{i}"])
            except Exception:
                w = 0.0
            idx = indices[i-1] if i-1 < len(indices) else ""
            pairs.append((w, idx, idx in neg_set))
        pairs.sort(key=lambda x: x[0], reverse=True)
        for i, (w, _, _) in enumerate(pairs, start=1):
            row[f"w{i}"] = w
        row["non_zero_indices"] = ", ".join(idx for (_, idx, _) in pairs)
        row["negative index"] = ", ".join(idx for (_, idx, f) in pairs if f)
        return row
    df = df.apply(reorder, axis=1)

    df.to_csv(output_csv, index=False)
    return output_csv

# ===================== Truth table generation =====================

def generate_truth_tables(input_csv, output_truth_csv):
    import itertools

    minterms = [''.join(bits) for bits in itertools.product('01', repeat=NUM_INPUTS)]
    df = pd.read_csv(input_csv)

    numeric_cols = [f"w{i}" for i in range(1, NUM_INPUTS + 1)] + ["bias"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    rows = []
    counter = 0
    for _, r in df.iterrows():
        param = str(r.get('parameter', ''))
        layer = get_layer_tag(param)
        if layer is None:
            continue

        try:
            w = [float(r[f"w{i}"]) for i in range(1, NUM_INPUTS + 1)]
            b = float(r["bias"])
        except Exception:
            continue

        truth = {}
        for combo in itertools.product([0, 1], repeat=NUM_INPUTS):
            s = sum(wi * xi for wi, xi in zip(w, combo))
            truth[''.join(map(str, combo))] = 1 if s >= b else 0

        func_label = f"row{int(r['row_index'])}_{layer}"
        row_data = {"Function": func_label, "neuron number": f"neuron{counter}"}
        row_data.update({m: truth[m] for m in minterms})
        rows.append(row_data)
        counter += 1

    if not rows:
        raise ValueError("generate_truth_tables: no rows produced. Check parameter names and layer mapping.")

    truth_df = pd.DataFrame(rows, columns=["Function","neuron number"] + minterms)
    truth_df.to_csv(output_truth_csv, index=False)
    return output_truth_csv

# ===================== Quine–McCluskey and minimization =====================

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
    os.makedirs(out_dir, exist_ok=True)

    proc_csv  = os.path.join(weights_dir, f"model_effective_params_{dataset_name}_changed.csv")
    truth_csv = os.path.join(weights_dir, f"truth_tables_generated_{dataset_name}.csv")
    output    = os.path.join(out_dir,    f"minimized_expressions_{dataset_name}.txt")

    var_names = [chr(ord('a') + i) for i in range(NUM_INPUTS)]  # ['a'..'h']
    order     = {v: i for i, v in enumerate(var_names)}

    df_proc = pd.read_csv(proc_csv)
    proc_map = {}
    for _, r in df_proc.iterrows():
        try:
            idx = int(r['row_index'])
        except Exception:
            continue
        param = str(r['parameter'])
        layer = get_layer_tag(param)
        if layer is None:
            continue
        key = f"row{idx}_{layer}"  # matches Function label
        proc_map[key] = {
            "non_zero": r.get('non_zero_indices', ""),
            "negative": r.get('negative index', "")
        }

    df_truth = pd.read_csv(truth_csv)
    if df_truth.empty:
        print(f"[minimize_truth_tables] No rows in {truth_csv}, skipping minimization.")
        return

    truth_cols = [c for c in df_truth.columns if c not in ['Function', 'neuron number']]

    expr_ids = {}
    next_id  = 0

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

# ===================== Pipeline wrapper =====================

def run_csv_pipeline(dataset_name, ckpt_path, weights_dir="weights", out_dir="out", do_stats=False):
    os.makedirs(weights_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    original_csv = os.path.join(weights_dir, f"model_effective_params_{dataset_name}.csv")
    state_dict = torch.load(ckpt_path, map_location='cpu')
    write_effective_params_csv(state_dict, original_csv)

    if do_stats:
        df = pd.read_csv(original_csv)
        wcols = [f"w{i}" for i in range(1, NUM_INPUTS + 1)]
        for c in wcols + ['bias']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        w_stats = (
            df.melt(id_vars=['parameter'], value_vars=wcols, value_name='w')
              .dropna(subset=['w'])
              .groupby('parameter')['w']
              .agg(weight_min='min', weight_max='max', num_weight_elements='count')
        )
        b_stats = (
            df[['parameter','bias']].dropna(subset=['bias'])
              .groupby('parameter')['bias']
              .agg(bias_min='min', bias_max='max', num_bias_elements='count')
        )
        stats = w_stats.join(b_stats, how='outer').fillna({'num_weight_elements':0,'num_bias_elements':0})
        # print(stats.head())

    processed_csv   = os.path.join(weights_dir, f"model_effective_params_{dataset_name}_changed.csv")
    truth_table_csv = os.path.join(weights_dir, f"truth_tables_generated_{dataset_name}.csv")

    process_input_csv(original_csv, processed_csv)
    generate_truth_tables(processed_csv, truth_table_csv)

    return original_csv, processed_csv, truth_table_csv

def run_full_logic_pipeline(dataset_name, ckpt_path, weights_dir="weights", out_dir="out", do_stats=False):
    original_csv, processed_csv, truth_table_csv = run_csv_pipeline(
        dataset_name=dataset_name,
        ckpt_path=ckpt_path,
        weights_dir=weights_dir,
        out_dir=out_dir,
        do_stats=do_stats,
    )
    minimize_truth_tables(dataset_name, weights_dir=weights_dir, out_dir=out_dir)
    return original_csv, processed_csv, truth_table_csv, os.path.join(out_dir, f"minimized_expressions_{dataset_name}.txt")
