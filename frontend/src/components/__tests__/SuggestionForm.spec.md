# SuggestionForm — manual QA checklist

The bottom-right "Leave a suggestion" lightbulb widget (`SuggestionForm.tsx`).

Root cause of the original bug: the `send()` handler used a raw `fetch` whose
request never reached the backend (zero `/support/suggestion` hits in 48h).
The fix routes the POST through the shared `api` axios client
(`src/api/client.ts`) — same `baseURL`, auth interceptor, and CORS as every
other working call — and the backend now logs every received/forwarded/failed
suggestion.

Run while logged in (token in `localStorage.access_token`), production build
pointed at the real API.

## Success
- [ ] Click the lightbulb → panel opens.
- [ ] Pick a category (Feature/Bug/UX/Other) → selected chip highlights violet.
- [ ] Type ≥ 5 chars → "Send to Theta team" button enables.
- [ ] Click Send → button shows **"Sending..."** and is disabled.
- [ ] On success → green ✅ panel "Sent — thank you / We read every one." appears.
- [ ] Panel auto-closes after ~2.2s; textarea is cleared.
- [ ] Network tab: exactly one `POST /api/v1/support/suggestion` → **201**.
- [ ] Backend log shows `[suggestion] received from user=...` and
      `[suggestion] forwarded to admin inbox — provider_status=ok`.
- [ ] Owner inbox (theta.algos@yahoo.com) receives the `[Admin] ... suggestion` email.

## Loading / double-submit
- [ ] While "Sending...", the button cannot be clicked again (disabled).
- [ ] Rapid double-click produces only ONE network request.

## Error
- [ ] Simulate failure (offline, or backend 502): red error box shows the real
      reason (`e.response.data.detail` or message), NOT a silent no-op.
- [ ] Button re-enables after the error so the user can retry.
- [ ] `done` state never shows on error.

## Validation
- [ ] Message < 5 chars → button stays disabled; clicking Send (if forced) shows
      "Just a bit more — at least a few words." and sends nothing.
- [ ] Char counter updates as you type.

## Auth
- [ ] Logged out / expired token → axios 401 interceptor redirects to /login
      (token is cleared). No suggestion is silently dropped.
