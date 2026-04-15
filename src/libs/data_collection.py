"""
Collect library snapshots from release commits and store them in the database.
built on top of Islam et al. 2023
"""

import json
import os
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from utils.DBConfig import DatabaseConfig
from utils.RawFileConfig import RawFileConfig

try:
    import tomllib as toml_loader  # Python 3.11+
except ImportError:
    import tomli as toml_loader  # type: ignore

# from the original paper
DEP_FILES_ALLOWED = {
    "requirements.txt",
    "environment.yml",
    "Pipfile",
    "pyproject.toml",
}

# Keep consistent with PTM change-context filtering
FP_PATH = ("example", "examples", 
           "lib/site-packages", 
           "demo", "demos", 
           "tutorial", "tutorials", 
           "sample", "samples", 
           ".venv", "environment", "environments", "env", "envs", 
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run_git(repo_path: str, args: List[str]) -> str:
    """
    Run a git command inside one repo and return the text output.
    """
    out = subprocess.check_output(["git", "-C", repo_path, *args], stderr=subprocess.STDOUT)
    return out.decode("utf-8", errors="replace")


def git_list_files(repo_path: str, commit_sha: str) -> List[str]:
    """
    List tracked files for one commit snapshot.
    """
    txt = _run_git(repo_path, ["ls-tree", "-r", "--name-only", commit_sha])
    return [line.strip() for line in txt.splitlines() if line.strip()]


def git_show_file(repo_path: str, commit_sha: str, path: str) -> Optional[str]:
    """
    Read one file from a specific commit if it exists there.
    """
    try:
        return _run_git(repo_path, ["show", f"{commit_sha}:{path}"])
    except subprocess.CalledProcessError:
        return None


def tag_to_commit_sha(tag: str, repo_dir: str) -> str:
    """
    Resolve a release tag to the commit SHA behind it.
    """
    if not tag:
        raise ValueError("Empty tag")
    try:
        sha = _run_git(repo_dir, ["rev-parse", f"{tag}^{{commit}}"]).strip()
        if sha:
            return sha
    except subprocess.CalledProcessError:
        pass
    sha = _run_git(repo_dir, ["rev-list", "-n", "1", tag]).strip()
    if not sha:
        raise RuntimeError(f"Cannot resolve tag {tag}")
    return sha

def normalize_pkg_name(name: str) -> str:
    """
    Normalize package names to one simple format.
    """
    return name.strip().lower().replace("_", "-")


def parse_requirements_txt(text: str) -> Dict[str, str]:
    """
    Parse basic dependencies from a requirements-style file.
    """
    deps: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r", "--requirement", "-e", "--editable")):
            continue
        if "://" in line or line.startswith((".", "/")):
            continue

        m = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]+\])?\s*(.*)$", line)
        if not m:
            continue

        name = normalize_pkg_name(m.group(1))
        spec = (m.group(3) or "").strip()
        if _NAME_RE.match(name):
            deps[name] = spec or ""
    return deps


def parse_pipfile_toml(text: str) -> Dict[str, str]:
    """
    Parse dependency entries from a Pipfile.
    """
    data = toml_loader.loads(text)
    deps: Dict[str, str] = {}
    for section in ("packages", "dev-packages"):
        tbl = data.get(section, {}) or {}
        if not isinstance(tbl, dict):
            continue
        for k, v in tbl.items():
            name = normalize_pkg_name(str(k))
            if isinstance(v, str):
                deps[name] = v
            elif isinstance(v, dict):
                deps[name] = str(v.get("version", "") or "")
    return deps


def parse_pyproject_toml(text: str) -> Dict[str, str]:
    """
    Parse dependency sections from a pyproject.toml file.
    """
    data = toml_loader.loads(text)
    deps: Dict[str, str] = {}

    proj = data.get("project", {}) or {}
    if isinstance(proj, dict):
        dep_list = proj.get("dependencies") or []
        if isinstance(dep_list, list):
            for dep_line in dep_list:
                if not isinstance(dep_line, str):
                    continue
                s = dep_line.strip()
                m = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", s)
                if m:
                    deps[normalize_pkg_name(m.group(1))] = m.group(2).strip()

    tool = data.get("tool", {}) or {}
    if isinstance(tool, dict):
        poetry = tool.get("poetry", {}) or {}
        if isinstance(poetry, dict):
            for section in ("dependencies", "dev-dependencies"):
                sec = poetry.get(section, {}) or {}
                if not isinstance(sec, dict):
                    continue
                for k, v in sec.items():
                    if normalize_pkg_name(str(k)) == "python":
                        continue
                    name = normalize_pkg_name(str(k))
                    if isinstance(v, str):
                        deps[name] = v
                    elif isinstance(v, dict):
                        deps[name] = str(v.get("version", "") or "")

            group_tbl = poetry.get("group", {}) or {}
            if isinstance(group_tbl, dict):
                for _, grp in group_tbl.items():
                    if not isinstance(grp, dict):
                        continue
                    gdeps = grp.get("dependencies", {}) or {}
                    if not isinstance(gdeps, dict):
                        continue
                    for k, v in gdeps.items():
                        if normalize_pkg_name(str(k)) == "python":
                            continue
                        name = normalize_pkg_name(str(k))
                        if isinstance(v, str):
                            deps[name] = v
                        elif isinstance(v, dict):
                            deps[name] = str(v.get("version", "") or "")

    return deps


def parse_environment_yml(text: str) -> Dict[str, str]:
    """
    Parse conda and nested pip dependencies from environment.yml.
    """
    deps: Dict[str, str] = {}
    lines = text.splitlines()
    in_deps = False
    in_pip_block = False
    deps_indent = None

    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or line.strip().startswith("#"):
            continue

        if re.match(r"^\s*dependencies\s*:\s*$", line):
            in_deps = True
            in_pip_block = False
            deps_indent = None
            continue

        if not in_deps:
            continue

        m = re.match(r"^(\s*)-\s*(.+?)\s*$", line)
        if not m:
            continue

        indent = len(m.group(1))
        item = m.group(2).strip()

        if deps_indent is None:
            deps_indent = indent

        if item == "pip:":
            in_pip_block = True
            continue

        if in_pip_block and deps_indent is not None and indent <= deps_indent:
            in_pip_block = False

        if in_pip_block:
            deps.update(parse_requirements_txt(item))
        else:
            parts = item.split("=", 1)
            name = normalize_pkg_name(parts[0].strip())
            spec = ""
            if len(parts) == 2:
                spec = "=" + parts[1].strip()
            if _NAME_RE.match(name):
                deps[name] = spec

    return deps


def parse_dep_file(path: str, text: str) -> Dict[str, str]:
    """
    Send one dependency file to the matching parser.
    """
    base = os.path.basename(path)
    try:
        if base == "requirements.txt":
            return parse_requirements_txt(text)
        if base == "Pipfile":
            return parse_pipfile_toml(text)
        if base == "pyproject.toml":
            return parse_pyproject_toml(text)
        if base == "environment.yml":
            return parse_environment_yml(text)
    except Exception as e:
        print(f"[WARN] Failed to parse dependency file {path}: {type(e).__name__}: {e}")
        return {}
    return {}


def path_excluded(path: str) -> bool:
    """
    Skip files that look like examples, docs, env folders, or similar noise.
    """
    pl = path.lower()
    return any(tok in pl for tok in FP_PATH)


def collect_dependency_files(file_list: List[str]) -> List[str]:
    """
    Keep only supported dependency files after path filtering.
    """
    out: List[str] = []
    seen = set()
    for p in file_list:
        if path_excluded(p):
            continue
        if os.path.basename(p) not in DEP_FILES_ALLOWED:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def select_release_triples(cur, repo_id: Optional[int], limit: Optional[int]) -> List[dict]:
    """
    Load the release snapshots that should be processed.
    """
    where = ""
    if repo_id is not None:
        where = "WHERE repo_id = %s"

    sql = f"""
        SELECT repo_id, release_line_id, release_id
        FROM (
            SELECT repo_id, release_line_id, prev_release_id AS release_id
            FROM final_repo_release_pairs
            {where}
            UNION
            SELECT repo_id, release_line_id, curr_release_id AS release_id
            FROM final_repo_release_pairs
            {where}
        ) t
        ORDER BY repo_id, release_line_id, release_id
    """
    if limit is not None:
        sql += " LIMIT %s"

    # params duplicated for UNION branches if repo filter enabled
    if repo_id is not None:
        base = (int(repo_id), int(repo_id))
        exec_params = base + ((int(limit),) if limit is not None else ())
    else:
        exec_params = (int(limit),) if limit is not None else ()

    cur.execute(sql, exec_params)
    return cur.fetchall() or []


def run_dependency_snapshot_extraction(
    repo_id: Optional[int] = None,
    limit: Optional[int] = None,
    commit_every: int = 50,
):
    """
    Walk through release snapshots, parse dependency files, and save the results.
    """
    db = DatabaseConfig()
    conn, cur = db.create_db_connection()
    raw = RawFileConfig(db=db.db)
    repo_root = raw.get_repo_folder()

    # Keep small caches so repeated lookups stay cheap during one run.
    repo_name_cache: Dict[int, str] = {}
    release_tag_cache: Dict[Tuple[int, int], str] = {}
    release_sha_cache: Dict[Tuple[int, int], str] = {}
    files_cache: Dict[Tuple[str, str], List[str]] = {}
    content_cache: Dict[Tuple[str, str, str], Optional[str]] = {}

    def get_repo_path(_repo_id: int) -> str:
        """
        Map one repo id to its local clone path.
        """
        if _repo_id in repo_name_cache:
            full_name = repo_name_cache[_repo_id]
        else:
            cur.execute("SELECT full_name FROM downstream_repos WHERE id = %s", (_repo_id,))
            row = cur.fetchone()
            if not row or not row.get("full_name"):
                raise ValueError(f"full_name not found for repo_id={_repo_id}")
            full_name = row["full_name"]
            repo_name_cache[_repo_id] = full_name
        owner, repo = full_name.split("/", 1)
        return os.path.join(repo_root, owner, repo)

    def get_release_tag(_repo_id: int, _release_id: int) -> str:
        """
        Load and cache the tag name for one release.
        """
        key = (_repo_id, _release_id)
        if key in release_tag_cache:
            return release_tag_cache[key]
        cur.execute(
            "SELECT tag_name FROM releases WHERE id = %s AND repo_id = %s",
            (_release_id, _repo_id),
        )
        row = cur.fetchone()
        if not row or not row.get("tag_name"):
            raise ValueError(f"tag_name not found for repo_id={_repo_id}, release_id={_release_id}")
        tag = row["tag_name"]
        release_tag_cache[key] = tag
        return tag

    def get_release_sha(_repo_id: int, _release_id: int) -> str:
        """
        Resolve and cache the commit SHA for one release tag.
        """
        key = (_repo_id, _release_id)
        if key in release_sha_cache:
            return release_sha_cache[key]
        repo_path = get_repo_path(_repo_id)
        tag = get_release_tag(_repo_id, _release_id)
        sha = tag_to_commit_sha(tag, repo_path)
        release_sha_cache[key] = sha
        return sha

    triples = select_release_triples(cur, repo_id, limit)
    print(f"Total release snapshots to process: {len(triples)}")

    done = 0
    failed = 0
    for t in triples:
        _repo_id = int(t["repo_id"])
        release_line_id = int(t["release_line_id"])
        release_id = int(t["release_id"])
        try:
            # Load the commit snapshot and find dependency files inside it.
            repo_path = get_repo_path(_repo_id)
            sha = get_release_sha(_repo_id, release_id)

            key = (repo_path, sha)
            if key in files_cache:
                files = files_cache[key]
            else:
                files = git_list_files(repo_path, sha)
                files_cache[key] = files

            dep_files = collect_dependency_files(files)

            # Replace any older snapshot rows for the same release.
            cur.execute(
                """
                DELETE FROM library_snapshots
                WHERE repo_id = %s AND release_line_id = %s AND release_id = %s
                """,
                (_repo_id, release_line_id, release_id),
            )

            insert_values = []
            for fp in dep_files:
                ckey = (repo_path, sha, fp)
                if ckey in content_cache:
                    content = content_cache[ckey]
                else:
                    content = git_show_file(repo_path, sha, fp)
                    content_cache[ckey] = content
                if content is None:
                    continue

                # Store both the dependency names and the version specs we saw.
                deps = parse_dep_file(fp, content)
                dep_names = sorted(deps.keys())
                dep_versions = {k: deps[k] for k in dep_names}
                insert_values.append(
                    (
                        _repo_id,
                        release_line_id,
                        release_id,
                        fp,
                        len(dep_names),
                        json.dumps(dep_names),
                        json.dumps(dep_versions),
                    )
                )

            if not insert_values:
                # Keep one explicit snapshot row for releases with no supported dependency files.
                insert_values.append(
                    (
                        _repo_id,
                        release_line_id,
                        release_id,
                        None,
                        0,
                        json.dumps([]),
                        json.dumps({}),
                    )
                )

            cur.executemany(
                """
                INSERT INTO library_snapshots (
                    repo_id, release_line_id, release_id, file_path,
                    dep_count, dep_names, dep_versions
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                insert_values,
            )

            done += 1
            if done % max(commit_every, 1) == 0:
                conn.commit()
                print(f"[OK] processed {done}/{len(triples)}")
        except Exception as e:
            failed += 1
            conn.rollback()
            print(
                f"[FAIL] repo={_repo_id} line={release_line_id} release={release_id} "
                f"{type(e).__name__}: {e}"
            )

    conn.commit()
    print(f"Done. success={done}, failed={failed}")
    db.close_db_connection()


TARGET_REPO_ID = None
LIMIT_RELEASES = None
COMMIT_EVERY = 50

if __name__ == "__main__":
    # Run the extractor as a script with the default local settings.
    run_dependency_snapshot_extraction(
        repo_id=TARGET_REPO_ID,
        limit=LIMIT_RELEASES,
        commit_every=COMMIT_EVERY,
    )
