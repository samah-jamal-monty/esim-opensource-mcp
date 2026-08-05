# Manual QA test: eSIM authentication through a normal conversation

This is a **manual** test plan against the real QA eSIM backend. You test it by chatting
with Claude in ordinary language — you never invoke an MCP tool yourself. Claude decides
which tool to call from the server instructions and the tool descriptions.

The automated test suite never touches QA; every automated test mocks HTTP.

---

## 0. Before you start

You need:

* the QA backend base URL (goes in your local `.env`, never in git);
* a QA email inbox you can read, and/or a QA phone number that can receive SMS;
* the project set up (`pip install -e ".[dev]"`) and Claude connected over stdio
  (see "Claude connection" in `README.md`).

Create `.env` in the project root — it is git-ignored:

```env
ESIM_API_BASE_URL=https://qa-placeholder.example.com
ESIM_MCP_ENVIRONMENT=development
ESIM_MCP_TRANSPORT=stdio
ESIM_MCP_DEVICE_ID_SALT=replace-with-a-long-random-local-secret
ESIM_MCP_DEFAULT_LOCALE=en
ESIM_MCP_DEFAULT_CURRENCY=USD
```

Replace both placeholders with the real QA URL and a long random local secret. The MCP
server needs **only** that public backend base URL — no Supabase, Stripe, eSIM Hub or
database credentials are used, requested or accepted anywhere in this project.

Sanity check before chatting — ask Claude:

> What eSIM tools do you have?

Expected: the six authentication tools — `request_login_otp`, `resend_login_otp`,
`verify_login_otp`, `get_login_status`, `get_user_profile`, `logout` — plus the seven
read-only catalogue tools added in Phase 2 (see
[`QA_CATALOGUE_TEST.md`](QA_CATALOGUE_TEST.md)). Nothing about orders, payments,
top-ups or activation.

Record for each scenario: what you typed, what Claude said, which tool it called, and
whether the outcome matched. A scenario passes only if **all** expectations hold.

---

## 1. Successful email login

**You type:**

```text
I want to log in using my email.
```

**Expected:**

1. Claude asks which email address to use (it must not invent one).

**You type:** your QA email address.

2. Claude calls `request_login_otp` **once**.
3. Claude tells you a code was sent — to a masked destination such as `m***@example.com` —
   and asks for the six-digit code. It must **not** say you are logged in yet.

**You type:** the six-digit code from the inbox.

4. Claude calls `verify_login_otp`.
5. Claude confirms you are signed in.

**You type:**

```text
Show my profile.
```

6. Claude calls `get_user_profile` and reports name, masked contact details and wallet
   balance.

**You type:**

```text
What's my email address?
```

7. Claude answers with the **masked** form (`t***@example.com`) — even though you typed the
   full address a moment ago. This is the Phase 1 QA finding that Phase 2 fixes: the tools
   only ever return masked values, and the instructions now forbid restoring the full one
   from the conversation.

**Fails if:** Claude asks you for a token or password; shows raw JSON, headers or backend
messages; prints a full email address, phone number or user id **at any point, including
one you typed yourself**; claims success before `verify_login_otp` returns; or calls
`request_login_otp` more than once.

---

## 2. Successful phone login

Same flow with a QA phone number in international format (`+CCXXXXXXXXX`).

**You type:**

```text
Log me in with my phone number instead.
```

**Expected:** Claude asks for the number, calls `request_login_otp` with `phone` (channel
defaults to `SMS`), tells you an SMS was sent to a masked number such as `+961******67`,
asks for the code, then calls `verify_login_otp` and confirms.

If your QA tenant is configured for email login only, the backend will answer with an
error — record that as an environment limitation, not a client bug.

---

## 3. Status and logout

While signed in, type each line and check the tool Claude reaches for:

| You type | Expected tool | Expected reply |
| --- | --- | --- |
| `Am I logged in?` | `get_login_status` | Yes, signed in (masked identity, expiry) |
| `Show my profile.` | `get_user_profile` | Masked profile and balance |
| `Log me out.` | `logout` | Confirms you are signed out |
| `Am I still logged in?` | `get_login_status` | No, not signed in |

**Fails if:** Claude answers "am I logged in?" from memory instead of calling
`get_login_status`, or logs you out without being asked.

---

## 4. Error scenarios

Each row is its own short conversation. The point is that Claude relays a plain-language
explanation and takes the right next step — never a stack trace, backend message or retry
loop.

| # | Setup | You type | Expected |
| --- | --- | --- | --- |
| 4.1 | Login requested | a wrong six-digit code | Claude calls `verify_login_otp` **once**, says the code was not valid, asks you to check it. It must **not** auto-resend and must **not** retry. |
| 4.2 | Request a code, wait past its lifetime (see the platform's OTP expiry), then use it | the expired code | Claude reports the code expired and offers to send a new one — only sending after you agree. |
| 4.3 | Login requested | `I didn't get the code, resend it` | Claude calls `resend_login_otp` once. If the platform says a code is still active or the limit is reached, Claude relays that and waits — no repeat call. |
| 4.4 | Signed out | `Show my profile` | Claude notices there is no session (`get_login_status` or an `authentication_required` result) and starts the login flow instead of asking for credentials. |
| 4.5 | Fresh chat | `Log me in` | Claude asks which email or phone to use. It must not guess one or call the tool with an empty argument. |
| 4.6 | Point `ESIM_API_BASE_URL` at an unreachable host and restart the server | `Log me in with my email` → give the address | Claude reports the eSIM service is unavailable right now. No URL, host name or trace is shown. |
| 4.7 | Set `ESIM_MCP_READ_TIMEOUT=0.001` and restart | same as 4.6 | Claude reports the service did not respond in time; it does not retry the OTP request. |
| 4.8 | Request codes repeatedly until QA rate-limits you | `Send me another code` | Claude relays the rate limit ("a code is still active" / "too many requests") and tells you to wait. No retry loop. |
| 4.9 | Sign in, then restart the MCP server (see "Disconnecting" in `README.md`) | `Am I still logged in?` | Not signed in. Sessions are in-memory in this phase, so a restart clears them — expected, and the reason production needs the encrypted Redis store. |

After 4.6 and 4.7, restore your normal `.env` values and restart before continuing.

---

## 5. Multi-user isolation

The rule: a session belongs to a **verified MCP client identity**, never to an email
address. Two identities must never see each other's session.

### 5.1 What stdio can and cannot show

**Honest limitation:** stdio has no transport-level principal. The MCP server process *is*
the trust boundary, so every caller of one stdio process is one user, and the development
identity provider supplies a single stable local identity. **You cannot represent two
verified production clients over stdio.**

What you *can* verify over stdio, which is still worth doing:

1. Sign in as user A and note the masked profile.
2. Stop the server, change `ESIM_MCP_DEV_CLIENT_ID` to a different value, restart, and sign
   in as user B.
3. Restore `ESIM_MCP_DEV_CLIENT_ID` to A's value and restart. Ask `Am I logged in?`

Because the store is in-memory, restarting clears both sessions — so this checks that the
identity **key** changes (each dev identity derives its own device id and session key), not
that two sessions coexist. Confirm in the server's stderr log that the two runs report
different `session_ref` values, and that the two logins sent different `X-Device-Id`
values.

The coexistence and isolation properties themselves are covered by automated tests
(`tests/test_session_manager.py`, `tests/test_auth_tools.py`): user A cannot read or
overwrite user B's session, each gets a different device id, and logging out A leaves B
signed in.

### 5.2 The real two-client test (Streamable HTTP + OAuth)

To test two genuinely verified clients end to end you need the deployed transport:

1. Run the server with `ESIM_MCP_TRANSPORT=streamable-http` (endpoint `/mcp`).
2. Configure the SDK's OAuth verification on the server —
   `MCPServer(auth=AuthSettings(...), token_verifier=...)` in `src/esim_mcp/server.py`.
   **This is not wired up yet** and is the next implementation step; without it the
   transport has no verified principal, and in `production` the server correctly refuses
   to open any session at all.
3. Connect two MCP clients with **different** OAuth principals (different `client_id`, or
   different `subject` under one client).
4. Sign in as a different QA user from each.

Expected once step 2 exists:

* A's `get_login_status` / `get_user_profile` show only A's data; B's show only B's.
* The two clients send different `X-Device-Id` values.
* `logout` from A leaves B signed in.
* Neither client can name, borrow or address the other's session — there is no tool
  argument that selects a session.

---

## 6. What must never happen (check throughout)

* No access token, refresh token, JWT or OTP appears in a Claude message.
* No unmasked email address, phone number, device id or session id appears.
* No raw backend envelope (`status`/`totalCount`/`developerMessage`/`responseCode`) or
  stack trace is shown.
* Claude never asks you to run a tool yourself, and never asks for a token or password.
* Claude never claims a login, logout or profile read succeeded without a tool result
  saying so.
* No plan, order, payment or activation is offered — this version has no such tool.

Inspect the server's own log on stderr (see `README.md`) for the same guarantees: secrets
are redacted before any record is written.

---

## 7. Recording the result

For each scenario record pass/fail plus the tool Claude actually called. Authentication is
signed off for QA when sections 1, 3 and 4 pass, section 2 passes or is documented as an
environment limitation, and section 5.1 is confirmed with 5.2 noted as pending the OAuth
verifier.
