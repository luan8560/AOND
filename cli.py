"""Local REPL to chat with the assistant family. Run: python cli.py

Works offline (mock provider) with no API key; set ANTHROPIC_API_KEY in .env for real
Claude replies. Type /quit to exit, /state to dump captured data + scores.
"""
from __future__ import annotations

import os
import sys
import time

# Vietnamese output needs UTF-8; Windows consoles default to cp1252.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

from aioi.config import load_settings
from aioi.delivery import chunk_reply, typing_delay
from aioi.drive import make_folder_namer, make_resolver
from aioi.engine import Engine, SqliteConversationStore
from aioi.fetch import FetchError
from aioi.llm import build_provider
from aioi.projects import SqliteProjectStore
from aioi.sharepoint import GraphAuth, SharePointMediaStore

USER = "demo-seller"

# Set AIOI_TYPING=0 to disable the paced "typing" delivery (e.g. for fast demos).
TYPING_ENABLED = os.getenv("AIOI_TYPING", "1") != "0"


def _emit_reply(name: str, reply: str) -> None:
    """Release the reply as Zalo-style bubbles, paced to feel like real typing. Every bubble is
    LEFT-ALIGNED (no indent) so multi-bubble replies are easy to read; the name prefix labels the
    first bubble only."""
    bubbles = chunk_reply(reply) or [reply]
    for i, bubble in enumerate(bubbles):
        if TYPING_ENABLED and i > 0:
            time.sleep(typing_delay(bubble))
        print(f"{name}> {bubble}" if i == 0 else bubble)


def main() -> None:
    settings = load_settings()
    provider = build_provider(settings)
    media_store = None
    if settings.sharepoint_configured:
        auth = GraphAuth(settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret)
        media_store = SharePointMediaStore(auth, settings.sharepoint_site_id)
    engine = Engine(
        provider,
        resolver=make_resolver(settings.google_api_key),
        folder_namer=make_folder_namer(settings.google_api_key),
        project_store=SqliteProjectStore(settings.db_path),
        conversation_store=SqliteConversationStore(settings.db_path),
        media_store=media_store,
        max_photos_per_listing=settings.max_photos_per_listing,
    )

    print(f"AI Ơi Nhà Đất — chế độ: {settings.effective_provider}")
    print("Lệnh (chỉ để TEST): /quit · /state · /listings · /new · /switch <n> · "
          "/import <file.csv> · /project <mô tả> · /link <url> · /seller <id> · /whoami.")
    print("Trạng thái (bán/cọc/ngưng rao…) → nhắn tự nhiên, vd \"anh bán rồi\", \"cọc rồi em\".\n")

    # The ACTIVE seller identity. Each user_key is a separate seller (own basket, price_book,
    # history); the project registry is GLOBAL, so switching seller is how you test that shared
    # project knowledge is reused across sellers. `/seller <id>` switches; `/whoami` shows it.
    user = USER

    def _greet_if_new(u: str) -> None:
        # Greet a brand-new seller (no prior history) — introduce the assistant + seller-only
        # boundary. A returning seller (restored from DB) isn't re-greeted.
        if engine.is_new_conversation(u):
            _emit_reply(engine.conversation(u).assistant.name, engine.greeting(u))
            print()

    print(f"[seller hiện tại: {user}]")
    _greet_if_new(user)

    while True:
        try:
            text = input("Bạn> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/whoami":
            conv = engine.conversation(user)
            print(f"   [seller hiện tại: {user} | trợ lý: {conv.assistant.name} | "
                  f"rổ hàng: {len(conv.listings)} căn]\n")
            continue
        if text.startswith("/seller"):
            arg = text[len("/seller"):].strip()
            if not arg:
                print(f"   [seller hiện tại: {user} — dùng /seller <id> để đổi identity]\n")
                continue
            user = arg
            conv = engine.conversation(user)
            print(f"   [đã chuyển sang seller: {user} | rổ hàng: {len(conv.listings)} căn "
                  f"| kho dự án là CHUNG, dùng lại được]")
            _greet_if_new(user)
            continue
        if text == "/state":
            _dump_state(engine, user)
            continue
        if text == "/new":
            li = engine.start_new_listing(user)
            n = len(engine.conversation(user).listings)
            print(f"   [bắt đầu căn mới — rổ hàng hiện có {n} căn]\n")
            continue
        if text == "/listings":
            conv = engine.conversation(user)
            print(f"--- Rổ hàng ({len(conv.listings)} căn) ---")
            for i, li in enumerate(conv.listings, 1):
                active = " ← đang nhập" if li.listing_id == conv.active_listing_id else ""
                print(f"  {i}. {li.label()}{active}")
            print()
            continue
        if text.startswith("/switch "):
            li = engine.switch_listing(user, int(text.split()[1]))
            print(f"   [đang nói tới: {li.label()}]\n" if li else "   [số căn không hợp lệ]\n")
            continue
        # NOTE: no /sold or /status command — this is a CHAT product, so every listing-state
        # change (đã bán / đã cọc / không rao / thôi đăng ký...) comes from a NATURAL MESSAGE
        # ("anh bán rồi", "cọc rồi em", "tạm ngưng rao căn này"), handled by the engine's
        # set_status operation. Nothing about the DB is driven by a slash command.
        if text.startswith("/import "):
            arg = text[len("/import "):].strip().strip('"')
            try:
                if arg.startswith(("http://", "https://")):
                    print("   [đang tải bảng tính...]")
                    n = engine.import_from_url(user, arg)
                else:
                    with open(arg, encoding="utf-8-sig") as f:
                        n = engine.import_csv(user, f.read())
            except (OSError, FetchError) as e:
                print(f"   [không nhập được: {e}]\n")
                continue
            print(f"   [đã nhập {n} căn vào rổ hàng]\n")
            continue
        if text.startswith("/project "):
            proj = engine.register_project_from_text(text[len("/project "):].strip(), voter=user)
            if proj:
                print(f"   [đã đăng ký dự án: {proj.name} | tiện ích: "
                      f"{', '.join(proj.amenities) or '—'}]\n")
            else:
                print("   [không nhận diện được tên dự án]\n")
            continue

        # Pasted link(s) → AOND auto-detects (spreadsheet → import; photo/PDF → read).
        # Any words sent WITH the link (e.g. "tôi bán 2 căn Cosmo Gen 2 trong này") are kept
        # as a caption so the seller's intent isn't dropped (it scopes a price-list import).
        rest = text[6:].strip() if text.startswith("/link ") else text
        urls = [u for u in rest.split() if u.startswith(("http://", "https://"))]
        caption = " ".join(u for u in rest.split() if not u.startswith(("http://", "https://"))).strip()
        try:
            if len(urls) == 1:
                print("   [đang xử lý link...]")
                result = engine.handle_link(user, urls[0], caption=caption or None)
            elif urls:
                print(f"   [đang đọc {len(urls)} link...]")
                result = engine.handle_media_batch(user, urls, caption=caption or None)
            else:
                result = engine.handle(user, text)
        except Exception as e:  # one bad turn (API/network error) must NOT kill the session
            msg = str(e)
            if "credit balance is too low" in msg:
                print("   [⚠️ Hết credit Anthropic API. Nạp credit, hoặc chạy chế độ offline: "
                      "PowerShell → $env:AIOI_LLM_PROVIDER=\"mock\"; python cli.py]\n")
            else:
                print(f"   [⚠️ Lỗi khi xử lý tin nhắn — bỏ qua lượt này: {type(e).__name__}: {msg[:200]}]\n")
            continue
        _emit_reply(result.assistant_name, result.reply)
        if result.captured:
            print(f"   [đã trích xuất: {', '.join(result.captured)}]")
        print(
            f"   [điểm BĐS {result.property_score} | uy tín {result.user_score} "
            f"| hạng {result.tier} | hoàn chỉnh {result.completeness:.0%}]\n"
        )


def _dump_state(engine: Engine, user: str) -> None:
    """Everything AOND has BÓC TÁCH (extracted) for this seller: every listing in the basket
    (fields + status), the seller's own saved prices, and the SHARED project knowledge."""
    conv = engine.conversation(user)
    print("\n======== AOND ĐÃ BÓC TÁCH ĐƯỢC ========")
    print(f"Người rao: {user}  |  loại khách: {conv.user_type.value}")

    # 1) Rổ hàng — từng căn + trạng thái + mọi trường đã bắt
    print(f"\n── RỔ HÀNG ({len(conv.listings)} căn) ──")
    for i, li in enumerate(conv.listings, 1):
        active = " ← đang nhập" if li.listing_id == conv.active_listing_id else ""
        print(f"  [{i}] {li.label()}{active}")
        for k, v in li.prop.fields.items():
            print(f"        {k}: {v}")
        miss = li.prop.missing_fields()
        if miss:
            print(f"        (còn thiếu: {', '.join(miss)})")

    # 2) Giá RIÊNG người rao đã gửi (bảng giá) — không phải kiến thức chung
    if conv.price_book:
        print(f"\n── GIÁ ĐÃ LƯU (riêng người rao, {len(conv.price_book)} căn) ──")
        for row in conv.price_book.values():
            gia = row.get("gia_goi") or row.get("gia") or "?"
            print(f"  căn {row.get('ma_so_can','?')} ({row.get('unit_type','?')}): {gia}")

    # 3) Kiến thức CHUNG của dự án (dùng lại cho mọi seller) — lấy từ căn active, nếu rỗng thì
    #    từ bất kỳ căn nào trong rổ có dự án.
    project = engine.project_for(conv.prop)
    if project is None:
        for li in conv.listings:
            p = engine._projects.get(li.prop.fields.get("du_an"))
            if p is not None:
                project = p
                break
    if project is not None:
        print(f"\n── KIẾN THỨC DỰ ÁN: {project.name} (CHUNG) ──")
        if project.facts():
            for line in project.facts():
                print(f"  • {line}")
        if project.unit_types:
            print("  Mẫu nhà:")
            for ut in project.unit_types:
                specs = "; ".join(ut.facts()) if ut.specs else "(chưa có thông số)"
                print(f"    - {ut.name}: {specs}")
        print(f"  Danh mục căn: {len(project.units)} căn  |  Tài liệu: "
              f"{', '.join(d.get('title','?') for d in project.docs) or '(chưa có)'}")
    print("=======================================\n")


if __name__ == "__main__":
    sys.exit(main())
