#!/usr/bin/env python3
import os
import sys
import re
import itertools
import pandas as pd

# ──────────────────────────────────────────────────────────────────────────────
# Layer mapping helper (UPDATED: gatebanks 0/2/4 → 1/3/5; FC 7/9 unchanged)
# ──────────────────────────────────────────────────────────────────────────────
GATEBANK_MAP = {0: 1, 2: 3, 4: 5}   # model.0.gatebank→1, model.2.gatebank→3, model.4.gatebank→5
WEIGHT_MAP   = {7: 7, 9: 9}         # model.7.(effective_)weight→7, model.9.(effective_)weight→9

def get_layer_tag(param: str):
    """
    Map:
      - model.{0,2,4}.gatebank[.w]           -> layers {1,3,5}
      - model.{7,9}.(effective_)?weight      -> layers {7,9}
    Return None for everything else.
    """
    if not isinstance(param, str):
        return None

    # Conv gatebanks (accept 'gatebank' or 'gatebank.w')
    m = re.search(r'model\.(\d+)\.gatebank(?:\.w)?\b', param)
    if m:
        return GATEBANK_MAP.get(int(m.group(1)))

    # FC weights (accept 'weight' or 'effective_weight')
    m = re.search(r'model\.(\d+)\.(?:effective_)?weight\b', param)
    if m:
        return WEIGHT_MAP.get(int(m.group(1)))

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Quine–McCluskey implementation (unchanged)
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

    minterms  = {i for i,v in enumerate(truth) if v==1}
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
                for b in groups.get(k+1, []):
                    c = _combine(a, b)
                    if c:
                        key = _count_ones([bit for bit in c if bit==1])
                        new_groups.setdefault(key, []).append(c)
                        marked.add(a); marked.add(b)
        for bucket in groups.values():
            for term in bucket:
                if term not in marked:
                    prime_implicants.add(term)
        groups = {k:list(set(v)) for k,v in new_groups.items()}

    chart = {}
    for pi in prime_implicants:
        covers = {
            t for t in minterms
            if all(b is None or b==tb for b,tb in zip(pi, _int_to_bits(t, m)))
        }
        if covers:
            chart[pi] = covers

    essential = []
    uncovered = set(minterms)
    for t in minterms:
        covering = [pi for pi,cov in chart.items() if t in cov]
        if len(covering)==1 and covering[0] not in essential:
            essential.append(covering[0])
    for pi in essential:
        uncovered -= chart[pi]

    best = set()
    if uncovered:
        P = []
        for t in uncovered:
            pis = [pi for pi,cov in chart.items() if t in cov]
            P.append({frozenset([pi]) for pi in pis})
        from functools import reduce
        def _mul(A,B): return {a|b for a in A for b in B}
        candidates = reduce(_mul, P)
        min_size   = min(len(sol) for sol in candidates)
        best       = next(sol for sol in candidates if len(sol)==min_size)

    final = set(essential) | set(best)

    def _term_to_str(term):
        parts = []
        for name, bit in zip(var_names, term):
            if bit==1:
                parts.append(name)
            elif bit==0:
                parts.append(f"~{name}")
        return "1" if not parts else "&".join(parts)

    return "|".join(_term_to_str(pi) for pi in final)

# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    dataset   = sys.argv[1] if len(sys.argv)>1 else "fashionMNIST"
    base_dir  = "/home/sampad/lab/thx_conv3/weights"
    base_dir2 = "/home/sampad/lab/thx_conv3/out"
    proc_csv  = os.path.join(base_dir,  f"model_effective_params_{dataset}_changed.csv")
    truth_csv = os.path.join(base_dir,  f"truth_tables_generated_{dataset}.csv")
    output    = os.path.join(base_dir2, f"minimized_expressions_{dataset}.txt")

    var_names = ['a','b','c','d','e','f']   # 6-input functions
    order     = {v:i for i,v in enumerate(var_names)}

    expr_ids = {}
    next_id  = 0

    # 1) read processed CSV for indices → build lookup by our Function label
    df_proc = pd.read_csv(proc_csv)
    proc_map = {}
    for _, r in df_proc.iterrows():
        idx   = int(r['row_index'])
        param = r['parameter']
        layer = get_layer_tag(param)    # <-- includes conv gatebanks AND FC layers per mapping
        if layer is None:
            continue
        key = f"row{idx}_{layer}"
        proc_map[key] = {
            "non_zero": r.get('non_zero_indices', ""),
            "negative": r.get('negative index', "")
        }

    # 2) read truth table
    df_truth   = pd.read_csv(truth_csv)
    truth_cols = [c for c in df_truth.columns if c not in ['Function','neuron number']]

    # 3) generate output with Func IDs (skip zero/one)
    with open(output, "w") as out:
        for _, r in df_truth.iterrows():
            func   = r['Function']          # must match "row<row_index>_<layer>"
            neuron = r['neuron number']
            truth  = [int(r[c]) for c in truth_cols]

            # compute expr
            if not any(truth):
                expr = "zero"
            else:
                expr = quine_mccluskey(truth, var_names=var_names)
                if expr == "1":
                    expr = "one"
                # sort terms deterministically by a..f (cosmetic)
                terms = expr.split("|")
                def key_term(t):
                    if t in ("one","zero"):
                        return [len(var_names)]
                    lits = [lit.lstrip("~") for lit in t.split("&")]
                    return [order.get(l, len(var_names)) for l in lits]
                expr = "|".join(sorted(terms, key=key_term))

            # assign Func ID only if not zero/one
            fid = None
            if expr not in ("zero","one"):
                if expr not in expr_ids:
                    expr_ids[expr] = next_id
                    next_id += 1
                fid = expr_ids[expr]

            # indices
            info = proc_map.get(func, {"non_zero":"", "negative":""})
            nz   = info["non_zero"]
            neg  = info["negative"]

            # build line
            line = f"{func} / {neuron} / {expr}"
            if fid is not None:
                line += f" / (Func: {fid})"
            line += f" / non_zero_indices: {nz} / negative index: {neg}\n"
            out.write(line)

    print(f"Wrote {len(expr_ids)} unique non-constant functions to {output}")
