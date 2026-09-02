# Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)

A streaming LLM gateway that proxies a text-generation request and redacts
emails, SSNs, and credit card numbers **in real time** — correctly even when a
pattern is split across chunk boundaries, and without buffering the response.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn app:app --port 8000            # LLM_PROVIDER defaults to "mock"
```

```bash
curl -sN -X POST localhost:8000/v1/generate \
     -H 'content-type: application/json' \
     -d '{"prompt":"summarise the customer"}'
```

```
Sure — here is the customer summary you asked for.

The account owner is Jane Roe and her contact address is [REDACTED]. She last logged in on Tuesday.
Her social security number on file is [REDACTED], and the card ending in the visible digits is [REDACTED].
Her support ticket reference is 123456789 and the server she reported the issue from was 192.168.1.1 running v1.2.3 — neither of which is PII.
```

The mock upstream deliberately splits the email, the SSN, and the card across
chunk boundaries (see `DEFAULT_SCRIPT` in `providers.py`), and deliberately
includes near-miss text that must survive untouched.

To use the real Anthropic API instead:

```bash
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-sonnet-5   # optional
uvicorn app:app --port 8000
```

## Run the tests

```bash
pip install -r requirements.txt
pytest
```

163 tests, no network and no API key required.

## How the sliding buffer works

Per-chunk regex matching leaks: `"...john.doe@exam"` + `"ple.com"` matches
nothing in either chunk. Buffering the whole response fixes that and destroys
time-to-first-token. So the redactor holds back **only the suffix that could
still grow into a match**.

For each chunk:

1. `buffer += chunk`.
2. Compute `emit_limit`, the point up to which the text is *settled* — no future
   input can change how it redacts. It is the earliest of:
   - the start of the longest suffix that is a viable **prefix** of some pattern
     (`PARTIAL` regex): a trailing `"john.doe@exam"` pins `emit_limit` to where
     that fragment starts;
   - the start of any match still *open* at the buffer end (so a 16-digit card
     that a later chunk extends to 20 digits is never redacted prematurely);
   - with `len(buffer) - max_hold` as a floor, capping how much can ever be
     held.
3. Redact every match ending at or before `emit_limit`. Matching runs against
   the **whole** buffer, so `\b` and neighbouring context are evaluated
   correctly, but only settled matches are substituted.
4. Emit `buffer[:emit_limit]`; keep the rest.

`close()` sets `emit_limit = len(buffer)`: the tail is redacted and flushed. It
is never dropped, and never emitted raw — including when the upstream stream
died mid-PII, which the tests cover explicitly.

### Guarantees

- **Split width.** Any PII token up to `max_hold` characters (default **256**)
  is redacted no matter how it is split — including one character per chunk.
  Tests assert this at every split point of every secret, and character by
  character.
- **Memory.** `len(buffer) <= max_hold + len(largest chunk)`, independent of
  stream length. Asserted two ways: a buffer high-water-mark invariant over a
  1 MB stream (including an adversarial all-digit flood, which would grow an
  unbounded "might still be a card" tail under a naive implementation), and a
  `tracemalloc` measurement over a 3 MB stream.
- **TTFT.** The hold-back is *adaptive*, not a fixed window. Ordinary prose ends
  in characters that cannot begin any pattern, so `emit_limit == len(buffer)`
  and the first chunk flushes in full. A test asserts the first byte reaches the
  client after roughly one upstream chunk rather than after all twenty.

## Avoiding false positives

The failure mode that actually hurts a gateway is not a missed match — a human
reviews the transcript — but redacting the answer the user asked for. So:

- **SSN** requires the `3-2-4` grouping with a consistent explicit separator. A
  bare 9-digit run is a product code far more often than an SSN, so
  `123456789` is left alone.
- **Cards** are 13–19 digits with single optional space/hyphen separators, then
  a **Luhn** checksum. That is what stops a 16-digit order number being redacted.
  Dots are not accepted as separators, so `192.168.1.1` and `v1.2.3` cannot
  match.
- **Emails** require a dot-separated TLD of 2–63 letters, so `user@localhost`
  and `@mentions` do not match.

Thirteen near-miss strings (phone numbers, IPs, version strings, dates,
Luhn-invalid card-length numbers, bare domains) are asserted unchanged, both as
a single chunk *and* streamed one character at a time.

## Unicode

Redaction operates on `str`, and encoding to UTF-8 happens in the response
generator **after** redaction, on complete strings. The redactor therefore never
sees half a multi-byte sequence and a wire chunk boundary can never fall inside
a character. Tests stream emoji and CJK text one character at a time with PII
embedded.

## Mid-stream failures

Once the first byte is sent the status code is committed, so an upstream failure
cannot become a 502. The gateway flushes whatever is safely redacted, appends
one sanitized sentinel, and closes:

```
\n[gateway-error] upstream_stream_failed\n
```

A clean close, not a hang, carrying no provider detail. Tests plant a password
and an internal hostname in the upstream exception and assert neither reaches
the client. A provider that fails *before* any bytes are sent still gets a
proper `502` with a structured error body.

## Key design tradeoff

The interesting choice was **adaptive hold-back versus a fixed tail window**. A
fixed N-character window is three lines of code, but it delays the first token
until N characters have accumulated — on every request, forever, to guard
against PII that is usually not there. Computing the longest suffix that could
still *become* PII costs one extra anchored regex per chunk and lets ordinary
prose flush immediately while still guaranteeing the full split-width. The
`max_hold` cap stays as a floor purely so an adversarial input (a megabyte of
digits, which is an unbounded "might still be a card" prefix) cannot grow the
buffer without limit.

The second tradeoff is **Luhn validation on cards**: it makes redaction
*less* aggressive, which sounds wrong for a guardrail. But a gateway that
redacts every 16-digit number destroys order IDs, tracking numbers, and
timestamps, and users route around a guardrail they cannot trust. Luhn is the
structural test the card networks themselves define, so a genuine PAN passes by
construction and the false-positive rate on long digit runs drops by roughly an
order of magnitude.
