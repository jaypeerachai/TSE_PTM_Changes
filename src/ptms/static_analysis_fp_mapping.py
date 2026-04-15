from __future__ import annotations

"""
Run static analysis on reused files to filter false-positives and map them to PTMs.

This script reads collected reused files, finds PTM-related calls from the
signature list, resolves likely model names, and stores file-to-model mappings.
"""

import ast
import json
import numbers
import os
import re
import time
from bisect import bisect_right
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import requests

from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utilities.HFConfig import HuggingFaceConfig

try:
    from utilities.RawFileConfig import RawFileConfig
    _HAS_RAW_FILE_CONFIG = True
except Exception:
    _HAS_RAW_FILE_CONFIG = False

HUGGINGFACE_API_URL = "https://huggingface.co/api/models/"
HF_CACHE_FILENAME = "hf_canonical_cache.json"

# These small framework rules help bridge short local names to canonical HF ids.
FRAMEWORK_NAMESPACE_MAP = {
    "spacy": "spacy",
    "stanza": "stanfordnlp",
    "sentence_transformers": "sentence-transformers",
    "timm": "timm",
    "bertopic": "sentence-transformers",
}

_SPACY_LANG_RE = re.compile(r"^(?:[a-z]{2,3}(?:_[a-z]+)*?)_(?:core|ent)_[a-z]+_(?:sm|md|lg|trf)$")

DIRECT_SPACY_SIG_ID_RANGE: Tuple[int, int] = (30, 311)

# Quick denylist for obvious non-model argument values.
NON_RESULT_CACHE = {
    "", "unknown", "none", "kwargs", "tuple", "list", "dict", "set", "params", "config",
    "api_key", "streaming", "model", "temp", "float", "cast", "int", "str", "deployment",
    "settings", "metadata", "args", "auto", "text"
}

def _unwrap_quotes(s: str) -> str:
    """
    Remove one layer of wrapping quotes from a token.
    """
    if not isinstance(s, str):
        return s
    s = s.strip()
    if len(s) >= 2 and ((s[0] == s[-1]) and s[0] in ('"', "'")):
        return s[1:-1]
    return s


def _to_py_int(x: Any) -> Any:
    """
    Convert integral values to plain Python ints for DB writes.
    """
    try:
        if isinstance(x, numbers.Integral):
            return int(x)
    except Exception:
        pass
    return x


def _safe_norm(v: Optional[str]) -> Optional[str]:
    """
    Lowercase and trim a string if it exists.
    """
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v.lower() if v else None


class NamespaceResolver:
    """
    Resolve canonical Hugging Face namespaces with memory and disk caching.
    """

    __slots__ = ("hf_token", "canonical_cache", "non_canonical_cache", "session")

    def __init__(self, hf_token: str):
        self.hf_token = hf_token
        self.canonical_cache: Dict[str, str] = {}
        self.non_canonical_cache: set[str] = set(NON_RESULT_CACHE)
        self.session = requests.Session()
        self._load_disk_cache()

    def _cache_path(self) -> str:
        """
        Return the cache file path next to this script.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(here, HF_CACHE_FILENAME)

    def _load_disk_cache(self) -> None:
        """
        Load previously resolved names from disk if the cache exists.
        """
        try:
            with open(self._cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.canonical_cache.update({k: v for k, v in data.get("canonical", {}).items()})
            self.non_canonical_cache.update(set(data.get("non_canonical", [])))
        except Exception:
            pass

    def _save_disk_cache(self) -> None:
        """
        Save the current canonical and non-canonical caches to disk.
        """
        try:
            data = {
                "canonical": self.canonical_cache,
                "non_canonical": sorted(self.non_canonical_cache),
            }
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_namespace_if_canonical(self, name: str) -> Optional[str]:
        """
        Return the canonical namespace/name if HF confirms it exists.
        """
        name_norm = _safe_norm(name)
        if not name_norm:
            return None

        if name_norm in self.canonical_cache:
            return f"{self.canonical_cache[name_norm]}/{name_norm}"
        if name_norm in self.non_canonical_cache:
            return None

        url = HUGGINGFACE_API_URL + name_norm
        headers = {"Authorization": f"Bearer {self.hf_token}"}

        last_status = None
        for attempt in range(3):
            resp = self.session.get(url, headers=headers, timeout=15)
            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    author = data.get("author")
                    if isinstance(author, str) and author:
                        self.canonical_cache[name_norm] = author
                        self._save_disk_cache()
                        return f"{author}/{name_norm}"
                except Exception:
                    pass
                break
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.8 * (attempt + 1))
                continue
            break

        # Only keep a negative cache entry when HF clearly says 404.
        if last_status == 404:
            self.non_canonical_cache.add(name_norm)
            self._save_disk_cache()
        return None


class PTMStaticAnalyzer:
    """
    Parse one reused file and extract imports, calls, and simple variable values.
    """
    __slots__ = (
        "file_path",
        "tree",
        "import_map",
        "assign_index",
        "func_calls",
    )

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.tree: Optional[ast.AST] = None
        self.import_map: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self.assign_index: Dict[str, Dict[str, List[Any]]] = {}
        self.func_calls: Optional[List[Dict[str, Any]]] = None

    def _read_text_source(self) -> str:
        """
        Read Python source from a local file or URL.
        """
        try:
            parsed = urlparse(self.file_path)
            is_http = parsed.scheme in ("http", "https")
        except Exception:
            is_http = False

        if is_http:
            resp = requests.get(self.file_path, timeout=30)
            resp.raise_for_status()
            text = resp.text
        else:
            with open(self.file_path, "r", encoding="utf-8") as f:
                text = f.read()

        if text.startswith("\ufeff"):
            text = text.lstrip("\ufeff")
        return text

    def load_and_parse(self) -> None:
        """
        Load the source, parse the AST, and build helper indexes.
        """
        code_text = self._read_text_source()
        lines = code_text.splitlines(True)

        # Blank notebook-style magics so line numbers still stay aligned.
        for i, line in enumerate(lines):
            s = line.lstrip()
            if s.startswith(("!", "%", "?")):
                lines[i] = "\n"

        code = "".join(lines)
        self.tree = ast.parse(code)
        # We build imports and assignments in one pass so later matching stays cheap.
        self.import_map, self.assign_index = self._index_tree(self.tree)
        self.func_calls = self._get_func_calls_try_scalpel(self.tree)

    def analyze(self, import_signature: str, call_signature: str) -> List[Dict[str, Any]]:
        """
        Find calls that match one import signature and one call signature.
        """
        if self.tree is None:
            raise RuntimeError("Call load_and_parse() first.")

        func_calls = self.func_calls or []
        out: List[Dict[str, Any]] = []
        for call in func_calls:
            call_name: str = call["name"]
            base = call_name.partition(".")[0]
            module, origin = self.import_map.get(base, (None, None))

            # Match both direct imports and nested module paths.
            mod = module or ""
            root = mod.split(".", 1)[0]
            if (
                mod == import_signature
                or mod.startswith(f"{import_signature}.")
                or root == import_signature
            ) and (
                call_name.endswith(f".{call_signature}") or call_signature == origin
            ):

                call_lineno = call["lineno"]
                resolved_params: List[Dict[str, Any]] = []

                for p in call["params"]:
                    possibles = self._resolve_variable_at_line(p, call_lineno)
                    if possibles:
                        seen = set()
                        for val, ln in possibles:
                            key = (val, ln)
                            if key not in seen:
                                seen.add(key)
                                resolved_params.append({"value": str(val), "assign_lineno": ln})
                    else:
                        resolved_params.append({
                            "value": p if isinstance(p, str) else str(p),
                            "assign_lineno": call_lineno,
                        })               

                entry = dict(call)
                entry["import_origin"] = module
                entry["resolved_params"] = resolved_params
                out.append(entry)
        return out

    def _index_tree(self, tree: ast.AST) -> Tuple[
        Dict[str, Tuple[Optional[str], Optional[str]]],
        Dict[str, Dict[str, List[Any]]]
    ]:
        """
        Index imports and simple literal assignments for later lookup.
        """
        import_map: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        tmp_assign: Dict[str, Dict[str, List[Tuple[int, Any]]]] = defaultdict(lambda: {"uncond": [], "branch": []})

        class _Indexer(ast.NodeVisitor):
            __slots__ = ("if_depth",)
            def __init__(self):
                self.if_depth = 0

            def visit_If(self, node: ast.If):
                """
                Track whether assignments happen inside conditional blocks.
                """
                self.if_depth += 1
                self.generic_visit(node)
                self.if_depth -= 1

            def visit_ImportFrom(self, node: ast.ImportFrom):
                """
                Record names imported with `from x import y`.
                """
                module = node.module
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    origin_name = alias.name
                    import_map[local_name] = (module, origin_name)

            def visit_Import(self, node: ast.Import):
                """
                Record names imported with plain `import x`.
                """
                for alias in node.names:
                    full_module = alias.name
                    local_name = alias.asname or full_module.split(".", 1)[0]
                    import_map[local_name] = (full_module, None)

            def visit_Assign(self, node: ast.Assign):
                if len(node.targets) != 1:
                    return
                tgt = node.targets[0]

                # Keep simple object or class defaults like self.model_name = "...".
                # Keep simple class-field literals like self.model_name = "x".
                if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) and tgt.value.id in ("self", "cls"):
                    if isinstance(node.value, ast.Constant):
                        var = f"{tgt.value.id}.{tgt.attr}"
                        value = node.value.value
                        entry = (node.lineno, value)
                        if self.if_depth > 0:
                            tmp_assign[var]["branch"].append(entry)
                        else:
                            tmp_assign[var]["uncond"].append(entry)
                    return

                if not (isinstance(tgt, ast.Name) and isinstance(node.value, ast.Constant)):
                    return
                var = tgt.id
                value = node.value.value
                entry = (node.lineno, value)
                if self.if_depth > 0:
                    tmp_assign[var]["branch"].append(entry)
                else:
                    tmp_assign[var]["uncond"].append(entry)

            def visit_AnnAssign(self, node: ast.AnnAssign):
                # Handle: VAR: Type = "literal"
                if isinstance(node.target, ast.Name) and isinstance(node.value, ast.Constant):
                    var = node.target.id
                    value = node.value.value
                    entry = (node.lineno, value)
                    if self.if_depth > 0:
                        tmp_assign[var]["branch"].append(entry)
                    else:
                        tmp_assign[var]["uncond"].append(entry)
                    # Mirror class defaults under self.<name> for simple self.xxx lookups.
                    tmp_assign[f"self.{var}"]["uncond"].append(entry)
                self.generic_visit(node)

        _Indexer().visit(tree)

        assign_index: Dict[str, Dict[str, List[Any]]] = {}
        for var, parts in tmp_assign.items():
            un = sorted(parts["uncond"], key=lambda x: x[0])
            br = sorted(parts["branch"], key=lambda x: x[0])
            assign_index[var] = {
                "uncond_lines": [ln for ln, _ in un],
                "uncond_vals":  [v  for _, v in un],
                "branch_lines": [ln for ln, _ in br],
                "branch_vals":  [v  for _, v in br],
            }
        return import_map, assign_index

    def _resolve_variable_at_line(self, var_name: str, call_lineno: int) -> List[Tuple[Any, int]]:
        """
        Resolve the latest literal value for a variable near one call site.
        """
        if not isinstance(var_name, str):
            return []

        # Try the full dotted name first, then a plain trailing attribute name.
        d = self.assign_index.get(var_name)
        if not d and "." in var_name:
            d = self.assign_index.get(var_name.rsplit(".", 1)[-1])
        if not d:
            return []
        ul, uv, bl, bv = d["uncond_lines"], d["uncond_vals"], d["branch_lines"], d["branch_vals"]

        ui = bisect_right(ul, call_lineno - 1) - 1
        if ui >= 0:
            last_u_line = ul[ui]
            last_u_val = uv[ui]
            b_start = bisect_right(bl, last_u_line)
            b_end = bisect_right(bl, call_lineno - 1)
            if b_end > b_start:
                # If a branch changes the value before the call, keep all branch options.
                seen = set()
                out: List[Tuple[Any, int]] = []
                for i in range(b_start, b_end):
                    tup = (bv[i], bl[i])
                    if tup not in seen:
                        seen.add(tup)
                        out.append(tup)
                return out
            else:
                return [(last_u_val, last_u_line)]
        else:
            b_end = bisect_right(bl, call_lineno - 1)
            seen = set()
            out: List[Tuple[Any, int]] = []
            for i in range(b_end):
                tup = (bv[i], bl[i])
                if tup not in seen:
                    seen.add(tup)
                    out.append(tup)
            return out

    def _get_func_calls_try_scalpel(self, tree: ast.AST) -> List[Dict[str, Any]]:
        scalpel_calls: List[Dict[str, Any]] = []
        try:
            from scalpel.core.func_call_visitor import get_func_calls as _scalpel_get
            try:
                scalpel_calls = _scalpel_get(tree)
            except Exception:
                scalpel_calls = []
        except Exception:
            scalpel_calls = []

        # The local extractor helps keep chained and nested calls too.
        robust_calls = self._get_func_calls_robust(tree)

        seen = set()
        merged: List[Dict[str, Any]] = []
        for c in scalpel_calls + robust_calls:
            key = (
                c.get("name"),
                c.get("lineno"),
                c.get("col_offset"),
                tuple(c.get("params") or []),
            )
            if key not in seen:
                seen.add(key)
                merged.append(c)
        return merged

    def _get_func_calls_robust(self, tree: ast.AST) -> List[Dict[str, Any]]:
        """
        Walk the AST directly and collect call expressions in a simple format.
        """
        calls: List[Dict[str, Any]] = []

        def unparse_safe(n: ast.AST) -> str:
            try:
                return ast.unparse(n)  # py3.9+
            except Exception:
                return type(n).__name__

        def full_name(n: ast.AST) -> str:
            if isinstance(n, ast.Name):
                return n.id
            if isinstance(n, ast.Attribute):
                return f"{full_name(n.value)}.{n.attr}"
            return unparse_safe(n)

        class _V(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call):
                name = full_name(node.func)
                params: List[str] = []
                for a in node.args:
                    params.append(unparse_safe(a))
                for kw in node.keywords:
                    params.append(unparse_safe(kw.value))
                calls.append({
                    "name": name,
                    "lineno": getattr(node, "lineno", -1),
                    "end_lineno": getattr(node, "end_lineno", getattr(node, "lineno", -1)),
                    "col_offset": getattr(node, "col_offset", -1),
                    "end_col_offset": getattr(node, "end_col_offset", getattr(node, "col_offset", -1)),
                    "params": params,
                })
                self.generic_visit(node)

        _V().visit(tree)
        return calls

def apply_framework_rule(import_sig: str, param_value: str) -> str:
    """
    Apply small framework-specific name fixes before model lookup.
    """
    param = _unwrap_quotes((param_value or "").strip())
    if _safe_norm(import_sig) == "stanza":
        return f"stanza-{param}"
    return param


def fetch_signatures_map(db: DatabaseConfig, sig_ids: List[int]) -> Dict[int, Mapping[str, Any]]:
    """
    Load signature rows in one batch and index them by signature id.
    """
    if not sig_ids:
        return {}
    placeholders = ",".join(["%s"] * len(sig_ids))
    rows = db.select_from_db(
        DBT.SIGNATURES.value,
        columns="*",
        where=f"{DBF.Signatures.ID} IN ({placeholders})",
        params=tuple(sig_ids),
        fetch_one=False,
        order_by=None,
    )
    out: Dict[int, Mapping[str, Any]] = {}
    for r in rows:
        out[r[DBF.Signatures.ID]] = r
    return out


_PATH_EXT_RE = re.compile(r"\.(pt|bin|safetensors|ckpt|pth|onnx|zip|tar|gz|json|yaml|yml)$", re.I)

def _looks_like_local_path(s: str) -> bool:
    """
    Heuristically skip values that look like local file paths.
    """
    p = (s or "").strip()
    return (
        p.startswith(("./", "../", "/", "~")) or
        ("\\" in p) or
        (len(p) > 2 and p[1] == ":" and p[2] in ("\\", "/")) or
        (("/" in p or "\\" in p) and _PATH_EXT_RE.search(p) is not None)
    )


if __name__ == "__main__":
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # Get the reused-file root from config when possible.
    if _HAS_RAW_FILE_CONFIG:
        raw_file_config = RawFileConfig(db=db_config.db)
        REUSED_FILES_DIR = raw_file_config.get_reused_file_folder()
    else:
        current_dir = os.path.dirname(__file__)
        REUSED_FILES_DIR = os.path.abspath(os.path.join(current_dir, "<path_to_reused_files>"))

    # This is the external PTM index used for repository-PTM mapping.
    hf_config = HuggingFaceConfig()
    hf_config.init_huggingface_access()
    hf_token = hf_config._token
    resolver = NamespaceResolver(hf_token)

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"{DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1 AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} in (0, -1)",
        params=None,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC",
        fetch_one=False,
    )

    print(f"Found {len(repos)} repositories with PRELIMINARY_FILTER_STATUS = 1 AND STATIC_ANALYSIS_STATUS in (0, -1).")

    # Keep one in-memory lookup from canonical HF name to model id.
    os_models = db_config.select_from_db(
        DBT.MODELS.value,
        columns="id, full_name, name"
    )
    index_model_full_name: Dict[str, Any] = {}
    for m in os_models:
        fn = _safe_norm(m.get("full_name"))
        if fn:
            index_model_full_name[fn] = m["id"]

    for repo in repos:
        static_analysis_status = 1
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        repo_full_name = repo[DBF.DownstreamRepoInfo.FULL_NAME]
        repo_name = (repo_full_name or "").split("/")[-1]
        print(f"\n\nProcessing repository ID {repo_id}: {repo_full_name} ({repo_name})")

        reused_files = db_config.select_from_db(
            DBT.FILES.value,
            columns="*",
            where=f"{DBF.Files.REPO_ID} = %s AND {DBF.Files.CONTENT_COLLECTION_STATUS} = 1",
            params=(repo_id,),
            order_by=f"{DBF.Files.ID} ASC",
            fetch_one=False,
        )
        print(f"Processing {len(reused_files)} reused files for repo ID {repo_id}.")

        for file in reused_files:
            file_id = file[DBF.Files.ID]
            file_name = file[DBF.Files.NAME]
            file_path = file[DBF.Files.PATH] or ""

            # Filter common false positives like examples, demos, and third-party paths.
            fp_path = (
                "example", "examples", 
                "lib/site-packages", 
                "demo", "demos", 
                "tutorial", "tutorials",
                "sample", "samples", 
                ".venv", "environment", "environments", "env", "envs", 
            )
            file_path_l = file_path.lower()
            if any(fp in file_path_l for fp in fp_path):
                print(f"Skipping file {file_name} in repo {repo_name} due to path filter.")
                continue

            repo_dir = os.path.join(REUSED_FILES_DIR, f"{repo_id}_{repo_name}")
            abs_file_path = os.path.join(repo_dir, f"{file_id}_{file_name}")

            analyzer = PTMStaticAnalyzer(abs_file_path)
            try:
                analyzer.load_and_parse()
            except (IndentationError, TabError, SyntaxError) as e:
                print(f"Error parsing file {file_name}: {e}")
                continue
            except FileNotFoundError:
                print(f"File not found: {abs_file_path}")
                continue
            except Exception as e:
                print(f"Unexpected error reading/parsing {file_name}: {e}")
                continue

            # Validate the earlier string-match result against parsed code structure.
            sig_to_files = db_config.select_from_db(
                DBT.SIG_TO_FILE.value,
                columns="*",
                where=f"{DBF.SigToFile.FILE_ID} = %s",
                params=(file_id,),
                order_by=f"{DBF.SigToFile.ID} ASC",
                fetch_one=False,
            )
            sig_ids = [stf[DBF.SigToFile.SIGNATURE_ID] for stf in sig_to_files]
            sig_map = fetch_signatures_map(db_config, sig_ids)

            # Keep inserts stable when chained calls produce overlapping matches.
            seen_sig_rows = set()
            seen_map_rows = set()

            for stf in sig_to_files:
                signature_id = stf[DBF.SigToFile.SIGNATURE_ID]
                print(f"\nProcessing signature ID {signature_id} for file {file_name}.")

                signature = sig_map.get(signature_id)
                if not signature:
                    print(f"Signature ID {signature_id} not found; skipping.")
                    continue

                import_signature = signature[DBF.Signatures.IMPORT]
                call_signature = signature[DBF.Signatures.CALL]

                try:
                    matches = analyzer.analyze(import_signature=import_signature, call_signature=call_signature)
                except Exception as e:
                    print(f"Error analyzing file {file_name} with signature ID {signature_id}: {e}")
                    static_analysis_status = -1
                    continue

                for match in matches:
                    if match.get('import_origin') != import_signature:
                        continue

                    import_origin = match.get("import_origin")
                    calling_line_no = match['lineno']
                    caller_name = match['name']
                    print(f"import_origin: {import_origin}, caller_name: {caller_name} (line {calling_line_no})")

                    key_sig = (signature_id, calling_line_no, caller_name)
                    if key_sig not in seen_sig_rows:
                        seen_sig_rows.add(key_sig)

                        # Store one evidence row for each matched call site.
                        try:
                            db_config.insert_to_db(
                                DBT.FILE_SIGNATURE_MATCHES.value,
                                data_dict={
                                    DBF.FileSignatureMatches.FILE_ID:       _to_py_int(file_id),
                                    DBF.FileSignatureMatches.SIGNATURE_ID:  _to_py_int(signature_id),
                                    DBF.FileSignatureMatches.IMPORT_ORIGIN: import_origin,
                                    DBF.FileSignatureMatches.CALL_SIGNATURE: caller_name,
                                    DBF.FileSignatureMatches.CALL_LINE_NO:  _to_py_int(calling_line_no),
                                    DBF.FileSignatureMatches.MATCH_JSON:    json.dumps(match, ensure_ascii=False),
                                },
                            )
                        except Exception as e:
                            print(f"Match evidence store failed for file {file_name}, signature {signature_id}: {e}")

                    for rp in match.get('resolved_params', []):
                        raw_val = rp.get('value')
                        s_val = str(raw_val) if raw_val is not None else ""
                        s_val = _unwrap_quotes(s_val).strip()
                        # Drop a common local prefix before trying model resolution.
                        if 'models/' in s_val:
                            s_val = s_val.split('/', 1)[-1]
                        param_lineno = rp.get('assign_lineno', calling_line_no)

                        # These lightweight filters cut common false positives before HF lookup.
                        low = s_val.lower()
                        if (not low) or (low in NON_RESULT_CACHE):
                            print(f"Skipping non-result token: {s_val}")
                            continue
                        if _looks_like_local_path(s_val):
                            print(f"Skipping local path-like token: {s_val}")
                            continue

                        resolved_full_name: Optional[str] = None

                        # Try framework-specific rules first, then fall back to HF canonical lookup.
                        mod_lower = (import_signature or '').lower()
                        if _SPACY_LANG_RE.match(mod_lower):
                            resolved_full_name = f"spacy/{import_signature}".lower()
                            print(f"✅ spaCy language-package detected by module name: {resolved_full_name}")
                        elif DIRECT_SPACY_SIG_ID_RANGE[0] <= int(signature_id) <= DIRECT_SPACY_SIG_ID_RANGE[1]:
                            resolved_full_name = f"spacy/{import_signature}".lower()
                            print(f"✅ Direct-import spaCy resolved by signature_id: {resolved_full_name}")
                        else:
                            if '/' in s_val:
                                resolved_full_name = s_val.lower()
                            else:
                                namespace = FRAMEWORK_NAMESPACE_MAP.get(mod_lower)
                                if namespace:
                                    mapped = apply_framework_rule(import_signature, s_val)
                                    mapped = _unwrap_quotes(mapped)
                                    resolved_full_name = f"{namespace}/{mapped}".lower()
                                    print(f"✅ Framework mapped: {resolved_full_name}")
                                else:
                                    ns = resolver.get_namespace_if_canonical(s_val)
                                    if ns:
                                        resolved_full_name = ns.lower()
                                    else:
                                        continue

                        if resolved_full_name in index_model_full_name:
                            model_id = index_model_full_name[resolved_full_name]
                            print(f"Found model {resolved_full_name} with ID {model_id} in index.")

                            # De-duplicate file-to-model rows before inserting.
                            key_map = (file_id, model_id, signature_id, param_lineno, calling_line_no)
                            if key_map in seen_map_rows:
                                continue
                            seen_map_rows.add(key_map)

                            db_config.insert_to_db(
                                DBT.FILE_TO_MODEL.value,
                                data_dict={
                                    DBF.FileToModel.FILE_ID: _to_py_int(file_id),
                                    DBF.FileToModel.MODEL_ID: _to_py_int(model_id),
                                    DBF.FileToModel.SIGNATURE_ID: _to_py_int(signature_id),
                                    DBF.FileToModel.CALL_LINE_NO: _to_py_int(calling_line_no),
                                    DBF.FileToModel.PARAM_LINE_NO: _to_py_int(param_lineno),
                                    DBF.FileToModel.IMPORT_ORIGIN: import_origin,
                                    DBF.FileToModel.CALL_SIGNATURE: caller_name,
                                },
                            )
                        else:
                            print(f"No model found for {resolved_full_name}.")

            # Keep the repo status in sync after each file pass.
            db_config.update_db(
                DBT.DOWNSTREAM_REPO_INFO.value,
                data_dict={DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS: static_analysis_status},
                where=f"{DBF.DownstreamRepoInfo.ID} = %s",
                params=(repo_id,),
            )
            print(f"Updated static_analysis_status for repo ID {repo_id} to {static_analysis_status}.")
