"""Resident footprint of a running Engraphy stack, read from the kernel.

    python footprint.py --project fpdefault --label "main, default profile"
    python footprint.py --names engram-engram-1,engram-postgres-1 --label live

`docker stats` reports the cgroup's `memory.current` minus `inactive_file`,
which folds page cache into the number and hides how much of the total is
shared memory. Both distinctions matter here. Postgres keeps `shared_buffers`
in shmem, and page cache is reclaimable under pressure, so quoting either as
"resident" overstates what a laptop actually loses to the stack.

Columns:

    anon      private and unreclaimable. The number that decides whether a
              machine with 8GB can run this beside a browser.
    shmem     shared memory. `shared_buffers` lives here, charged once to the
              cgroup no matter how many Postgres backends map it.
    file      page cache. Reclaimable, reported for completeness, and NOT in
              the headline.
    current   the cgroup's whole charge, so the figure reconciles against
              `docker stats`.

Headline is anon + shmem: what the kernel cannot reclaim without swapping.

Cgroups are read from the HOST side through a throwaway privileged container
rather than with `docker exec` into the target, so measuring a running stack
never starts a process inside it. That matters when the target is somebody's
live server.
"""
import argparse
import json
import subprocess
import sys

MB = 1048576
READER_IMAGE = "alpine"


def run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        sys.exit(f"{' '.join(cmd[:3])}...: {out.stderr.strip()}")
    return out.stdout


def containers(project: str | None, names: list[str]) -> list[tuple[str, str]]:
    """[(container id, display name)] for a compose project or an explicit list."""
    if names:
        ids = [run(["docker", "inspect", n, "--format", "{{.Id}}"]).strip() for n in names]
        return list(zip(ids, names))
    ids = run(["docker", "ps", "-q", "--no-trunc", "--filter",
               f"label=com.docker.compose.project={project}"]).split()
    if not ids:
        sys.exit(f"no running containers in compose project {project!r}")
    out = []
    for cid in ids:
        out.append((cid, run(["docker", "inspect", cid, "--format",
                              "{{.Name}}"]).strip().lstrip("/")))
    return out


def read_cgroups(ids: list[str]) -> dict[str, dict[str, int]]:
    """One privileged reader container for the whole batch, so measuring N
    containers costs one container start rather than N."""
    script = "; ".join(
        f'echo "== {cid}"; cat /hostcg/docker/{cid}/memory.stat 2>/dev/null; '
        f'echo "current $(cat /hostcg/docker/{cid}/memory.current 2>/dev/null || echo 0)"'
        for cid in ids)
    raw = run(["docker", "run", "--rm", "--privileged",
               "-v", "/sys/fs/cgroup:/hostcg:ro", READER_IMAGE, "sh", "-c", script])
    stats: dict[str, dict[str, int]] = {}
    current = None
    for line in raw.splitlines():
        if line.startswith("== "):
            current = line[3:].strip()
            stats[current] = {}
        elif current and line:
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                stats[current][parts[0]] = int(parts[1])
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--names", default="", help="comma-separated container names")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    names = [n for n in args.names.split(",") if n]
    if not args.project and not names:
        sys.exit("pass --project or --names")

    found = containers(args.project, names)
    stats = read_cgroups([cid for cid, _ in found])

    rows, total_anon, total_shmem = [], 0, 0
    for cid, name in found:
        s = stats.get(cid, {})
        anon, shmem, file_ = s.get("anon", 0), s.get("shmem", 0), s.get("file", 0)
        rows.append({"container": name, "anon_mb": round(anon / MB, 1),
                     "shmem_mb": round(shmem / MB, 1), "file_mb": round(file_ / MB, 1),
                     "current_mb": round(s.get("current", 0) / MB, 1),
                     "resident_mb": round((anon + shmem) / MB, 1)})
        total_anon += anon
        total_shmem += shmem

    report = {"label": args.label or args.project or ",".join(names),
              "containers": rows,
              "total_resident_mb": round((total_anon + total_shmem) / MB, 1),
              "total_anon_mb": round(total_anon / MB, 1),
              "total_shmem_mb": round(total_shmem / MB, 1)}

    if args.json:
        print(json.dumps(report, indent=2))
        return
    print(f"=== {report['label']} ===")
    print(f"{'container':<26}{'anon':>9}{'shmem':>9}{'file':>9}{'current':>10}{'resident':>10}")
    for r in rows:
        print(f"{r['container']:<26}{r['anon_mb']:>9.1f}{r['shmem_mb']:>9.1f}"
              f"{r['file_mb']:>9.1f}{r['current_mb']:>10.1f}{r['resident_mb']:>10.1f}")
    print(f"{'TOTAL':<26}{report['total_anon_mb']:>9.1f}{report['total_shmem_mb']:>9.1f}"
          f"{'':>9}{'':>10}{report['total_resident_mb']:>10.1f}")


if __name__ == "__main__":
    main()
