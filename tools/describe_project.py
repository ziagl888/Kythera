"""Dev utility: dumps the directory tree + full source of every .py/.xml/.css/.scss
file under a selected directory into ``directory_info.txt``.

⚠ INFO-LEAK (P3.5, T-2026-CU-9050-096): the output file contains the COMPLETE
source of the codebase. Run it only on a local machine and never commit or share
``directory_info.txt`` — for Kythera it would expose the full trading logic, and
any secret hardcoded in a .py file (there should be none — secrets live in the
gitignored ``.env``/``.local/`` — but this tool would surface a regression). The
ignore set below skips ``.git``/``.local``/``.venv`` so those never land in the
dump; it still does not sanitise file *contents*.
"""

import os
import subprocess
import tkinter as tk
from tkinter import filedialog

# Directories never traversed — VCS internals, the gitignored secret store,
# virtualenvs and caches. Kept defensive because the tool dumps full source.
IGNORE_DIRS = {'.venv', '.git', '.local', '__pycache__', 'node_modules'}


def main():
    # Hide the root window
    root = tk.Tk()
    root.withdraw()

    # Ask user to select a directory
    selected_dir = filedialog.askdirectory(title="Select Directory")
    if not selected_dir:
        print("No directory selected. Exiting.")
        return

    print(
        "⚠ This writes the FULL source of the selected tree into "
        "directory_info.txt — do not share or commit that file."
    )

    # Create output file in the selected directory
    output_file = os.path.join(selected_dir, "directory_info.txt")

    with open(output_file, 'w', encoding='utf-8') as f:
        # Step 1: Execute tree command and write to file
        f.write("Directory Tree:\n")
        try:
            # -I ignores the secret/vcs/cache dirs (also works without -a)
            tree_output = subprocess.run(
                ['tree', '-I', '|'.join(IGNORE_DIRS), selected_dir],
                capture_output=True,
                text=True,
                encoding='utf-8'
            )
            f.write(tree_output.stdout)
            f.write("\n\n")
        except FileNotFoundError:
            f.write("Tree command not found. Falling back to manual directory listing.\n")
            # Manual tree-like listing using os.walk
            for root_dir, dirs, files in os.walk(selected_dir):
                # .venv-Verzeichnisse aus der weiteren Traversierung entfernen
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

                level = root_dir.replace(selected_dir, '').count(os.sep)
                indent = ' ' * 4 * level
                f.write(f"{indent}{os.path.basename(root_dir)}/\n")
                sub_indent = ' ' * 4 * (level + 1)
                for file in files:
                    f.write(f"{sub_indent}{file}\n")
            f.write("\n\n")

        # Step 2: List all .py, .xml, .css, .scss files with full paths and contents
        f.write("List of .py, .xml, .css, .scss files with contents:\n")
        for root_dir, dirs, files in os.walk(selected_dir):
            # .venv-Verzeichnisse aus der weiteren Traversierung entfernen
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

            for file in files:
                if file.lower().endswith(('.py', '.xml', '.css', '.scss')):
                    full_path = os.path.join(root_dir, file)
                    f.write(f"File: {full_path}\n")
                    try:
                        with open(full_path, 'r', encoding='utf-8') as infile:
                            content = infile.read()
                            f.write(content + "\n")
                    except UnicodeDecodeError:
                        f.write("Could not read file content due to encoding issues.\n")
                    f.write("\n---\n\n")  # Separator for next file

    print(f"Output written to: {output_file}")


if __name__ == "__main__":
    main()