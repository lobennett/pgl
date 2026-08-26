# PGL

## USB failure harness

Start with the KVM provocation. It is the easiest controlled intervention and
records markers before and after the switch in the same CSV as the soak:

```bash
python3 -m harness.provoke --kvm --csv dpx-soak.csv
```

Exactly one provocation is required per run. The alternatives are `--power`,
`--bandwidth --bandwidth-source /path/to/existing-large-file`,
`--stale-handle`, `--no-flush`, and `--wiggle`. Bandwidth mode opens its source
read-only and rewinds it at EOF; it never creates or modifies traffic files.

For a short hardware-free check of the recommended path:

```bash
printf '\n\n' | python3 -m harness.provoke --kvm --simulate --max-triggers 3
```

## Setup
### 1. Clone library
```bash
git clone https://github.com/justingardner/pgl.git pgl
```

### 2. Install Conda Environment
```bash
# adding this solver can speed up environment creation, but is not necessary on all systems
conda install -n base conda-libmamba-solver
conda config --set solver libmamba

# must run this to create environment
conda env create -f pgl.yml
```

### 3. Install pgl into Conda environment

```
cd pgl
pip install -e .
```

### 4. Allow Accessibility for keyboard and mouse events

To get keyboard/mouse events you need to go to System Settings (in Apple menu at top left), choose Privacy & Security then Accessibility and make sure that Terminal.app is turned on.

If you are running VS Code, make sure to **run it from Terminal** instead of by double-clicking on the icon (so that Apple gives it the Accessibility permissions):
 ```bash
 /Applications/Visual\ Studio\ Code.app/Contents/MacOS/Code
```
