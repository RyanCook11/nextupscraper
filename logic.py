import os
from pathlib import Path

root = Path(r"E:\Work\NextUpRecruitment\scraper")

def tree(dir_path: Path, prefix=""):
    contents = list(dir_path.iterdir())
    contents.sort(key=lambda p: str(p).lower())

    pointers = [("├── " if i < len(contents) - 1 else "└── ") for i in range(len(contents))]
    for pointer, path in zip(pointers, contents):
        if path.is_dir():
            print(f"{prefix}{pointer}{path.name}/")
            new_prefix = prefix + ("│   " if pointer == "├── " else "    ")
            tree(path, new_prefix)
        else:
            print(f"{prefix}{pointer}{path.name}")

print(root.name + "/")
tree(root)