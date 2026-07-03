import ast, os, subprocess, sys

COMMIT = sys.argv[1] if len(sys.argv) > 1 else "b6735d9"
REPO = r"c:\Users\Michael\Documents\Kythera"
B = r"c:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"
SKIP_DIRS = {".git", "__pycache__", ".venv", ".idea", ".claude", "logs"}

def git_files():
    out = subprocess.run(["git", "-C", REPO, "ls-tree", "-r", "--name-only", COMMIT],
                         capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l.endswith(".py")]

def git_content(path):
    r = subprocess.run(["git", "-C", REPO, "show", f"{COMMIT}:{path}"], capture_output=True)
    return r.stdout

def live_files():
    out = {}
    for dirpath, dirnames, filenames in os.walk(B):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, f), B).replace("\\", "/")
                out[rel] = os.path.join(dirpath, f)
    return out

def dump(src):
    try:
        return ast.dump(ast.parse(src), include_attributes=False)
    except SyntaxError as e:
        return f"<SYNTAX ERROR {e}>"

ga = {p: None for p in git_files()}
lb = live_files()
common = sorted(set(ga) & set(lb))
diff, same = [], []
for rel in common:
    if dump(git_content(rel)) == dump(open(lb[rel], "rb").read()):
        same.append(rel)
    else:
        diff.append(rel)
print(f"commit {COMMIT}: common={len(common)} identical={len(same)} DIFFERENT={len(diff)}")
for r in diff: print("  DIFF:", r)
only_a = sorted(set(ga) - set(lb)); only_b = sorted(set(lb) - set(ga))
print("only in commit:", only_a)
print("only in live:", only_b)
