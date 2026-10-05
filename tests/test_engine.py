from aioi.config import Settings
from aioi.engine import Engine, SqliteConversationStore
from aioi.fetch import FetchedMedia, FetchError
from aioi.llm.mock import MockProvider
from aioi.projects import Project, UnitType
from aioi.sharepoint import MediaRef


def _engine(fetcher=None, resolver=None, provider=None, folder_namer=None, media_store=None,
            max_photos_per_listing=20) -> Engine:
    settings = Settings(provider="mock", anthropic_api_key=None,
                        chat_model="x", vision_model="y")
    return Engine(provider or MockProvider(settings), fetcher=fetcher or _fetch_default,
                  resolver=resolver, folder_namer=folder_namer, media_store=media_store,
                  max_photos_per_listing=max_photos_per_listing)


def _fetch_default(url):
    return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url)


def test_handle_extracts_and_replies():
    eng = _engine()
    r = eng.handle("u1", "Anh muốn gửi bán căn nhà phố ở Quận 5, hẻm 123 đường Trần Bình Trọng, 50m2, giá 10 tỷ.")
    assert r.reply  # got a reply
    assert "loai_hinh" in r.captured
    assert r.assistant_name  # a persona was assigned
    assert 0.0 <= r.completeness <= 1.0
    assert 0.0 <= r.property_score <= 100.0


def test_greeting_introduces_and_redirects_buyers():
    eng = _engine()
    assert eng.is_new_conversation("g1")  # nobody has spoken yet
    g = eng.greeting("g1")
    assert eng.conversation("g1").assistant.name in g   # names itself
    assert "AI Ơi Nhà Đất - Buyers" in g                # redirects buyers to sibling channel
    lg = g.lower()
    assert "bán" in lg and "thuê" in lg                 # states the seller-only scope
    eng.handle("g1", "chào em")
    assert not eng.is_new_conversation("g1")            # greeting is a first-contact thing only


def test_project_name_concern_asks_instead_of_guessing():
    # "Nghi ngờ thì hỏi": a model name or a near-miss to an existing project must be flagged so
    # AOND asks the seller, instead of silently spawning a duplicate/mis-modelled project.
    eng = _engine()
    p = Project("Ny'ah Phú Định")
    p.upsert_unit_type(UnitType("Cosmo"))
    eng._projects.save(p)

    assert eng.project_name_concern("Ny'ah Phú Định") is None   # confident match — not ambiguous
    assert eng.project_name_concern("Nyah Phú Định") is None    # matcher already collapses variant
    assert eng.project_name_concern("Valéo Đầm Sen") is None    # genuinely different — no false ask

    model = eng.project_name_concern("Cosmo")                   # a MẪU NHÀ, not a project
    assert model is not None and model[0] == "model" and model[1] == "Ny'ah Phú Định"

    near = eng.project_name_concern("Nhã Đạt Phú Định")         # shares the distinctive tail
    assert near is not None and near[0] == "near" and near[1] == "Ny'ah Phú Định"


def test_media_read_answers_the_pending_seller_question():
    # Regression: seller asked "đường nội bộ rộng bao nhiêu?" then re-sent the phân-lô image so
    # AOND could read the answer — but the read-out ignored context and asked "đang rao lô nào".
    # The seller's pending question must be handed to the vision read so it answers THAT.
    seen = {}

    class Spy(MockProvider):
        def describe_media(self, items, instruction, max_tokens=1024):
            seen["instr"] = instruction
            return "Đây là sơ đồ phân lô tổng thể."

    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = Engine(Spy(settings), fetcher=_fetch_default)
    eng.handle("s", "đường nội bộ trước căn 50 rộng bao nhiêu?")
    eng.handle_link("s", "https://x/phan-lo.jpg")
    assert "đường nội bộ trước căn 50 rộng bao nhiêu" in seen.get("instr", "")


def test_model_specs_are_surfaced_to_every_reply_path():
    # Regression (seller Sơn): AOND had the Cosmo Gen 2 model specs on the UnitType but neither the
    # reply memory nor the grounded-Q&A path rendered them, so it wrongly said "em không tra cứu
    # được". The structured shared block (facts + mẫu nhà specs) must appear in BOTH paths.
    from aioi.engine import Conversation
    from aioi.assistants import assign_assistant
    eng = _engine()
    p = Project("Ny'ah Phú Định")
    p.upsert_unit_type(UnitType("Cosmo (Gen 2)", {"số tầng": "1 trệt + 3 lầu", "số phòng ngủ": "4"}))
    p.docs.append({"title": "danh mục", "markdown": "mã lô ↔ mẫu"})
    eng._projects.save(p)
    conv = Conversation(user_key="k", assistant=assign_assistant("k"))

    mem = eng._project_memory_block(p)
    assert "MẪU NHÀ" in mem and "số tầng" in mem and "4" in mem   # reply-memory path (agent loop)
    shared = eng._structured_shared_block(p)                       # shared helper (grounded Q&A path)
    assert "Cosmo (Gen 2)" in shared and "số phòng ngủ: 4" in shared


def test_map_request_consults_stored_phan_lo_not_denies_it():
    # Regression: seller asked "xem trong bản đồ phân lô" and AOND kept insisting "em CHƯA có
    # bản vẽ" although the project's phân-lô doc was stored. A project-map request must READ the
    # stored doc, never fall through to the per-căn "chưa có bản vẽ" denial.
    from aioi.engine import Conversation
    from aioi.assistants import assign_assistant
    eng = _engine()
    p = Project("Ny'ah Phú Định")
    p.docs.append({"kind": "phân lô", "title": "NPD so do phan lo-02",
                   "markdown": "# Phân lô\n- Lô #50 nằm cụm giữa.", "web_url": "https://x/phanlo"})
    eng._projects.save(p)
    conv = Conversation(user_key="m", assistant=assign_assistant("m"))
    assert eng._project_map_doc(p) is not None
    reply = eng._media_request_reply(conv, p, "em xem lại trong bản đồ phân lô xem")
    assert "CHƯA có bản vẽ" not in reply           # must not deny the map it holds
    assert "https://x/phanlo" in reply             # offers the stored file


def test_listing_label_shows_rental_status_not_dang_ban():
    # A rental's default lifecycle status ("đang bán") must DISPLAY as "đang cho thuê" — /listings
    # showing "[đang bán]" for a for-rent căn is misleading.
    from aioi.engine import Listing
    from aioi.schema import Property
    assert "[đang bán]" in Listing(prop=Property(fields={"ma_so_can": "50"})).label()
    rent = Listing(prop=Property(is_rental=True, fields={"ma_so_can": "50"}))
    assert "[đang cho thuê]" in rent.label() and "đang bán" not in rent.label()
    # An explicit terminal status is shown verbatim.
    assert "[đã cho thuê]" in Listing(prop=Property(is_rental=True), status="đã cho thuê").label()


def test_price_book_persists_and_basket_prices_surface(tmp_path):
    # Regression: after a restart the seller's uploaded prices vanished (price_book wasn't
    # persisted), so AOND lied "em không đọc được file bảng giá" for a căn it had priced. Now
    # price_book persists, AND a căn already in the basket surfaces its own gia_air/gia_max.
    from aioi.engine import Conversation, Listing
    from aioi.assistants import assign_assistant
    from aioi.schema import Property
    db = str(tmp_path / "t.db")
    store = SqliteConversationStore(db)
    conv = Conversation(user_key="s", assistant=assign_assistant("s"))
    conv.price_book["7"] = {"ma_so_can": "7", "gia_air": "5 tỷ", "gia_goi": "Air: 5 tỷ"}
    conv.listings = [Listing(prop=Property(fields={
        "ma_so_can": "3", "unit_type": "Cosmo", "gia_air": "9,710,000,000",
        "gia_max": "9,957,800,000", "gia_goi": "Air: 9,710,000,000 · Max: 9,957,800,000"}))]
    conv.active_listing_id = conv.listings[0].listing_id
    store.save(conv)

    reloaded = SqliteConversationStore(db).load("s")            # simulate a restart
    assert reloaded.price_book.get("7", {}).get("gia_air") == "5 tỷ"   # price_book survived
    block = _engine().__class__._seller_price_block(reloaded)
    assert "căn 3" in block and "9,710,000,000" in block and "9,957,800,000" in block  # from basket
    assert "căn 7" in block                                            # from price_book


def test_canonical_du_an_collapses_variants_and_model_names():
    # SOURCE fix for the recurring SharePoint folder mess: every project-name variant, and a MẪU
    # NHÀ name used as a project, must resolve to the ONE canonical project name BEFORE any folder
    # path is built — so they don't spawn duplicate folders.
    eng = _engine()
    p = Project("Ny'ah Phú Định")
    p.upsert_unit_type(UnitType("Cosmo"))
    eng._projects.save(p)
    for variant in ["Ny'ah Phú Định", "Nyah Phú Định", "Khu Nhà Biệt Lập Ny'ah Phú Định", "Cosmo"]:
        assert eng._canonical_du_an(variant) == "Ny'ah Phú Định", variant
    # A genuinely different / new project keeps its own name (no false merge).
    assert eng._canonical_du_an("Vinhomes Grand Park") == "Vinhomes Grand Park"
    # Registering under a variant enriches the ONE project, not a duplicate.
    eng.register_project("Nyah Phú Định", amenities=["hồ bơi"])
    names = [pr.name for pr in eng._projects.all()]
    assert names == ["Ny'ah Phú Định"]


def test_near_miss_project_name_triggers_ask_on_ingest():
    # Extend "nghi ngờ thì hỏi" to the media/doc ingest path: a mishearing that differs only in the
    # tail ("Nyah Phú BÌNH" vs canonical "…ĐỊNH") must be flagged so AOND asks — not silently forked.
    eng = _engine()
    eng._projects.save(Project("Ny'ah Phú Định"))
    assert eng.project_name_concern("Nyah Phú Bình")[0] == "near"      # tail differs, long shared prefix
    assert eng.project_name_concern("Nhã Đạt Phú Định")[0] == "near"   # front differs, shared tail
    assert eng.project_name_concern("Vinhomes Grand Park") is None     # genuinely different — no ask
    # Registering the mishearing stashes a clarifying question that the ingest reply drains once.
    eng.register_project("Nyah Phú Bình", voter="u")
    q = eng._drain_name_concern()
    assert "Ny'ah Phú Định" in q and eng._drain_name_concern() == ""   # asked, then cleared


def test_so_do_personal_data_redacted_but_land_facts_kept():
    from aioi.engine import _redact_personal_data, _redact_doc_title, _looks_personal_doc
    so_do = (
        "### I. Người sử dụng đất\n"
        "**Ông: LÝ QUỐC QUANG**\n"
        "- Năm sinh: 1964, CCCD số: 079064022135\n"
        "- Địa chỉ thường trú: 215D Phạm Hữu Chí, Q5\n"
        "### II. Thửa đất\n"
        "- b) Địa chỉ: Phường 16, Quận 8\n"
        "- c) Diện tích: 57,0m²\n"
    )
    out = _redact_personal_data(so_do)
    assert "LÝ QUỐC QUANG" not in out and "079064022135" not in out and "215D" not in out  # PII gone
    assert "Phường 16, Quận 8" in out and "57,0m²" in out and "Thửa đất" in out              # land facts kept
    assert _looks_personal_doc(so_do) is True
    assert _redact_doc_title("NPD#50, GCNQSDĐ 933551, Vũ Ngọc Nga") == "Sổ đỏ / GCN QSDĐ căn 50"

    # A phân-lô / sơ-đồ map must NOT be mistaken for a sổ đỏ ("sơ đồ" vs "sổ đỏ"): not flagged,
    # not retitled, land content untouched.
    phan_lo = "# Sơ đồ phân lô\n- Lô #50 giáp đường nội bộ 7m, diện tích 57.21m²\n- quy hoạch: đất ở"
    assert _looks_personal_doc(phan_lo) is False
    assert _redact_doc_title("NPD so do phan lo-02") == "NPD so do phan lo-02"
    assert _redact_personal_data(phan_lo) == phan_lo


def test_reply_is_complete_and_not_truncated():
    # We no longer hard-cap; the reply must be a complete sentence, never ending in "…".
    eng = _engine()
    r = eng.handle("u2", "nhà phố quận 5")
    assert r.reply
    assert not r.reply.rstrip().endswith(("…", "..."))


def test_next_field_is_single_and_missing():
    eng = _engine()
    r = eng.handle("u3", "nhà phố")
    assert r.next_field is not None
    conv = eng.conversation("u3")
    assert r.next_field in conv.prop.missing_fields()


def test_data_accumulates_across_turns():
    eng = _engine()
    eng.handle("u4", "Anh muốn gửi bán nhà phố")
    eng.handle("u4", "Hẻm 123 đường Trần Bình Trọng, đất 60m2, nhà 50m2")
    conv = eng.conversation("u4")
    assert conv.prop.loai_hinh == "nha_pho"
    assert conv.prop.fields.get("dia_chi")
    assert conv.prop.fields.get("dien_tich_dat") == "60m2"
    assert conv.prop.fields.get("dien_tich_nha") == "50m2"


def test_ambiguous_house_area_is_parked_not_dropped():
    # A house type with only ONE m2 figure, no đất/nhà qualifier: captured into the
    # generic dien_tich (not silently dropped) — the bot asks to resolve it later.
    eng = _engine()
    eng.handle("u4b", "Anh bán nhà phố Quận 5, hẻm 123, 80m2, giá 10 tỷ.")
    conv = eng.conversation("u4b")
    assert conv.prop.loai_hinh == "nha_pho"
    assert conv.prop.fields.get("dien_tich") == "80m2"
    assert conv.prop.fields.get("dien_tich_dat") is None
    assert conv.prop.fields.get("dien_tich_nha") is None
    # missing_fields() still asks for the split areas — the ambiguous value just sits
    # ready for the prompt layer (build_chat_system) to reference when it asks.
    assert "dien_tich_dat" in conv.prop.missing_fields()


def test_chat_turn_is_not_a_listing_post_while_data_missing():
    eng = _engine()
    r = eng.handle("u5", "nhà phố")
    assert r.next_field is not None
    assert r.is_listing_post is False


def test_complete_listing_drafts_ad_completely():
    eng = _engine()
    conv = eng.conversation("u6")
    # Fill every expected field for a residential listing so nothing is missing.
    conv.prop.loai_hinh = "nha_pho"
    conv.prop.fields.update({
        "dia_chi": "146 Trần Bình Trọng, Q5",
        "dien_tich_dat": "60m2",
        "dien_tich_nha": "50m2",
        "gia": "10 tỷ",
        "ket_cau": "4 tầng, 4PN",
        "lo_gioi_hem": "HXH 5m",
        "tinh_trang_ban_giao": "đang trống",
        "phap_ly_hoan_cong": "sổ hồng, hoàn công đủ",
    })
    assert conv.prop.missing_fields() == []

    r = eng.handle("u6", "vậy là đủ rồi em")
    assert r.next_field is None
    assert r.is_listing_post is True
    assert r.reply  # a full draft, complete and not truncated
    assert not r.reply.rstrip().endswith(("…", "..."))


def test_identity_only_is_not_yet_summary_ready():
    # Identity fields alone are below the completeness threshold -> still collecting.
    eng = _engine()
    conv = eng.conversation("u9")
    conv.prop.loai_hinh = "nha_pho"
    conv.prop.fields.update({
        "dia_chi": "146 Trần Bình Trọng, Q5",
        "dien_tich": "50m2",
        "gia": "10 tỷ",
    })
    assert not conv.prop.summary_ready()  # 4/8 fields < 75%
    r = eng.handle("u9", "giá 10 tỷ nha em")
    assert r.is_listing_post is False


def test_draft_then_fee_only_after_approval():
    eng = _engine()
    conv = eng.conversation("u10")
    # Identity + enough core fields to cross the "khá đầy đủ" threshold (6/8 = 75%).
    conv.prop.loai_hinh = "nha_pho"
    conv.prop.fields.update({
        "dia_chi": "146 Trần Bình Trọng, Q5",
        "dien_tich_dat": "60m2",
        "dien_tich_nha": "50m2",
        "gia": "10 tỷ",
        "ket_cau": "1 trệt 2 lầu, 4PN",
        "lo_gioi_hem": "hẻm xe hơi 4m",
    })
    assert conv.prop.summary_ready()

    # Step 1: draft the ad and ask if it's appealing — NO fee yet.
    r1 = eng.handle("u10", "vậy đó em")
    assert r1.is_listing_post is True
    assert "hấp dẫn" in r1.reply
    assert "phí môi giới" not in r1.reply
    assert conv.listing_proposed is True

    # Step 2: customer approves -> only now does the fee come up.
    r2 = eng.handle("u10", "ok nhìn hấp dẫn đó em")
    assert "phí môi giới" in r2.reply


def test_seller_role_detected_and_sticky():
    eng = _engine()
    # First turn: a broker self-identifies via "rổ hàng".
    r = eng.handle("u7", "Em là môi giới, có nguyên rổ hàng nhà phố Quận 5")
    assert r.user_type == "moi_gioi"
    # A later, neutral turn must not downgrade the classification back to unknown.
    r2 = eng.handle("u7", "căn này 50m2")
    assert r2.user_type == "moi_gioi"


def test_listing_inherits_registered_project():
    eng = _engine()
    eng.register_project("Botanic Tower", amenities=["hồ bơi", "gym"],
                         attributes={"cổng": "Nguyễn Thượng Hiền"})
    conv = eng.conversation("u16")
    conv.prop.fields["du_an"] = "botanic tower"  # case-insensitive match
    p = eng.project_for(conv.prop)
    assert p is not None
    assert "hồ bơi" in p.amenities and "gym" in p.amenities


def test_unit_type_registered_and_inherited():
    eng = _engine()
    eng.register_unit_type("Botanic Towers", "Type B", {"diện tích": "113m2", "số phòng ngủ": "3"})
    conv = eng.conversation("uT")
    conv.prop.fields["du_an"] = "Botanic Tower"   # lenient project match
    conv.prop.fields["unit_type"] = "Type B"
    ut = eng.unit_type_for(conv.prop)
    assert ut is not None and ut.specs["diện tích"] == "113m2"


def test_register_unit_type_from_text():
    eng = _engine()
    ut = eng.register_unit_type_from_text(
        "Mẫu nhà Fusion Gen 5, dự án Nyah Phú Định, 250m2, 4 phòng ngủ."
    )
    assert ut is not None and ut.name.startswith("Fusion Gen 5")
    p = eng._projects.get("Nyah Phú Định")
    assert p is not None and p.find_unit_type("Fusion Gen 5") is not None


def test_model_learned_from_chat_answer_is_shared_across_sellers():
    eng = _engine()
    eng.register_project("Nyah Phú Định", amenities=["hồ bơi"])
    conv = eng.conversation("A")
    conv.prop.fields["du_an"] = "Nyah Phú Định"      # (mock doesn't extract du_an; set directly)
    conv.prop.fields["unit_type"] = "Cosmo Gen 2"
    assert eng._projects.get("Nyah Phú Định").find_unit_type("Cosmo Gen 2") is None
    # Seller A ANSWERS the acquisition question in chat (a file was never sent).
    eng.handle("A", "Mẫu nhà Cosmo Gen 2. Có 3 phòng ngủ, 70m2 nha em.")
    ut = eng._projects.get("Nyah Phú Định").find_unit_type("Cosmo Gen 2")
    assert ut is not None and ut.specs.get("số phòng ngủ") == "3"   # learned from chat
    # A DIFFERENT seller's unit of the same model now inherits it.
    convB = eng.conversation("B")
    convB.prop.fields.update({"du_an": "Nyah Phú Định", "unit_type": "Cosmo Gen 2"})
    assert eng.unit_type_for(convB.prop) is ut


def test_non_descriptive_message_does_not_trigger_learning():
    # Small talk must NOT spend an extract trying to learn a project/model.
    eng = _engine()
    eng.register_project("Nyah Phú Định")
    conv = eng.conversation("A")
    conv.prop.fields.update({"du_an": "Nyah Phú Định", "unit_type": "Cosmo Gen 2"})
    eng.handle("A", "Ok em, cảm ơn nha!")
    assert eng._projects.get("Nyah Phú Định").find_unit_type("Cosmo Gen 2") is None


def test_project_knowledge_is_global_and_accumulates_across_sellers():
    eng = _engine()
    # Seller A registers Botanic Towers (in seller A's context, but the registry is global).
    eng.register_project("Botanic Towers", amenities=["hồ bơi"], attributes={"cổng": "NTH"})
    # A DIFFERENT seller later adds more about the SAME project — must MERGE, not overwrite.
    eng.register_project("Botanic Tower", amenities=["gym"], attributes={"an ninh": "24/7"})
    p = eng._projects.get("Botanic Towers")
    assert set(p.amenities) == {"hồ bơi", "gym"}                 # unioned — nothing lost
    assert p.attributes == {"cổng": "NTH", "an ninh": "24/7"}     # merged
    # Seller B (a different user) listing a unit in it inherits the accumulated facts.
    conv = eng.conversation("sellerB")
    conv.prop.fields["du_an"] = "Botanic Towers"
    proj = eng.project_for(conv.prop)
    assert "hồ bơi" in proj.amenities and "gym" in proj.amenities


def test_conflicting_attribute_is_flagged_and_kept_both():
    eng = _engine()
    eng.register_project("Botanic Towers", attributes={"cổng": "Nguyễn Thượng Hiền"}, voter="s1")
    eng.register_project("Botanic Towers", attributes={"cổng": "Phan Đăng Lưu"}, voter="s2")
    p = eng._projects.get("Botanic Towers")
    # both values kept as seller-votes; the key is contested (tie), no silent overwrite
    assert p.votes["cổng"] == {"Nguyễn Thượng Hiền": ["s1"], "Phan Đăng Lưu": ["s2"]}
    assert "cổng" in p.contested_keys()
    facts = " ".join(p.facts())
    assert "CHƯA CHẮC" in facts and "Nguyễn Thượng Hiền" in facts        # surfaced for verify


def test_majority_vote_breaks_the_tie():
    eng = _engine()
    eng.register_project("Botanic Towers", attributes={"cổng": "Nguyễn Thượng Hiền"}, voter="s1")
    eng.register_project("Botanic Towers", attributes={"cổng": "Phan Đăng Lưu"}, voter="s2")
    eng.register_project("Botanic Towers", attributes={"cổng": "Phan Đăng Lưu"}, voter="s3")
    p = eng._projects.get("Botanic Towers")
    assert p.attributes["cổng"] == "Phan Đăng Lưu"          # leading value = most votes
    assert p.votes["cổng"]["Phan Đăng Lưu"] == ["s2", "s3"]
    assert "cổng" in p.contested_keys()                     # still flagged, not yet settled


def test_no_phantom_votes_same_seller_counts_once():
    eng = _engine()
    for _ in range(4):  # same seller repeats — must NOT inflate
        eng.register_project("X Tower", attributes={"cổng": "A"}, voter="s1")
    p = eng._projects.get("X Tower")
    assert p.votes["cổng"] == {"A": ["s1"]}
    assert "cổng" not in p.contested_keys()


def test_seller_changing_mind_moves_their_vote():
    eng = _engine()
    eng.register_project("X Tower", attributes={"cổng": "A"}, voter="s1")
    eng.register_project("X Tower", attributes={"cổng": "B"}, voter="s1")  # same seller, new answer
    p = eng._projects.get("X Tower")
    assert p.votes["cổng"] == {"B": ["s1"]}                  # A dropped, not left dangling
    assert "cổng" not in p.contested_keys()


def test_conflict_settles_when_a_value_wins_decisively():
    eng = _engine()
    for i in range(6):  # 6 distinct sellers say A (>= n=6)...
        eng.register_project("Y Tower", attributes={"cổng": "A"}, voter=f"a{i}")
    for i in range(2):  # ...vs 2 dissenters (<= n/3 = 2)
        eng.register_project("Y Tower", attributes={"cổng": "B"}, voter=f"b{i}")
    p = eng._projects.get("Y Tower")
    assert p.attributes["cổng"] == "A"
    assert "cổng" not in p.contested_keys()                 # settled → no longer flagged
    assert "CHƯA CHẮC" not in " ".join(p.facts())


def test_not_settled_while_dissent_too_high():
    eng = _engine()
    for i in range(6):
        eng.register_project("Z Tower", attributes={"cổng": "A"}, voter=f"a{i}")
    for i in range(3):  # 3 dissenters > n/3 = 2
        eng.register_project("Z Tower", attributes={"cổng": "B"}, voter=f"b{i}")
    p = eng._projects.get("Z Tower")
    assert "cổng" in p.contested_keys()                     # still contested (dissent too high)


def test_not_settled_while_leader_below_threshold():
    eng = _engine()
    for i in range(5):  # only 5 sellers say A — below n=6
        eng.register_project("W Tower", attributes={"cổng": "A"}, voter=f"a{i}")
    eng.register_project("W Tower", attributes={"cổng": "B"}, voter="b0")  # 1 dissenter
    p = eng._projects.get("W Tower")
    assert "cổng" in p.contested_keys()                     # leader hasn't hit n=6 yet


def test_register_unit_type_preserves_project_amenities():
    eng = _engine()
    eng.register_project("Nyah Phú Định", amenities=["hồ bơi"])
    eng.register_unit_type("Nyah Phú Định", "Fusion Gen 5", {"số tầng": "6"})
    p = eng._projects.get("Nyah Phú Định")
    assert p.amenities == ["hồ bơi"]                    # unit-type registration kept amenities
    assert p.find_unit_type("Fusion Gen 5") is not None


def test_register_project_from_text():
    eng = _engine()
    p = eng.register_project_from_text("Dự án Botanic Tower, có hồ bơi và gym, bảo vệ 24/7.")
    assert p is not None
    assert p.name.startswith("Botanic Tower")
    assert "hồ bơi" in p.amenities and "gym" in p.amenities


def test_presale_unit_captures_code_and_labels_and_folders_by_it():
    eng = _engine()
    conv = eng.conversation("ps1")
    conv.prop.loai_hinh = "chung_cu"
    conv.prop.fields["du_an"] = "Nyah Phú Định"     # a project unit, still pre-sale
    eng.handle("ps1", "Căn số 42, 70m2, giá 3 tỷ")
    li = conv.active
    assert li.prop.fields["ma_so_can"] == "42"       # captured the code
    assert "42" in li.label() and "Nyah Phú Định" in li.label()
    assert eng._folder_for(li) == f"Nyah Phú Định/42_{li.listing_id[:8]}"  # folder by code
    assert "dia_chi" not in li.prop.missing_fields()  # not blocked on an address


def test_multiple_units_of_same_model_split_in_one_message():
    # "2 căn 42 và 41 cùng mẫu Fusion Gen 5" → 2 listings, shared model/price, distinct code.
    eng = _engine()
    eng.handle("mu", "Tôi có 2 căn chung cư cùng mẫu Fusion Gen 5, mỗi căn 70m2 giá 3 tỷ, là căn 42 và 41")
    conv = eng.conversation("mu")
    assert len(conv.listings) == 2
    assert sorted(li.prop.fields.get("ma_so_can") for li in conv.listings) == ["41", "42"]
    for li in conv.listings:  # the shared fields landed on BOTH
        assert li.prop.fields.get("unit_type") == "Fusion Gen 5"
        assert li.prop.fields.get("gia") == "3 tỷ"
        assert li.prop.fields.get("dien_tich") == "70m2"


def test_fan_out_does_not_duplicate_on_a_later_turn():
    # The describing message stays in history; a later turn must NOT re-create the units.
    eng = _engine()
    eng.handle("mu3", "Hai căn chung cư cùng mẫu Fusion Gen 5, 70m2 giá 3 tỷ, là căn 42 và 41")
    assert len(eng.conversation("mu3").listings) == 2
    eng.handle("mu3", "Cả hai đều sổ hồng chính chủ nhé em")     # more chat, same basket
    assert len(eng.conversation("mu3").listings) == 2            # still 2, not 4


def test_single_unit_does_not_fan_out():
    # One code, no list → a single listing (cac_can must need ≥2 to split).
    eng = _engine()
    eng.handle("mu2", "Bán căn số 42 chung cư, 70m2, 3 tỷ")
    conv = eng.conversation("mu2")
    assert len(conv.listings) == 1
    assert conv.active.prop.fields.get("ma_so_can") == "42"


def test_match_listing_by_code_and_address():
    eng = _engine()
    conv = eng.conversation("d3")
    conv.active.prop.fields["ma_so_can"] = "42"
    li2 = eng.start_new_listing("d3")
    li2.prop.fields["dia_chi"] = "198A/22 Trương Đình Hội"
    assert eng._match_listing(conv, "42") is conv.listings[0]          # by unit code
    assert eng._match_listing(conv, "căn ở Trương Đình Hội") is li2    # by address tokens
    assert eng._match_listing(conv, "99") is None                     # no match


def test_status_report_resolves_referenced_can_and_marks_it():
    # Seller reports a SPECIFIC căn sold — system switches to it and marks it, no /switch.
    eng = _engine()
    eng.handle("d1", "Hai căn chung cư cùng mẫu Fusion Gen 5, 70m2 giá 3 tỷ, là căn 42 và 41")
    conv = eng.conversation("d1")
    assert conv.active.prop.fields.get("ma_so_can") == "41"   # active is the last unit (41)
    eng.handle("d1", "Căn 42 bán được rồi nha em!")            # refers to 42, not the active
    by_code = {li.prop.fields.get("ma_so_can"): li for li in conv.listings}
    assert by_code["42"].status == "đã bán"                    # the right căn got marked
    assert by_code["41"].status == "đang bán"                  # the other is untouched


def test_reference_switches_active_for_a_price_change():
    # After fan-out the active căn is 41; a price change referencing 42 must switch to 42.
    eng = _engine()
    eng.handle("d5", "Hai căn chung cư cùng mẫu Fusion Gen 5, 70m2 giá 3 tỷ, là căn 42 và 41")
    conv = eng.conversation("d5")
    assert conv.active.prop.fields.get("ma_so_can") == "41"
    eng.handle("d5", "Căn 42 giảm còn 2.8 tỷ nha em")   # refers to 42, the non-active căn
    by_code = {li.prop.fields.get("ma_so_can"): li for li in conv.listings}
    assert by_code["42"].prop.fields.get("gia") == "2.8 tỷ"   # the referenced căn updated
    assert by_code["41"].prop.fields.get("gia") == "3 tỷ"     # the other left alone


def test_status_applies_to_multiple_referenced_cans():
    # "Căn 42 với 41 bán rồi" → BOTH căn marked sold, not just one.
    eng = _engine()
    eng.handle("d6", "Hai căn chung cư cùng mẫu Fusion Gen 5, 70m2 giá 3 tỷ, là căn 42 và 41")
    eng.handle("d6", "Căn 42 với 41 bán hết rồi em ơi!")
    for li in eng.conversation("d6").listings:
        assert li.status == "đã bán"   # both, not just one


def test_status_without_basket_still_marks_the_only_listing():
    eng = _engine()
    eng.handle("d4", "Bán căn số 7, chung cư, 60m2, 2 tỷ")
    eng.handle("d4", "Căn đó bán rồi nha em")
    assert eng.conversation("d4").active.status == "đã bán"


def test_user_type_defaults_unknown():
    eng = _engine()
    r = eng.handle("u8", "nhà phố quận 5")
    assert r.user_type == "unknown"


def test_handle_media_reads_link_and_replies():
    # Inject a fake fetcher so no network is touched; mock provider "reads" it offline.
    def fake(url):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename="So_Hong.jpg")
    eng = _engine(fetcher=fake)
    r = eng.handle_media("u11", "https://example.com/x.jpg")
    assert r.reply  # a confirmation reply was produced
    assert "So_Hong.jpg" in r.reply  # the filename is shown back to the seller
    assert "xác nhận" in r.reply  # mock vision reply asks to confirm
    conv = eng.conversation("u11")
    assert len(conv.history) == 2  # one user (the read content) + one assistant turn


def test_handle_media_bad_link_is_graceful():
    def boom(url):
        raise FetchError("blocked")
    eng = _engine(fetcher=boom)
    r = eng.handle_media("u12", "http://127.0.0.1/x.jpg")
    assert "chưa mở được link" in r.reply  # honest fallback, no crash
    assert r.captured == []


def test_handle_media_folder_link_gets_specific_nudge():
    def boom(url):
        raise FetchError("folder")  # fetcher refuses; engine recognizes the folder URL
    eng = _engine(fetcher=boom)
    r = eng.handle_media("u13", "https://drive.google.com/drive/folders/ABC?usp=sharing")
    assert "thư mục" in r.reply.lower()  # asks for individual files, not generic fallback


def test_resolver_expands_a_drive_folder_into_files():
    # A folder URL resolves to several (download_url, name) pairs; all get read in one go.
    def resolver(url):
        return [("https://dl/a", "mat_tien.jpg"), ("https://dl/b", "so_hong.pdf")]
    def fetcher(url):
        return FetchedMedia(b"x", "image/jpeg", url)
    eng = _engine(fetcher=fetcher, resolver=resolver)
    r = eng.handle_media("u15", "https://drive.google.com/drive/folders/FLD")
    assert "mat_tien.jpg" in r.reply and "so_hong.pdf" in r.reply  # both names shown


def test_oversize_send_is_escalated_to_admin_not_processed():
    # A send whose TOTAL exceeds 20 MB isn't processed — it's handed to a human admin, and the
    # seller is told not to worry (imagine Zalo: a big file would time out / cost too much).
    big = b"\x00" * (11 * 1024 * 1024)  # 11 MB each → 22 MB total > 20 MB cap
    def fetcher(url):
        return FetchedMedia(big, "image/jpeg", url)
    eng = _engine(fetcher=fetcher)
    r = eng.handle_media_batch("u14", ["https://x/a.jpg", "https://x/b.jpg"])
    assert r.escalated is True                 # flagged for the Live-Chat-Monitor
    assert ">20MB" in r.reply and "admin" in r.reply
    assert eng.conversation("u14").active.media == []   # nothing stored
    esc = eng.conversation("u14").admin_escalations
    assert esc and esc[0]["reason"] == "media_over_size"


def test_send_under_20mb_is_processed_normally():
    small = b"\xff\xd8\xff" + b"\x00" * (8 * 1024 * 1024)  # 8 MB each → 16 MB total, under cap
    def fetcher(url):
        return FetchedMedia(small, "image/jpeg", url, filename="a.jpg")
    eng = _engine(fetcher=fetcher)
    r = eng.handle_media_batch("u14b", ["https://x/a.jpg", "https://x/b.jpg"])
    assert r.escalated is False
    assert r.reply and ">20MB" not in r.reply


def test_brochure_pdf_becomes_a_grounded_project_doc():
    # Phase 3 op `ingest_doc`: a seller-sent brochure/phân-lô PDF is transcribed ONCE to a
    # faithful .md on the project, so factual questions can be answered from it later (Phase 2).
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(provider=MockProvider(settings))
    eng.register_project("Nyah Phú Định")
    pdf = FetchedMedia(b"%PDF-1.7 brochure", "application/pdf", "u", filename="cosmo.pdf")
    photo = FetchedMedia(b"\xff\xd8\xff", "image/jpeg", "u", filename="render.jpg")
    titles = eng._ingest_group_docs("Nyah Phú Định", [pdf, photo], kind="mẫu nhà",
                                    default_title="Cosmo Gen 2")
    assert titles == ["Cosmo Gen 2"]                     # only the PDF became a doc, not the photo
    proj = eng._projects.get("Nyah Phú Định")
    assert proj.doc("mẫu nhà", "Cosmo Gen 2") is not None  # stored on the project, grounded-ready
    # No project → nothing ingested (needs a home).
    assert eng._ingest_group_docs(None, [pdf], kind="mẫu nhà") == []


class _FakeMediaStore:
    """Records every upload call instead of hitting SharePoint — verifies the ENGINE
    wires files to the right folder/listing, independent of the real Graph client."""

    def __init__(self):
        self.calls = []  # (folder, filename)
        self.moves = []  # (old_folder, new_folder)
        self.deletes = []  # item_ids

    def upload(self, folder, filename, data, content_type):
        self.calls.append((folder, filename))
        return MediaRef(item_id=f"id-{len(self.calls)}", web_url="https://sp/x",
                         path=f"{folder}/{filename}", filename=filename)

    def move_folder(self, old_path, new_path):
        self.moves.append((old_path, new_path))

    def delete(self, item_id):
        self.deletes.append(item_id)


def test_single_file_media_is_uploaded_and_referenced_on_the_listing():
    store = _FakeMediaStore()
    def fetcher(url):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename="mat_tien.jpg")
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle_media("u16", "https://x/mat_tien.jpg")
    conv = eng.conversation("u16")
    assert len(store.calls) == 1
    assert store.calls[0][1] == "mat_tien.jpg"
    assert len(conv.active.media) == 1
    assert conv.active.media[0]["filename"] == "mat_tien.jpg"


def test_media_folder_is_renamed_when_address_is_captured_later():
    # Seller sends photos first (no address) → stored under an id-only folder; later they
    # type the address → the SharePoint folder is renamed to the readable label, and the
    # listing's stored refs are updated to the new path (item_ids unchanged).
    store = _FakeMediaStore()
    def fetcher(url):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename="mat_tien.jpg")
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle_media("u20", "https://x/mat_tien.jpg")
    conv = eng.conversation("u20")
    short_id = conv.active.listing_id[:8]
    old_folder = conv.active.media[0]["path"].rsplit("/", 1)[0]
    assert old_folder == f"Nhà lẻ/{short_id}"  # id-only label, no address yet

    eng.handle("u20", "Nhà ở hẻm 123 đường Trần Bình Trọng Quận 5, giá 10 tỷ")
    assert len(store.moves) == 1
    old, new = store.moves[0]
    assert old == old_folder
    assert new != old and new.endswith(short_id)  # id preserved in the new readable label
    assert conv.active.media[0]["path"].rsplit("/", 1)[0] == new  # refs updated to new folder


def test_media_folder_not_renamed_when_folder_unchanged():
    store = _FakeMediaStore()
    def fetcher(url):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename="a.jpg")
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle("u21", "Nhà phố hẻm 5, đất 60m2 nhà 50m2, 8 tỷ")   # address already known
    eng.handle_media("u21", "https://x/a.jpg")                     # then photos
    eng.handle("u21", "Sổ hồng chính chủ nha em")                  # more chat, address unchanged
    assert store.moves == []                                       # nothing to move


class _BrochureProvider(MockProvider):
    """Vision read-out that describes a house-MODEL brochure (a mẫu nhà, not a specific
    căn) — so `register_unit_type_from_text` fires and media routes to the UnitType."""
    def describe_media(self, items, instruction):
        return "Mẫu nhà Fusion Gen 5. Dự án Nyah Phú Định. 3 phòng ngủ 4 tầng."
    def group_media(self, items, labels, instruction, json_schema):
        return {"nhom": [{"pham_vi": "can_moi", "tep": labels, "mo_ta": "brochure"}]}  # 1 group → fallback


def test_model_brochure_media_stored_on_unittype_not_a_listing():
    # A Fusion Gen 5 brochure folder → images belong to the MẪU NHÀ (shared by every unit),
    # NOT dumped onto whatever căn is active.
    store = _FakeMediaStore()
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    def fetcher(url):
        return FetchedMedia(url.encode(), "image/jpeg", url)  # unnamed, distinct bytes
    eng = _engine(fetcher=fetcher, media_store=store, provider=_BrochureProvider(settings))
    eng.handle_media_batch("m1", ["https://x/p1.jpg", "https://x/p2.jpg", "https://x/p3.jpg"])

    conv = eng.conversation("m1")
    assert conv.active.media == []                       # NOT attached to a căn
    ut = eng._projects.get("Nyah Phú Định").find_unit_type("Fusion Gen 5")
    assert ut is not None
    assert len(ut.media) == 3                             # attached to the model
    assert all("Fusion Gen 5/_Chung" in folder for folder, _ in store.calls)


def test_mau_nha_group_routes_media_to_unittype():
    # The mau_nha grouping scope routes that group's files to the UnitType. Two groups so
    # the batch takes the multi-group path (single group would fall back to a plain read).
    store = _FakeMediaStore()
    groups = [
        {"pham_vi": "mau_nha", "ten_mau_nha": "Fusion Gen 5",
         "tep": ["fusion_layout.jpg", "fusion_phoicanh.jpg"],
         "mo_ta": "Mẫu nhà Fusion Gen 5. Dự án Nyah Phú Định. Layout 4 tầng."},
        {"pham_vi": "chung", "tep": ["cong.jpg"], "mo_ta": "Dự án Nyah Phú Định. Có hồ bơi."},
    ]
    eng = _grouping_engine(groups, fetcher=_named_fetcher, media_store=store)
    eng.handle_media_batch("m2", ["https://x/fusion_layout.jpg", "https://x/fusion_phoicanh.jpg",
                                  "https://x/cong.jpg"])
    ut = eng._projects.get("Nyah Phú Định").find_unit_type("Fusion Gen 5")
    assert ut is not None and len(ut.media) == 2
    assert all(folder.endswith("Fusion Gen 5/_Chung")
               for folder, name in store.calls if "fusion" in name)


def test_unnamed_media_gets_a_real_extension_and_unique_names():
    # Regression (the "1 of 12 folder photos survived" bug): unnamed files used to upload
    # as bare "ảnh" (no extension, identical name) → same folder → overwrote down to one.
    # Now each unnamed file is named by CONTENT HASH + extension: distinct bytes → distinct
    # names (no collision), and the extension makes them openable.
    store = _FakeMediaStore()
    def fetcher(url):
        return FetchedMedia(url.encode(), "image/jpeg", url)  # distinct bytes per url, no filename
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle_media_batch("u18", ["https://x/a.jpg", "https://x/b.jpg"])
    uploaded_names = [name for _, name in store.calls]
    assert len(uploaded_names) == 2
    assert len(set(uploaded_names)) == 2          # distinct — no overwrite collision
    assert all(name.endswith(".jpg") for name in uploaded_names)
    assert all("ảnh" in name for name in uploaded_names)  # still recognizable as photos


def test_unnamed_media_naming_is_idempotent_for_identical_bytes():
    # Two byte-identical unnamed files dedupe to ONE name (content hash) — re-sending the
    # exact same photo overwrites itself rather than piling up duplicates.
    store = _FakeMediaStore()
    def fetcher(url):
        return FetchedMedia(b"\xff\xd8\xff\x00SAMEBYTES", "image/jpeg", url)  # identical, no filename
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle_media_batch("u19", ["https://x/a.jpg", "https://x/b.jpg"])
    uploaded_names = [name for _, name in store.calls]
    assert len(set(uploaded_names)) == 1          # same bytes → same name → dedupe


def test_no_media_store_configured_means_no_upload_and_no_crash():
    def fetcher(url):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename="a.jpg")
    eng = _engine(fetcher=fetcher)  # media_store=None (default)
    r = eng.handle_media("u17", "https://x/a.jpg")
    assert r.reply
    assert eng.conversation("u17").active.media == []


class _CapturingMediaStore:
    """Records the uploaded BYTES + content-type, to verify downscaling before storage."""
    def __init__(self):
        self.uploads = []  # (filename, data, content_type)

    def upload(self, folder, filename, data, content_type):
        self.uploads.append((filename, data, content_type))
        return MediaRef(f"id{len(self.uploads)}", "https://sp/x", f"{folder}/{filename}", filename)


def _big_png(color=(120, 90, 60)) -> bytes:
    from PIL import Image
    from io import BytesIO
    buf = BytesIO()
    Image.new("RGB", (3000, 2000), color).save(buf, format="PNG")
    return buf.getvalue()


def test_photos_are_downscaled_before_upload():
    from PIL import Image
    from io import BytesIO
    store = _CapturingMediaStore()
    def fetcher(url):
        return FetchedMedia(_big_png(), "image/png", url, filename="mat_tien.png")
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle_media("z1", "https://x/mat_tien.png")
    assert len(store.uploads) == 1
    name, data, ctype = store.uploads[0]
    w, h = Image.open(BytesIO(data)).size
    assert max(w, h) <= 1600          # actually shrunk to the display cap
    assert ctype == "image/jpeg" and name.endswith(".jpg")  # re-encoded


def test_pdf_documents_are_kept_original_not_downscaled():
    store = _CapturingMediaStore()
    pdf = b"%PDF-1.4 fake so hong bytes"
    def fetcher(url):
        return FetchedMedia(pdf, "application/pdf", url, filename="so_hong.pdf")
    eng = _engine(fetcher=fetcher, media_store=store)
    eng.handle_media("z3", "https://x/so_hong.pdf")
    assert store.uploads[0][1] == pdf   # untouched (legal-grade original kept)


def test_photo_cap_per_listing():
    store = _FakeMediaStore()
    def fetcher(url):
        n = int(url.rsplit("/p", 1)[1].split(".")[0])
        return FetchedMedia(_big_png((n, n, n)), "image/png", url)  # a distinct photo per url
    eng = _engine(fetcher=fetcher, media_store=store)
    urls = [f"https://x/p{i}.png" for i in range(25)]   # 25 photos of one căn
    eng.handle_media_batch("z2", urls)
    assert len(store.calls) == 20   # capped at MAX_PHOTOS_PER_LISTING (extras skipped)
    assert len(eng.conversation("z2").active.media) == 20


def test_photo_cap_is_configurable():
    store = _FakeMediaStore()
    def fetcher(url):
        n = int(url.rsplit("/p", 1)[1].split(".")[0])
        return FetchedMedia(_big_png((n, n, n)), "image/png", url)
    eng = _engine(fetcher=fetcher, media_store=store, max_photos_per_listing=5)
    eng.handle_media_batch("z4", [f"https://x/p{i}.png" for i in range(12)])
    assert len(store.calls) == 5   # honors the custom cap


def test_seller_is_told_when_photos_exceed_the_cap():
    store = _FakeMediaStore()
    def fetcher(url):
        n = int(url.rsplit("/p", 1)[1].split(".")[0])
        return FetchedMedia(_big_png((n, n, n)), "image/png", url)
    eng = _engine(fetcher=fetcher, media_store=store, max_photos_per_listing=3)
    r = eng.handle_media_batch("cap1", [f"https://x/p{i}.png" for i in range(7)])
    assert "tối đa 3 ảnh" in r.reply     # the limit is communicated
    assert "4 tấm sau" in r.reply        # 7 − 3 = 4 not kept


def test_no_cap_note_when_under_the_limit():
    store = _FakeMediaStore()
    def fetcher(url):
        n = int(url.rsplit("/p", 1)[1].split(".")[0])
        return FetchedMedia(_big_png((n, n, n)), "image/png", url)
    eng = _engine(fetcher=fetcher, media_store=store, max_photos_per_listing=10)
    r = eng.handle_media_batch("cap2", [f"https://x/p{i}.png" for i in range(4)])
    assert "CHƯA lưu" not in r.reply and "tối đa" not in r.reply


class _GroupingProvider(MockProvider):
    """Test double that returns a FIXED grouping, so the routing logic in
    `_group_media_batch` can be verified independently of the mock's filename heuristic."""

    def __init__(self, settings, groups):
        super().__init__(settings)
        self._groups = groups

    def group_media(self, items, labels, instruction, json_schema):
        return {"nhom": self._groups}


def _grouping_engine(groups, fetcher=None, media_store=None):
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    return _engine(fetcher=fetcher, provider=_GroupingProvider(settings, groups), media_store=media_store)


class _PriceListProvider(MockProvider):
    """Returns a FIXED price-list extraction, to verify the import-inventory routing. Its scope
    classifier defaults to a clear RAO-ALL intent (so the import tests, which pass a rao caption,
    populate the basket)."""
    def __init__(self, settings, payload):
        super().__init__(settings)
        self._payload = payload

    def extract_media(self, items, instruction, json_schema):
        return self._payload

    def extract(self, system, messages, json_schema):
        if "RAO BÁN" in system:                      # the inventory-scope classifier
            return {"co_y_rao": True, "ban_tat_ca": True}
        return super().extract(system, messages, json_schema)


def _pdf_fetcher(url):
    return FetchedMedia(b"%PDF-1.7 fake price list", "application/pdf", url, filename="bang_gia.pdf")


def test_price_list_pdf_is_imported_into_listings():
    payload = {
        "la_bang_gia": True,
        "ten_du_an": "Nyah Phú Định",
        "cac_can": [
            {"ma_so_can": "42", "mau_nha": "Cosmo Gen 2", "gia": "8,98 tỷ"},
            {"ma_so_can": "50", "mau_nha": "Cosmo Gen 2", "gia": "11,47 tỷ"},
            {"ma_so_can": "24", "mau_nha": "Opus v3", "gia": "12,75 tỷ"},
        ],
    }
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(fetcher=_pdf_fetcher, provider=_PriceListProvider(settings, payload))
    r = eng.handle_link("broker", "https://drive.google.com/open?id=ABC1234567",
                        caption="tôi bán hết bảng này ạ")  # explicit rao intent → import to rổ
    conv = eng.conversation("broker")
    assert len(conv.listings) == 3
    by_code = {li.prop.fields.get("ma_so_can"): li for li in conv.listings}
    assert by_code["42"].prop.fields["gia"] == "8,98 tỷ"
    assert by_code["42"].prop.fields["unit_type"] == "Cosmo Gen 2"
    assert by_code["42"].prop.fields["du_an"] == "Nyah Phú Định"
    assert "3 căn" in r.reply
    # project + models registered to the SHARED registry
    proj = eng._projects.get("Nyah Phú Định")
    assert proj is not None
    assert proj.find_unit_type("Cosmo Gen 2") is not None
    assert proj.find_unit_type("Opus v3") is not None


def test_price_list_keeps_all_package_prices():
    # A căn with Air + Max package prices must keep BOTH — in the inventory, the basket, and the
    # grounded .md. Regression: the extractor used to collapse to one price.
    payload = {"la_bang_gia": True, "ten_du_an": "Nyah Phú Định", "cac_can": [
        {"ma_so_can": "50", "mau_nha": "Cosmo Gen 2",
         "quy_cach_ban_giao": "Xây thô + hoàn thiện mặt tiền + hoàn thiện nội thất (chọn Air/Max)",
         "gia_goi": [{"ten_goi": "Air", "gia": "11.470.000.000"},
                     {"ten_goi": "Max", "gia": "11.797.600.000"}]},
        {"ma_so_can": "26", "mau_nha": "Office 2",
         "quy_cach_ban_giao": "Xây thô + hoàn thiện mặt tiền", "gia": "16.566.000.000"}]}
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(fetcher=_pdf_fetcher, provider=_PriceListProvider(settings, payload))
    eng.handle_link("b", "https://drive.google.com/open?id=ABC1234567", caption="tôi bán hết bảng này")
    # GIÁ is the seller's OWN offer → kept on their BASKET listing, with ALL packages preserved.
    b50 = next(li for li in eng.conversation("b").listings if li.prop.fields.get("ma_so_can") == "50")
    assert b50.prop.fields.get("gia_air") == "11.470.000.000"   # both packages kept
    assert b50.prop.fields.get("gia_max") == "11.797.600.000"
    assert "Air" in b50.prop.fields.get("gia_goi", "") and "Max" in b50.prop.fields.get("gia_goi", "")
    # SHARED knowledge keeps the physical facts + the QUY CÁCH BÀN GIAO (handover spec incl. which
    # finishing package the căn can be built with) — but NOT the price per gói.
    proj = eng._projects.get("Nyah Phú Định")
    u50 = proj.find_unit("50")
    assert u50.attrs.get("mau_nha") == "Cosmo Gen 2"
    assert "nội thất" in u50.attrs.get("quy_cach_ban_giao", "")   # handover spec IS shared
    assert "Air/Max" in u50.attrs.get("quy_cach_ban_giao", "")
    assert not any(k.startswith("gia") for k in u50.attrs)        # ...but NO price in shared inventory
    assert "mặt tiền" in proj.find_unit("26").attrs.get("quy_cach_ban_giao", "")  # base căn: spec too
    doc = proj.doc("danh mục", "danh mục Nyah Phú Định")
    assert "Cosmo Gen 2" in doc["markdown"] and "nội thất" in doc["markdown"]   # catalog shows spec
    assert "11.470.000.000" not in doc["markdown"]               # ...but NO price


def test_thin_starter_listing_is_dropped_once_real_can_exist():
    # The blank starter every conversation opens with — if the opening message only set loại
    # hình/dự án (a thin stub, no mã lô/địa chỉ/giá) — must NOT linger as junk 'căn 1 · nha_pho'
    # once real project căn are imported.
    eng = _engine()
    conv = eng.conversation("s")
    conv.active.prop.fields.update({"loai_hinh": "nha_pho", "du_an": "Nyah Phú Định"})  # thin stub
    eng.import_listings("s", [{"ma_so_can": "42", "du_an": "Nyah Phú Định"},
                              {"ma_so_can": "50", "du_an": "Nyah Phú Định"}])
    eng.handle("s", "ok em")                       # any turn → _build_result cleans up the stub
    codes = sorted(str(li.prop.fields.get("ma_so_can")) for li in eng.conversation("s").listings)
    assert codes == ["42", "50"]                   # the thin nha_pho starter is gone, only real căn


def test_web_link_is_ingested_to_shared_knowledge_with_source(monkeypatch):
    # Seller pastes a project WEB PAGE link → AOND reads it, summarizes to a grounded .md stored
    # on the SHARED project knowledge, tagged with the source URL (the "ask for a trusted link"
    # loop, completed). No price fabricated.
    import aioi.engine as E
    page = (b"<html><head><title>Nyah</title><style>x{}</style></head>"
            b"<body><h1>Du an Nyah Phu Dinh</h1><p>Chu dau tu: Cong ty X. Tong 50 lo.</p></body></html>")
    monkeypatch.setattr(E, "fetch_raw", lambda url, **kw: (page, "text/html"))
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(provider=MockProvider(settings))
    conv = eng.conversation("s")
    conv.prop.fields["du_an"] = "Nyah Phú Định"
    r = eng.handle_link("s", "https://nhaphudinh.vn/gioi-thieu")
    doc = eng._projects.get("Nyah Phú Định").doc("tham khảo web")
    assert doc is not None                                        # stored on the SHARED project
    assert "https://nhaphudinh.vn/gioi-thieu" in doc["markdown"]  # source URL recorded (provenance)
    assert "nhaphudinh.vn" in r.reply                             # told the seller it was read


class _ScopedPriceListProvider(_PriceListProvider):
    """Price-list double that also returns a FIXED import-scope, to verify a seller caption
    narrows the basket import to only the căn they rao."""
    def __init__(self, settings, payload, scope):
        super().__init__(settings, payload)
        self._scope = scope
    def extract(self, system, messages, json_schema):
        if "RAO BÁN" in system:                      # the inventory-scope classifier
            return self._scope
        return super().extract(system, messages, json_schema)


def test_price_list_import_is_scoped_by_seller_caption():
    # Bảng giá = whole project (3 căn); seller says "tôi bán 2 căn Cosmo Gen 2" → basket gets
    # ONLY those 2, but the FULL list is still kept as a grounded reference doc.
    payload = {"la_bang_gia": True, "ten_du_an": "Nyah Phú Định",
               "cac_can": [{"ma_so_can": "42", "mau_nha": "Cosmo Gen 2", "gia": "8,98 tỷ"},
                           {"ma_so_can": "50", "mau_nha": "Cosmo Gen 2", "gia": "11,47 tỷ"},
                           {"ma_so_can": "3", "mau_nha": "Cosmo", "gia": "9,71 tỷ"}]}
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    prov = _ScopedPriceListProvider(settings, payload, {"co_y_rao": True, "cac_ma_lo": ["42", "50"]})
    eng = _engine(fetcher=_pdf_fetcher, provider=prov)
    r = eng.handle_media("seller", "https://x/bang_gia.pdf",
                         caption="tôi bán 2 căn Cosmo Gen 2 trong này")
    codes = sorted(li.prop.fields.get("ma_so_can") for li in eng.conversation("seller").listings)
    assert codes == ["42", "50"]                         # only the 2 rao, not căn 3 (Cosmo)
    assert "2 căn" in r.reply and "9" not in r.reply.split("bảng có")[0]  # scoped, not all 3
    proj = eng._projects.get("Nyah Phú Định")
    doc = proj.doc("danh mục", "danh mục Nyah Phú Định")
    assert doc is not None and "3" in doc["markdown"] and "Cosmo Gen 2" in doc["markdown"]  # full catalog kept


def test_price_list_sent_without_rao_intent_is_kept_as_reference_not_listed():
    # GỬI FILE ≠ RAO: a price list with NO rao intent (just sent, no caption) populates SHARED
    # knowledge (catalog + inventory) and the seller's private price_book, but NOTHING enters the
    # rổ hàng — AOND asks which căn to rao instead of auto-listing all.
    payload = {"la_bang_gia": True, "ten_du_an": "Nyah Phú Định",
               "cac_can": [{"ma_so_can": "42", "mau_nha": "Cosmo Gen 2", "gia": "8 tỷ"},
                           {"ma_so_can": "3", "mau_nha": "Cosmo", "gia": "9 tỷ"}]}
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(fetcher=_pdf_fetcher, provider=_PriceListProvider(settings, payload))
    r = eng.handle_media("s2", "https://x/bang_gia.pdf")     # no caption → no rao intent
    conv = eng.conversation("s2")
    assert all(not li.prop.fields.get("ma_so_can") for li in conv.listings)  # NOTHING in the rổ
    assert "rao" in r.reply.lower() and "?" in r.reply                       # asks what to rao
    proj = eng._projects.get("Nyah Phú Định")
    assert proj.find_unit("42") is not None                  # catalog/inventory still populated (shared)
    assert "42" in conv.price_book and "3" in conv.price_book  # seller's price kept as reference


def test_non_price_list_pdf_falls_through_to_normal_read():
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(fetcher=_pdf_fetcher, provider=_PriceListProvider(settings, {"la_bang_gia": False}))
    r = eng.handle_link("u", "https://drive.google.com/open?id=XYZ7654321")
    assert len(eng.conversation("u").listings) == 1   # not fanned out
    assert "bảng giá" not in r.reply                    # took the normal read path


def test_price_list_image_screenshot_is_imported():
    # A SINGLE image (screenshot of a price table) is also imported, not just PDFs.
    payload = {"la_bang_gia": True, "ten_du_an": "Botanic Towers",
               "cac_can": [{"ma_so_can": "A-1", "mau_nha": "Type B", "gia": "3 tỷ"},
                           {"ma_so_can": "A-2", "mau_nha": "Type B", "gia": "3.1 tỷ"}]}
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    def img(url):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename="bang_gia.jpg")
    eng = _engine(fetcher=img, provider=_PriceListProvider(settings, payload))
    r = eng.handle_media("broker2", "https://x/bang_gia.jpg", caption="tôi rao hết bảng này ạ")
    assert len(eng.conversation("broker2").listings) == 2
    assert "2 căn" in r.reply


def _basket_with_codes(eng, user, codes):
    """Seed a basket where each listing has a distinct ma_so_can (like a price-list import)."""
    eng.import_listings(user, [{"ma_so_can": c, "du_an": "Nyah Phú Định"} for c in codes])


def test_reimport_same_price_list_does_not_duplicate():
    payload = {"la_bang_gia": True, "ten_du_an": "Nyah Phú Định",
               "cac_can": [{"ma_so_can": "42", "mau_nha": "Cosmo Gen 2", "gia": "8,98 tỷ"},
                           {"ma_so_can": "50", "mau_nha": "Cosmo Gen 2", "gia": "11,47 tỷ"}]}
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(fetcher=_pdf_fetcher, provider=_PriceListProvider(settings, payload))
    cap = "tôi bán hết bảng này ạ"
    eng.handle_link("b", "https://drive.google.com/open?id=ABC1234567", caption=cap)
    assert len(eng.conversation("b").listings) == 2
    r = eng.handle_link("b", "https://drive.google.com/open?id=ABC1234567", caption=cap)  # SAME again
    assert len(eng.conversation("b").listings) == 2   # NOT 4 — updated, not duplicated
    assert "KHÔNG thêm trùng" in r.reply


def test_import_updates_price_of_existing_can():
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(provider=MockProvider(settings))
    eng.import_listings("b", [{"ma_so_can": "42", "du_an": "Nyah Phú Định", "gia": "8 tỷ"}])
    eng.import_listings("b", [{"ma_so_can": "42", "du_an": "Nyah Phú Định", "gia": "8,5 tỷ"}])  # new price
    lis = eng.conversation("b").listings
    assert len(lis) == 1 and lis[0].prop.fields["gia"] == "8,5 tỷ"


def test_fan_out_updates_existing_code_not_duplicate():
    eng = _engine()
    _basket_with_codes(eng, "b", ["23", "24"])
    # Seller re-states "2 căn 23 và 24" — must NOT add duplicates.
    eng.handle("b", "Hai căn 23 và 24 cùng mẫu Fusion Gen 5.")
    codes = sorted(li.prop.fields.get("ma_so_can") for li in eng.conversation("b").listings)
    assert codes == ["23", "24"]                       # still 2, not 4
    # and the shared field (unit_type) got applied to the existing căn
    assert all("Fusion Gen 5" in (li.prop.fields.get("unit_type") or "")
               for li in eng.conversation("b").listings)


def test_load_dedups_a_legacy_duplicated_basket(tmp_path):
    # A DB persisted BEFORE the unique-code invariant can hold duplicate căn; loading it
    # must self-heal (collapse duplicates, merge fields).
    from aioi.engine import Listing
    from aioi.schema import Property
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "dup.db")
    e1 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    conv = e1.conversation("u")
    conv.listings = [
        Listing(prop=Property(fields={"ma_so_can": "23", "du_an": "X", "gia": "9 tỷ"})),
        Listing(prop=Property(fields={"ma_so_can": "23", "du_an": "X"})),   # duplicate căn 23
        Listing(prop=Property(fields={"ma_so_can": "24", "du_an": "X"})),
    ]
    conv.active_listing_id = conv.listings[0].listing_id
    e1._convstore.save(conv)
    # a fresh engine loads the dirty DB and heals it
    e2 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    conv2 = e2.conversation("u")
    assert sorted(li.prop.fields.get("ma_so_can") for li in conv2.listings) == ["23", "24"]
    kept23 = next(li for li in conv2.listings if li.prop.fields["ma_so_can"] == "23")
    assert kept23.prop.fields["gia"] == "9 tỷ"   # merged field survived
    # and it's persisted healed — a THIRD engine also sees 2
    e3 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    assert len(e3.conversation("u").listings) == 2


def test_same_code_different_du_an_is_one_can():
    # Bug: căn "2" once with du_an="Cosmo" (a MODEL leaked into du_an) and once with the real
    # project → must be ONE căn (code is the key), preferring the registered-project du_an.
    eng = _engine()
    eng.register_project("Nyah Phú Định")
    eng.import_listings("u", [{"ma_so_can": "2", "du_an": "Cosmo", "unit_type": "Cosmo"}])
    eng.import_listings("u", [{"ma_so_can": "2", "du_an": "Nyah Phú Định", "gia": "9 tỷ"}])
    lis = eng.conversation("u").listings
    assert len(lis) == 1                                   # not two căn 2
    assert lis[0].prop.fields["gia"] == "9 tỷ"             # merged
    assert lis[0].prop.fields["du_an"] == "Nyah Phú Định"  # the registered project won


def test_dedup_on_load_merges_same_code_across_du_an(tmp_path):
    from aioi.engine import Listing
    from aioi.schema import Property
    from aioi.projects import SqliteProjectStore
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "d.db")
    e1 = Engine(MockProvider(settings), project_store=SqliteProjectStore(db),
                conversation_store=SqliteConversationStore(db))
    e1.register_project("Nyah Phú Định")
    conv = e1.conversation("u")
    conv.listings = [
        Listing(prop=Property(fields={"ma_so_can": "2", "du_an": "Cosmo"})),          # wrong du_an
        Listing(prop=Property(fields={"ma_so_can": "2", "du_an": "Nyah Phú Định", "gia": "9 tỷ"})),
    ]
    conv.active_listing_id = conv.listings[0].listing_id
    e1._convstore.save(conv)
    e2 = Engine(MockProvider(settings), project_store=SqliteProjectStore(db),
                conversation_store=SqliteConversationStore(db))
    lis2 = e2.conversation("u").listings
    assert len(lis2) == 1 and lis2[0].prop.fields["du_an"] == "Nyah Phú Định"


class _SystemCapturingProvider(MockProvider):
    """Records the last `system` prompt passed to chat, to verify doc grounding is injected."""
    last_system = ""
    def chat(self, system, messages, max_tokens=256):
        type(self).last_system = system
        return super().chat(system, messages, max_tokens)


def test_project_memory_is_always_in_context_for_a_project_seller():
    # AOND must NOT 'forget' the project's price list / phân lô between turns — a seller whose
    # listing belongs to a project carries that memory into EVERY turn (so a later "à quên căn
    # góc lớn nhất" can be resolved instead of creating a junk listing).
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(provider=_SystemCapturingProvider(settings))
    eng.register_project("Nyah Phú Định")
    proj = eng._projects.get("Nyah Phú Định")
    proj.docs.append({"title": "phân lô", "markdown": "Lô 42: 43.75m²; Lô 50: 57.31m²"})
    eng._projects.save(proj)
    conv = eng.conversation("q")
    conv.prop.fields["du_an"] = "Nyah Phú Định"

    eng.handle("q", "Cho em hỏi diện tích lô 42 bao nhiêu vậy?")
    assert "43.75m²" in _SystemCapturingProvider.last_system   # factual → docs in context

    _SystemCapturingProvider.last_system = ""
    eng.handle("q", "Dạ em chào anh")                          # even a plain turn keeps the memory
    assert "43.75m²" in _SystemCapturingProvider.last_system

    # but a seller NOT in that project doesn't get its memory dumped
    _SystemCapturingProvider.last_system = ""
    eng.handle("other", "Dạ em chào anh")
    assert "43.75m²" not in _SystemCapturingProvider.last_system


def test_ingest_source_doc_stores_markdown_on_project(monkeypatch):
    import aioi.engine as E
    monkeypatch.setattr(E, "fetch_raw", lambda url, **kw: (b"%PDF-1.4 fake phan lo", "application/pdf"))
    store = _FakeMediaStore()
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    eng = _engine(media_store=store, provider=MockProvider(settings))
    eng.register_project("Nyah Phú Định")
    doc = eng.ingest_source_doc("https://x/phanlo.pdf", "Nyah Phú Định", kind="phân lô", title="Phân lô")
    assert doc["markdown"]                                  # transcribed to markdown
    proj = eng._projects.get("Nyah Phú Định")
    assert proj.doc(kind="phân lô") is not None             # stored on the project (reusable)
    folder, name = store.calls[0]
    assert name == "Phân lô.md" and folder.endswith("_Chung")  # uploaded to SharePoint as .md


def test_label_shows_unit_type():
    eng = _engine()
    eng.import_listings("b", [{"ma_so_can": "23", "du_an": "Nyah Phú Định",
                               "unit_type": "Cosmo Gen 2", "gia": "9 tỷ"}])
    label = eng.conversation("b").listings[0].label()
    assert "Cosmo Gen 2" in label and "23" in label


def _status_by_code(eng, user):
    return {li.prop.fields.get("ma_so_can"): li.status for li in eng.conversation(user).listings}


def test_keep_only_unlists_the_others_keeps_all_rows():
    # AOND never deletes: "chỉ giữ 23, 24" moves the OTHERS to 'không rao' — every row is kept.
    eng = _engine()
    _basket_with_codes(eng, "p", ["50", "42", "3", "24", "23"])
    r = eng.handle("p", "Trong 5 căn tôi chỉ có căn 23, 24 thôi, các căn khác của người khác nha")
    assert len(eng.conversation("p").listings) == 5 and "đúng" in r.reply.lower()  # held for confirm
    eng.handle("p", "đúng")
    st = _status_by_code(eng, "p")
    assert len(st) == 5                                    # NOTHING deleted
    assert st["23"] == "đang bán" and st["24"] == "đang bán"
    assert all(st[c] == "không rao" for c in ("50", "42", "3"))
    assert eng._rao_count(eng.conversation("p")) == 2


def test_clear_unlists_all_but_keeps_every_row():
    eng = _engine()
    _basket_with_codes(eng, "c", ["50", "42", "3", "24", "23"])
    r = eng.handle("c", "Xóa hết rổ hàng của anh đi em, làm lại từ đầu")
    assert len(eng.conversation("c").listings) == 5 and "đúng" in r.reply.lower()  # NOT wiped — confirm
    eng.handle("c", "đúng")
    lis = eng.conversation("c").listings
    assert len(lis) == 5                                   # all rows KEPT (no one-shot wipe)
    assert all(li.status == "không rao" for li in lis)     # just hidden from active rao
    assert eng._rao_count(eng.conversation("c")) == 0


def test_clear_all_variants_only_unlist():
    for msg in ("Bỏ hết các căn đi", "Xóa tất cả nha em", "Dọn sạch rổ hàng giúp anh"):
        eng = _engine()
        _basket_with_codes(eng, "c2", ["1", "2", "3"])
        eng.handle("c2", msg)
        eng.handle("c2", "đúng")                           # confirm
        lis = eng.conversation("c2").listings
        assert len(lis) == 3, msg                          # nothing deleted
        assert all(li.status == "không rao" for li in lis), msg


def test_destructive_op_waits_for_confirmation_not_applied_on_misread():
    # Regression: a garbled/benign message ("tránh nhau vi vu nhé") must NEVER touch the basket.
    # Unlist asks first; only an explicit "đúng" applies; "thôi" or moving on keeps everything rao.
    eng = _engine()
    _basket_with_codes(eng, "cf", ["1", "2", "3"])
    r = eng.handle("cf", "xóa hết đi em")
    assert eng._rao_count(eng.conversation("cf")) == 3 and "đúng" in r.reply.lower()  # held, asked
    eng.handle("cf", "thôi khỏi")                          # deny → still all rao
    assert eng._rao_count(eng.conversation("cf")) == 3
    eng.handle("cf", "xóa hết đi em")                      # ask again
    eng.handle("cf", "ừ")                                  # a plain yes applies
    assert eng._rao_count(eng.conversation("cf")) == 0     # all unlisted
    assert len(eng.conversation("cf").listings) == 3       # but all rows still kept


def test_drop_unlists_the_named_cans_keeps_rows():
    eng = _engine()
    _basket_with_codes(eng, "q", ["50", "42", "3"])
    eng.handle("q", "Bỏ giúp anh căn 50 và 3 nha em")
    eng.handle("q", "đúng")                                # confirm
    st = _status_by_code(eng, "q")
    assert len(st) == 3                                    # nothing deleted
    assert st["50"] == "không rao" and st["3"] == "không rao" and st["42"] == "đang bán"


def test_unlist_persists_across_restart(tmp_path):
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "prune.db")
    e1 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    _basket_with_codes(e1, "r", ["50", "42", "23"])
    e1.handle("r", "Anh chỉ có căn 23 thôi, còn lại bỏ hết")
    e1.handle("r", "đúng")                                 # confirm the unlist
    # fresh engine on the same DB — the căn are KEPT (just 'không rao'), never deleted
    e2 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    st = _status_by_code(e2, "r")
    assert sorted(st) == ["23", "42", "50"]                # all 3 rows survived the restart
    assert st["23"] == "đang bán" and st["42"] == "không rao" and st["50"] == "không rao"


def test_unlist_keeps_the_listing_media_never_deletes():
    # AOND never deletes data — unlisting a căn must KEEP its stored photos (reversible).
    store = _FakeMediaStore()
    eng = _engine(fetcher=lambda u: FetchedMedia(b"\xff\xd8\xff", "image/jpeg", u, filename="a.jpg"),
                  media_store=store)
    eng.import_listings("m", [{"ma_so_can": "7"}, {"ma_so_can": "8"}])
    eng.switch_listing("m", 1)                        # căn 7 active
    eng.handle_media("m", "https://x/a.jpg")          # attach a photo to căn 7
    item_id = eng.conversation("m").listings[0].media[0]["item_id"]
    eng.handle("m", "Bỏ căn 7 nha em")
    eng.handle("m", "đúng")                           # confirm the unlist
    can7 = next(li for li in eng.conversation("m").listings if li.prop.fields.get("ma_so_can") == "7")
    assert can7.status == "không rao"                 # hidden, not gone
    assert can7.media and can7.media[0]["item_id"] == item_id   # media kept on the row
    assert item_id not in store.deletes               # and NOT deleted from storage


def _named_fetcher(url):
    return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", url, filename=url.rsplit("/", 1)[-1])


def test_media_batch_single_group_falls_back_to_one_listing():
    # Only one group → not "actually" multiple căn; behaves like the old single-blob read.
    eng = _grouping_engine(
        [{"pham_vi": "can_moi", "tep": ["a.jpg", "b.jpg"], "mo_ta": "1 căn nhà phố"}],
        fetcher=_named_fetcher,
    )
    r = eng.handle_media_batch("g1", ["https://x/a.jpg", "https://x/b.jpg"])
    assert len(eng.conversation("g1").listings) == 1
    assert "a.jpg" in r.reply and "b.jpg" in r.reply


def test_media_batch_splits_into_new_listings():
    groups = [
        {"pham_vi": "can_moi", "tep": ["can1_a.jpg"], "mo_ta": "nhà phố quận 5, 50m2, giá 10 tỷ"},
        {"pham_vi": "can_moi", "tep": ["can2_a.jpg"], "mo_ta": "chung cư 70m2, giá 5 tỷ"},
    ]
    eng = _grouping_engine(groups)
    r = eng.handle_media_batch("g2", ["https://x/can1_a.jpg", "https://x/can2_a.jpg"])
    conv = eng.conversation("g2")
    assert len(conv.listings) == 2                    # first fills the empty starter, 2nd is new
    assert conv.listings[0].prop.fields.get("gia") == "10 tỷ"
    assert conv.listings[1].prop.fields.get("gia") == "5 tỷ"
    assert "2 nhóm" in r.reply
    assert "xác nhận" in r.reply.lower() or "đúng không" in r.reply  # asks seller to verify


def test_infometric_group_extracts_data_but_does_not_store_the_image():
    # A data-only page (bảng giá) → the căn gets the data (giá), but NO photo is stored.
    store = _FakeMediaStore()
    groups = [
        {"pham_vi": "can_moi", "tep": ["mat_tien.jpg"], "mo_ta": "nhà phố quận 5, 50m2"},
        {"pham_vi": "can_da_co", "chi_so_can_da_co": 1, "tep": ["bang_gia.jpg"],
         "mo_ta": "bảng giá: 10 tỷ", "chi_luu_du_lieu": True},
    ]
    eng = _grouping_engine(groups, fetcher=_named_fetcher, media_store=store)
    r = eng.handle_media_batch("inf1", ["https://x/mat_tien.jpg", "https://x/bang_gia.jpg"])
    conv = eng.conversation("inf1")
    assert conv.listings[0].prop.fields.get("gia") == "10 tỷ"        # data extracted
    stored_names = [name for _, name in store.calls]
    assert "mat_tien.jpg" in stored_names                            # real photo kept
    assert "bang_gia.jpg" not in stored_names                        # data page abandoned
    assert "chỉ lấy dữ liệu" in r.reply


def test_single_group_infometric_extracts_but_abandons_image():
    store = _FakeMediaStore()
    groups = [{"pham_vi": "can_moi", "tep": ["bang_thong_so.jpg"],
               "mo_ta": "nhà phố 50m2 giá 8 tỷ", "chi_luu_du_lieu": True}]
    eng = _grouping_engine(groups, fetcher=_named_fetcher, media_store=store)
    r = eng.handle_media_batch("inf2", ["https://x/bang_thong_so.jpg", "https://x/x2.jpg"])
    assert eng.conversation("inf2").active.media == []   # nothing stored
    assert store.calls == []
    assert "không cần lưu ảnh" in r.reply


def test_media_batch_common_group_enriches_project_not_a_new_listing():
    groups = [
        {"pham_vi": "can_moi", "tep": ["can1.jpg"], "mo_ta": "chung cư 70m2, giá 5 tỷ"},
        {"pham_vi": "chung", "tep": ["hoboi.jpg"], "mo_ta": "Dự án Botanic Towers. Có hồ bơi."},
    ]
    eng = _grouping_engine(groups)
    eng.handle_media_batch("g3", ["https://x/can1.jpg", "https://x/hoboi.jpg"])
    conv = eng.conversation("g3")
    assert len(conv.listings) == 1                     # "chung" doesn't create a listing
    proj = eng._projects.get("Botanic Towers")
    assert proj is not None and "hồ bơi" in proj.amenities


def test_media_batch_uploads_route_chung_to_project_and_can_moi_to_listing():
    store = _FakeMediaStore()
    groups = [
        {"pham_vi": "can_moi", "tep": ["can1.jpg"], "mo_ta": "chung cư 70m2, giá 5 tỷ"},
        {"pham_vi": "chung", "tep": ["hoboi.jpg"], "mo_ta": "Dự án Botanic Towers. Có hồ bơi."},
    ]
    eng = _grouping_engine(groups, fetcher=_named_fetcher, media_store=store)
    eng.handle_media_batch("g5", ["https://x/can1.jpg", "https://x/hoboi.jpg"])
    conv = eng.conversation("g5")

    assert len(store.calls) == 2
    by_file = {fname: folder for folder, fname in store.calls}
    assert by_file["hoboi.jpg"].startswith("Botanic Towers/")  # project-level, not a listing
    assert "_Chung" in by_file["hoboi.jpg"]
    assert not by_file["can1.jpg"].startswith("Botanic Towers/_Chung")  # went to the listing's own folder

    # only the listing's own file is referenced on it — the "chung" upload has no listing
    filenames_on_listing = [m["filename"] for m in conv.listings[0].media]
    assert filenames_on_listing == ["can1.jpg"]


def test_media_batch_matches_existing_basket_listing():
    eng = _engine()
    eng.handle("g4", "Anh bán căn nhà phố Quận 5, hẻm 10, 40m2, giá 6 tỷ.")
    eng.start_new_listing("g4")
    eng.handle("g4", "Còn 1 căn nữa: chung cư Quận 7, 60m2, giá 4 tỷ.")
    assert len(eng.conversation("g4").listings) == 2

    groups = [
        {"pham_vi": "can_da_co", "chi_so_can_da_co": 1, "tep": ["them_anh.jpg"],
         "mo_ta": "khu này 45m2, giá 6.5 tỷ"},
        {"pham_vi": "can_da_co", "chi_so_can_da_co": 2, "tep": ["extra.jpg"],
         "mo_ta": "sân thượng rộng"},
    ]
    eng2 = _grouping_engine(groups)
    eng2._conversations = eng._conversations  # reuse the same in-memory basket
    eng2.handle_media_batch("g4", ["https://x/them_anh.jpg", "https://x/extra.jpg"])
    conv = eng2.conversation("g4")
    assert len(conv.listings) == 2                      # no new listing created
    assert conv.listings[0].prop.fields.get("dien_tich") == "45m2"  # applied to #1, not #2
    assert conv.listings[1].prop.fields.get("dien_tich") == "60m2"  # #2 untouched


def test_listing_persists_across_restart(tmp_path):
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "c.db")
    e1 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    e1.handle("uP", "Bán nhà phố hẻm 123 đường Trần Bình Trọng, đất 60m2, nhà 50m2, giá 10 tỷ")
    # A fresh engine on the same DB (simulating a restart) restores the listing.
    e2 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    conv = e2.conversation("uP")
    assert conv.prop.loai_hinh == "nha_pho"
    assert conv.prop.fields.get("dien_tich_dat") == "60m2"
    assert conv.prop.fields.get("dien_tich_nha") == "50m2"
    assert conv.history  # chat history survived too


def test_basket_listings_are_isolated():
    eng = _engine()
    eng.handle("uB", "Bán nhà phố Quận 5, hẻm 123, đất 60m2, nhà 50m2, 10 tỷ")
    eng.start_new_listing("uB")  # seller lists another property
    eng.handle("uB", "Căn này chung cư, 70m2, 3 tỷ")
    conv = eng.conversation("uB")
    assert len(conv.listings) == 2
    # The new listing's data is its own — NOT contaminated by the first (history sliced).
    assert conv.listings[0].prop.loai_hinh == "nha_pho"
    assert conv.listings[0].prop.fields.get("dien_tich_dat") == "60m2"
    assert conv.listings[0].prop.fields.get("dien_tich_nha") == "50m2"
    assert conv.listings[1].prop.loai_hinh == "chung_cu"
    assert conv.listings[1].prop.fields.get("dien_tich") == "70m2"
    assert conv.active.listing_id == conv.listings[1].listing_id


def test_basket_persists_across_restart(tmp_path):
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "b.db")
    e1 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    e1.handle("uB2", "Bán nhà phố hẻm 1, 50m2, 10 tỷ")
    e1.start_new_listing("uB2")
    e1.handle("uB2", "Bán chung cư, 70m2, 3 tỷ")
    e2 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    conv = e2.conversation("uB2")
    assert len(conv.listings) == 2                       # whole basket restored
    assert conv.active.prop.loai_hinh == "chung_cu"      # active listing restored


def test_basket_block_prompts_to_disambiguate_when_many():
    eng = _engine()
    conv = eng.conversation("uD")
    conv.prop.fields["dia_chi"] = "146 Trần Bình Trọng"
    eng.start_new_listing("uD")
    conv.prop.fields["dia_chi"] = "Chung cư Quận 7"
    block = eng._basket_block(conv)
    assert "RỔ HÀNG" in block and "căn nào ạ?" in block          # asks which căn
    assert "Trần Bình Trọng" in block and "Quận 7" in block        # lists both
    assert eng._basket_block(eng.conversation("uSolo")) == ""      # single listing → no block


def test_switch_and_set_status(tmp_path):
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "s.db")
    e1 = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db))
    e1.handle("uE", "Bán nhà phố hẻm 1, 50m2, 10 tỷ")
    e1.start_new_listing("uE")
    e1.handle("uE", "Bán chung cư, 70m2, 3 tỷ")
    e1.switch_listing("uE", 1)          # seller picked căn 1 (the nhà phố)
    e1.set_status("uE", "đã bán")
    # Persisted: căn 1 sold, căn 2 still selling.
    conv = Engine(MockProvider(settings), conversation_store=SqliteConversationStore(db)).conversation("uE")
    assert conv.listings[0].status == "đã bán"
    assert conv.listings[1].status == "đang bán"


def test_handle_link_auto_imports_a_spreadsheet(monkeypatch):
    eng = _engine()
    csv_bytes = "Loại hình,Giá\nNhà phố,10 tỷ\n".encode()
    monkeypatch.setattr("aioi.engine.fetch_raw", lambda url, **k: (csv_bytes, "text/csv"))
    r = eng.handle_link("uL", "https://x/rohang.csv")   # no /import needed
    assert "nhập 1 căn" in r.reply
    assert eng.conversation("uL").listings[0].prop.loai_hinh == "nha_pho"


def test_handle_link_auto_reads_an_image(monkeypatch):
    def img_fetcher(u):
        return FetchedMedia(b"\xff\xd8\xff", "image/jpeg", u, filename="a.jpg")
    eng = _engine(fetcher=img_fetcher)
    monkeypatch.setattr("aioi.engine.fetch_raw", lambda url, **k: (b"\xff\xd8\xff", "image/jpeg"))
    r = eng.handle_link("uI", "https://x/photo")
    assert "xác nhận" in r.reply   # routed to the vision read, not spreadsheet import


def test_auto_detect_new_listing_in_chat():
    eng = _engine()
    eng.handle("uN", "Bán nhà phố Q5, hẻm 123, 50m2, 10 tỷ")
    eng.handle("uN", "Còn một căn nữa, chung cư Quận 7, 70m2, 5 tỷ")  # different property
    conv = eng.conversation("uN")
    assert len(conv.listings) == 2                       # auto-split, no /new needed
    assert conv.listings[0].prop.loai_hinh == "nha_pho"  # first kept clean
    assert conv.listings[1].prop.loai_hinh == "chung_cu"  # second is its own
    assert conv.listings[1].prop.fields.get("dien_tich") == "70m2"


def test_import_csv_llm_maps_odd_headers():
    # "Đơn giá (tỷ)" / "Khu vực" aren't in the deterministic aliases → LLM maps them.
    eng = _engine()
    csv_text = "Khu vực,Đơn giá (tỷ),Loại\nQuận 5,10,Nhà phố\n"
    n = eng.import_csv("impL", csv_text)
    assert n == 1
    li = eng.conversation("impL").listings[0]
    assert li.prop.fields.get("dia_chi") == "Quận 5"   # "Khu vực" → dia_chi (LLM)
    assert li.prop.fields.get("gia") == "10"           # "Đơn giá (tỷ)" → gia (LLM)
    assert li.prop.loai_hinh == "nha_pho"


def test_bulk_import_creates_a_listing_per_row():
    from aioi.bulk import parse_csv
    csv_text = (
        "Loại hình,Địa chỉ,Diện tích,Giá\n"
        "Nhà phố,Hẻm 123 Q5,50m2,10 tỷ\n"
        "Căn hộ,Botanic Q7,70m2,5 tỷ\n"
    )
    eng = _engine()
    n = eng.import_listings("imp", parse_csv(csv_text))
    assert n == 2
    conv = eng.conversation("imp")
    assert len(conv.listings) == 2          # the empty starter listing was filled, then one more
    assert conv.listings[0].prop.loai_hinh == "nha_pho"
    assert conv.listings[0].prop.fields["dien_tich"] == "50m2"
    assert conv.listings[1].prop.fields["dia_chi"] == "Botanic Q7"


def test_seller_assistant_stable_per_user():
    eng = _engine()
    a = eng.handle("same-user", "nhà phố").assistant_name
    b = eng.handle("same-user", "thêm thông tin").assistant_name
    assert a == b
