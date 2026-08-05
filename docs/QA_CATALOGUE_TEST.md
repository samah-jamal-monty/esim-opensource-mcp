# Manual QA test: eSIM catalogue browsing through a normal conversation

Phase 2 test plan against the real QA eSIM backend. As in
[`QA_AUTHENTICATION_TEST.md`](QA_AUTHENTICATION_TEST.md), you test by **chatting with
Claude in ordinary language** — you never invoke an MCP tool yourself, and you never type a
country GUID, a region code or a bundle code.

The automated suite never touches QA: every automated test mocks HTTP.

---

## 0. Before you start

Same setup as the authentication test plan (`.env` with the QA URL, Claude connected over
stdio). Nothing extra is needed: **catalogue browsing requires no login.**

Sanity check — ask Claude:

> What eSIM tools do you have?

Expected: thirteen tools — the six authentication tools plus `list_countries`,
`list_regions`, `browse_home_catalog`, `find_bundles_by_country`, `find_bundles_by_region`,
`list_cruise_bundles` and `get_bundle_details`. Nothing about orders, payments, top-ups or
activation.

Record for each scenario: what you typed, what Claude said, which tool it called, and
whether the outcome matched. A scenario passes only if **all** expectations hold.

---

## 1. The conversation to run

Type these lines in order, in one conversation. Expectations follow in section 2.

```text
Show me the available countries.
I need an eSIM for France.
Show me the cheapest plans first.
Only show plans with at least 5GB.
Tell me more about the second plan.
Show me bundles for Europe.
Do you have global bundles?
Show me cruise bundles.
Show me all bundles.
```

---

## 2. Expectations, line by line

| # | You type | Expected tool | Expected behaviour |
| --- | --- | --- | --- |
| 2.1 | `Show me the available countries.` | `list_countries` | Claude reports how many destinations exist and names a handful. It must **not** dump the whole list, and must ask where you are travelling. |
| 2.2 | `I need an eSIM for France.` | `find_bundles_by_country` (country = "France") | A short **numbered** list (about five) with data, validity and price for each. Claude asks whether you want details on one. It must not ask you to log in. |
| 2.3 | `Show me the cheapest plans first.` | `find_bundles_by_country` with `sort_by="price"` | Same destination, cheapest first. Prices ascending. |
| 2.4 | `Only show plans with at least 5GB.` | `find_bundles_by_country` with `minimum_data_gb=5` | Every plan shown has ≥ 5 GB (an unlimited plan qualifies). Claude says the list was narrowed. |
| 2.5 | `Tell me more about the second plan.` | `get_bundle_details` | Claude uses the **second option's own code** from the previous list — it must not ask you for a code, invent one, or read a code out to you. The answer covers data, validity, price, coverage, plan type, activation policy and availability, and passes on the tax note. |
| 2.6 | `Show me bundles for Europe.` | `find_bundles_by_region` (region resolves to `EUR`) | Regional plans, presented the same way. Claude says these cover the region, not the whole platform. |
| 2.7 | `Do you have global bundles?` | `browse_home_catalog` | Claude reports the global plans from the home catalogue (a few, with a count). It must not claim these are all the plans that exist. |
| 2.8 | `Show me cruise bundles.` | `list_cruise_bundles` | Cruise plans with an indication of how many ships each covers. If you name a ship, Claude checks the details rather than promising coverage. |
| 2.9 | `Show me all bundles.` | `browse_home_catalog` (or a plain question, no tool) | **The key scenario.** See below. |

### 2.9 in detail — "Show me all bundles"

There is no backend endpoint that returns every bundle for every country, so:

* Claude **explains that browsing needs a destination or a category** — it does not pretend
  to be listing everything;
* it **asks** whether you want a specific country, a region, a global plan or a cruise plan;
* it may call `browse_home_catalog` to show which categories exist and how many
  destinations and regions there are;
* it **does not invent or dump fake bundles**, and does not silently substitute one
  country's list for "all".

**Fails if:** Claude says "here are all our bundles", lists plans without you having named a
destination or category, or produces plan names, prices or validities that no tool returned.

---

## 3. Verifying the values against the QA API

Tool results are summaries, so check a sample by hand.

1. Pick one plan Claude showed you and ask: *"what's its exact price and validity?"*
2. Call the same backend route yourself and compare. `X-Device-Id` may be any non-empty
   string for a manual check — the catalogue routes need no token:

   ```bash
   # countries (find the "id" GUID of the country you asked about)
   curl -s -H 'X-Device-Id: manual-qa' -H 'Accept-Language: en' \
     "$ESIM_API_BASE_URL/api/v1/bundles/countries" | jq '.data[] | select(.country=="France")'

   # that country's bundles, in the same currency Claude used
   curl -s -H 'X-Device-Id: manual-qa' -H 'Accept-Language: en' -H 'X-Currency: USD' \
     "$ESIM_API_BASE_URL/api/v1/bundles/by-country?country_codes=<the-guid>" \
     | jq '.data[] | {bundle_code, gprs_limit_display, validity_display, price_display}'

   # one bundle's details
   curl -s -H 'X-Device-Id: manual-qa' -H 'Accept-Language: en' -H 'X-Currency: USD' \
     "$ESIM_API_BASE_URL/api/v1/bundles/<bundle-code>" | jq '.data | {bundle_code, price_display}'
   ```

Check that:

* the **data allowance, validity and price** Claude reported match `gprs_limit_display`,
  `validity_display` and `price_display` exactly for the same currency;
* the **number of plans** Claude said were available matches the backend's list once
  inactive plans (`is_active: false`) are excluded;
* the **currency** matches the one you asked for (`X-Currency`);
* Claude's plans for a country came from that country's GUID — not from an ISO code and
  not from a different destination.

Note that the backend deduplicates bundles with the same allowance and validity, keeping
the cheapest, and sorts by price. So a country's list is already short and price-ordered
before this server narrows it further.

---

## 4. Result-size and context protection

| Check | Expected |
| --- | --- |
| Ask for plans without a limit | About five options, never dozens |
| `Show me 50 plans for France` | At most 20; Claude says there are more |
| `Show me the available countries` | A limited extract plus a total count, not the whole list |
| A plan covering many countries | A count plus a few example countries, not a wall of names |

---

## 5. Error scenarios

Each row is its own short conversation.

| # | You type | Expected |
| --- | --- | --- |
| 5.1 | `I need an eSIM for Atlantis` | Claude says it is not a destination the platform sells for and asks for another one. No invented plans. |
| 5.2 | `I need an eSIM for Fren` | Claude offers the close matches from the catalogue (e.g. French Guiana) and asks which you meant — it must not silently pick one. |
| 5.3 | `Plans for Congo` (if QA lists more than one) | Claude asks which one you mean. |
| 5.4 | `Show me plans for Scandinavia` | Claude says it is not a region it sells and names the real regions. |
| 5.5 | `A plan for France under $1` | Claude says nothing matches that budget and asks whether to relax it. It does not show a dearer plan as if it matched. |
| 5.6 | Point `ESIM_API_BASE_URL` at an unreachable host, restart, then `Plans for France` | Claude says the catalogue is temporarily unavailable and offers to try again. No URL, host name or trace. |
| 5.7 | Set `ESIM_MCP_READ_TIMEOUT=0.001`, restart, then `Plans for France` | Claude says the service did not respond in time. |
| 5.8 | `Tell me about plan 12345` | Claude does not invent a plan. It searches again or asks which of the options you meant. |

Restore your normal `.env` values after 5.6 and 5.7.

---

## 6. Login is never demanded for browsing

Run the whole of section 1 **before signing in**.

* Claude must complete every step without asking you to log in.
* `Am I logged in?` still answers "no".
* Signing in afterwards must not change the plans shown.

**Fails if:** Claude asks for an email address or a code before showing plans.

---

## 7. What must never happen (check throughout)

* No claim that a result is every bundle on the platform.
* No invented bundle code, price, data allowance, validity, country or availability — every
  value Claude states must be traceable to a tool result.
* No bundle code, country GUID, region GUID or device id read out to you.
* No unmasked email address or phone number — **including one you typed earlier in the same
  conversation**. After signing in, ask `what's my email?` and check that Claude answers
  with the masked form (`t***@example.com`), not the full address.
* No raw backend envelope (`status`/`totalCount`/`developerMessage`/`responseCode`), JSON
  or stack trace.
* No offer to buy, order, reserve, activate or top up anything — this version has no such
  tool.

---

## 8. Recording the result

Record pass/fail per scenario plus the tool Claude actually called. Phase 2 is signed off
for QA when sections 1–2 pass (including 2.9), section 3 shows values matching the QA API,
sections 4–6 pass, and no item in section 7 was observed.
