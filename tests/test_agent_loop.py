"""Step 2: the native-tool-use agent loop. A scripted provider returns FIXED tool-call
sequences so the loop's execution + guardrails are verified deterministically (independent of
a live model) — same pattern as the media/price-list test doubles."""
from aioi.config import Settings
from aioi.engine import Engine
from aioi.llm.base import ToolCall, ToolTurn
from aioi.llm.mock import MockProvider
from aioi.projects import SqliteProjectStore, Unit


class _ScriptedAgent(MockProvider):
    """Returns a fixed list of ToolTurns from `converse`, one per call — lets a test drive the
    exact tool sequence and assert the engine executes each against the real store/basket."""
    def __init__(self, settings, script):
        super().__init__(settings)
        self._script = list(script)
        self.calls: list[dict] = []

    def converse(self, system, messages, tools, max_tokens=1024):
        self.calls.append({"messages": messages})
        return self._script[len(self.calls) - 1]


def _engine(script, tmp_path):
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    db = str(tmp_path / "loop.db")
    eng = Engine(_ScriptedAgent(settings, script), project_store=SqliteProjectStore(db))
    eng.register_project("Nyah Phú Định")
    proj = eng._projects.get("Nyah Phú Định")
    proj.units = [
        Unit("42", {"mau_nha": "Cosmo Gen 2", "dien_tich_dat": "43.75"}, loai_hinh="nha_pho"),
        Unit("26", {"mau_nha": "Office 2", "dien_tich_dat": "92", "gia": "16.566 tỷ"}, loai_hinh="nha_pho"),
    ]
    eng._projects.save(proj)
    conv = eng.conversation("s")
    conv.prop.fields["du_an"] = "Nyah Phú Định"
    return eng


def test_agent_loop_resolves_by_characteristic_and_adds_to_basket(tmp_path):
    # Seller: "căn góc lớn nhất" → the model queries the inventory (largest area = 26), then adds
    # it. The engine executes tim_can + them_can_vao_ro against the REAL store/basket.
    script = [
        ToolTurn("", [ToolCall("t1", "tim_can", {"lon_nhat": "dien_tich_dat"})]),
        ToolTurn("", [ToolCall("t2", "them_can_vao_ro", {"ma_so_can": "26"})]),
        ToolTurn("Dạ em thêm căn 26 (Office 2, 16.566 tỷ) vào rổ rồi ạ!", []),
    ]
    eng = _engine(script, tmp_path)
    r = eng.handle("s", "à quên, tôi bán luôn căn góc diện tích lớn nhất nữa nha em")
    assert "26" in r.reply
    codes = [li.prop.fields.get("ma_so_can") for li in eng.conversation("s").listings]
    assert "26" in codes                                   # the resolved căn is in the basket
    can26 = next(li for li in eng.conversation("s").listings if li.prop.fields.get("ma_so_can") == "26")
    assert can26.prop.fields.get("unit_type") == "Office 2"  # mẫu nhà is SHARED → pulled from catalog
    assert can26.prop.fields.get("gia") is None              # GIÁ is the seller's OWN — never from the shared catalog


def test_agent_loop_learns_a_fact_into_the_project(tmp_path):
    # Seller tells us a new fact ("căn 26 là lô góc"); the model calls ghi_nho_can → it's stored
    # on the project (shared for the next seller), and becomes queryable.
    script = [
        ToolTurn("", [ToolCall("t1", "ghi_nho_can", {"ma_so_can": "26", "thuoc_tinh": {"vi_tri": "góc"}})]),
        ToolTurn("Dạ em ghi nhớ căn 26 là lô góc rồi ạ!", []),
    ]
    eng = _engine(script, tmp_path)
    eng.handle("s", "căn 26 là lô góc đó nha em")
    unit26 = eng._projects.get("Nyah Phú Định").find_unit("26")
    assert unit26.attrs.get("vi_tri") == "góc"             # learned + persisted on the project
    # persisted across restart too
    eng2 = Engine(MockProvider(Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")),
                  project_store=SqliteProjectStore(str(tmp_path / "loop.db")))
    assert eng2._projects.get("Nyah Phú Định").find_unit("26").attrs.get("vi_tri") == "góc"


def test_drawing_request_never_touches_the_basket(tmp_path):
    # "bản vẽ căn 24" is a request to SEE a drawing — it must NOT run basket ops (a mis-parse
    # once pruned the basket to 1 căn). The basket stays intact and the reply is a media answer.
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    prov = _ScriptedAgent(settings, [])   # if it entered ops/agent-loop, converse would IndexError
    eng = Engine(prov, project_store=SqliteProjectStore(str(tmp_path / "d.db")))
    eng.import_listings("s", [{"ma_so_can": c, "du_an": "Nyah Phú Định"} for c in ("42", "50", "24")])
    r = eng.handle("s", "bản vẽ căn 24")
    codes = sorted(li.prop.fields.get("ma_so_can") for li in eng.conversation("s").listings)
    assert codes == ["24", "42", "50"]                # basket UNTOUCHED (was being pruned)
    assert "bản vẽ" in r.reply.lower() or "mặt bằng" in r.reply.lower()   # a media answer
    assert "còn 1 căn" not in r.reply                 # not the destructive receipt


def test_agent_loop_not_triggered_without_inventory(tmp_path):
    # A plain listing (no project inventory) must NOT enter the agent loop — the scripted turns
    # would never be consumed. Guards the narrow gate.
    settings = Settings(provider="mock", anthropic_api_key=None, chat_model="x", vision_model="y")
    prov = _ScriptedAgent(settings, [ToolTurn("should not be used", [])])
    eng = Engine(prov, project_store=SqliteProjectStore(str(tmp_path / "x.db")))
    eng.handle("u", "Bán nhà phố Quận 5, căn góc, 80m2, 10 tỷ")   # "góc" hint but NO inventory
    assert prov.calls == []                                        # converse never called
