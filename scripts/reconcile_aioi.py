"""Reconcile stray project duplicates in the DB + SharePoint into their CANONICAL project.

This is the SAFETY-NET the folder mess needs — NOT an always-on service. The real fix is the
source-side canonicalization in the engine (Engine._canonical_du_an), which now stops spelling/
apostrophe/prefix variants and MẪU-NHÀ-as-project names from ever creating a duplicate folder.
This script mops up whatever slipped through anyway (e.g. a genuine mishearing) and any legacy
mess made before the fix.

Run it occasionally, by hand. DRY-RUN by default — it only prints its plan. Pass --apply to
actually merge the DB rows and move/delete the SharePoint folders.

    python scripts/reconcile_aioi.py            # dry run (shows the plan, changes nothing)
    python scripts/reconcile_aioi.py --apply    # execute

It NEVER touches the default "General" folder or a registered CANONICAL project folder; a top
folder is "stray" only when its name canonicalizes to a DIFFERENT registered project.
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aioi.config import load_settings                       # noqa: E402
from aioi.engine import Engine                              # noqa: E402
from aioi.llm.mock import MockProvider                      # noqa: E402
from aioi.projects import SqliteProjectStore, normalize_project_name  # noqa: E402
from aioi.sharepoint import GraphAuth, SharePointMediaStore  # noqa: E402

KEEP_TOP = {"General"}  # SharePoint's default library folder — never reconcile it


def main(apply: bool) -> None:
    s = load_settings()
    store = SqliteProjectStore(s.db_path)
    # A MockProvider is fine — we only use the engine's pure name-canonicalization helpers here.
    eng = Engine(MockProvider(s), project_store=store)
    canonical_names = {p.name for p in store.all()}
    tag = "APPLY" if apply else "DRY-RUN"
    print(f"[{tag}] reconcile AOND — canonical projects: {sorted(canonical_names)}\n")

    # 1) DB: merge every project row whose name canonicalizes to a DIFFERENT registered project.
    for p in list(store.all()):
        canon = eng._canonical_du_an(p.name)
        if canon == p.name:
            continue
        print(f"DB  merge project {p.name!r}  ->  {canon!r}  "
              f"(docs={len(p.docs)}, units={len(p.units)}, models={[u.name for u in p.unit_types]})")
        if apply:
            target = store.get(canon)
            seen = {(d.get('kind'), d.get('title')) for d in target.docs}
            for d in p.docs:
                if (d.get('kind'), d.get('title')) not in seen:
                    target.docs.append(d)
            for ut in p.unit_types:
                target.upsert_unit_type(ut)
            for u in p.units:
                target.upsert_unit(u.ma_so_can, u.attrs, loai_hinh=u.loai_hinh)
            store.save(target)
            store._conn.execute("DELETE FROM projects WHERE key=?", (normalize_project_name(p.name),))
            store._conn.commit()

    # 2) SharePoint: move each stray top folder's files into the canonical tree, then delete it.
    if not s.sharepoint_configured:
        print("\n(SharePoint not configured — skipping folder reconcile.)")
        return
    sp = SharePointMediaStore(GraphAuth(s.azure_tenant_id, s.azure_client_id, s.azure_client_secret),
                              s.sharepoint_site_id)
    gd = sp._drive()

    def item(path: str):
        loc = f"{gd}/root:/{urllib.parse.quote(path)}" if path else f"{gd}/root"
        return sp._json("GET", loc, allow_404=True)

    def children(fid: str):
        return (sp._json("GET", f"{gd}/items/{fid}/children", allow_404=True) or {}).get("value", [])

    def files(fid: str):
        out = []
        for it in children(fid):
            out += files(it["id"]) if "folder" in it else [it]
        return out

    for top in children(item("")["id"]):
        name = top["name"]
        if "folder" not in top or name in KEEP_TOP:
            continue
        canon = eng._canonical_du_an(name)
        if canon == name:
            continue  # this IS a canonical project folder — leave it
        print(f"\nSP  stray folder {name!r}  ->  merge into {canon!r}")
        proj_chung = f"{canon}/_Chung"
        for f in files(top["id"]):
            fn = f["name"]
            # A model brochure ("... mau nha <Model> ...") belongs in <canon>/<Model>/_Chung.
            dest = proj_chung
            low = fn.lower()
            if "mau nha" in low or "mẫu nhà" in low:
                model = next((u.name for u in (store.get(canon).unit_types or [])
                              if normalize_project_name(u.name) in normalize_project_name(fn)), None)
                if model:
                    dest = f"{canon}/{model}/_Chung"
            print(f"     move  {fn}   ->  {dest}")
            if apply:
                dest_id = sp.ensure_folder(dest)
                existing = {c["name"] for c in children(dest_id)}
                nm = fn
                if nm in existing:
                    base, dot, ext = nm.rpartition(".")
                    nm = f"{base}_dup.{ext}" if dot else nm + "_dup"
                sp._json("PATCH", f"{gd}/items/{f['id']}",
                         {"parentReference": {"id": dest_id}, **({"name": nm} if nm != fn else {})})
        if apply:
            sp.delete(top["id"])
            print(f"     deleted stray folder {name!r}")

    print(f"\n[{tag}] done." + ("" if apply else "  (nothing changed — pass --apply to execute)"))


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
