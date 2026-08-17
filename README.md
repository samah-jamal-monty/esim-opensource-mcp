# eSIM MCP Server

The action layer between a chatbot (Claude or ChatGPT) and the existing eSIM platform:

```
Claude / ChatGPT   ← conversation and reasoning happen here
      ↓ MCP
   esim-mcp        ← this project: safe, typed actions
      ↓ HTTPS
eSIM backend       ← existing service, unchanged
```

There is exactly one new service. This project is **not** a chatbot, not a second API, not
an agent backend and not a UI — Claude or ChatGPT holds the conversation and decides which
tool to call; this server performs the action and returns short structured facts that the
model phrases naturally.

The user experience it is built for:

```text
User:      I need an eSIM for France.
Assistant: Here are the plans available for France:
           1. 1 GB / 7 days — 5.00 USD
           2. 5 GB / 30 days — 12.50 USD
           3. 10 GB / 30 days — 20.00 USD
           Want details on any of them?
User:      Tell me more about the second one.
Assistant: That's 5 GB, valid 30 days from first connection, 12.50 USD…
```

Browsing needs no login. Signing in is asked for only when something needs the account:

```text
User:      Log me in.
Assistant: What email or phone should I use?
User:      user@example.com
Assistant: I've sent a code to u***@example.com. What's the six-digit code?
User:      482915
Assistant: You're logged in.
```

Buying is a two-step conversation, on purpose — the amount is quoted, said out loud, and
agreed to before anything is charged:

```text
User:      I'll take the second one.
Assistant: That's 12.50 USD from your wallet balance. Shall I buy it?
User:      Yes, buy it.
Assistant: Bought — France 5 GB / 30 days for 12.50 USD, paid from your wallet.
```

The assistant is never told to "call a tool"; it decides from the server instructions and
the tool descriptions shipped by this server. Preparing a purchase never charges; only
`confirm_purchase` does, and only after an explicit yes — see scope below.

It is a standalone HTTP client of the eSIM platform: it never talks to Supabase, Stripe,
Firebase or an eSIM hub directly, and holds no credentials for them. Built on the official
Python MCP SDK (`mcp` 2.x), `httpx.AsyncClient`, Pydantic v2 and `pydantic-settings`.

---

## Scope

Implemented:

* **Phase 1** — production-ready server foundation (settings, logging/redaction, error
  model, HTTP client, Docker image) and multi-user OTP authentication with isolated
  server-side sessions;
* **Phase 2** — read-only catalogue and bundle discovery: countries, regions, the home
  catalogue, and country / region / cruise / global bundles, plus bundle details, with
  client-side filtering, sorting and result limiting. **Browsing requires no login.**
* **Phase 3** — safe purchase *preparation*: an MCP-local quote for a plan a signed-in user
  picked, priced from a fresh backend read, with a wallet-balance snapshot for wallet
  quotes. A quote reserves nothing and has no counterpart in the backend.
* **Phase 4** — purchase **execution**, wallet only: `confirm_purchase` turns a prepared
  quote into a real order over the platform's idempotent MCP purchase route. One
  idempotency key per quote, a repeated confirmation replays the first purchase instead of
  making a second, and an unclear outcome is never reported as either success or failure.
* **Phase 5B** — **card checkout**: `create_card_checkout` asks the platform to open its
  Stripe-hosted payment page for a prepared *Card* quote and returns the link;
  `check_card_payment_status` reads what happened to that payment. One idempotency key per
  quote, one page per quote, and **the card never touches this server** — it is entered on
  Stripe's own hosted page, in the user's browser.
* **Phase 6A** — **live eSIM usage**: `get_esim_consumption` reports the platform's own
  reading for one eSIM the signed-in user owns — total, used, remaining, plan status and
  expiry. Every figure is copied from the platform; none is derived from a date, a price or
  a catalogue allowance, and an empty answer is reported as "nothing yet", never as zero.
* **Phase 6B** — **eSIM top-up**: `get_esim_topup_options` lists the platform's own
  compatibility list for one owned eSIM and `prepare_esim_topup` prices one of them. Both are
  free. `confirm_esim_topup` performs the top-up, but **only in QA** and behind a flag that
  defaults to off — see *`confirm_esim_topup` is QA-only* below.
* **Phase 6C** — **wallet top-up**: `prepare_wallet_topup` checks an amount against the
  platform's own minimum and rolling limits, `create_wallet_topup_checkout` asks the platform
  to open its Stripe-hosted page and returns the link, and `get_wallet_topup_status` reads
  what happened. Duplicate protection is the platform's own durable pending-order reuse, and
  **the card never touches this server**.

**Not implemented, by design:** taking card details here (a Stripe integration of any kind
beyond passing on the platform's own link), performing an eSIM top-up *outside QA*, promotions,
vouchers, DCB, refunds or order cancellation, eSIM provisioning, activation, callbacks and
the backend's translation/maintenance routes.

### `confirm_esim_topup` is QA-only, and not idempotent

Set `MCP_ESIM_TOP_UP_ENABLED=true` and one more tool appears: `confirm_esim_topup`, which
performs a **real** top-up over the platform's *legacy* `POST /user/bundle/assign-top-up` —
the same route the portal uses. **The flag defaults to false and must stay false in
production.**

The reason is idempotency. A top-up that runs twice costs the user twice and puts data on a
SIM they did not ask for, and the only honest protection is a *durable* record that lets a
retry be recognised as the same request. The platform has none: that route accepts no
idempotency key, and the `Topup` row it writes to `user_order` carries neither an ICCID nor a
request key, so two identical requests are indistinguishable from one sent twice. It also
debits the wallet *before* provisioning and swallows a failed provisioning.

**So nothing here tries to make a second request safe. It makes a second request impossible
instead** — a different and weaker promise:

* one quote, one attempt, whatever the outcome. The attempt is counted *before* the request
  leaves, under a per-quote lock, so a request that dies in flight still locks the quote;
* an unknown outcome is terminal. Everywhere else an unresolved write may be presented again
  with the key it already used; here there is no key, so asking again is a *second top-up*;
* everything is revalidated against the platform immediately before sending — ownership,
  compatibility, availability, price, currency, balance, expiry, payment method;
* the caller must echo the exact amount from the quote, so a confirmation can only come from
  something that actually read it back;
* **the lock lives in this process.** A restart between sending and recording loses it. That
  is exactly why this does not go to production.

Three independent gates rest on the flag, and all three are asserted by tests: the tool is
not registered, the service refuses, and `enforce_route_is_permitted` refuses the path.
`Settings` **refuses to construct** when the flag is true and the environment is production,
so a production process with it set will not start.

Production blocker: durable idempotency (a request-key column or table on `user_order`, which
is a schema change) plus wallet compensation for a debit whose provisioning failed.

### Preparation and purchase are two different tools, deliberately

`prepare_purchase` **never charges**. It reads the plan and the balance and writes a record
in this process; it creates no backend order, starts no payment, debits no wallet and
reserves nothing. Every preparation result states `order_created: false` and
`charged: false` in every branch.

`confirm_purchase` **may charge**. It is the only tool in this codebase that can spend a
user's money: it creates an order at the eSIM platform and debits the wallet balance, and
nothing here can undo or refund that. It takes a single argument — the reference of a quote
this same caller prepared — so the plan, the price, the currency and the payment method all
come from the stored quote and cannot be supplied, altered or hallucinated at purchase time.

The split exists so that quoting an amount and agreeing to pay it are separate acts, and so
the model cannot slide from one to the other: a user asking what a plan costs never reaches
a tool that can charge them.

### The two routes that can lead to a charge

`POST /api/v1/mcp/user/bundle/assign` is the platform's MCP-only purchase endpoint. It
requires an `Idempotency-Key`, is wallet-only, and replays its stored answer rather than
executing a second purchase when the same key returns. **Calling it spends the user's
money.**

`POST /api/v1/mcp/user/bundle/card/checkout` opens the platform's hosted card payment page
and returns a link. It also requires an `Idempotency-Key` and also replays — but it **moves
nothing**: the user pays on Stripe's page or they do not, and this server never learns a card
number either way. `GET /api/v1/mcp/user/bundle/card/status/{payment_reference}` is the read
that says what happened.

`POST /api/v1/mcp/wallet/top-up/checkout` opens the platform's hosted page for a **wallet
top-up** and returns a link. It moves nothing either: the wallet is credited only after the
user pays, and only by the platform's own signature-verified Stripe webhook. Duplicate
protection lives at the platform, which reuses the caller's own pending `user_order` row and
keys the Stripe call on it — durable across a restart of this process, which is why this
server mints no key of its own for that route.

Those three `POST`s are the **only** mutating routes this server may call, allowlisted by
exact method and path in `PERMITTED_MUTATION_ROUTES` (`src/esim_mcp/client/base.py`). Reads
whose paths end in caller-supplied references cannot be matched exactly; their prefixes are
allowlisted in `PERMITTED_REFERENCE_READ_ROUTES` together with the exact number of segments
allowed after them, and every segment must be one plain opaque token — a path that starts
with a prefix and fails that check is refused outright rather than falling through to the
marker scan. That covers the two payment-status reads, `GET /user/consumption/{iccid}` and
`GET /user/related-topup/{bundle_code}/{iccid}`. One fixed read,
`GET /mcp/wallet/top-up/options`, contains a forbidden marker in its own path and is named
back in by exact match in `PERMITTED_EXACT_READ_ROUTES`.

The whole `bundle/card` family is a forbidden marker, exactly like `bundle/assign`, with the
two members above allowlisted back. A `card/capture`, a `card/refund` or a future
`card/confirm` route is therefore unreachable by construction rather than by omission. The
same treatment now covers `top-up` and `topup`: the whole family is banned and the four
routes named above are allowlisted back, so `/mcp/user/bundle/topup`, a future
`/wallet/credit` or any other top-up route is unreachable simply by not spelling a banned
word.

The *legacy* `POST /api/v1/user/bundle/assign` stays forbidden and always will be: it has
no idempotency key and no duplicate-order protection, so a retried call there can buy a plan
twice. That route — and its neighbours (`assign-top-up`, `verify_order_otp`, `wallet/top-up`,
vouchers, promotions, callbacks, the unauthenticated `wallet/user_wallet_by_id/{id}`) — is
refused by `BackendApiClient` itself, before any I/O, for **any** HTTP method. The allowlist
is checked before the marker list and compares whole paths, so the legacy route cannot slip
through on the strength of being a substring of the permitted one. See
`FORBIDDEN_PATH_MARKERS` and the proofs in `tests/test_no_backend_mutation.py`.

---

## Architecture

```
src/esim_mcp/
├── server.py            # MCPServer wiring, lifespan, transports, entry point
├── settings.py          # typed configuration, production fail-fast validation
├── errors.py            # typed errors with MCP-safe messages
├── logging_config.py    # JSON logging, correlation id, mandatory redaction filter
├── client/
│   ├── base.py          # pooled httpx client, envelope parsing, route guard + allowlist
│   ├── auth.py          # one method per /api/v1/auth route
│   ├── card.py          # the hosted card checkout + its status read, and nothing else
│   ├── catalog.py       # one method per read-only /home and /bundles route
│   ├── purchase.py      # the single idempotent wallet purchase, and nothing else
│   └── wallet.py        # the single authenticated wallet read, and nothing else
├── models/
│   ├── common.py        # backend response envelope
│   ├── auth.py          # login / verify / auth-response models, JWT exp reader
│   ├── card.py          # checkout + payment-status payloads; closed field set, link guard
│   ├── catalog.py       # Country, Region, Bundle, HomeCatalog + defensive parsers
│   ├── purchase.py      # the platform's purchase result, parsed defensively
│   └── wallet.py        # UserWallet; backend floats → exact Decimal via str()
├── catalog/
│   ├── resolution.py    # name/ISO → country tag GUID, name/code → region code
│   ├── selection.py     # filters, sort orders, result limits
│   └── summaries.py     # the small conversational shapes tools return
├── purchase/
│   ├── models.py        # PurchaseQuote and its typed parts; no secret is storable
│   ├── store.py         # PurchaseQuoteStore abstraction + in-memory implementation
│   ├── service.py       # quote lifecycle: create / read / cancel / consume, supersede
│   ├── execution.py     # one idempotency key per quote, attempt limit, replayable outcome
│   ├── card.py          # one key and one payment page per quote, bounded status checks
│   └── validation.py    # quote ids, lifecycle gates, all Decimal money arithmetic
├── session/
│   ├── identity.py      # ClientIdentityProvider + HMAC device-id derivation
│   ├── models.py        # UserSession, LoginChallenge
│   ├── store.py         # SessionStore abstraction + in-memory implementation
│   └── manager.py       # session lifecycle, token refresh, 401 replay
├── safety/redaction.py  # masking (for output) and redaction (for logs)
└── tools/
    ├── guard.py         # correlation id + "only typed errors escape" boundary
    ├── authentication.py     # the six authentication tools + AuthenticationService
    ├── catalog.py            # the seven catalogue tools + CatalogService
    ├── purchase_preparation.py  # the three preparation tools + PurchasePreparationService
    ├── purchase_execution.py    # confirm_purchase + PurchaseConfirmationService
    └── card_checkout.py         # the two card tools + CardPaymentService
```

Layering rule: **tools never touch HTTP or tokens.** Authentication tools call
`AuthenticationService`, which uses `SessionManager` (state) and `AuthApiClient`
(transport). Tokens exist only inside the session layer and the HTTP header builder.

Catalogue layering is the same shape with one less layer, because there is no state to
keep: `CatalogService` → resolvers → `CatalogApiClient` → the shared `BackendApiClient`.
It holds **no session and no token at all** — it takes the caller's verified identity only
to derive the `X-Device-Id` the backend requires.

Purchase preparation splits along the same seam, with the domain half kept deliberately
inert: `PurchasePreparationService` (fetches authoritative facts, shapes results) →
`PurchaseQuoteService` (the rules) → `PurchaseQuoteStore` (storage). The whole
`esim_mcp/purchase/` package performs **no I/O of any kind** — it imports no HTTP client and
holds no reference to one, which is asserted by a test rather than left to review.

Purchase *execution* keeps that seam even though it is the part that spends money:
`PurchaseConfirmationService` (the only caller of `PurchaseApiClient`) →
`PurchaseExecutionService` (mints and remembers the key, decides whether a purchase may be
sent at all) → `PurchaseExecutionStore`. The middle layer still cannot reach the network, so
the decision "may this be sent?" is made by code that has no way to send it.

Card checkout repeats that shape exactly: `CardPaymentService` (the only caller of
`CardCheckoutApiClient`) → `CardCheckoutService` (mints and remembers the key, decides
whether a page may be opened and whether a payment may be checked again) →
`CardCheckoutStore`. Everything in `esim_mcp/purchase/card.py` is inert domain logic like the
rest of that package, so "may a second payment page exist?" is answered by code that could
not create one.

```
find_bundles_by_country("France")
  → CountryResolver: GET /bundles/countries, exact match → tag GUID   (never guessed)
  → CatalogApiClient: GET /bundles/by-country?country_codes=<guid>
  → filter (client-side) → sort → limit → small summaries + counts + price note
```

Request flow for an authenticated call:

```
MCP tool → verified client identity → session key → SessionManager
        → (refresh if inside the expiry window) → AuthApiClient
        → httpx (Authorization header set here only) → backend envelope
        → data → masked tool result
```

---

## MCP tools

| Tool | Purpose | Backend route |
| --- | --- | --- |
| `request_login_otp` | Send a one-time code to an email or phone | `POST /api/v1/auth/login` |
| `resend_login_otp` | Resend the code for a pending challenge | `POST /api/v1/auth/resend-otp` |
| `verify_login_otp` | Exchange the six-digit code for a server-side session | `POST /api/v1/auth/verify_otp` |
| `get_login_status` | Report session state; no backend call | – |
| `get_user_profile` | Masked profile and wallet balance | `GET /api/v1/auth/user-info` |
| `logout` | End this client's session | `POST /api/v1/auth/logout` |

Token refresh (`POST /api/v1/auth/refresh-token`) is **not** a tool. It happens inside
`SessionManager`, proactively before expiry and once reactively on a `401`.

### Catalogue tools (Phase 2 — read-only, no login required)

| Tool | Purpose | Backend route |
| --- | --- | --- |
| `list_countries` | List destinations, or resolve the one the user named | `GET /api/v1/bundles/countries` |
| `list_regions` | List regions, or resolve the one the user named | `GET /api/v1/bundles/region` |
| `browse_home_catalog` | Overview: counts plus cruise and global previews | `GET /api/v1/home/` |
| `find_bundles_by_country` | Plans for one country | `GET /api/v1/bundles/countries` + `GET /api/v1/bundles/by-country` |
| `find_bundles_by_region` | Plans for one region | `GET /api/v1/bundles/region` + `GET /api/v1/bundles/by-region/{code}` |
| `list_cruise_bundles` | Plans sold for cruise ships | `GET /api/v1/home/cruise` |
| `get_bundle_details` | Full detail for one plan | `GET /api/v1/bundles/{bundle_code}` |

`GET /api/v1/home/land` is wrapped by `CatalogApiClient.get_land_catalog` but is not
exposed as a tool: `browse_home_catalog` already covers the same ground for a conversation.

Deliberately not used: `/bundles/translate_*` (maintenance), every `/callback/*` route, and
anything that orders, pays, provisions or mutates a wallet.

### Purchase-preparation tools (Phase 3 — sign-in required, nothing is bought)

| Tool | Purpose | Backend route |
| --- | --- | --- |
| `prepare_purchase` | Quote a plan the user picked, for Wallet or Card | `GET /api/v1/bundles/{bundle_code}` + `GET /api/v1/wallet/user_wallet_by_user` |
| `get_prepared_purchase` | Read one of this caller's own quotes back | – (local only) |
| `cancel_prepared_purchase` | Discard one of this caller's own quotes | – (local only) |

`prepare_purchase` accepts **no** price, balance, tax, discount, card, token or identity
argument: every priced value is re-read from the platform, so a model cannot put an invented
figure into a quote. Wallet arithmetic is `Decimal` throughout and monetary values cross the
wire as strings.

Quotes are owned by the *verified MCP client identity plus the authenticated eSIM user*, are
looked up only within that owner (a foreign quote is invisible, not refused), expire after
`ESIM_MCP_PURCHASE_QUOTE_TTL_SECONDS`, are capped at `ESIM_MCP_MAX_ACTIVE_QUOTES_PER_USER`,
and are cancelled whenever their session ends. Re-preparing the same plan and payment method
**supersedes** the older quote rather than returning it, so a quoted price is never stale.

`InMemoryPurchaseQuoteStore` keeps quotes in the process heap: **they are lost on restart and
are not shared between replicas.** That is expected — a quote holds no money, no reservation
and no backend order, so losing one costs a re-prepare and nothing else. `PurchaseQuoteStore`
is the seam for an encrypted Redis implementation later, and the MCP tools would not change.

### Purchase execution (Phase 4 — sign-in required, **this one charges**)

| Tool | Purpose | Backend route |
| --- | --- | --- |
| `confirm_purchase` | Buy a prepared plan from the user's wallet | `POST /api/v1/mcp/user/bundle/assign` |

**Contract**

```
confirm_purchase(quote_reference: str) -> purchase result
```

One argument, and it must be the `quote_id` from this caller's own `prepare_purchase`
result. There is no price, amount, balance, bundle, payment-method, currency, idempotency-key
or identity argument — every one of those comes from the stored quote or from the session, so
a hallucinated figure has no way into the request.

**Request mapping** (built from the stored quote only):

| Backend field | Source |
| --- | --- |
| `bundle_code` | `quote.bundle.bundle_code`, captured from `GET /bundles/{code}` at preparation |
| `payment_type` | always `"Wallet"` — the only value this endpoint and this phase accept |
| `related_search` | `quote.search_context`, resolved against the platform's own country/region lists at preparation: `{"region": {"iso_code", "region_name"}}` or `{"countries": [{"iso3_code", "country_name"}]}`, omitted when the quote recorded no usable context |
| `quote_reference` | `quote.quote_id` (opaque correlation reference; the platform never trusts it for pricing or idempotency) |

Headers: `Authorization` (from the session, refreshed *before* the call), `X-Device-Id`,
`Accept-Language` (`quote.locale`), `X-Currency` (`quote.price.currency`, sent explicitly so
the platform refuses a purchase it would settle in a different currency) and
`Idempotency-Key`.

**Idempotency and repetition**

* One cryptographically random key (`secrets.token_urlsafe(48)`) is minted **per quote**, on
  first confirmation, and reused for every later attempt on that quote.
* A new key is produced only by a new quote. A timeout, a dropped connection or an
  "in progress" answer keeps the existing key — the recovery is to ask the platform again
  with it, never to start a fresh purchase.
* A terminal outcome is stored and **replayed**: confirming the same quote twice returns the
  first purchase's result with `replayed: true` and sends nothing.
* Attempts are capped (`MAX_EXECUTION_ATTEMPTS = 3`) so an unresolved purchase cannot become
  an unattended retry loop; past the cap the tool refuses and points at support.
* The quote is marked `consumed` **only** after the platform confirms a completed purchase.
* Purchases for one quote are serialized by a lock held across the backend call, so two
  concurrent confirmations cannot both reach the platform.

**Outcome mapping**

| Backend | MCP error / result | Recorded as | Retry with the same key? |
| --- | --- | --- | --- |
| `200` + `status: COMPLETED` | success result (`order_created: true`, `charged: true`) | succeeded | replayed, never re-sent |
| `400/422` business refusal | `insufficient_wallet_balance`, `bundle_unavailable`, `purchase_currency_mismatch`, `unsupported_quote_payment_method`, else `purchase_rejected` | failed (nothing charged) | replayed, never re-sent |
| `409` key conflict | `purchase_idempotency_conflict` | failed (nothing charged) | replayed, never re-sent |
| `409` still processing | `purchase_in_progress` | unresolved | yes — same key |
| `424` manual intervention | `purchase_needs_support` (carries `order_id`) | escalated | **never** |
| `401/403` | `authentication_required` | not recorded | yes — same key |
| `404` | `purchase_endpoint_unavailable` | not recorded | yes — same key |
| `429` | `rate_limited` | not recorded | yes — same key |
| `503` | `purchase_unavailable` | not recorded | yes — same key |
| timeout / lost connection / unreadable `2xx` / `5xx` | `purchase_outcome_unknown` | unresolved | yes — same key |

The two ambiguous outcomes (`purchase_needs_support`, `purchase_outcome_unknown`) carry
instructions never to claim success *or* failure, never to prepare another quote for the
same plan, and never to re-key. Nothing else in the codebase sets `charged: true`.

Example result (nothing sensitive is ever returned — no token, key, key fingerprint,
correlation id, developer message, activation code or ICCID):

```json
{
  "status": "purchased",
  "quote_reference": "…",
  "order_created": true,
  "charged": true,
  "payment_method": "Wallet",
  "order_id": "…",
  "order_status": "SUCCESS",
  "payment_status": "COMPLETED",
  "provisioning_status": "COMPLETED",
  "next_state": "GET_ESIM_BY_ORDER",
  "bundle": { "bundle_code": "…", "name": "France 5GB / 30 Days", "data": "5.0 GB", "validity": "30 Day" },
  "pricing": { "quoted_amount": "8.06", "currency": "USD" },
  "replayed": false,
  "message": "The order was created and the wallet was charged."
}
```

`InMemoryPurchaseExecutionStore` holds execution records — including the idempotency key, as
a `SecretStr` — in the process heap, and they are dropped whenever their session ends. A
restart between sending a purchase and recording its answer loses this server's memory of the
key; the *platform's* record survives, so the purchase is still protected from duplication
there, but this server can no longer replay the result. `PurchaseExecutionStore` is the same
Redis seam as `PurchaseQuoteStore`.

### Card checkout (Phase 5B — sign-in required, **this one never sees a card**)

| Tool | Purpose | Backend route |
| --- | --- | --- |
| `create_card_checkout` | Open the platform's Stripe-hosted payment page for a prepared Card quote | `POST /api/v1/mcp/user/bundle/card/checkout` |
| `check_card_payment_status` | Read what happened to that payment | `GET /api/v1/mcp/user/bundle/card/status/{payment_reference}` |

**Contract**

```
create_card_checkout(quote_reference: str)      -> checkout result (link, amount, reference)
check_card_payment_status(payment_reference: str) -> payment state
```

One argument each, and neither can carry a card, an amount or an outcome. There is no
`card_number`, `expiry`, `cvv`, `cardholder`, `payment_token`, `amount`, `currency`,
`checkout_url` or `idempotency_key` argument on either tool, and no `paid` / `success` /
`redirect` argument through which a model could *assert* that a payment happened. The card is
entered on Stripe's own hosted page, in the user's browser; this process never renders it,
never proxies it and never receives its contents.

**Request body — exactly three fields, and no more.** The endpoint declares
`extra="forbid()"`, so one unexpected key is a `422` for the whole request rather than a field
the platform politely ignores:

```json
{
  "bundle_code": "<from the stored quote>",
  "quote_reference": "<the stored quote's id>",
  "related_search": { "countries": [ { "iso3_code": "FRA", "country_name": "France" } ] }
}
```

| Backend field | Source |
| --- | --- |
| `bundle_code` | `quote.bundle.bundle_code`, captured from `GET /bundles/{code}` at preparation |
| `quote_reference` | `quote.quote_id` |
| `related_search` | `quote.search_context`, resolved against the platform's own lists at preparation. **Omitted entirely** — never sent as `null` — when the quote recorded no usable context |

There is **no `payment_type`**: the backend fixes the payment type internally for this route,
so sending one would be both redundant and rejected. There is likewise no amount, price, tax,
discount, currency, user id, order id, URL, card detail, provider token or access token —
none of them belongs in the body, and several belong nowhere at all. The dict is built
literally, with no code path that can add a key.

Headers: `Authorization` (refreshed *before* the call), `X-Device-Id`, `Accept-Language`
(`quote.locale`), `X-Currency` (`quote.price.currency`) and `Idempotency-Key`. **Currency
travels only as `X-Currency`**, so the settlement currency is negotiated in exactly one place.
The status read reuses the same locale and currency, so an amount read back to the user is the
one they agreed to rather than a server-default conversion of it.

**Backend response fields this server reads.** Exactly the documented field sets, with **no
invented aliases** — a name this server reads has to be a name the platform actually sends —
and `extra="ignore"` closing the rest:

| Payload | Fields read |
| --- | --- |
| create checkout | `payment_reference`, `order_id`, `checkout_url`, `status`, `amount`, `currency`, `expires_at`, `idempotent_replay`, `correlation_id`\*, `message`\* |
| payment status | `payment_reference`, `status`, `order_id`, `amount`, `currency`, `bundle_code`, `quote_reference`, `expires_at`, `provisioned`, `next_action`, `correlation_id`\*, `message`\* |

\* `correlation_id` and `message` are **named so they are known to be dropped**. A correlation
id is a backend tracing handle and a `message` is prose written for the platform, not for a
traveller; neither ever reaches a result, which is asserted by a test. A provider session id,
a client secret, a publishable key or a `developerMessage` is not named at all, so it cannot
be forwarded even by mistake — the field set is closed rather than filtered.

**One page per quote**

* One cryptographically random key (`secrets.token_urlsafe(48)`) is minted **per quote**, on
  first checkout, and reused for every later attempt on that quote.
* Once a page exists, its stored result is **replayed** — the same link, `replayed: true`,
  and no request sent. The user is never handed two links, and never asked to pay twice.
* The replay is checked *before* the quote-lifecycle gates, so a quote whose short TTL lapsed
  while the user was paying still returns the link they have open rather than an expiry error.
* Attempts are capped (`MAX_CHECKOUT_ATTEMPTS = 3`) so an unresolved checkout cannot become an
  unattended loop.

**The link is validated, not trusted**

`safe_checkout_url` passes on only a plain `https` address with a host, no embedded
credentials, no whitespace or control characters and a sane length. `javascript:`, `data:`,
plain `http` and anything malformed are refused outright and never shown — a payment link is
the one value a user is asked to *act on*, so repairing a bad one would be worse than
declining it. A page whose `payment_reference` this server could not use afterwards is also
refused: the user would otherwise pay with no way for anyone here to confirm it.

**Who is allowed to say a payment happened**

The backend's existing signature-verified payment webhook (`POST
/api/v1/callback/payment-webhook`), and nothing else. Stripe calls it; it settles the payment
and triggers provisioning. **This server never calls that webhook, never simulates one, never
marks a payment paid and never provisions** — it holds no Stripe credential with which it
could, and the route is refused by the transport guard for every method. Every `paid` and
`provisioned` below is *copied from a status read*, never decided here.

**Payment status, and what may be concluded from it**

The eight statuses are the platform's own normalized words. Only case and surrounding
whitespace are normalized on this side; **no synonym is accepted**, because inventing a
mapping for a word the platform does not send would mean guessing.

| Platform status | What the tool returns | What the model is told to do |
| --- | --- | --- |
| `PENDING` | `paid: false`, `is_final: false`, the link again | Re-offer the link; wait; never ask for card details |
| `PAID` | `paid: true`, `provisioned` as reported, `is_final: false` | Payment arrived, eSIM **not** ready — never say active/installed |
| `PROVISIONING` | `paid: true`, `provisioned` as reported, `is_final: false` | Being set up; offer to check again in a minute |
| `COMPLETED` | `paid: true`, `is_final: true`, `order_id`, `next_action` | Say plainly it was paid by card; quote is consumed |
| `FAILED`, `EXPIRED`, `CANCELLED` | `paid: false`, `is_final: true`, `new_checkout_required: true` | Say nothing was charged; offer a new page |
| `AMBIGUOUS` | raises `card_payment_ambiguous` | **Stop.** Never say succeeded or failed; contact support |
| unrecognized word | raises `card_payment_status_unavailable` | Never guessed at, in either direction |

Three facts are kept deliberately separate, because collapsing any two is how a user gets told
something untrue: `paid` (the platform says money arrived), `provisioned` (the platform says
the eSIM exists — never inferred from `paid`), and `is_final` (no later check can change the
answer). `next_action` is the platform's own eSIM-retrieval instruction, passed through
verbatim when it sends one and never invented when it does not.

`paid` comes from the platform's own status word and from nothing else. A browser redirect, a
success screen, the user returning to the conversation and the user saying "I paid" are
explicitly named as **not** evidence, in the tool description, in every status result and in
the server instructions.

**Checking is bounded and never automatic.** One backend read per tool call — there is no
retry loop inside either tool — and at most `MAX_STATUS_CHECKS = 20` reads per payment before
the server refuses and points at support. A terminal answer is stored and replayed instead of
re-read, so a settled payment costs zero further requests. An `AMBIGUOUS` answer is recorded
as terminal: it stops later checks *and* stops a second checkout for the same quote.

Example checkout result:

```json
{
  "status": "checkout_ready",
  "quote_reference": "…",
  "payment_reference": "…",
  "checkout_url": "https://checkout.test/pay/…",
  "payment_method": "Card",
  "amount": "10.00",
  "currency": "USD",
  "expires_at": "2026-01-01T00:30:00+00:00",
  "payment_status": "PENDING",
  "charged": false,
  "paid": false,
  "provisioned": false,
  "order_id": "…",
  "order_state": "unpaid",
  "bundle": { "name": "France 5GB / 30 Days", "data": "5.0 GB", "validity": "30 Day" },
  "message": "A secure payment page was opened for this plan. Nothing has been charged yet."
}
```

The backend records an **unpaid** order alongside the page, so `order_id` is reported — next
to `paid: false` and a `next_step` that says in words that the order becomes a purchase only
once the user pays on Stripe. There is deliberately no `order_created: true` here: that flag
means "bought and charged" everywhere else in this codebase, and it would be a lie here.

`InMemoryCardCheckoutStore` holds checkout records — including the idempotency key, as a
`SecretStr` — in the process heap, and they are dropped whenever their session ends. A restart
loses this server's memory of an open page: the *platform's* record survives, so the user's
payment is unaffected and the same key would still resolve there, but this server can no
longer replay the link or check the payment. `CardCheckoutStore` is the same Redis seam as
`PurchaseQuoteStore`.

Example catalogue result:

```json
{
  "status": "ok",
  "destination": "France",
  "country": { "country": "France", "country_code": "FR", "iso3_code": "FRA" },
  "total_count": 6,
  "returned_count": 5,
  "more_available": true,
  "bundles": [
    { "bundle_code": "…", "name": "France 5GB / 30 Days", "data": "5.0 GB", "unlimited": false,
      "validity": "30 Day", "price": "12.50 USD", "price_amount": 12.5, "currency": "USD",
      "coverage": { "countries_count": 1, "countries": ["France"] } }
  ],
  "price_note": "Displayed catalogue price may not include final tax; final amount will be confirmed before purchase.",
  "note": "Plans the platform sells for France. This is that destination's list, not the platform's entire catalogue."
}
```

Example results (nothing sensitive is ever returned):

```json
{ "status": "otp_requested", "channel": "EMAIL", "destination": "m***@example.com", "expires_in_seconds": 300 }
```

```json
{
  "status": "authenticated",
  "is_verified": true,
  "user": { "user_id": "b3f1...6666", "email": "m***@example.com", "phone": "+961******67", "currency": "USD" }
}
```

### How the model knows when to call what

Three mechanisms, all supported by the installed SDK (`mcp` 2.0.0) and all verified by
tests in `tests/test_tool_guidance.py`:

1. **Server instructions** (`SERVER_INSTRUCTIONS` in `src/esim_mcp/server.py`) are returned
   in the `initialize` result, so the model sees them once per session. They cover how to
   behave: talk naturally, never tell the user to invoke a tool, never show raw requests or
   responses, never ask for a token, ask only for what the current step needs, check login
   status before authenticated actions, never claim success without a tool result, confirm
   amounts before any future financial action, never repeat a complete identifier, that
   browsing needs no login, that plans are always found for a destination and that no
   endpoint lists every plan — plus the scope this version actually has.
2. **Tool descriptions** state, per tool, *when* to call it, what to ask the user first,
   what to say afterwards, and what never to do (for example: after `request_login_otp`,
   ask for the code and do **not** say login is complete; resend only on explicit request;
   log out only on explicit request; after `find_bundles_by_country`, offer a short
   numbered list and keep each `bundle_code` for the follow-up).
3. **Argument descriptions and tool annotations** — each argument says where its value must
   come from ("the user's email address, exactly as they gave it… never invent"; "the
   destination country the user named, in their own words"), and annotations mark every
   read-only tool as such, and `logout` and `confirm_purchase` destructive.
   `create_card_checkout` is deliberately **not** destructive: it charges nothing, and a
   client that gates destructive tools should not be made to block a payment page that costs
   the user nothing.

### The purchase conversation, end to end

```
login            request_login_otp → verify_login_otp        (needed before any purchase step)
browse           find_bundles_by_country / …                 (no login needed)
prepare quote    prepare_purchase(bundle_code, "Wallet")     ← NEVER charges
read it back     "That's 8.06 USD from your wallet. Buy it?" ← the amount is said out loud
explicit yes     "yes, buy it" / "confirm" / "pay now"       ← the user's own words
buy              confirm_purchase(quote_reference)           ← MAY charge; creates the order
result           "Bought — France 5GB for 8.06 USD."
```

The middle two steps are not decoration. `prepare_purchase` never charges and
`confirm_purchase` may, so the amount has to be spoken and agreed to *between* them; asking
for a price is not agreement to pay it, and the server instructions say so in those words.
A lapsed quote means starting again: prepare, say the new amount, get a fresh yes.

### The card conversation, end to end

```
login            request_login_otp → verify_login_otp
browse           find_bundles_by_country / …                  (no login needed)
prepare quote    prepare_purchase(bundle_code, "Card")        ← NEVER charges, opens no page
read it back     "That's 8.06 USD by card. Shall I open the payment page?"
explicit yes     "yes, pay by card" / "open it" / "go ahead"  ← the user's own words
open the page    create_card_checkout(quote_reference)        ← opens a page; charges NOTHING
give the link    "Here's the secure payment link — 8.06 USD…" ← and then WAIT
user pays        …on Stripe's hosted page, in their browser   ← never in this conversation
user says so     "I paid" / "can you check?"                  ← still not proof of anything
check            check_card_payment_status(payment_reference) ← the ONLY source of truth
result           "Paid — France 5GB for 8.06 USD."
```

The two steps that carry the whole design are **"and then WAIT"** and **"still not proof"**.
Opening a page is not a payment, and returning from one is not either: only the status read
can say what happened, so the model is told to wait for the user rather than poll, and never
to conclude a payment from a redirect, a success screen or the user's own account of it.

Never, at any point, does the assistant ask for or accept a card number, an expiry, a
security code or a cardholder name. There is nowhere to put one, and the guidance says so at
every step — in the tool description, in every card result, and in the server instructions.

Tools return short structured facts, never pre-written chat sentences: the model does the
phrasing. Errors come back as a safe code plus a plain message (for example
`authentication_required: No active eSIM session…`), which the model turns into the right
next step.

### "Show me all bundles"

There is no backend endpoint that returns every bundle for every country, so the server
never lets the model imply otherwise. `browse_home_catalog` returns counts and a small
preview per category, and its result carries a note saying it is an overview, not every
plan; the server instructions say the same. The expected behaviour is that the assistant
explains that browsing needs a destination and asks for a country, a region, or global vs
cruise — never a fabricated list. This is asserted in `tests/test_tool_guidance.py` and
`tests/test_catalog_tools.py`, and is scenario 2.9 of the manual QA plan.

---

## Country and region resolution

Chat users say "France", "FR", "FRA" or "Europe". The backend wants a country **tag GUID**
or a **region code**. The mapping is always fetched from the backend
(`GET /bundles/countries`, `GET /bundles/region`) — never remembered, never invented.

Country matching is deterministic and tried in a fixed priority order:

1. exact ISO2 → 2. exact ISO3 → 3. exact country name → 4. exact alternative name.

Comparison is case-insensitive with collapsed whitespace, and nothing else — no stemming,
no edit distance, no substring matching. The first order that produces matches decides.
Then:

* **exactly one match** → its tag GUID is used;
* **several matches** → `ambiguous_country`, listing the options for the user to choose;
* **no match** → `country_not_found`, carrying up to five real catalogue destinations whose
  names start with or contain the query, for the model to offer. Suggestions are never
  auto-selected: "Franc" does not silently become France.

Regions match the same way on exact region code then exact region name. The backend's
placeholder values (`"Unknown"`, `""`) are normalized to nothing and can never match.

Because the MCP SDK renders a tool error as `str(exception)`, the suggestions and choices
are written into the error **message** rather than a structured payload — otherwise the
model would never see them.

### A region code is sent back exactly as the backend spelled it

`GET /bundles/by-region/{region_code}` selects with `region.region_code == region_code`
against the same list `GET /bundles/region` returned, and raises `400 "Region Not Found"`
when nothing matches. The comparison is **case-sensitive**, and region codes originate as
the upstream hub's zone tag (`EUROPE`, `ASIA`, `GLOBAL`) — upper-case by convention, not by
contract.

`Region` therefore exposes two codes, and they are not interchangeable:

| Property | Value | Used for |
| --- | --- | --- |
| `Region.api_code` | the backend's spelling, untouched | the URL path — the only one that may go on the wire |
| `Region.code` | `api_code` upper-cased | display in a tool result, and matching a user's wording |

Matching a user's wording is case-insensitive either way, so a model that passes back the
upper-cased `region_code` it was shown still resolves to the right region, and the search
still goes out under the backend's own spelling.

This was a live defect: `find_bundles_by_region` sent `Region.code`, so any region whose
code was not already upper-case was listed happily by `list_regions` and then rejected by
`/bundles/by-region` — which the assistant reported to the user as "no plans for that
region". `tests/test_catalog_tools.py::test_a_region_is_searched_by_the_code_the_backend_gave_not_an_upper_cased_one`
stubs both spellings and fails if it ever comes back.

### The by-region route has no pagination

`GET /bundles/by-region/{region_code}` declares no `page`, `limit`, `offset` or `size`
parameter — its only inputs are the path code and the `X-Device-Id` / `Accept-Language` /
`X-Currency` headers, and the whole list comes back in one response. The client sends no
paging parameter, and there is never a second page to fetch. Bounding the *result* is this
server's own job (see [Result sizes](#result-sizes)), not the backend's.

### An error is never an empty catalogue

A failed region search raises a typed error; only a `200` with `data: []` produces an empty
result. `401`/`403` → `authentication_required`, `429` → `rate_limited`, `400`/`404` →
`region_not_found`, `5xx` → `catalog_unavailable`. The `region_not_found` raised by the
*client* (as opposed to the resolver) means the backend rejected a code that came out of its
own region list, so its message says explicitly that this is not a statement that the region
has no plans. The server instructions and the tool description repeat the rule: say a
destination has no plans only when a search returned successfully and said so.

## Result sizes

A destination can carry dozens of bundles and the catalogue has hundreds of countries, so
every result is bounded and every result says how much was left out.

| Result | Default | Maximum |
| --- | --- | --- |
| Bundles (`find_bundles_by_*`, `list_cruise_bundles`) | 5 | 20 |
| Countries / regions (`list_*`) | 20 | 100 |
| Home overview | 8 countries, 20 regions, 5 cruise, 5 global | fixed |
| Coverage inside a bundle summary | 3 country names + count | fixed |
| Coverage inside `get_bundle_details` | 30 country names + count | fixed |
| Ships inside `get_bundle_details` | 20 + count | fixed |

Every bundle result carries `total_count` (how many matched) and `returned_count` (how many
are in the result), plus `more_available` when the list was truncated, and
`total_available` and `filters_applied` when filters narrowed it. An oversized `limit` is
capped rather than rejected, so a model asking for "all of them" still gets a usable
answer.

Raw home/catalogue responses are never returned: icons, marketing copy, category codes,
region objects, operator lists and full country lists are dropped in the summary layer.

---

## Multi-user session model

Multiple eSIM users are supported from this first version.

* Every MCP caller gets an **isolated server-side session**, keyed by a SHA-256 digest of
  its verified identity. There is no global "current user" and no global token.
* A session holds: identity source, device id, access token, refresh token, token expiry,
  eSIM user id, masked email/phone, currency, and creation/update times.
* One client can never read or overwrite another client's session: the key is derived
  from identity, never from an argument, an email or a phone number.
* All store operations are asynchronous and concurrency-safe, and each session has its own
  async lock so parallel calls cause exactly one token refresh.

### Client identity

`ClientIdentityProvider` is the only source of identity.

* **Streamable HTTP with OAuth configured** — the SDK verifies the bearer token; identity
  is the `(client_id, issuer, subject)` principal from
  `mcp.server.auth.middleware.auth_context.get_access_token()`. This is the production
  path.
* **stdio** — the transport has no principal; the process itself is the trust boundary, so
  a configured local identity (`ESIM_MCP_DEV_CLIENT_ID`) is used. Development only.
* **Production without a verified principal** — the call fails closed with
  `client_identity_unavailable`. The development provider refuses to even be constructed
  for a production configuration.

An `X-Client-Id` (or any other) request header is **not** an identity assertion and is
never trusted — the SDK documents request headers as client-supplied input.

### Stable device id

The backend requires `X-Device-Id`. It is derived as:

```
HMAC-SHA256(ESIM_MCP_DEVICE_ID_SALT, verified_client_identity)   # hex, 64 chars
```

Stable across logins and restarts for a given salt, different per client, non-reversible,
and never built from Python's randomized `hash()`. The salt is never logged, and a missing
or short (< 32 chars) salt aborts startup in production.

### Session storage

`SessionStore` is an abstract, async interface (`get/save/delete` for sessions and
challenges, plus `lock`). Phase 1 ships `InMemorySessionStore`.

> **The in-memory store is for local or single-instance operation only.** State lives in
> the process heap, so a horizontally scaled deployment would drop sessions whenever a
> client is routed to another replica, and nothing is encrypted at rest. Multi-instance
> production must replace it with an **encrypted Redis store** (encryption at rest,
> per-session TTL, distributed lock behind `SessionStore.lock`). No tool, service or API
> client code changes when that happens — only the injected store.

---

## Security model

* **Tokens are never MCP tool arguments and never tool results.** An argument would put
  the token in the model's context, in client-side transcripts and in tool-call logs, and
  would let any caller present a token that is not theirs — identity, not a bearer value
  the caller hands over, decides which session is used. `Authorization` and
  `X-Refresh-Token` are attached inside the HTTP client and nowhere else.
* The OTP is never persisted and never logged. The login challenge stores only a masked
  identifier, the login type, the device id and a timestamp.
* JWTs are decoded **without signature verification and only to read `exp`**, purely to
  schedule refresh. This is never treated as authorization: the backend remains the sole
  authority for validating tokens.
* Errors are typed and mapped to safe messages. Stack traces, raw provider responses,
  JWTs, refresh tokens, OTPs and the backend's `developerMessage` never reach a client.
* Every log record is rendered and then redacted: authorization headers, access/refresh
  tokens, OTPs, emails, phone numbers, device ids, session identifiers, ICCIDs, activation
  codes and future Stripe client secrets. Full request/response bodies are never logged at
  any level. Logs go to stderr so stdout stays pure JSON-RPC for the stdio transport.
* Retry policy: reads may retry with bounded exponential backoff — including every
  catalogue call, which is always a `GET`. OTP request, OTP resend, OTP verification,
  refresh-token rotation and logout are **never** retried.
* Production mode fails fast on unsafe configuration (non-https base URL, missing or weak
  device-id salt) and fails closed on unverified identity.

### Identifier privacy

The Phase 1 QA run found the assistant repeating the user's **full** email address after
login and again inside the profile answer, even though the tool result was masked — it had
simply reused what the user typed earlier in the chat. Both layers are now closed:

* **Output.** `get_user_profile` masks at the point of use (`mask_email(info.email)`), not
  merely by inheriting a mask from the session, so no branch can return the complete value
  even though the backend payload contains it. The same holds for `verify_login_otp`,
  `get_login_status` and the OTP destination. The account id is truncated
  (`b3f1...6666`). Useful non-sensitive fields — name, country, language, currency,
  verification status and wallet balance — are unaffected.
* **Instructions.** The server instructions and the `get_user_profile` description now say
  to repeat the masked form exactly and never to write out a complete email address, phone
  number or account id — *"not even when the user typed it earlier in this conversation"* —
  and never to read a `bundle_code` out to the user.
* **Logs.** Unchanged and already strict: every record is rendered and redacted before a
  handler sees it.

Regression tests live in `tests/test_privacy.py`: they assert that the full address and
number never appear in any tool result (including when the backend returns them in full and
the session recorded no mask) and never survive into a log record, message, structured
extra or traceback. `tests/test_repository_hygiene.py` additionally fails the build if a
real backend host or a real-looking email address is ever committed.

The identifier the user *supplies* is still sent to the backend in full where the contract
requires it (`/auth/login`, `/auth/resend-otp`, `/auth/verify_otp`) — masking is about what
comes back out, not about what the platform needs to receive.

### Catalogue error behaviour

Every failure the model can act on has its own code and an actionable, data-free message:

| Situation | Code | The next action it suggests |
| --- | --- | --- |
| Country not in the catalogue | `country_not_found` | offer the close catalogue names, or another destination |
| Several countries match | `ambiguous_country` | ask the user which one |
| Region not in the catalogue | `region_not_found` | name the real regions |
| Several regions match | `ambiguous_region` | ask the user which one |
| Unknown bundle code | `bundle_not_found` | use a code from a result already shown; never invent one |
| Filters exclude everything | `no_matching_bundles` | say what is constraining the search and offer to relax it |
| Catalogue unreachable | `catalog_unavailable` | tell the user and offer to try again shortly |
| Backend too slow | `backend_timeout` | tell the user it did not respond in time |
| Unparseable response | `invalid_backend_response` | generic failure; nothing internal is shown |

A destination that genuinely has no plans is **not** an error: the result comes back with
`total_count: 0` and a note offering a neighbouring country, a regional plan or a global
plan. Bundles the backend marks `is_active: false` are dropped from every list, and
`get_bundle_details` reports availability honestly.

---

## Environment configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `ESIM_API_BASE_URL` | – | Required. Backend base URL **without** `/api/v1`. https in production |
| `ESIM_MCP_ENVIRONMENT` | `local` | `local`, `development`, `qa`, `staging`, `production` |
| `ESIM_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `ESIM_MCP_HOST` | `127.0.0.1` | HTTP transport bind host. `0.0.0.0` when deployed |
| `ESIM_MCP_PORT` | `8080` | HTTP transport bind port. Falls back to a platform `PORT` |
| `ESIM_MCP_DEVICE_ID_SALT` | – | Required in production, >= 32 chars. Ephemeral outside production |
| `ESIM_MCP_DEFAULT_LOCALE` | `en` | Sent as `Accept-Language` |
| `ESIM_MCP_DEFAULT_CURRENCY` | `USD` | Sent as `X-Currency` |
| `ESIM_MCP_CONNECT_TIMEOUT` | `5` | Seconds |
| `ESIM_MCP_READ_TIMEOUT` | `20` | Seconds. The general read budget, sized for cached catalogue lookups |
| `ESIM_MCP_ACCOUNT_READ_TIMEOUT` | `120` | Seconds. `get_my_esims` and `get_order_history` only — see below |
| `ESIM_MCP_CHECKOUT_READ_TIMEOUT` | `45` | Seconds. The card-checkout `POST` only |
| `ESIM_MCP_PURCHASE_READ_TIMEOUT` | `90` | Seconds. The wallet-purchase `POST` only |
| `ESIM_MCP_WRITE_TIMEOUT` | `20` | Seconds |
| `ESIM_MCP_POOL_TIMEOUT` | `5` | Seconds |
| `ESIM_MCP_TOKEN_REFRESH_WINDOW_SECONDS` | `120` | Refresh this long before `exp` |
| `ESIM_MCP_LOGIN_CHALLENGE_TTL_SECONDS` | `300` | Pending-OTP lifetime |
| `ESIM_MCP_PURCHASE_QUOTE_TTL_SECONDS` | `300` | Prepared-quote lifetime (30…1800) |
| `ESIM_MCP_MAX_ACTIVE_QUOTES_PER_USER` | `5` | Simultaneous prepared quotes per user (1…50) |
| `ESIM_MCP_LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `ESIM_MCP_DEV_CLIENT_ID` | `local-dev-client` | stdio/dev identity; ignored in production |

Only these prefixed names are read by the settings model, so a platform-provided `HOST`,
`ENVIRONMENT` or `LOG_LEVEL` cannot silently reconfigure the server.

### Read budgets are per-route, never global

There are four, and widening one never widens another. `ESIM_MCP_READ_TIMEOUT` is the general
one and it is sized for what most of this server does: cached catalogue lookups that answer in
well under a second. The other three exist because three specific routes do far more work than
that, and each is applied to its own route and to nothing else.

`ESIM_MCP_ACCOUNT_READ_TIMEOUT` covers the two authenticated account-history reads —
`GET /api/v1/user/my-esim` behind `get_my_esims`, and `GET /api/v1/user/order-history` behind
`get_order_history`. Neither is cached: the platform builds the answer per user and per
request, re-reading every bundle the account owns and re-localizing every row, so the time it
takes grows with the account. On a real account that ran past the 20-second general budget
while the portal — which imposes no budget of its own — got the same answer and rendered it.
The default of `120` is deliberately well clear of the measured latency rather than tight
against it.

Two rules go with that budget, and both are enforced in code:

- **One attempt.** These two reads do not use the shared three-attempt read retry. Three
  attempts at a two-minute budget is six minutes of a chat client waiting to be told the
  platform was slow. One request goes out per tool call, and one answer or one typed timeout
  comes back.
- **A timeout is not an empty account, and not an authentication failure.** A read that runs
  out of budget raises the typed `account_read_timeout` error, which says in words that the
  account was not read rather than that it is empty. The access token is not refreshed, the
  read is not replayed, and nothing retries on its own. Only a real `401` from the platform
  causes a refresh, and that refresh replays the read exactly once.

`ESIM_MCP_CHECKOUT_READ_TIMEOUT` and `ESIM_MCP_PURCHASE_READ_TIMEOUT` are the two payment
budgets and are unrelated to the above; see the purchase and card-checkout sections for why
they are sized the way they are. Changing the account budget leaves both untouched, and
changing either of them leaves the account budget untouched.

The one deliberate exception is the listening port, and it lives in the HTTP entry point
(`esim_mcp/http_app.py`) rather than in the settings model: `ESIM_MCP_PORT` > a
platform-supplied `PORT` (Render, Heroku, Cloud Run) > the `8080` default. A hosted service
has to listen where its platform routes, the port is not a security decision, and the
precedence keeps an explicit setting of your own on top. A `PORT` that is not a valid port
number aborts startup rather than falling back.

The QA URL belongs in your local, git-ignored `.env` — never in committed source.
`.env.example` contains placeholders only. Tests never read a `.env`.

QA example — copy into `.env` and replace both placeholders with your QA URL and a long
random local secret:

```env
ESIM_API_BASE_URL=https://qa-placeholder.example.com
ESIM_MCP_ENVIRONMENT=development
ESIM_MCP_TRANSPORT=stdio
ESIM_MCP_DEVICE_ID_SALT=replace-with-a-long-random-local-secret
ESIM_MCP_DEFAULT_LOCALE=en
ESIM_MCP_DEFAULT_CURRENCY=USD
```

`ESIM_MCP_ENVIRONMENT` describes *this server's* mode, not the backend's: `development`
(or `qa`) keeps the local stdio identity available while pointing at the QA backend. Only
`production` switches on fail-closed identity and the https/salt requirements.

The MCP server needs nothing beyond that public backend URL — no Supabase, Stripe, eSIM
Hub or database credentials are used, requested or accepted anywhere in this project.

---

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env      # then fill in your QA values
```

### Run over stdio

```bash
ESIM_MCP_TRANSPORT=stdio python -m esim_mcp.server
# or, after installing the project:
esim-mcp
```

### Run over Streamable HTTP

```bash
ESIM_MCP_TRANSPORT=streamable-http ESIM_MCP_PORT=8080 python -m esim_mcp.server
# MCP endpoint: http://127.0.0.1:8080/mcp
# health:       http://127.0.0.1:8080/health
```

Docker:

```bash
docker build -t esim-mcp .
docker run --rm -p 8080:8080 --env-file .env esim-mcp
```

---

## Deploying over Streamable HTTP (Render and friends)

`src/esim_mcp/http_app.py` is the deployed entry point. `create_app()` builds the ASGI
application around the *same* `MCPServer` the stdio entry point uses and serves exactly two
routes:

| Route | Purpose |
| --- | --- |
| `POST /mcp` | Streamable HTTP MCP endpoint. The remote server URL a client is given **is** this URL |
| `GET /health` | Liveness for the platform's health check. No backend call, no session state |

Nothing else is mounted. In particular there is no `/register` and no
`/.well-known/oauth-*`: the SDK publishes those only when an OAuth `AuthSettings` and token
verifier are configured, and none is (see *Security model*). A client that probes them gets
a 404, which is the honest answer from a server that cannot issue or verify tokens — a stub
there would advertise an authorization server that does not exist.

**Build command**

```bash
pip install --upgrade pip && pip install .
```

`requirements.txt` is the legacy FastAPI skeleton's dependency list and does **not**
install this server. `pip install .` installs `esim_mcp` and its real dependencies from
`pyproject.toml`.

**Start command**

```bash
uvicorn esim_mcp.http_app:create_app --factory --host 0.0.0.0 --port $PORT
```

`python -m esim_mcp.server` with `ESIM_MCP_TRANSPORT=streamable-http` is equivalent: it
serves the same app and resolves the same host and port.

**Environment**

| Variable | Value | Why |
| --- | --- | --- |
| `ESIM_API_BASE_URL` | your backend base URL, no `/api/v1` | required |
| `ESIM_MCP_ENVIRONMENT` | `qa` | keeps the explicit QA/dev identity available. `production` fails closed without an OAuth token verifier — see below |
| `ESIM_MCP_TRANSPORT` | `streamable-http` | |
| `ESIM_MCP_HOST` | `0.0.0.0` | listen on the platform's interface, and drop the SDK's loopback-only `Host` allow-list (kept, it answers 421 to every request arriving through the platform's proxy) |
| `ESIM_MCP_DEVICE_ID_SALT` | a long random value | keeps device ids stable across restarts |
| `ESIM_MCP_LOG_LEVEL` | `INFO` | |

The port comes from the platform's own `PORT`. `ESIM_MCP_PORT` still wins when it is set —
set it only if you mean to override the platform.

**Verify a deployment**

```bash
curl -sS https://<your-service>.onrender.com/health

curl -sS -X POST https://<your-service>.onrender.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

The second call must return `200` with the server's `initialize` result. A `404` means the
platform is running something other than this app (the legacy `app.main:app` skeleton, for
instance); a `421 Invalid Host header` means `ESIM_MCP_HOST` was left at its loopback
default.

> **Who the caller is.** Over Streamable HTTP without OAuth the transport has no verified
> principal, so every caller of a deployed instance shares one server-side session and one
> device id. That is acceptable for a single-tester QA URL and is exactly why
> `ESIM_MCP_ENVIRONMENT=production` refuses to serve without a token verifier. Wiring the
> verifier remains the prerequisite for a shared deployment — deploying is not that step.

---

## Connecting Claude to this server (stdio)

**Which command starts the server**

```bash
.venv/bin/python -m esim_mcp.server
```

`python -m esim_mcp.server` calls `main()` in `src/esim_mcp/server.py`, which reads the
settings, configures logging and runs the transport named by `ESIM_MCP_TRANSPORT`
(`stdio` by default). `esim-mcp` is the same entry point once the project is installed.
The interpreter must be the one with the dependencies installed — the project venv.

**Which working directory is required**

The project root (the directory holding `pyproject.toml`). Two things depend on it:
`PYTHONPATH=src` resolves from there when the project is not pip-installed, and `.env` is
read from the current directory.

**How environment variables are loaded**

`pydantic-settings` reads the process environment first, then `./.env` as a fallback, and
only the `ESIM_*` names listed under *Environment configuration*. Two equivalent options:

* keep everything in the git-ignored `.env` (recommended locally — no secrets in any MCP
  config file);
* or set the variables in the MCP client's `env` block, which then wins over `.env`.

**How Claude discovers the server**

*Claude Code, project-scoped (already committed here):* `.mcp.json` in the project root
declares the server. It deliberately contains no URL, no salt and no absolute path — the
QA URL and salt come from your local `.env`:

```json
{
  "mcpServers": {
    "esim": {
      "type": "stdio",
      "command": ".venv/bin/python",
      "args": ["-m", "esim_mcp.server"],
      "env": { "PYTHONPATH": "src" }
    }
  }
}
```

Open Claude Code **in this directory** and approve the project server when prompted
(`/mcp` lists it). Adjust `command` if your virtualenv lives elsewhere.

*Claude Desktop (user-level config, not managed by this repo — edit it yourself):* it does
not run the server from this directory, so give absolute paths and the variables inline.
Placeholders only below; substitute your own values and never commit the result:

```json
{
  "mcpServers": {
    "esim": {
      "command": "/absolute/path/to/mcp-service/.venv/bin/python",
      "args": ["-m", "esim_mcp.server"],
      "cwd": "/absolute/path/to/mcp-service",
      "env": {
        "PYTHONPATH": "src",
        "ESIM_API_BASE_URL": "https://qa-placeholder.example.com",
        "ESIM_MCP_ENVIRONMENT": "development",
        "ESIM_MCP_TRANSPORT": "stdio",
        "ESIM_MCP_DEVICE_ID_SALT": "replace-with-a-long-random-local-secret",
        "ESIM_MCP_DEFAULT_LOCALE": "en",
        "ESIM_MCP_DEFAULT_CURRENCY": "USD"
      }
    }
  }
}
```

**How to verify the nineteen tools are available**

* In Claude Code: `/mcp` → the `esim` server → its tool list.
* Or ask in chat: *"What eSIM tools do you have?"* — expect exactly `request_login_otp`,
  `resend_login_otp`, `verify_login_otp`, `get_login_status`, `get_user_profile`, `logout`,
  `list_countries`, `list_regions`, `browse_home_catalog`, `find_bundles_by_country`,
  `find_bundles_by_region`, `list_cruise_bundles`, `get_bundle_details`, `prepare_purchase`,
  `get_prepared_purchase`, `cancel_prepared_purchase`, `confirm_purchase`,
  `create_card_checkout` and `check_card_payment_status`. There must be nothing called
  `buy`, `pay`, `capture`, `top_up`, `voucher`, `refund`, `activate` or `provision`.
* Or without a client at all (`tools/list` is answered locally — it makes no backend call):

```bash
printf '%s\n%s\n%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | PYTHONPATH=src .venv/bin/python -m esim_mcp.server
```

**How to inspect startup errors**

The server logs JSON to **stderr** (stdout carries only JSON-RPC). Claude Code shows it
under `/mcp` → the server → logs; Claude Desktop writes it to its own MCP log files. To
see it directly, run the command above in a terminal: a bad configuration fails fast and
loudly, e.g. `ESIM_API_BASE_URL … Field required`, `ESIM_MCP_DEVICE_ID_SALT is required in
production`, or `ESIM_API_BASE_URL must use https:// in production`. Raise detail with
`ESIM_MCP_LOG_LEVEL=DEBUG` — secrets stay redacted at every level.

**How to disconnect**

Remove or rename `.mcp.json` (or the entry in the Claude Desktop config) and restart the
client; in Claude Code you can also decline/disable the project server from `/mcp`. The
client owns the process lifetime — closing it stops the server, which closes its HTTP pool
and drops all in-memory sessions.

**Streamable HTTP:** run with `ESIM_MCP_TRANSPORT=streamable-http` and point the client at
`http://<host>:<port>/mcp`.

---

## Testing

Automated (no network — `httpx` is mocked with `respx`):

```bash
pytest
ruff check .
ruff format --check .
```

Manual QA against the real backend, all five conversation-level plans:

* **[`docs/QA_AUTHENTICATION_TEST.md`](docs/QA_AUTHENTICATION_TEST.md)** — email login,
  phone login, status/logout, error cases, multi-user isolation, and the masked-identifier
  check.
* **[`docs/QA_CATALOGUE_TEST.md`](docs/QA_CATALOGUE_TEST.md)** — countries, a country
  search, sorting and filtering, "the second one", regions, global, cruise, and the
  "show me all bundles" scenario, plus how to verify the displayed values against the QA
  API by hand.
* **[`docs/QA_PURCHASE_PREPARATION_TEST.md`](docs/QA_PURCHASE_PREPARATION_TEST.md)** —
  preparing a wallet quote and a card quote, insufficient balance, expiry, logout
  invalidation, client isolation, and the backend checks that prove no order, charge,
  payment intent or provisioning happened at *preparation* time.
* **[`docs/QA_PURCHASE_EXECUTION_TEST.md`](docs/QA_PURCHASE_EXECUTION_TEST.md)** — the one
  plan that spends real money: consent before the charge, the single wallet purchase, the
  replay check, the ambiguous-outcome drill, and the database and wallet checks that prove
  exactly one order and one debit exist. Read its preconditions before running it.
* **[`docs/QA_CARD_CHECKOUT_TEST.md`](docs/QA_CARD_CHECKOUT_TEST.md)** — the other plan that
  spends real money, on a real card: consent before the link, the single payment page, the
  replay check, paying and *not* paying, the redirect-is-not-proof drill, the ambiguous
  outcome, and the checks that prove exactly one Stripe Session and one order exist. Read its
  preconditions before running it.

**No automated test ever buys anything or opens a real payment page.** The suite mocks `httpx`
with `respx`, so the purchase route and both card routes are stubs in every test;
`tests/test_repository_hygiene.py` fails if a real backend host or a real-looking email
address is committed. The only real payments in this project are the ones a human makes
deliberately by following the QA plans above.

---

## Known limitations

* **In-memory sessions.** Single instance only; replace with encrypted Redis before
  scaling out (see above).
* **In-memory purchase quotes, lost on restart.** `InMemoryPurchaseQuoteStore` keeps quotes
  in the process heap, so a restart drops every prepared quote and two replicas do not share
  them. This is expected rather than a defect: a quote reserves nothing and has no backend
  counterpart, so the worst case is that the user is asked to prepare again. Replacing it
  needs no change to the MCP tools — implement `PurchaseQuoteStore` and pass it to
  `build_components(quote_store=...)`.
* **A quote is a snapshot, not a hold.** The price and the wallet balance in a prepared quote
  were true when it was made. Neither is reserved, and both can change a second later, which
  is why quotes are short-lived, why re-preparing supersedes rather than reuses, and why the
  final payable amount is never reported as confirmed. The platform re-reads the plan, the
  price and the balance authoritatively at purchase time, so a stale quote is refused there
  rather than honoured.
* **In-memory execution records, lost on restart.** A restart between sending a purchase and
  recording its outcome loses this server's copy of the idempotency key. The *platform's*
  record survives, so the purchase is still protected from duplication at the backend, but
  this server can no longer replay the answer and a fresh quote would carry a fresh key.
  Implement `PurchaseExecutionStore` and pass it to `build_components(execution_store=...)`
  to make this durable.
* **In-memory card checkouts, lost on restart.** A restart loses this server's copy of an
  open payment page and its idempotency key. The *platform's* record survives, so the user's
  payment is unaffected and the same key would still resolve there, but this server can no
  longer replay the link or check that payment, and a fresh quote would carry a fresh key.
  Implement `CardCheckoutStore` and pass it to `build_components(checkout_store=...)` to make
  this durable.
* **A card payment is completed by the user, not by this server.** `create_card_checkout`
  opens the page and stops. Whether money moves is decided on Stripe's hosted page, in the
  user's browser, and this server learns it only by asking the platform. It cannot capture,
  confirm, cancel or refund a card payment, and it never sees a card number, an expiry or a
  security code — there is no argument, no field and no route through which one could arrive.
* **A card payment can end up genuinely unknown.** If the platform reports the payment as
  ambiguous, this server stops: no further checks, no second page, and a message that says
  plainly that it is neither confirmed nor failed. Resolving that needs eSIM support, not a
  retry.
* **No refund, cancellation or provisioning.** Once a purchase completes there is no tool
  here that can reverse it, cancel the order, or install, activate or check the usage of the
  eSIM. That is why consent is required *before* the call rather than recoverable after it.
* **DCB is not offered.** The backend's `PaymentTypeEnum` also has `DCB`; only `Wallet` and
  `Card` can be prepared here, and `DCB` is refused with the same message as any other
  unsupported method.
* **Tool guidance is advisory, enforcement is not.** Instructions and descriptions steer
  the model's choices, but the model can still call a tool at an odd moment. Nothing
  security-relevant depends on it: the server enforces its own rules regardless — no tool
  takes or returns a token, sessions are keyed by verified identity, a resend needs a
  pending challenge, and mutations are never retried. Wording changes therefore affect
  conversation quality, never safety.
* **Identity depends on the transport.** A verified principal exists only when the server
  runs over Streamable HTTP with OAuth configured (`MCPServer(auth=..., token_verifier=...)`).
  Over stdio there is no transport principal at all, so the development identity is used
  and every caller of that process is one user — correct for a local single-operator setup,
  not for shared hosting. Production refuses to start a session without a verified
  principal rather than guessing. Wiring the OAuth token verifier for a deployed
  environment is the next step and is intentionally not configured here.
* **No machine-readable backend error code.** The envelope's `title` is a *localized*
  message that falls back to the backend's internal error key when no translation exists,
  so error classification matches both forms. A dedicated error-code field in the backend
  envelope would make this exact.
* **OTP delivery window is a local setting.** The backend does not return an OTP expiry, so
  `expires_in_seconds` reflects `ESIM_MCP_LOGIN_CHALLENGE_TTL_SECONDS`.
* **Identifier repeated at verify/resend.** The challenge deliberately stores only the
  masked identifier, and the backend requires the real email/phone in the resend and verify
  bodies, so the caller supplies it again. The server checks it against the pending
  challenge's mask.
* **The backend logs refresh tokens internally.** This server does not repeat that: refresh
  tokens are `SecretStr`, travel only in the `X-Refresh-Token` header, and are redacted from
  every log record.
* **No full-catalogue and no free-text search.** The platform has no endpoint returning
  every bundle for every country, and no real search endpoint. `list_countries` therefore
  resolves and suggests against the country list rather than searching, and every bundle
  result is scoped to one destination. This is a backend capability limit, not a client
  choice.
* **The country list is fetched per call.** Resolving a country costs one extra `GET`
  before the bundle call. The backend caches both, and the read is retryable, so no local
  cache is kept — one less thing to invalidate. If profiling ever justifies it, a short TTL
  cache belongs in `CatalogApiClient`, behind the same interface.
* **Catalogue prices are display prices.** They come from the backend's exchange-rate
  conversion and may exclude final payment tax, which is why every priced result carries the
  tax note. The final amount can only be confirmed by a purchase flow, which does not exist
  in this codebase.
* **Data allowances are compared via the display string.** `gprs_limit` is unit-less on its
  own, so `minimum_data_gb` reads the unit out of `gprs_limit_display` (`"5.0 GB"`). A
  bundle whose allowance cannot be established is excluded from a minimum-data filter
  rather than assumed to qualify.
* **Bundle deduplication happens upstream.** The backend already collapses bundles sharing
  a data allowance and validity, keeping the cheapest, and sorts by price — so a "complete"
  list for a country is the backend's deduplicated one, not every underlying SKU.
* The legacy FastAPI health skeleton from the initial scaffold still lives in `app/` with
  its own tests. It is unrelated to the MCP server and can be removed once it is no longer
  wanted.
