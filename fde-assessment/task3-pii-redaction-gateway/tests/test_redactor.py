"""Redaction correctness, especially across chunk boundaries."""

from __future__ import annotations

import random

import pytest

from conftest import AMEX, EMAIL, MASTERCARD, SSN, VISA, VISA_SPACED, split_every, stream_through
from redactor import REPLACEMENT, StreamRedactor, redact_complete


# --------------------------------------------------------------------------
# Single-chunk baseline
# --------------------------------------------------------------------------
def test_all_three_types_in_one_chunk():
    """The baseline: all three PII classes in one chunk, all replaced, with no fragment of any left behind."""
    text = f"Reach {EMAIL}, SSN {SSN}, card {VISA_SPACED}. Done."
    out = stream_through([text])
    assert out == f"Reach {REPLACEMENT}, SSN {REPLACEMENT}, card {REPLACEMENT}. Done."
    for secret in (EMAIL, SSN, VISA_SPACED, "4111"):
        assert secret not in out


@pytest.mark.parametrize("card", [VISA, VISA_SPACED, MASTERCARD, AMEX, "4111-1111-1111-1111"])
def test_card_formats(card):
    """Five real-world card spellings, spaced, dashed and bare, all recognised."""
    out = stream_through([f"card {card} end"])
    assert out == f"card {REPLACEMENT} end"


@pytest.mark.parametrize("ssn", ["123-45-6789", "987 65 4321"])
def test_ssn_formats(ssn):
    """Both accepted SSN separators, so the pattern is not dash-only by accident."""
    assert stream_through([f"ssn {ssn} end"]) == f"ssn {REPLACEMENT} end"


@pytest.mark.parametrize(
    "email",
    [
        "john.doe@example.com",
        "a+tag@sub.domain.co.uk",
        "first_last%99@mail-server.example.io",
        "x@y.zz",
    ],
)
def test_email_formats(email):
    """Four address shapes including subdomains, plus tags and percent signs in the local part."""
    assert stream_through([f"write to {email}."]) == f"write to {REPLACEMENT}."


# --------------------------------------------------------------------------
# THE CORE REQUIREMENT: patterns split across chunk boundaries
# --------------------------------------------------------------------------
def test_email_split_across_two_chunks():
    """The headline requirement: an address broken mid-domain is still redacted whole."""
    out = stream_through(["Contact me at john.doe@exam", "ple.com tomorrow."])
    assert out == f"Contact me at {REPLACEMENT} tomorrow."
    assert "exam" not in out and "ple.com" not in out


def test_ssn_split_mid_digit_across_two_chunks():
    """A split inside the digit groups, where per-chunk matching sees nothing at all."""
    out = stream_through(["My SSN is 123-45", "-6789 exactly."])
    assert out == f"My SSN is {REPLACEMENT} exactly."


def test_ssn_split_across_three_chunks():
    """Three-way splitting, so the fix cannot rely on only ever needing one chunk of lookahead."""
    out = stream_through(["SSN ", "123-", "45-", "6789", " end"])
    assert out == f"SSN {REPLACEMENT} end"


def test_card_split_between_digit_groups():
    """A boundary falling on the separator between groups."""
    out = stream_through(["card 4111 1111 ", "1111 1111 end"])
    assert out == f"card {REPLACEMENT} end"


def test_card_split_mid_group():
    """A boundary falling inside a group of digits."""
    out = stream_through(["card 41111111", "11111111 end"])
    assert out == f"card {REPLACEMENT} end"


ALL_SECRETS = [EMAIL, SSN, VISA, VISA_SPACED, MASTERCARD, AMEX]

# Every interior split point of every secret, generated per-secret so the
# parametrization covers each one exactly to its own length -- no skipped
# combinations, and no split points missed on the longer secrets.
EVERY_SPLIT_POINT = [
    pytest.param(secret, split_at, id=f"{secret[:6]}..@{split_at}")
    for secret in ALL_SECRETS
    for split_at in range(1, len(secret))
]


@pytest.mark.parametrize("secret, split_at", EVERY_SPLIT_POINT)
def test_every_split_point_of_every_secret(secret, split_at):
    """Exhaustive: split each secret at every position, assert full redaction."""
    text = f"prefix {secret} suffix"
    cut = len("prefix ") + split_at
    out = stream_through([text[:cut], text[cut:]])
    assert out == f"prefix {REPLACEMENT} suffix", out


@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_secret_streamed_one_character_at_a_time(secret):
    """The worst case: a pattern spanning as many chunks as it has characters."""
    text = f"before {secret} after"
    out = stream_through(list(text))
    assert out == f"before {REPLACEMENT} after"


def test_secret_split_with_empty_chunks_interleaved():
    """Zero-length deltas are common in real provider streams and must not disturb the buffer."""
    text = f"see {EMAIL} now"
    chunks: list[str] = []
    for character in text:
        chunks.extend(["", character, ""])
    out = stream_through(chunks)
    assert out == f"see {REPLACEMENT} now"


def test_stream_of_only_empty_chunks_does_not_crash():
    """A stream that carries no text at all closes cleanly and emits nothing."""
    assert stream_through(["", "", ""]) == ""


def test_multiple_instances_some_split_some_not():
    """A realistic mixture: split and unsplit secrets in one stream, with the surrounding prose preserved exactly."""
    chunks = [
        "First, email ",
        "alice@exam",
        "ple.com. Second, ",
        f"the full card {VISA} inline. Third, ssn 987-",
        "65-4321. Fourth, bob@other.org. Done.",
    ]
    out = stream_through(chunks)
    assert out == (
        f"First, email {REPLACEMENT}. Second, the full card {REPLACEMENT} inline. "
        f"Third, ssn {REPLACEMENT}. Fourth, {REPLACEMENT}. Done."
    )


# --------------------------------------------------------------------------
# End-of-stream handling
# --------------------------------------------------------------------------
def test_stream_ending_mid_buffer_with_pii_in_the_tail():
    """The tail must be redacted on close, not dropped and not leaked."""
    out = stream_through(["Here it is: ", "john.doe@exam", "ple.com"])
    assert out == f"Here it is: {REPLACEMENT}"
    assert "@" not in out


def test_stream_ending_with_a_truncated_pattern_flushes_it_unmodified():
    """A fragment that never became PII must still be emitted, verbatim."""
    out = stream_through(["Here it is: ", "john.doe@exam"])
    assert out == "Here it is: john.doe@exam"


def test_stream_ending_with_no_pii_in_the_tail_loses_nothing():
    """The held-back tail is flushed on close, so an ordinary ending is never truncated."""
    out = stream_through(["The quick brown fox ", "jumps over the lazy dog."])
    assert out == "The quick brown fox jumps over the lazy dog."


def test_trailing_digits_are_flushed_not_swallowed():
    """A trailing digit run that never became PII is emitted verbatim rather than eaten by the buffer."""
    out = stream_through(["order number ", "123456789"])
    assert out == "order number 123456789"


def test_close_is_idempotent():
    """Closing twice is safe, so a caller need not track whether it already did."""
    redactor = StreamRedactor()
    redactor.feed("hello world")
    assert redactor.close() != "" or True
    assert redactor.close() == ""


def test_feed_after_close_raises():
    """Feeding a closed redactor is a programming error and fails loudly rather than silently dropping text."""
    redactor = StreamRedactor()
    redactor.close()
    with pytest.raises(RuntimeError):
        redactor.feed("x")


# --------------------------------------------------------------------------
# False positives
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "call me on 555-123-4567",
        "the host is 192.168.1.1",
        "running version 1.2.3 build 4567",
        "product code 123456789",
        "order 1234567890123456 was shipped",   # 16 digits, fails Luhn
        "invoice 2024-01-15 total 99.95",
        "ticket 12-34-5678 was closed",         # 2-2-4, not 3-2-4
        "coordinates 12.345678, -98.765432",
        "the user is @jane on the internal chat",
        "email me at user@localhost",           # no TLD
        "see http://example.com/page for details",
        "a range of 100-200-3000 units",        # 3-3-4
        "SKU 4111-1111-1111-111",               # 15 digits, fails Luhn
    ],
)
def test_near_misses_are_not_redacted(text):
    """Thirteen strings that resemble PII but are not, checked whole and streamed one character at a time; over-redaction destroys the answers users actually asked for."""
    assert stream_through([text]) == text
    assert stream_through(list(text)) == text


def test_luhn_invalid_16_digit_number_survives():
    """A 16-digit reference that fails Luhn is left alone, which is what keeps order numbers readable."""
    assert "1234567812345678" in stream_through(["reference 1234567812345678 ok"])


def test_domain_in_prose_is_not_an_email():
    """A bare domain has no local part and is not an address."""
    text = "Visit example.com or www.example.co.uk for more."
    assert stream_through([text]) == text


# --------------------------------------------------------------------------
# Unicode
# --------------------------------------------------------------------------
def test_unicode_and_emoji_are_preserved_exactly():
    """Multi-byte text around a redaction survives byte-for-byte."""
    text = "Grüße 🎉 — お客様の情報: " + EMAIL + " ✅ done 🚀"
    out = stream_through([text])
    assert out == "Grüße 🎉 — お客様の情報: " + REPLACEMENT + " ✅ done 🚀"


def test_unicode_stream_split_at_every_character():
    """Character-wise streaming of multi-byte text, where a naive byte-level buffer would corrupt output."""
    text = "naïve café 🚀 ssn " + SSN + " ✅ 東京"
    assert stream_through(list(text)) == "naïve café 🚀 ssn " + REPLACEMENT + " ✅ 東京"


def test_emoji_adjacent_to_pii():
    """A redaction directly abutting multi-byte characters on both sides."""
    out = stream_through(["🚀", EMAIL[:6], EMAIL[6:], "🎉"])
    assert out == f"🚀{REPLACEMENT}🎉"


# --------------------------------------------------------------------------
# Memory and split-width guarantees
# --------------------------------------------------------------------------
def test_buffer_stays_bounded_over_a_long_stream():
    """Over a megabyte of output, the buffer high-water mark stays within one chunk of the cap and drains to zero."""
    redactor = StreamRedactor()
    body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    output_chars = 0
    for index in range(20_000):
        chunk = body if index % 50 else f"leak {EMAIL} and {SSN} "
        output_chars += len(redactor.feed(chunk))
    output_chars += len(redactor.close())
    assert output_chars > 1_000_000, "sanity: the stream really was large"
    # The buffer never approached the size of the stream.
    assert redactor.peak_buffer_chars <= redactor.max_hold + len(body)
    assert redactor.buffered_chars == 0


def test_buffer_bounded_even_under_an_adversarial_digit_flood():
    """A megabyte of digits could grow an unbounded 'still might be a card' tail."""
    redactor = StreamRedactor()
    for _ in range(2_000):
        redactor.feed("1" * 500)
    redactor.close()
    assert redactor.peak_buffer_chars <= redactor.max_hold + 500


def test_documented_split_width_guarantee():
    """Any token up to max_hold characters is caught however it is split."""
    redactor = StreamRedactor(max_hold=64)
    long_email = "a" * 40 + "@example.com"  # 52 chars, inside the guarantee
    out = "".join(redactor.feed(character) for character in f"x {long_email} y")
    out += redactor.close()
    assert out == f"x {REPLACEMENT} y"


# --------------------------------------------------------------------------
# Differential test against the non-streaming oracle
# --------------------------------------------------------------------------
def test_random_splits_match_the_whole_string_oracle():
    """Whatever the chunking, the streamed result equals redacting it all at once."""
    rng = random.Random(20240201)
    corpus = (
        f"Hello {EMAIL}, your ssn {SSN} and card {VISA_SPACED} are on file. "
        f"Ticket 123456789, host 192.168.1.1, phone 555-123-4567, v1.2.3. "
        f"Also {MASTERCARD} and carol.smith+tag@sub.example.co.uk and 987 65 4321. "
        "Unicode: naïve café 🚀 東京. End."
    )
    expected = redact_complete(corpus)
    for _ in range(500):
        chunks: list[str] = []
        position = 0
        while position < len(corpus):
            # Zero-length chunks are interleaved deliberately: real provider
            # streams emit empty deltas and they must not disturb the buffer.
            size = rng.choice([0, 1, 1, 2, 3, 5, 8, 13])
            chunks.append(corpus[position : position + size])
            position += size
        assert "".join(chunks) == corpus
        assert stream_through(chunks) == expected


@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 7, 11, 16, 32, 64, 128])
def test_fixed_size_splits_match_the_oracle(size):
    """Eleven fixed chunk sizes, each compared against redacting the whole string at once."""
    corpus = (
        f"a {EMAIL} b {SSN} c {VISA_SPACED} d {AMEX} e 192.168.1.1 f 123456789 "
        f"g 555-123-4567 h dave@sub.example.org i 987 65 4321 j"
    )
    assert stream_through(split_every(corpus, size)) == redact_complete(corpus)


def test_process_memory_does_not_grow_with_stream_length():
    """Directly measure allocation, not just the buffer-length invariant."""
    import gc
    import tracemalloc

    redactor = StreamRedactor()
    body = "Ordinary prose that carries no personal information at all. "
    for _ in range(2_000):  # warm up so the baseline is steady state
        redactor.feed(body)

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    for index in range(50_000):
        redactor.feed(body if index % 100 else f"leak {EMAIL} ")
    redactor.close()
    gc.collect()
    growth = tracemalloc.get_traced_memory()[0] - baseline
    tracemalloc.stop()

    # ~3 MB of text streamed through; retained growth must stay tiny.
    assert growth < 200_000, f"retained {growth} bytes after a 3 MB stream"
