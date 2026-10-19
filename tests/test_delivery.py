from aioi.delivery import chunk_reply, typing_delay


def test_short_reply_stays_one_bubble():
    assert chunk_reply("Dạ em ghi nhận rồi ạ!") == ["Dạ em ghi nhận rồi ạ!"]


def test_empty_reply_yields_nothing():
    assert chunk_reply("   ") == []


def test_blank_line_forces_new_bubble():
    reply = "Em sẽ rao như vầy nhé:\n\nAnh thấy hấp dẫn chưa ạ?"
    assert chunk_reply(reply) == ["Em sẽ rao như vầy nhé:", "Anh thấy hấp dẫn chưa ạ?"]


def test_never_truncates_content():
    reply = "🏠 NHÀ PHỐ Q5\n- 4 tầng, 4PN\n- Sổ hồng đầy đủ\n\nAnh thấy hấp dẫn chưa ạ?"
    bubbles = chunk_reply(reply)
    # Every non-empty source line survives somewhere in the bubbles.
    joined = "\n".join(bubbles)
    for line in ("🏠 NHÀ PHỐ Q5", "4 tầng, 4PN", "Sổ hồng đầy đủ", "Anh thấy hấp dẫn chưa ạ?"):
        assert line in joined


def test_formatted_ad_stays_one_bubble_with_blank_lines():
    # A listing ad (bullets + '---' + blank lines) must be delivered WHOLE so its spacing survives
    # — splitting it into bubbles dropped the blank lines and made it a dense unreadable block.
    ad = ("🔥 TIÊU ĐỀ HẤP DẪN MÃ LÒ\n\n"
          "Căn nhà 2 mặt tiền ngay Mã Lò, kết nối thuận tiện, công năng linh hoạt.\n\n"
          "- 2 mặt tiền, ngang 12m\n- 6 tầng thang máy, 132m²\n- sổ hồng chính chủ\n- 19 tỷ (TL)")
    assert chunk_reply(ad) == [ad]            # one bubble, blank lines + bullets intact


def test_short_lines_group_under_max_chars():
    reply = "- a\n- b\n- c"
    # All three short bullets fit well under the default max -> one bubble.
    assert chunk_reply(reply) == ["- a\n- b\n- c"]


def test_long_single_line_splits_on_sentences():
    line = "Câu một dài ơi là dài. " * 10  # well over the default 160 chars, no newlines
    bubbles = chunk_reply(line.strip())
    assert len(bubbles) > 1
    assert not any(b.endswith(("…", "...")) for b in bubbles)  # no mid-word cuts


def test_typing_delay_is_clamped_and_scales():
    assert typing_delay("x") == 0.3                      # min floor
    assert typing_delay("x" * 1000) == 2.0               # max ceiling
    mid = typing_delay("x" * 25)                          # ~1s at 25 cps
    assert 0.3 <= mid <= 2.0
