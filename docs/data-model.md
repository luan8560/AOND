# AI Ơi Nhà Đất — Mô hình dữ liệu & Lưu trữ (Data Model)

> Tài liệu thiết kế cho **milestone lưu trữ** (SRD §VII) và phía người mua (M•ai search).
> Bản xuất từ ghi nhớ dự án — cập nhật 2026-06-28. Trạng thái build được ghi rõ trong từng mục.
> Liên quan: tài liệu build engine (`docs/build-status.md` nếu xuất), `req + chat examples.docx`.

## Vì sao cần lưu bền
Phải lưu **MỌI** chi tiết BĐS người rao cung cấp (toàn bộ listing có cấu trúc, không chỉ state chat), ở dạng **truy vấn được**. Mục đích: phục vụ bên mua/thuê — M•ai search truy vấn dữ liệu này để khớp BĐS cho người mua. Claude là stateless; "trí nhớ" = thứ app lưu và phát lại.

> *Trạng thái:* listing nay đã lưu bền qua `SqliteConversationStore` (sống sót restart) — nhưng dạng blob theo từng conversation; **chỉ mục tìm-kiếm-chéo cho bên mua vẫn chưa build.**

## Hai tầng lưu trữ (đã chốt)
1. **SharePoint = kho gốc (system of record)** — file gốc (ảnh, scan sổ hồng, hợp đồng, giấy tờ), cây thư mục §VII, tích hợp M365, con người duyệt, phân quyền, versioning.
2. **Chỉ mục tìm kiếm (serving layer)** — kho truy vấn nhanh (SQLite local → Postgres + pgvector khi lớn): cột lọc + vector embeddings. `search_listings` truy vấn realtime.

**Lớp Store đã khởi động (2026-06-24):** các mảnh lưu bền hiện có — `SqliteProjectStore` (dự án) và `SqliteConversationStore` (`aioi/engine.py`) cho listing/conversation (Property + lịch sử chat + user_type + listing_proposed + `Conversation.listing_id` UUID ổn định). Engine nhận `conversation_store=`; dict in-memory là cache, load-on-miss khôi phục sau restart, save sau mỗi lượt. CLI dùng cả hai trên một `aioi.db` (`Settings.db_path`/`AIOI_DB_PATH`, đã gitignore) — đã verify: dự án + listing đều sống sót restart mô phỏng. Cùng interface cắm-rút (→ SharePoint/Postgres sau). **Còn lại:** chỉ mục truy vấn (cột lọc + embeddings cho bên mua), UnitType, các trường dự án giàu, media SharePoint.

### SharePoint hoạt động thế nào (thiết kế, CHƯA build)
SharePoint = một document library M365 = một "drive" trong **Microsoft Graph API**. Xác thực = app Azure AD (Entra) + OAuth client-credentials, quyền application **`Sites.Selected`** (least-privilege, một site) + admin consent — tương đương Google Drive API key. Upload: nhỏ (<4MB) `PUT /sites/{id}/drive/root:/<path>/<file>:/content`; lớn dùng upload session (chunk). **Bố cục thư mục map theo taxonomy:** `/<Quận>/<Dự án>/_Chung/` cho file cấp dự án (sơ đồ tổng thể, brochure, bản đồ vị trí, mặt bằng, phân lô), `/<Dự án>/<Loại căn>/_Chung/` cho asset UnitType (floor plan, render), `/<Loại căn>/<Block-Căn>/{Pháp lý,Nội thất,...}` cho Unit — + metadata columns để truy vấn. **Quan trọng: BYTES lưu ở SharePoint, chỉ lưu THAM CHIẾU (Graph item id + webUrl + path) trên record DB** (Project/UnitType/Unit) — không bao giờ lưu bytes trong SQLite. Sanitize tên file (SharePoint cấm `~ " # % & * : < > ? / \ { | }`; cùng vấn đề mojibake của Drive) vd `<listing_id>_mat_tien_01.jpg`. Đọc lại cho bên mua: sharing link SharePoint = link first-party (đúng quy tắc M•ai chỉ gửi link nhà mình). Cần: app Azure AD → rồi `SharePointMediaStore` sau interface Store. Lưu ý: ngưỡng 4MB, throttling 429 (retry), ngưỡng 5.000 mục của List.

Khi lưu/sửa: ghi file gốc vào SharePoint **và** cập nhật chỉ mục. Bên mua truy vấn → lấy ID → app lấy ảnh/giấy tờ từ SharePoint theo ID/link. Lý do tách: SharePoint là hệ thống lưu trữ file, không phải cỗ máy tìm kiếm (không có vector search; List bị throttle ở ngưỡng 5.000 mục, dự án nhắm ~5.000+ listing).

## Định danh & phân loại có thể đổi (nguyên tắc cốt lõi)
- **Định danh = surrogate ID ổn định (UUID/`listing_id`)**, gán một lần, không bao giờ đổi.
- Loại hình, phân loại, địa chỉ, `du_an`, folder path đều là **metadata thay đổi được** gắn vào ID đó. **Không bao giờ** dùng địa chỉ hay folder path làm khóa — một lần sửa sẽ làm mồ côi record hoặc tạo trùng.
- Phân loại là **tạm thời** — dữ liệu tích lũy dần (§III), nên `loai_hinh`/group/`du_an`/địa chỉ đều có thể được sửa trong đời listing. Khi phân loại lại, `Store` **cập nhật ĐÚNG record theo ID (không tạo trùng)**: re-file ở SharePoint (lý tưởng chỉ sửa cột), re-index + **re-embed** vector, giữ **audit trail**. Có thể cần **đường merge** khi hai record tạm hóa ra cùng một BĐS. Engine đã hỗ trợ phân loại lại tại chỗ (`Property.update()` merge; `group` tính từ `loai_hinh`) — phần lưu bền phải làm theo, không nhân bản.

## Cây thư mục (§VII "5 lớp", đã tinh chỉnh)
- 5 lớp (Quận/Dự án/Đường/Số nhà/Pháp lý) là **taxonomy để con người duyệt**. **"Dự án" là TÙY CHỌN** — phần lớn là nhà lẻ lâu đời (nhà phố, nhà lẻ, đất) không thuộc dự án. Độ sâu thích ứng theo loại:
  - **Nhà lẻ** = `Quận / Đường / Số nhà / Pháp lý` (4 lớp). Không tạo folder placeholder rác.
  - **Theo dự án** = `Quận / Dự án / Loại căn / Block-Căn / Pháp lý`.
- **Số nhà ≈ BĐS/ID *phần lớn trường hợp*** — nhưng một số nhà có thể chứa nhiều căn, nên surrogate ID mới là sự thật.
- Folder cho con người; liên kết chuẩn (`du_an`, `unit_type_id`) là **cột**. Phân loại lại = sửa cột / chuyển folder, ID không đổi.

## Giai đoạn dự án + phân loại vai người rao (kiến thức ngành 2026-06-24, CHƯA build)
Hai trục mới cần thêm vào model:
- **Giai đoạn dự án** (thuộc tính Project, kế thừa xuống listing; từng căn có thể lệch): hoàn thiện pháp lý → mở bán → thi công → bàn giao nhà → **bàn giao sổ** → sinh sống ổn định. Giai đoạn quyết định: (a) pháp lý có gì — **sổ hồng chỉ từ "bàn giao sổ" trở đi; trước đó chỉ có HĐMB**; (b) sơ cấp (mở bán, từ chủ đầu tư) vs thứ cấp (bán lại); (c) bot nên hỏi gì.
- **Vai người rao — giàu hơn nhị phân CCRB/NMG (§V).** Năm vai thực tế, gom theo "sở hữu cái mình bán" vs "bán hộ": **chủ đầu tư** (sở hữu rổ hàng dự án, bán sơ cấp; CA PHÍ ĐẶC BIỆT — CĐT thường trả hoa hồng cho sàn/môi giới, không trả AOND 1%), **sàn giao dịch** (phân phối F1 cho CĐT — bán hộ, quy mô), **nhân viên môi giới** (NMG, bán hộ), **nhà đầu tư lướt sóng** (sở hữu HĐMB, bán thứ cấp qua sang nhượng HĐMB / bán chênh; bên sở hữu, ≈CCRB?), **chủ nhà** (sở hữu, thứ cấp, =CCRB). Vậy 1%/0.5% của §V là đơn giản hóa; map = bên sở hữu (chủ nhà, lướt sóng, [CĐT đặc biệt]) vs bên môi giới (sàn, môi giới). **MỞ: phí theo từng vai — CĐT/sàn nhiều khả năng có cơ chế riêng, lướt sóng 1%? — chờ user chốt trước khi build.**

Hệ quả hành vi: bot phải hỏi đúng theo giai đoạn+vai — lướt sóng trước bàn giao → hỏi HĐMB / sang nhượng / mức chênh, KHÔNG hỏi sổ hồng; chủ nhà ở "ổn định" → hỏi sổ hồng; chủ đầu tư "mở bán" → hỏi theo UnitType/mẫu nhà, không hỏi "đang ở hay trống". Không đòi sổ hồng ở giai đoạn chưa thể có sổ. Mở rộng `user_type` (`revenue.py`) và business model §V.

## Mô hình dữ liệu dự án giàu (CHƯA build ngoài tiện ích phẳng)
Một dự án mang nhiều hơn tiện ích. Cấu trúc đầy đủ khi mở rộng tầng Project:
- **Project**: tên + alias, vị trí, **tình trạng pháp lý** (pháp lý/quy hoạch cấp dự án), tiện ích (phần duy nhất đã build), **phân lô** (đất nền: lô + kích thước + sơ đồ phân lô), **mặt bằng** (floor plate / mặt bằng căn hộ), tài liệu/ảnh cấp dự án (sơ đồ tổng thể, bản đồ vị trí, brochure), và tập **UnitType**.
- **UnitType / mẫu nhà** (vd "Fusion Gen 5", "Type B 3PN 113m²"): tên/mã, **layout** (công năng từng tầng, diện tích, số phòng, hướng), thông số (mặt tiền/sâu, số tầng, thang máy), **ẢNH RIÊNG** (floor plan, render, brochure), giá tham khảo / pháp lý mẫu nếu có.
- **Unit** kế thừa Project + UnitType; chỉ thêm vị trí (block/tầng/căn), giá, tình trạng, ảnh thực tế.
Mỗi tầng có media/tài liệu riêng → lưu ở SharePoint (kho gốc); trường có cấu trúc → chỉ mục. Bộ đọc folder Drive đã build là nguồn nạp tự nhiên cho UnitType. `aioi/projects.py` `Project` hiện là bản PHẲNG (name/amenities/attributes) — cần lớn lên thành cấu trúc trên + thực thể con `UnitType`.

## Kế thừa 3 tầng: Project → UnitType → Unit
Thuộc tính sống một lần ở đúng tầng, các căn kế thừa (giải tại read = `Project ⊕ UnitType ⊕ Unit overrides`, unit thắng; **join, không copy**):
- **Project** — tiện ích (hồ bơi, gym), cổng, đường vào, vị trí khu. (Chung cư có hồ bơi ⇒ mọi căn có hồ bơi.) **Tầng này ĐÃ BUILD (in-memory + SQLite):** đăng ký dự án một lần (`register_project` / `register_project_from_text`), listing bắt `du_an`, khớp thì chèn dữ liệu đã-xác-thực vào prompt để bot nêu hồ bơi/cổng mà không hỏi/bịa. Đã persist qua `SqliteProjectStore`.
- **UnitType / mẫu nhà** — layout, diện tích, số phòng, floor plan, render, hoàn thiện chuẩn. Các căn cùng mẫu chia sẻ ~**99%** đặc điểm.
- **Unit** — vị trí cụ thể (block/tầng/căn), hướng, giá, tình trạng, **ảnh thực tế**, + ~1% override.
- **Vị trí KHÔNG kế thừa từ mẫu** (theo từng căn; chỉ chia sẻ ở cấp dự án). UnitType **tùy chọn** (nhà lẻ & căn cá biệt không có) và vừa là folder (`Loại căn/`, có `_Chung/` cho asset chung) vừa là cột (`unit_type_id`). Luôn cho **override cấp căn** (đã sửa/khác) — 99% chứ không 100%. Đây là nguồn xác thực sau "Botanic Tower Block A căn 9 → 3PN 113m²", an toàn hơn để model đoán.

**Nguồn nạp dữ liệu UnitType = bộ đọc media (ĐÃ build).** Pipeline đọc folder Google Drive đã build là nguồn nạp tự nhiên: người rao dán folder brochure mẫu nhà, vision đọc cả bộ một lần. Ví dụ thật (2026-06-24): folder 12 ảnh "Fusion Gen 5 / dự án Nyah Phú Định" → đọc ra spec đầy đủ (6 tầng, 4×11m, thang máy, công năng từng tầng). Brochure đó tả một **UnitType / mẫu nhà**, không phải một căn vật lý — nên khi xây Store, read kiểu này nạp vào **tầng UnitType**, mọi căn cùng mẫu kế thừa; chỉ vị trí/giá/tình trạng/ảnh-thực là của từng Unit. Luồng read→capture→store phải định tuyến "đây là mẫu/brochure" → UnitType, "đây là tin một căn" → Unit.

## File cho một BĐS (trong folder lá)
Folder lồng tốt ở đây (con người đọc được, **ổn định** — ảnh bếp không phân loại lại). **Sâu 2 lớp rồi dừng:**
```
<property>/  Pháp lý/ · Nội thất/ · Ngoại thất/ · Khu vực/ · Khác/
```
(Nội thất = bếp/phòng ngủ/khách/WC; Ngoại thất = mặt tiền/cổng/sân; Khu vực = đường vào/hẻm/hàng xóm/cổng dự án/đường lớn/view; Khác = video/3D/floor plan.) **Đừng tạo folder cho từng phòng** — chi tiết để trong tên file (`noi_that_phong_ngu_1.jpg`). Nếu app bên mua cần lọc ảnh theo phòng, đó là **tag per-ảnh** (`room: bep`, `view: interior`), không phải folder sâu hơn (một ảnh có thể vừa "bếp" vừa "nội thất"). Nguyên tắc: folder nông cho người, metadata/tag cho máy.

## Link ngoài người rao gửi (phía T•ai)
Luồng **fetch → show → confirm → snapshot** phía người rao (mẫu trung thực/xác nhận §II, tái dùng pipeline vision/OCR):
- Fetch link, mô tả/trích (ảnh → vision Opus 4.8; PDF → OCR; trang → text), nói với người rao "Em thấy [X], đúng không?", và chỉ **khi xác nhận** mới thành dữ liệu. Nguyên tắc trung thực: "hình như" khi đoán, không khẳng định.
- **Lưu snapshot nếu kích thước hợp lý** (cap cứng; stream + abort khi vượt) vào folder BĐS (`Khác/` hoặc `Pháp lý/`) kèm `{original_url, fetched_at, checksum, kind, status}`. Vượt cap → chỉ lưu tham chiếu.
- **Rào chắn (phần rủi ro — fetch URL người dùng):** chống SSRF (chỉ http/https, phân giải DNS và chặn IP nội bộ, re-check redirect, fetch từ worker sandbox/giới-hạn-egress), timeout + allowlist content-type + cap kích thước; thất bại lịch sự với link cần đăng nhập/JS/video. "Đáng tin" = người rao xác nhận + đối soát, không phải điểm tin tự động.

## Tìm kiếm phía người mua — bám chặt DB
Khi người mua (M•ai) tìm, AOND chỉ giới thiệu BĐS hợp từ DB của mình — không lướt Internet, không bịa:
1. Model **không có tool web** (`ClaudeProvider` hiện không có — giữ vậy).
2. **Tìm bằng code xác định** (SQL/lọc theo quận/loại hình/giá/diện tích/phòng/hướng), tùy chọn + **xếp hạng ngữ nghĩa** (embeddings) — không phải model. Phễu 5.000 → lọc SQL → ~50 → xếp hạng → top ~10–20 → chỉ nhúm đó vào prompt. Model không bao giờ đọc cả DB.
3. Prompt: chỉ giới thiệu trong danh sách; không có thì nói "chưa có căn phù hợp" và nới tiêu chí; không kiến thức ngoài, không bịa.
4. **Khóa mạnh nhất:** model trả **ID** (qua tool `search_listings` chỉ trả từ DB); **app render sự thật** (giá/địa chỉ/ảnh) từ DB theo ID và **kiểm mỗi ID nằm trong tập ứng viên**. Mọi con số khách thấy đều từ DB; model chỉ khớp + diễn đạt.
- **Chỉ link first-party:** M•ai **không bao giờ** gửi link ngoài cho người mua — chỉ link của AOND (trang aioinhadat.com, media tự host). Tham chiếu ngoài đã lưu chỉ dùng nội bộ.

## Tìm kiếm ngữ nghĩa (khi thêm)
Embeddings biến mô tả mỗi listing thành vector (một lần lúc lưu; re-embed khi đổi); câu hỏi → top-K gần nhất theo cosine. **Hybrid: lọc cứng SQL trước (ngân sách/quận — embeddings dở về số/ràng buộc), xếp hạng ngữ nghĩa trong số còn lại.** Embedding model tách khỏi chat model (Anthropic không có API embeddings — Voyage AI, hoặc model đa ngữ mã nguồn mở như BGE-M3 / multilingual-E5 chạy local, hợp lộ trình Blackwell). Ở ~5.000 listing, quét cosine brute-force trong app là mili-giây — chưa cần vector DB tới khi vài trăm nghìn+ (lúc đó pgvector/Qdrant/FAISS).

## Thứ tự build đề xuất
Interface `Store` + backend **SQLite** (PK surrogate-ID + cột lọc/index) → lưu listing từ T•ai → `search_listings` (lọc + xếp hạng, chưa vector) → thêm **embeddings** → cắm **SharePoint** làm kho gốc + đồng bộ.
