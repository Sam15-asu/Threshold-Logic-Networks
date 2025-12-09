import csv
import re
import itertools

def parse_out_file(path):
    """
    Parses lines like:
      row0_1 / neuron0 / a|b|c|d / (Func: 9) / ...
    and normalizes 'zero'->'0', 'one'->'1'.
    Returns dict: { "row0_1": "a|b|c|d", ... }
    """
    mapping = {}
    with open(path) as f:
        for line in f:
            parts = line.strip().split(' / ')
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            expr = parts[2].strip().lower()
            if expr == 'zero':
                expr = '0'
            elif expr == 'one':
                expr = '1'
            mapping[name] = expr
    return mapping

def parse_min_file(path):
    """
    Parses lines like:
      row52_1(neuron52):a&b|a&c|a&d|b&c
    Returns dict: { "row52_1": "a&b|a&c|a&d|b&c", ... }
    """
    mapping = {}
    with open(path) as f:
        for line in f:
            if ':' not in line:
                continue
            left, expr = line.strip().split(':', 1)
            name = left.strip().split('(')[0]
            mapping[name] = expr.strip()
    return mapping

def eval_expr(expr, var_names, assignment):
    """
    Evaluates a boolean expression (~,&,|) using Python's not/and/or.
    0 and 1 are valid constants matching False/True.
    """
    # Handle negation
    def repl_not(m):
        v = m.group(1)
        return f"(not assignment['{v}'])"
    tmp = re.sub(r'~([a-z])', repl_not, expr)
    # Replace variables
    for v in var_names:
        tmp = re.sub(rf'\b{v}\b', f"assignment['{v}']", tmp)
    # Replace operators
    tmp = tmp.replace('&', ' and ').replace('|', ' or ')
    return eval(tmp)

def equivalent(expr1, expr2, var_names):
    for bits in itertools.product([0,1], repeat=len(var_names)):
        assign = {v: bool(b) for v,b in zip(var_names, bits)}
        if eval_expr(expr1, var_names, assign) != eval_expr(expr2, var_names, assign):
            return False
    return True

if __name__ == "__main__":
    out_path = "/home/sampad/lab/difflogic_thx/out/out_adult.txt"
    min_path = "/home/sampad/lab/difflogic_thx/minimized_expressions.txt"
    var_names = ['a','b','c','d','e']

    out_map = parse_out_file(out_path)
    min_map = parse_min_file(min_path)

    for name, out_expr in out_map.items():
        if name in min_map:
            min_expr = min_map[name]
            same = equivalent(out_expr, min_expr, var_names)
            result = "MATCH" if same else "DIFFER"
        else:
            result = "NO_MINIMIZED_EXPR"
        print(f"{name}: {result}")
