"""Test-run technical-debt tools from the literature on the session branches.

Each tool below has been used in published TD / code-quality studies (or, for
the architecture checks, implements smell definitions from them). This script
runs them all on every session snapshot so their output can be compared with
SonarQube's before deciding which ones go into the notebook.

    tool                     TD type            used in
    -----------------------  -----------------  ------------------------------------------
    ESLint smell rules       code smells        Johannes et al. 2019 (SQJ), JS smells vs faults
    Semgrep                  security           AI-generated PR security studies (arXiv 2604.19965)
    SATD comment tags        self-admitted TD   Potdar & Shihab 2014; tag-based (MAT) detection
    PMD CPD                  duplication        standard clone detector (.ts only - no .tsx support)
    jscpd                    duplication        cross-check for CPD (also handles .tsx)
    Arcan-style smells       architecture       Arcan smell definitions (Fontana et al.), on a
                                                dependency-cruiser import graph (Arcan is Java-only)
    archlint                 architecture       TS-native smell detector - not yet validated in a study
    Sonargraph (Explorer)    architecture       cycles, propagation cost (MacCormack et al.), structural
                                                debt index; needs .sonargraph-activation + a one-off
                                                TypeScript system in .tools/sonargraph-template/

Everything installs into .tools/ (gitignored) on first run. Snapshots are taken
with `git archive`, so the clones under projects/ are never checked out or modified.

usage:
    python test.py                         # all repos, all tools
    python test.py --repos aida-makbar3    # one repo
    python test.py --tools eslint,satd     # subset of tools
"""
import argparse, collections, io, json, os, re, shutil, subprocess, sys, tarfile, urllib.request, zipfile
from pathlib import Path

import networkx as nx
import pandas as pd

WORKSPACE = Path(__file__).resolve().parent
PROJECTS  = WORKSPACE / "projects"
RESULTS   = WORKSPACE / "results"
TOOLS     = WORKSPACE / ".tools"
NODE_DIR  = TOOLS / "node"
VENV_DIR  = TOOLS / "venv"
PMD_VER   = "7.28.0"
PMD_DIR   = TOOLS / f"pmd-bin-{PMD_VER}"
SNAP_DIR  = TOOLS / "snapshots"
RAW_DIR   = TOOLS / "raw"

# Sonargraph-Build is installed by hand (licensed download), not by setup()
SONARGRAPH_BUILD = Path(os.environ.get(
    "SONARGRAPH_BUILD",
    r"E:\SonarGraph\SonargraphBuild-26.6.1.203_2026-10-29\SonargraphBuild-26.6.1.203_2026-10-29"))
SONARGRAPH_TEMPLATE   = TOOLS / "sonargraph-template" / "aida.sonargraph"   # made once in the GUI
SONARGRAPH_ACTIVATION = WORKSPACE / ".sonargraph-activation"               # gitignored

# session branches, in session order. dpenner2's `microservice` is skipped: it is
# the same code as s3.
REPOS = {
    "aida-dpenner2": ["main", "s1", "s2", "s3", "s4"],
    "aida-makbar3":  ["main", "mujtaba/session-1", "mujtaba/session-2", "mujtaba/session-3"],
}

# scope matches the notebooks: apps/ + services/, minus the VS Code extension
SRC_DIRS     = ["apps", "services"]
EXCLUDE_GLOB = ["**/node_modules/**", "**/.venv/**", "**/dist/**", "**/build/**", "apps/extension/**"]
SKIP_PARTS   = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".turbo"}
CODE_EXT     = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"}

IS_WIN = os.name == "nt"
NODE_BIN = NODE_DIR / "node_modules" / ".bin"
def nbin(name):
    return NODE_BIN / (f"{name}.cmd" if IS_WIN else name)


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


# ===================================================================== setup ==
def setup():
    TOOLS.mkdir(exist_ok=True)
    if not (NODE_DIR / "node_modules" / "@archlinter").exists():
        print("installing node tools ...")
        NODE_DIR.mkdir(exist_ok=True)
        if not (NODE_DIR / "package.json").exists():
            (NODE_DIR / "package.json").write_text('{"name": "td-tools", "private": true}')
        # typescript@5: dependency-cruiser does not support typescript 7 yet
        r = run(["npm", "install", "--silent", "eslint@9", "typescript-eslint", "typescript@5",
                 "jscpd", "dependency-cruiser", "@archlinter/cli"], cwd=NODE_DIR, shell=IS_WIN)
        if r.returncode:
            sys.exit(r.stderr)

    if not PMD_DIR.exists():
        print(f"downloading PMD {PMD_VER} ...")
        url = (f"https://github.com/pmd/pmd/releases/download/pmd_releases/{PMD_VER}/"
               f"pmd-dist-{PMD_VER}-bin.zip")
        with urllib.request.urlopen(url) as resp:
            zipfile.ZipFile(io.BytesIO(resp.read())).extractall(TOOLS)

    semgrep = VENV_DIR / ("Scripts/semgrep.exe" if IS_WIN else "bin/semgrep")
    if not semgrep.exists():
        print("installing semgrep ...")
        run([sys.executable, "-m", "venv", VENV_DIR])
        pip = VENV_DIR / ("Scripts/pip.exe" if IS_WIN else "bin/pip")
        r = run([pip, "install", "-q", "semgrep"])
        if r.returncode:
            sys.exit(r.stderr)
    return semgrep


# ================================================================= snapshots ==
def snapshot(repo, branch, refresh=False):
    """Export origin/<branch> without touching the clone. Symlinks and committed
    virtualenvs / node_modules are skipped (counted, not extracted)."""
    dest = SNAP_DIR / f"{repo}--{branch.split('/')[-1]}"
    meta = dest / ".snapshot.json"
    if meta.exists() and not refresh:
        return dest, json.loads(meta.read_text())
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(PROJECTS / repo), "archive", f"origin/{branch}"],
                         capture_output=True, check=True).stdout
    skipped = collections.Counter()
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        members = []
        for m in t.getmembers():
            parts = set(Path(m.name).parts)
            if m.issym() or m.islnk():
                continue
            if parts & {".venv", "venv", "node_modules"}:
                skipped["committed_vendored_files"] += m.isfile()
                continue
            members.append(m)
        t.extractall(dest, members=members, filter="data")
    meta.write_text(json.dumps(dict(skipped)))
    return dest, dict(skipped)


def code_files(root):
    out = []
    for d in SRC_DIRS:
        for p in (root / d).rglob("*") if (root / d).exists() else []:
            rel = p.relative_to(root).parts
            if p.is_file() and p.suffix in CODE_EXT and not (set(rel) & SKIP_PARTS) \
                    and rel[:2] != ("apps", "extension"):
                out.append(p)
    return out


def src_dirs(root):
    return [d for d in SRC_DIRS if (root / d).exists()]


# ==================================================================== tools ==
# --- ESLint smells -------------------------------------------------------------
# Johannes et al. (2019) detected 12 JavaScript smells with ESLint. The 9 below
# map onto built-in rules; "chained methods", "complex switch case" and
# "variable re-assign" (beyond parameters) need custom rules and are left out.
# Thresholds are ESLint's defaults - check them against the paper before reporting.
ESLINT_SMELLS = {
    "lengthy_lines":        ("max-len", ["warn", {"code": 120, "ignoreStrings": True,
                                                  "ignoreTemplateLiterals": True, "ignoreUrls": True}]),
    "long_parameter_list":  ("max-params", ["warn", 4]),
    "nested_callbacks":     ("max-nested-callbacks", ["warn", 3]),
    "complex_code":         ("complexity", ["warn", 10]),
    "depth":                ("max-depth", ["warn", 4]),
    "long_method":          ("max-statements", ["warn", 15]),
    "assign_in_condition":  ("no-cond-assign", ["warn", "always"]),
    "param_reassign":       ("no-param-reassign", ["warn"]),
    "extra_bind":           ("no-extra-bind", ["warn"]),
}
RULE_TO_SMELL = {rule: smell for smell, (rule, _) in ESLINT_SMELLS.items()}

def eslint(root, tag, _):
    cfg = NODE_DIR / "eslint-smells.config.mjs"
    rules = {rule: opts for rule, opts in ESLINT_SMELLS.values()}
    cfg.write_text(
        "import tseslint from 'typescript-eslint';\n"
        "export default [\n"
        f"  {{ ignores: {json.dumps(EXCLUDE_GLOB)} }},\n"
        "  { files: ['**/*.{ts,tsx,js,jsx,mjs,cjs}'],\n"
        "    languageOptions: { parser: tseslint.parser,\n"
        "      parserOptions: { ecmaFeatures: { jsx: true } } },\n"
        f"    rules: {json.dumps(rules)} }},\n"
        "];\n", encoding="utf-8")
    # --no-inline-config: the projects' own eslint-disable comments name plugin
    # rules that are not installed here, and would otherwise hide smells
    # services/ is Python-only, and ESLint aborts on a path with no JS in it
    r = run([nbin("eslint"), "-c", cfg, "--no-inline-config", "--no-warn-ignored",
             "--format", "json", "apps"], cwd=root)
    if r.returncode == 2:                                   # 2 = ESLint crashed, not "found problems"
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "eslint crashed")
    (RAW_DIR / f"eslint-{tag}.json").write_text(r.stdout, encoding="utf-8")
    msgs = [m for f in json.loads(r.stdout or "[]") for m in f["messages"]]
    parse_errors = sum(1 for m in msgs if m.get("fatal"))
    c = collections.Counter(RULE_TO_SMELL.get(m.get("ruleId")) for m in msgs if not m.get("fatal"))
    return {"eslint_smells": sum(c.values()), "eslint_parse_errors": parse_errors,
            **{f"eslint_{s}": c.get(s, 0) for s in ESLINT_SMELLS}}


COMPLEX_FN_THRESHOLD = 10      # McCabe; same cut-off as the "complex code" smell above

def eslint_function_complexity(root, tag, _):
    """Cyclomatic complexity of every function, via ESLint's `complexity` rule
    with a limit of 0 (so it reports all of them). Gives how concentrated the
    complexity is - the share of functions over the threshold - which averages hide."""
    cfg = NODE_DIR / "eslint-complexity.config.mjs"
    cfg.write_text(
        "import tseslint from 'typescript-eslint';\n"
        "export default [\n"
        f"  {{ ignores: {json.dumps(EXCLUDE_GLOB)} }},\n"
        "  { files: ['**/*.{ts,tsx,js,jsx,mjs,cjs}'],\n"
        "    languageOptions: { parser: tseslint.parser,\n"
        "      parserOptions: { ecmaFeatures: { jsx: true } } },\n"
        "    rules: { complexity: ['warn', 0] } },\n"
        "];\n", encoding="utf-8")
    r = run([nbin("eslint"), "-c", cfg, "--no-inline-config", "--no-warn-ignored",
             "--format", "json", "apps"], cwd=root)
    if r.returncode == 2:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "eslint crashed")
    (RAW_DIR / f"eslint-complexity-{tag}.json").write_text(r.stdout, encoding="utf-8")
    ccn = [int(m.group(1)) for f in json.loads(r.stdout or "[]") for msg in f["messages"]
           if msg.get("ruleId") == "complexity" and (m := re.search(r"complexity of (\d+)", msg["message"]))]
    if not ccn:
        return {"fn_count": 0}
    over = sum(c > COMPLEX_FN_THRESHOLD for c in ccn)
    ccn.sort()
    return {"fn_count": len(ccn), "fn_over_threshold": over,
            "fn_pct_over_threshold": round(100 * over / len(ccn), 1),
            "fn_ccn_p90": ccn[int(0.9 * (len(ccn) - 1))], "fn_ccn_max": ccn[-1]}


# --- Semgrep -------------------------------------------------------------------
SEMGREP_CONFIGS = ["p/javascript", "p/typescript", "p/react", "p/nodejs", "p/secrets", "p/python"]

def semgrep(root, tag, semgrep_bin):
    out = RAW_DIR / f"semgrep-{tag}.json"
    cfg = [a for c in SEMGREP_CONFIGS for a in ("--config", c)]
    run([semgrep_bin, "scan", "--metrics=off", *cfg, "--exclude", "apps/extension",
         "--json", "-q", "-o", out, *src_dirs(root)],
        cwd=root, env=dict(os.environ, PYTHONUTF8="1"))
    res = json.loads(out.read_text(encoding="utf-8"))["results"] if out.exists() else []
    sev = collections.Counter(r["extra"]["severity"] for r in res)
    return {"semgrep_findings": len(res), "semgrep_error": sev.get("ERROR", 0),
            "semgrep_warning": sev.get("WARNING", 0)}


# --- SATD ----------------------------------------------------------------------
# tag-based detection (TODO/FIXME/HACK/XXX, as in MAT) plus a few of Potdar &
# Shihab's recurring textual patterns
SATD_TAGS     = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
SATD_PATTERNS = re.compile(r"\b(workaround|temporary|for now|quick fix|hacky|ugly|"
                           r"needs? (to be )?(refactor|cleanup|fix)|not (ideal|sure))\b", re.I)
COMMENT       = re.compile(r"(//.*$|/\*.*?\*/|#.*$|^\s*\*.*$)")

def satd(root, tag, _):
    tags = pats = files_with = 0
    for f in code_files(root):
        hit = False
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = COMMENT.search(line)
            if not m:
                continue
            t, p = len(SATD_TAGS.findall(m.group())), bool(SATD_PATTERNS.search(m.group()))
            tags, pats = tags + t, pats + p
            hit = hit or t or p
        files_with += bool(hit)
    return {"satd_tag_comments": tags, "satd_pattern_comments": pats, "satd_files": files_with}


# --- PMD CPD -------------------------------------------------------------------
CPD_MIN_TOKENS = [100, 50]     # 100 = PMD's default; 50 matches jscpd's default

def _cpd_mirror(root, tag):
    """CPD's TypeScript module only picks up .ts, which would skip every React
    page. CPD only *tokenizes* (no parsing), and the TypeScript lexer handles JSX
    fine, so mirror the sources with .tsx renamed to .tsx.ts."""
    mirror = RAW_DIR / f"cpd-src-{tag}"
    if mirror.exists():
        shutil.rmtree(mirror)
    for f in code_files(root):
        if f.suffix not in (".ts", ".tsx"):
            continue
        dst = mirror / f.relative_to(root)
        dst = dst.with_name(dst.name + ".ts") if f.suffix == ".tsx" else dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dst)
    return mirror

def pmd_cpd(root, tag, _):
    mirror = _cpd_mirror(root, tag)
    if not mirror.exists():
        return {}
    exe = PMD_DIR / "bin" / ("pmd.bat" if IS_WIN else "pmd")
    out = {}
    for tokens in CPD_MIN_TOKENS:
        r = run([exe, "cpd", "--minimum-tokens", tokens, "--language", "typescript",
                 "--format", "csv", "-d", mirror])
        (RAW_DIR / f"cpd{tokens}-{tag}.csv").write_text(r.stdout, encoding="utf-8")
        # csv row: lines,tokens,occurrences,(line,file) * occurrences
        rows = [l.split(",") for l in r.stdout.splitlines()[1:] if l[:1].isdigit()]
        cross = sum(1 for x in rows if len({x[i] for i in range(4, len(x), 2)}) > 1)
        out.update({f"cpd{tokens}_clones": len(rows),
                    f"cpd{tokens}_cross_file": cross,
                    f"cpd{tokens}_dup_lines": sum(int(x[0]) * (int(x[2]) - 1) for x in rows)})
    return out


# --- jscpd ---------------------------------------------------------------------
def jscpd(root, tag, _):
    out = RAW_DIR / f"jscpd-{tag}"
    run([nbin("jscpd"), *src_dirs(root), "--silent", "--reporters", "json", "--output", out,
         "--min-tokens", "50", "--format", "typescript,tsx,javascript,jsx,python",
         "--ignore", ",".join(EXCLUDE_GLOB)], cwd=root)
    rep = out / "jscpd-report.json"
    if not rep.exists():
        return {"jscpd_clones": 0, "jscpd_dup_pct": 0.0}
    t = json.loads(rep.read_text(encoding="utf-8"))["statistics"]["total"]
    return {"jscpd_clones": t["clones"], "jscpd_dup_pct": round(t["percentage"], 2)}


# --- Arcan-style architectural smells -------------------------------------------
GRAPH_TARGETS = ["apps/api/src", "apps/web/src", "apps/shared"]

def _resolve_alias(spec, root):
    """'@/x' -> apps/web/src/x and the 'shared' workspace package, which
    dependency-cruiser can't resolve without installed node_modules."""
    if spec.startswith("@/"):
        base = root / "apps/web/src" / spec[2:]
    elif spec == "shared" or spec.startswith("shared/"):
        base = root / "apps/shared" / (spec[7:] or "index")
    else:
        return None
    for c in [base, *(base.with_name(base.name + e) for e in (".ts", ".tsx", ".js")),
              *(base / f"index{e}" for e in (".ts", ".tsx"))]:
        if c.is_file():
            return c.relative_to(root).as_posix()
    return None

def import_graph(root, tag):
    targets = [d for d in GRAPH_TARGETS if (root / d).exists()]
    r = run([nbin("depcruise"), *targets, "--no-config", "--output-type", "json",
             "--exclude", "node_modules"], cwd=root)
    (RAW_DIR / f"depcruise-{tag}.json").write_text(r.stdout, encoding="utf-8")
    G = nx.DiGraph()
    inside = lambda p: p and any(p.startswith(t) for t in targets)
    for m in json.loads(r.stdout)["modules"]:
        if not inside(m["source"]):
            continue
        G.add_node(m["source"])
        for d in m["dependencies"]:
            dst = _resolve_alias(d["module"], root) if d["couldNotResolve"] else d["resolved"]
            if inside(dst):
                G.add_edge(m["source"], dst)
    return G

def arcan_smells(root, tag, _):
    """Arcan's four classic smells, computed on the file-level import graph:
      cyclic dependency  - strongly connected components of size > 1
      hub-like           - fan-in and fan-out both above the median, and
                           |in - out| < (in + out) / 4  (Arcan's balance rule)
      unstable dependency- a file depending on files less stable than itself
                           (instability I = out / (in + out)), > 30% of its deps
      god component      - a file over 400 non-blank lines (Arcan uses a
                           benchmark-derived size threshold; 400 is a stand-in)
    """
    G = import_graph(root, tag)
    fi, fo = dict(G.in_degree()), dict(G.out_degree())
    inst = {v: fo[v] / (fi[v] + fo[v]) if fi[v] + fo[v] else 0 for v in G}
    cycles = [c for c in nx.strongly_connected_components(G) if len(c) > 1]

    nonzero_in  = sorted(v for v in fi.values() if v)
    nonzero_out = sorted(v for v in fo.values() if v)
    med = lambda xs: xs[len(xs) // 2] if xs else 0
    mi, mo = med(nonzero_in), med(nonzero_out)
    hubs = [v for v in G if fi[v] > mi and fo[v] > mo and abs(fi[v] - fo[v]) < (fi[v] + fo[v]) / 4]

    unstable = 0
    for v in G:
        deps = list(G.successors(v))
        if deps and sum(inst[d] > inst[v] for d in deps) / len(deps) > 0.3:
            unstable += 1

    god = sum(1 for p in G if (root / p).is_file() and
              sum(1 for l in (root / p).read_text(encoding="utf-8", errors="replace").splitlines()
                  if l.strip()) > 400)
    n = G.number_of_nodes()
    reach = sum(len(nx.descendants(G, v)) + 1 for v in G)
    return {"arc_modules": n, "arc_edges": G.number_of_edges(),
            "arc_cyclic_dependency": len(cycles),
            "arc_files_in_cycles": sum(len(c) for c in cycles),
            "arc_hub_like": len(hubs), "arc_unstable_dependency": unstable,
            "arc_god_component": god,
            "arc_propagation_cost_pct": round(100 * reach / n**2, 1) if n else 0.0}


# --- archlint ------------------------------------------------------------------
def archlint(root, tag, _):
    (root / ".archlint.yaml").write_text(
        "ignore:\n" + "".join(f"  - '{g}'\n" for g in EXCLUDE_GLOB) +
        "aliases:\n  '@/*': 'apps/web/src/*'\n", encoding="utf-8")
    out = RAW_DIR / f"archlint-{tag}.json"
    run([nbin("archlint"), "scan", ".", "--json", "-q", "--no-git", "--no-cache", "-r", out],
        cwd=root)
    if not out.exists():
        return {}
    s = json.loads(out.read_text(encoding="utf-8"))["summary"]
    keep = ["total_smells", "cyclic_dependencies", "god_modules", "hub_dependencies",
            "dead_symbols", "dead_code", "high_cognitive_complexity_functions",
            "high_cyclomatic_complexity_functions", "large_files", "shotgun_surgery",
            "feature_envy", "unstable_interfaces"]
    return {**{f"al_{k}": s.get(k) for k in keep},
            "al_grade": s.get("architecture_grade", {}).get("score")}


# --- Sonargraph ----------------------------------------------------------------
# Sonargraph-Build can only analyse an existing "system", and its TypeScript
# system takes a single tsconfig. The repos have one per app, so each snapshot
# gets a generated root tsconfig.json spanning api + web + shared (with the
# '@/' and 'shared' aliases), and a copy of the system made once in the GUI -
# its paths are relative, so the same system works in every snapshot.
SONARGRAPH_TSCONFIG = {
    "extends": "./tsconfig.base.json",
    "compilerOptions": {
        "module": "esnext", "moduleResolution": "bundler", "allowImportingTsExtensions": True,
        "jsx": "react-jsx", "noEmit": True, "baseUrl": ".",
        "paths": {"@/*": ["apps/web/src/*"], "shared": ["apps/shared/index.ts"]},
    },
    "include": ["apps/api/src", "apps/web/src", "apps/shared"],
    "exclude": ["**/node_modules", "**/dist", "apps/extension"],
}

def _sonargraph_metrics(report_xml):
    """System-level metric values from a Sonargraph XML report, keyed by metric
    name. Elements are matched by tag/attribute names so this survives minor
    schema differences between versions."""
    # layout: <systemMetricValues><levelValues levelRef=L><metric ref=M><int|float>v
    # with M -> <metricId id=M presentationName=...> and L -> <level id=L name=...>
    import xml.etree.ElementTree as ET
    root = ET.parse(report_xml).getroot()
    strip = lambda t: t.rsplit("}", 1)[-1]
    metric_names = {e.get("id"): e.get("presentationName") or e.get("name")
                    for e in root.iter() if strip(e.tag) == "metricId"}
    level_names = {e.get("id"): e.get("name") or e.get("presentationName")
                   for e in root.iter() if strip(e.tag) == "level"}
    out = {}
    for smv in (e for e in root.iter() if strip(e.tag) == "systemMetricValues"):
        for lv in (e for e in smv if strip(e.tag) == "levelValues"):
            if (level_names.get(lv.get("levelRef")) or "").lower() != "system":
                continue
            for m in (e for e in lv if strip(e.tag) == "metric"):
                val = next((v.text for v in m if strip(v.tag) in ("int", "float")), None)
                if val is not None:
                    out[metric_names.get(m.get("ref"), m.get("ref"))] = float(val)
    return out

def sonargraph(root, tag, _):
    if not SONARGRAPH_ACTIVATION.exists():
        raise RuntimeError(f"put your Sonargraph-Build activation code in {SONARGRAPH_ACTIVATION.name}")
    if not SONARGRAPH_TEMPLATE.exists():
        raise RuntimeError(f"create the TypeScript system once in the GUI and copy it to {SONARGRAPH_TEMPLATE}")

    (root / "tsconfig.json").write_text(json.dumps(SONARGRAPH_TSCONFIG, indent=2), encoding="utf-8")
    system_dir = root / SONARGRAPH_TEMPLATE.name
    if system_dir.exists():
        shutil.rmtree(system_dir)
    shutil.copytree(SONARGRAPH_TEMPLATE, system_dir)

    out_dir = RAW_DIR / f"sonargraph-{tag}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    config = out_dir / "startup.xml"
    config.write_text(
        f'<sonargraphBuild activationCode="{SONARGRAPH_ACTIVATION.read_text().strip()}" '
        f'waitForLicense="2" languages="Typescript" '
        f'installationDirectory="{SONARGRAPH_BUILD}" systemDirectory="{system_dir}" '
        f'reportDirectory="{out_dir}" reportFileName="report" reportFormat="xml" '
        f'reportType="full" logFile="{out_dir / "build.log"}" logLevel="info"/>\n',
        encoding="utf-8")

    plugins = SONARGRAPH_BUILD / "plugins"
    client = next(plugins.glob("com.hello2morrow.sonargraph.build.client_*.jar"))
    osgi = next(plugins.glob("org.eclipse.osgi_*.jar"))
    # Sonargraph's TypeScript parser is a Node script whose stdout the Java side
    # parses as a number. If Node colours that output (ANSI codes), Sonargraph
    # can't parse it and hangs instead of failing - so force colours off, and
    # cap the run so a hang can't stall everything.
    r = run(["java", "-cp", f"{client}{os.pathsep}{osgi}",
             "com.hello2morrow.sonargraph.build.client.SonargraphBuildRunner", config],
            cwd=out_dir, timeout=600, env=dict(os.environ, NO_COLOR="1", FORCE_COLOR="0"))
    (out_dir / "console.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
    if r.returncode not in (0, 1):                       # 1 = failset hit, report still written
        raise RuntimeError(f"Sonargraph-Build exit {r.returncode}; see {out_dir / 'console.txt'}")
    report = out_dir / "report.xml"
    if not report.exists():
        raise RuntimeError(f"no report written; see {out_dir / 'build.log'}")
    return {f"sg_{k}": v for k, v in _sonargraph_metrics(report).items()}


TOOLS_ALL = {"eslint": eslint, "semgrep": semgrep, "satd": satd, "cpd": pmd_cpd,
             "jscpd": jscpd, "arcan": arcan_smells, "archlint": archlint, "sonargraph": sonargraph}


# ===================================================================== main ==
def sonar_baseline(repo):
    """SonarQube numbers from the notebook's export, for side-by-side comparison."""
    p = RESULTS / repo / "branch_metrics.csv"
    if not p.exists():
        return {}
    df = pd.read_csv(p).set_index("branch")
    cols = ["code_smells", "bugs", "vulnerabilities", "duplicated_lines_density", "sqale_index"]
    return {b: {f"sonar_{c}": df.at[b, c] for c in cols if c in df} for b in df.index}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", default=",".join(REPOS))
    ap.add_argument("--tools", default=",".join(TOOLS_ALL))
    ap.add_argument("--refresh", action="store_true", help="re-export snapshots from git")
    args = ap.parse_args()
    tools = [t for t in args.tools.split(",") if t]

    semgrep_bin = setup()
    RAW_DIR.mkdir(exist_ok=True)

    for repo in args.repos.split(","):
        sonar = sonar_baseline(repo)
        rows = []
        for branch in REPOS[repo]:
            tag = f"{repo}--{branch.split('/')[-1]}"
            print(f"== {tag}", flush=True)
            root, skipped = snapshot(repo, branch, args.refresh)
            files = code_files(root)
            row = {"branch": branch,
                   "loc": sum(sum(1 for l in f.read_text(encoding="utf-8", errors="replace")
                                  .splitlines() if l.strip()) for f in files),
                   "committed_vendored_files": skipped.get("committed_vendored_files", 0),
                   **sonar.get(branch, {})}
            for name in tools:
                try:
                    row.update(TOOLS_ALL[name](root, tag, semgrep_bin))
                except Exception as e:                       # keep going if one tool breaks
                    print(f"   {name} failed: {e}")
            rows.append(row)

        df = pd.DataFrame(rows).set_index("branch")
        (RESULTS / repo).mkdir(parents=True, exist_ok=True)
        out = RESULTS / repo / "td_test.csv"
        if out.exists():                        # a --tools subset updates its columns, keeps the rest
            old = pd.read_csv(out, index_col="branch")
            df = old.drop(columns=[c for c in df.columns if c in old.columns]).join(df, how="outer")
            df = df.reindex([b for b in REPOS[repo] if b in df.index])
        df.to_csv(out)
        with pd.option_context("display.width", 250, "display.max_rows", 200):
            print(f"\n##### {repo}\n{df.T.to_string()}\n")


if __name__ == "__main__":
    main()
