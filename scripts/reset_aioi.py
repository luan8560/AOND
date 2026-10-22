"""Reset AOND to a clean slate — TEST / ADMIN ONLY.

Deletes ALL local DB data (projects + conversations + listings) and ALL AOND SharePoint folders
(every top-level folder except the library default "General"). This is the only place a full wipe
lives — a seller can never delete data from the chat.

Safe by default: it BACKS UP aioi.db first and runs as a DRY-RUN (prints what it would delete,
changes nothing). Pass --apply to actually delete.

    python scripts/reset_aioi.py                     # dry run — show what would be deleted
    python scripts/reset_aioi.py --apply             # wipe DB + SharePoint (keeps "General")
    python scripts/reset_aioi.py --apply --keep-sharepoint   # wipe the DB only, leave files

Quit any running `python cli.py` first — it holds aioi.db open and the delete will fail.
"""
from __future__ import annotations

import shutil
import sys
import time
import urllib.parse
from pathlib import Path

# Run from the repo without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aioi.config import load_settings                        # noqa: E402
from aioi.sharepoint import GraphAuth, SharePointMediaStore  # noqa: E402

KEEP_TOP = {"General"}  # SharePoint's default document-library folder — never delete it


def _wipe_db(db_path: str, apply: bool) -> None:
    db = Path(db_path)
    if not db.exists():
        print(f"DB   {db_path}: (không tồn tại — bỏ qua)")
        return
    backup = db.with_name(f"{db.name}.bak_{time.strftime('%Y%m%d_%H%M%S')}")
    print(f"DB   {db_path}  ->  DELETE   (backup: {backup.name})")
    if apply:
        shutil.copy2(db, backup)                 # always back up before deleting
        try:
            db.unlink()                          # a fresh empty DB is recreated on next run
        except PermissionError:
            print("   ⚠️ Không xóa được — aioi.db đang bị khóa. Thoát `python cli.py` rồi chạy lại.")
            return
        print("   đã xóa DB (schema rỗng sẽ tự tạo lại khi chạy app).")


def _wipe_sharepoint(apply: bool) -> None:
    s = load_settings()
    if not s.sharepoint_configured:
        print("SP   (SharePoint chưa cấu hình — bỏ qua)")
        return
    sp = SharePointMediaStore(GraphAuth(s.azure_tenant_id, s.azure_client_id, s.azure_client_secret),
                              s.sharepoint_site_id)
    gd = sp._drive()
    root = sp._json("GET", f"{gd}/root", allow_404=True)
    children = (sp._json("GET", f"{gd}/items/{root['id']}/children", allow_404=True) or {}).get("value", [])
    tops = [c for c in children if "folder" in c and c["name"] not in KEEP_TOP]
    if not tops:
        print("SP   (không có folder dự án nào để xóa — chỉ còn 'General')")
        return
    for c in tops:
        n = c.get("folder", {}).get("childCount", "?")
        print(f"SP   folder {c['name']!r}  ({n} mục)  ->  DELETE")
        if apply:
            sp.delete(c["id"])
            print(f"     đã xóa {c['name']!r}")


def main(apply: bool, keep_sharepoint: bool) -> None:
    tag = "APPLY" if apply else "DRY-RUN"
    print(f"[{tag}] reset AOND — xóa sạch DB" + ("" if keep_sharepoint else " + SharePoint") + "\n")
    _wipe_db(load_settings().db_path, apply)
    if not keep_sharepoint:
        _wipe_sharepoint(apply)
    print(f"\n[{tag}] xong." + ("" if apply else "  (chưa đổi gì — thêm --apply để thực thi)"))


if __name__ == "__main__":
    main(apply="--apply" in sys.argv, keep_sharepoint="--keep-sharepoint" in sys.argv)
