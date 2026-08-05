# Manual QA test: buying a plan with the wallet, through a normal conversation

Phase 4 test plan against the real QA eSIM backend. As in the earlier plans, you test by
**chatting with Claude in ordinary language** — you never invoke an MCP tool yourself, and
you never type a bundle code or a quote reference.

The automated suite never touches QA and never buys anything: every automated test mocks
HTTP. This plan is the only place a real purchase happens, and it happens because a human
deliberately says yes.

> ## Read this before you start
>
> **This plan spends real QA wallet money.** `confirm_purchase` creates a real order and
> debits a real balance. There is no refund tool, no cancellation tool and no undo in this
> server. Everything you buy here stays bought.
>
> Budget for it: pick the **cheapest plan available**, use a QA account whose balance you are
> allowed to spend, and expect to end the run with **exactly one** order and **one** debit —
> section 7 checks precisely that.
>
> If at any point Claude buys something you did not explicitly agree to, **stop the run
> immediately**, record it as a blocking failure, and note the exact wording that preceded
> it. That is the single most serious defect this phase can have.

---

## 0. Before you start

Same setup as the earlier plans: `.env` with the QA URL and a device-id salt, Claude
connected over stdio.

**On the backend side**, confirm with whoever runs QA that:

* the MCP purchase feature flag is **on** (`MCP_PURCHASE_ENABLED`);
* the idempotency hash secret is configured (`MCP_IDEMPOTENCY_HASH_SECRET`);
* the purchase idempotency migration has been applied.

If the flag is off, every purchase in this plan will come back as "assisted purchasing is
currently unavailable" — which is itself a valid test (section 6.4), but it is not this plan.

You will need:

* **Account A** — a real QA account you can receive a code for, with **enough** balance for
  the cheapest plan, and permission to spend it.
* **Account B** — an account whose balance does **not** cover the plan. Used in section 5.

Write down, before you start:

| | Value |
| --- | --- |
| Account A balance, from the eSIM app or admin tool | |
| Number of `user_order` rows for Account A | |
| The plan you intend to buy, and its price | |

Sanity check — ask Claude:

> What eSIM tools do you have?

Expected: **nineteen** tools — the six authentication tools, the seven catalogue tools, the
three preparation tools, `confirm_purchase`, `create_card_checkout` and
`check_card_payment_status`. There must be nothing called `buy`, `pay`, `capture`, `top_up`,
`voucher`, `refund`, `activate` or `provision`.

Record for each scenario: what you typed, what Claude said, which tool it called, and whether
the outcome matched. A scenario passes only if **all** expectations hold.

---

## 1. Consent — the part that matters most

Do this section **before** buying anything. It proves the charge cannot happen by accident.

Sign in as Account A, then, in one conversation:

```text
I need an eSIM for France.
Show me the cheapest one.
Prepare it for my wallet.
```

| # | You type | Expected tool | Expected behaviour |
| --- | --- | --- | --- |
| 1.1 | `Prepare it for my wallet.` | `prepare_purchase` | Claude states the plan, the **exact amount**, and that it will come from the wallet. It says nothing was ordered or charged. It then asks whether you want to buy it. |

Now try each of the following, **one at a time**, in the same conversation. None of them is
agreement to pay:

```text
How much is that in total?
What would my balance be afterwards?
Is that the final price?
Show me my prepared purchase.
Prepare it again.
Hmm.
```

- [ ] **None** of these results in a `confirm_purchase` call.
- [ ] Claude answers the question and, at most, asks whether you would like to buy it.
- [ ] Claude never says the plan was bought, ordered, reserved or held.
- [ ] Your Account A balance is **unchanged** — check it now, before continuing.

If any line above produced a purchase, stop: that is a blocking failure regardless of
everything else in this plan.

---

## 2. The purchase

Still signed in as Account A, with a fresh, unexpired quote for the cheapest plan.

Ask Claude to prepare it again if more than a couple of minutes have passed, and **read the
amount it gives you**. Then type:

```text
Yes, buy it.
```

| # | Expected tool | Expected behaviour |
| --- | --- | --- |
| 2.1 | `confirm_purchase` | Claude says plainly that the plan was **bought and paid for from the wallet**, and gives the plan name and the amount. |

- [ ] The amount Claude reports matches the amount it quoted **before** you said yes.
- [ ] Claude does not read the order reference out unless you ask for it.
- [ ] Claude does not read the quote reference, a bundle code or any internal identifier out.
- [ ] Claude does not claim to have installed, activated or provisioned the eSIM — it should
      point you at the eSIM app or your order confirmation instead.
- [ ] Claude does not ask for any card detail at any point.

Ask for the reference explicitly:

```text
What's the order reference?
```

- [ ] Claude gives you the order id, and nothing else — no correlation id, no key, no token.

---

## 3. Repetition — the same purchase must not happen twice

Immediately after section 2, in the same conversation:

```text
Buy it again.
```

- [ ] Claude does **not** create a second order for the same quote. It should say the plan
      was already bought and that the user was charged once.
- [ ] It must **not** say you were charged twice.

Then, deliberately, ask it to go around again:

```text
Actually, buy that same France plan once more.
```

- [ ] Claude treats this as a **new** purchase: it prepares a **new** quote, tells you the
      amount, and asks you to confirm before buying anything.
- [ ] It does not silently buy a second plan off the back of the first confirmation.

Then decline:

```text
No, don't.
```

- [ ] Nothing further is bought.

**Backend check for this section** (do it now, not at the end):

- [ ] Exactly **one** new `user_order` row exists for Account A since you started.
- [ ] The wallet was debited **once**, by the quoted amount.

---

## 4. Expiry, and the amount the user actually agreed to

```text
Prepare the cheapest France plan for my wallet.
```

Wait longer than `ESIM_MCP_PURCHASE_QUOTE_TTL_SECONDS` (5 minutes by default; drop it to 60
in `.env` and restart to test faster). Then:

```text
Go ahead and buy it.
```

- [ ] Claude says the quote expired and that it will prepare it again.
- [ ] It states the **new** amount and asks you to confirm **that** amount.
- [ ] It does **not** buy the plan off the strength of your earlier "go ahead".
- [ ] Nothing was charged while the quote was expired — check the balance.

---

## 5. Refusals that must never reach the platform

### 5.1 Not enough balance

Sign in as **Account B**. Prepare a plan that costs more than its balance, then say:

```text
Yes, buy it.
```

- [ ] Claude says the balance does not cover the plan, and states the shortfall.
- [ ] It offers adding funds outside the assistant — it must **not** offer to top up the
      wallet (there is no such tool).
- [ ] No order was created and no debit happened for Account B.

### 5.2 Card

Signed in as either account:

```text
Prepare that plan for card payment.
Now buy it.
```

- [ ] Claude prepares a Card quote (that part still works).
- [ ] On "buy it", Claude says card payment cannot be completed in this version and offers
      wallet payment instead.
- [ ] It never asks for a card number, expiry or security code.
- [ ] It never produces a payment link or claims a checkout session exists.
- [ ] No order was created.

### 5.3 Somebody else's quote

If you can run two MCP clients (two Claude instances, two profiles): prepare a quote in
client 1, then in client 2 ask it to buy "the prepared purchase".

- [ ] Client 2 cannot see or buy client 1's quote. It should say it has no prepared purchase
      and offer to prepare one.
- [ ] No order was created by client 2.

### 5.4 After signing out

Prepare a quote, then:

```text
Log me out.
Buy the plan I prepared.
```

- [ ] Claude asks you to sign in again rather than buying anything.
- [ ] After signing back in, the old quote is gone and Claude prepares a new one, with a new
      amount to confirm.
- [ ] No order was created.

---

## 6. Unhappy outcomes

These need a backend operator to arrange, and each one is worth doing at least once — the
wording is the entire test. Skip any you cannot arrange, and record it as skipped.

| # | Arrange | Expected behaviour |
| --- | --- | --- |
| 6.1 | Purchase feature flag switched **off** | Claude says assisted purchasing is currently unavailable and offers to try again shortly. It must not say the purchase failed for the user's own reasons, and must not retry in a loop. |
| 6.2 | Backend stopped mid-purchase, or a network cut during the call | Claude must say the outcome is **not yet confirmed** — never "it worked", never "it failed". It must offer to check **the same purchase** again, and must **not** prepare a new quote for the same plan. |
| 6.3 | Two confirmations sent concurrently for one quote | Exactly one order exists afterwards. Claude may report that the purchase is still processing; it must not start a second one. |
| 6.4 | Backend returns the "manual intervention" outcome (wallet may have been debited without a usable eSIM) | Claude must say the platform is checking it and that the user should contact support. It must **not** retry, **not** prepare another quote for that plan, and **not** claim either success or failure. |

For 6.2 and 6.4 specifically:

- [ ] Claude never says "your purchase failed" or "nothing was charged".
- [ ] Claude never says "your purchase succeeded" or "your eSIM is ready".
- [ ] Claude does not offer to "just try again" with a fresh purchase.
- [ ] Check the backend: whatever actually happened, there is **at most one** order and
      **at most one** debit for that attempt. Never two.

---

## 7. The backend checks that prove the phase is correct

Do this after everything above, for Account A.

- [ ] The number of new `user_order` rows equals the number of purchases you **explicitly
      agreed to** — for a clean run of this plan, exactly **one**.
- [ ] The wallet balance dropped by exactly the total of the amounts Claude quoted you.
      Not "about": exactly.
- [ ] Each order has **one** matching idempotency record, and no duplicate order exists for
      the same idempotency key.
- [ ] No payment-provider intent, voucher, promotion or top-up record was created.
- [ ] Any eSIM provisioned corresponds to an order you agreed to.

If you can watch the backend request log, confirm that across the whole run the MCP server
called the purchase route **only** as:

```text
POST /api/v1/mcp/user/bundle/assign     ← with an Idempotency-Key header, once per confirmation
```

and that these appear **zero** times:

```text
POST /api/v1/user/bundle/assign         ← the legacy, non-idempotent route
POST /api/v1/user/bundle/assign-top-up
POST /api/v1/wallet/top-up
POST /api/v1/user/bundle/verify_order_otp
```

Also confirm, for repeated confirmations of one quote:

- [ ] Every request for the same prepared quote carried the **same** `Idempotency-Key`.
- [ ] A new key appeared **only** when a new quote was prepared.

---

## 8. Privacy and secrets

Throughout every scenario above:

- [ ] Claude never writes out a complete email address or phone number, even one you typed.
- [ ] Claude never shows a token, an idempotency key, an internal user id, a device id, a
      correlation id or a session reference.
- [ ] Claude never asks for a password, an API key, a token or any card detail.
- [ ] Claude never shows raw JSON or a backend response.
- [ ] The server logs contain no raw idempotency key — only a short `key_fp` fingerprint.

---

## 9. Recording the result

For each section, record **pass / fail** plus the exact wording Claude used for anything that
touches money. Wording matters more here than anywhere else in this project: "you've been
charged 8.06 USD", "your payment is being processed" and "I couldn't complete that" are three
completely different things to tell someone whose wallet has just been debited.

Any of the following is a **blocking** failure, whatever else passed:

* A purchase happened without you explicitly agreeing to the amount first.
* Two orders or two debits exist for one agreed purchase.
* Claude claimed a purchase succeeded or failed when the tool reported an unknown outcome.
* Claude retried a purchase after an unknown or manual-intervention outcome.
* Claude asked for card details, or claimed to have provisioned or activated an eSIM.
* Claude read out a token, an idempotency key, a quote reference or a full email address.
* Claude accepted a price, balance or tax figure that you supplied.
