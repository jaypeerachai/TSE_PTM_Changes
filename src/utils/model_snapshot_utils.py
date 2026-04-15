"""
Shared helpers for PTM model snapshot extraction.

These functions are reused by the release-level snapshot scripts so the
matching logic lives in one place.
"""

from typing import Dict, List, Optional, Set

from filter_fp_files_new import (
    DIRECT_SPACY_SIG_ID_RANGE,
    FRAMEWORK_NAMESPACE_MAP,
    NON_RESULT_CACHE,
    PTMStaticAnalyzer,
    NamespaceResolver,
    _SPACY_LANG_RE,
    _looks_like_local_path,
    _safe_norm,
    _unwrap_quotes,
    apply_framework_rule,
)
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF

_SIG_CACHE_BY_IMPORT: Dict[str, List[Dict]] = {}


def _is_numeric_like(s: str) -> bool:
    """
    Skip values that are really just numbers.
    """
    try:
        float(s)
        return True
    except Exception:
        return False


def build_model_name_to_id(db_config) -> Dict[str, int]:
    """
    Build a lookup from canonical model full name to model id.
    """
    print("\nBuilding model name to ID index...")
    rows = db_config.select_from_db(
        DBT.MODELS.value,
        columns="id, full_name, name",
        fetch_one=False,
    ) or []
    out: Dict[str, int] = {}
    for m in rows:
        fn = _safe_norm(m.get("full_name"))
        if fn:
            out[fn] = int(m["id"])
    print(f"(init) model index built: {len(out)} entries")
    return out


def _root_mod(mod: Optional[str]) -> Optional[str]:
    """
    Keep only the root module from an import path.
    """
    if not mod:
        return None
    return mod.split(".", 1)[0].lower()


def collect_frameworks_from_analyzer(analyzer: PTMStaticAnalyzer) -> Set[str]:
    """
    Collect imported framework roots from one parsed file.
    """
    mods: Set[str] = set()
    for _local, (module, _origin_name) in (analyzer.import_map or {}).items():
        root = _root_mod(module)
        if root:
            mods.add(root)

    # Keep spaCy when language-package modules are imported directly.
    for _local, (module, _) in (analyzer.import_map or {}).items():
        if module and _SPACY_LANG_RE.match(module.lower()):
            mods.add("spacy")

    return mods


def load_signatures_for_imports(db_config, imports: Set[str]) -> List[Dict]:
    """
    Load and cache signatures for a set of imported frameworks.
    """
    if not imports:
        return []

    need = [imp for imp in imports if imp not in _SIG_CACHE_BY_IMPORT]
    if need:
        placeholders = ",".join(["%s"] * len(need))
        print(f"(sig) loading signatures for imports: {need}")
        rows = db_config.select_from_db(
            DBT.SIGNATURES.value,
            columns=(
                f"`{DBF.Signatures.ID}` AS id, "
                f"`{DBF.Signatures.IMPORT}` AS import_origin, "
                f"`{DBF.Signatures.CALL}` AS raw_call"
            ),
            where=f"`{DBF.Signatures.IMPORT}` IN ({placeholders})",
            params=tuple(need),
            fetch_one=False,
        ) or []
        print(f"(sig) fetched {len(rows)} raw signature rows")

        grouped_unique: Dict[str, Dict[str, Dict]] = {}
        for r in rows:
            io = (r.get("import_origin") or "").strip()
            raw = r.get("raw_call")
            tail = str(raw).rsplit(".", 1)[-1] if raw is not None else ""
            if not tail:
                continue
            cur = grouped_unique.setdefault(io, {})
            if tail not in cur or int(r["id"]) < int(cur[tail]["id"]):
                cur[tail] = {
                    "id": int(r["id"]),
                    "import_origin": io,
                    "call_signature": tail,
                }

        for imp in need:
            _SIG_CACHE_BY_IMPORT[imp] = list(grouped_unique.get(imp, {}).values())

    out: List[Dict] = []
    for imp in imports:
        out.extend(_SIG_CACHE_BY_IMPORT.get(imp, []))
    return out


def resolve_param_to_full_name(
    import_sig: str,
    signature_id: int,
    raw_param: str,
    resolver: NamespaceResolver,
) -> Optional[str]:
    """
    Normalize one raw call parameter into a canonical owner/name string.
    """
    s_val = _unwrap_quotes(str(raw_param or "")).strip()
    if not s_val:
        return None
    low = s_val.lower()
    if low in NON_RESULT_CACHE or _is_numeric_like(s_val):
        return None
    if _looks_like_local_path(s_val):
        return None

    if "models/" in s_val:
        s_val = s_val.split("/", 1)[-1]
        low = s_val.lower()

    mod_lower = (import_sig or "").lower()

    if _SPACY_LANG_RE.match(mod_lower):
        return f"spacy/{import_sig}".lower()
    if DIRECT_SPACY_SIG_ID_RANGE[0] <= int(signature_id) <= DIRECT_SPACY_SIG_ID_RANGE[1]:
        return f"spacy/{import_sig}".lower()

    if "/" in low:
        return low

    namespace = FRAMEWORK_NAMESPACE_MAP.get(mod_lower)
    if namespace:
        mapped = apply_framework_rule(import_sig, s_val)
        mapped = _unwrap_quotes(mapped)
        return f"{namespace}/{mapped}".lower()

    ns = resolver.get_namespace_if_canonical(s_val)
    return ns.lower() if ns else None


def extract_snapshot_occurrences_with_analyzer(
    analyzer: PTMStaticAnalyzer,
    file_id: int,
    signature_rows: List[dict],
    model_name_to_id: Dict[str, int],
    resolver: NamespaceResolver,
) -> List[dict]:
    """
    Extract model-loading occurrences from one parsed file snapshot.
    """
    occs: List[dict] = []
    seen_sites: set[tuple] = set()

    for sig in signature_rows:
        io = sig["import_origin"]
        tail = sig["call_signature"]
        sig_id = sig["id"]

        for call in analyzer.analyze(io, tail):
            call_import = call.get("import_origin") or ""
            io_norm = (io or "").strip()
            same_root = call_import.split(".", 1)[0] == io_norm.split(".", 1)[0]
            if not (call_import == io_norm or call_import.startswith(io_norm + ".") or same_root):
                continue

            call_ln = int(call.get("lineno") or 0)
            end_ln = int(call.get("end_lineno") or call_ln)
            fullname = call.get("name") or tail

            for rp in (call.get("resolved_params") or []):
                raw_param = rp.get("value")
                param_ln = int(rp.get("assign_lineno") or call_ln)

                s_val = _unwrap_quotes(str(raw_param or "")).strip()
                if not s_val:
                    continue

                resolved_full_name = resolve_param_to_full_name(io, int(sig_id), s_val, resolver)
                if not resolved_full_name:
                    continue

                model_id = model_name_to_id.get(resolved_full_name)
                if model_id is None:
                    continue

                site_key = (io, tail, call_ln, fullname, resolved_full_name)
                if site_key in seen_sites:
                    continue
                seen_sites.add(site_key)

                occs.append(
                    {
                        "file_id": file_id,
                        "model_id": model_id,
                        "model_name": resolved_full_name,
                        "call_line_number": call_ln,
                        "end_call_line_number": end_ln,
                        "param_line_number": param_ln,
                        "end_param_line_number": param_ln,
                        "import_origin": io,
                        "call_signature": fullname,
                        "signature_id": sig_id,
                    }
                )
    return occs
