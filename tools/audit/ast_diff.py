import ast, sys, os

A = r"c:\Users\Michael\Documents\Kythera"
B = r"c:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"
SKIP_DIRS = {".git", "__pycache__", ".venv", ".idea", ".claude", "logs"}

def py_files(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                out[rel] = full
    return out

def ast_dump(path):
    with open(path, "rb") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"<SYNTAX ERROR: {e}>"
    return ast.dump(tree, include_attributes=False)

fa, fb = py_files(A), py_files(B)
only_a = sorted(set(fa) - set(fb))
only_b = sorted(set(fb) - set(fa))
common = sorted(set(fa) & set(fb))

same, diff = [], []
for rel in common:
    if ast_dump(fa[rel]) == ast_dump(fb[rel]):
        same.append(rel)
    else:
        diff.append(rel)

print(f"common .py files: {len(common)}, AST-identical: {len(same)}, AST-DIFFERENT: {len(diff)}")
print("\n--- AST-DIFFERENT (real code differences) ---")
for r in diff: print(" ", r)
print("\n--- only in Kythera ---")
for r in only_a: print(" ", r)
print("\n--- only in live (crypto_trading_bot_v2) ---")
for r in only_b: print(" ", r)
