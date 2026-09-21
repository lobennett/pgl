# PGL

## Setup

```bash
git clone https://github.com/justingardner/pgl.git pgl
cd pgl

# Optional: use the faster Conda solver.
conda install -n base conda-libmamba-solver
conda config --set solver libmamba

conda env create -f pgl.yml
conda activate pgl
pip install -e .
```

## macOS permissions

PGL needs Accessibility permission for keyboard and mouse events. In **System Settings → Privacy & Security → Accessibility**, enable your terminal.

If you use VS Code, launch it from that terminal so it inherits the permission:

```bash
/Applications/Visual\ Studio\ Code.app/Contents/MacOS/Code
```
