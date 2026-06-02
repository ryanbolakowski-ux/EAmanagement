# Help.tsx — manual verification checklist

The Help page is the public FAQ that anyone (logged in or out) can browse.
This file lists the smoke-test cases a human should walk through before
shipping a change to the page.

## Render

- [ ] Visit `/help` while logged out — page renders, hero shows title,
      intro paragraph, search input, and the "X entries across Y categories"
      counter.
- [ ] Visit `/faq` — redirects (replace) to `/help` with no flash.
- [ ] Visit `/help` while logged in — same page renders (no auth wall).

## Search

- [ ] Type `KYC` — only KYC-related items remain visible, counter updates
      to "N of total entries match", and all matching items auto-open.
- [ ] Matched substrings are wrapped in `<mark>` and visually highlighted.
- [ ] Clearing the search box returns the full list and re-collapses items.
- [ ] Typing a non-matching string ("zzzzzz") shows the empty-state card
      with an `Email support` link.
- [ ] Search matches on question text, answer text (including JSX children),
      and category name — `Tradier`, `EOD close`, `Theta Scanner` all return
      sensible hits.

## Sidebar / category nav

- [ ] Desktop: sidebar is sticky on scroll. Clicking a category scrolls the
      main column to that section.
- [ ] When a search is active, sidebar categories with no matching items
      are dimmed (text-slate-400) so the user sees what was filtered out.
- [ ] Each category shows the raw entry count next to its name.
- [ ] Sidebar shows an "Email support" link below the category list.

## Mobile

- [ ] At < md, sidebar is hidden and a horizontal chip rail appears at the
      top with all categories. Tapping a chip scrolls to that section.
- [ ] Tapping the "Categories" button opens a stacked drawer listing every
      category vertically; tapping one collapses the drawer and scrolls.
- [ ] Accordions are full-width and tap-friendly.

## Accordion behavior

- [ ] Each item is closed by default.
- [ ] Clicking the question opens the item; clicking again closes it.
- [ ] Chevron icon rotates 180° when open.
- [ ] Multiple items can be open at once.
- [ ] `aria-expanded` reflects open state.

## Dark mode

- [ ] Toggle dark mode (system theme). Every section flips:
  - Hero gradient: `from-white to-slate-100` → `dark:from-slate-900 dark:to-slate-950`
  - Card bg: white → slate-900
  - Borders: slate-200 → slate-800
  - Primary text: slate-900 → slate-100
  - Secondary text: slate-700 → slate-200
  - Highlight `<mark>`: amber-200 → amber-700/60
  - CTA gradient remains violet-600 → indigo-700 with white text.
- [ ] No text/background pair has zero contrast in dark mode.

## Links

- [ ] Internal `<A>` links (to `/disclosures`, `/pricing`, `/privacy`, etc.)
      go to existing pages.
- [ ] External links (`mailto:support@thetaalgos.com`,
      `theocc.com/...`) open in a new tab with `rel="noopener noreferrer"`.

## Back-to-top

- [ ] Floating back-to-top button shows in the bottom-right at all scroll
      positions. Clicking smooth-scrolls to top.

## Chat-bubble interaction

- [ ] When `VITE_ENABLE_AI_CHAT` is unset (or `false`), the ChatBubble is
      NOT mounted on protected app pages — confirmed by checking the DOM
      for `[aria-label="Open Theta Assistant"]` on `/app`.
- [ ] When the chat is disabled, `/api/v1/support/chat/status` is NEVER
      requested (check the Network tab).
- [ ] The "The chat bubble isn't showing — why?" FAQ entry under
      Troubleshooting explains the disabled state to the user.

## Landing-page integration

- [ ] Landing page top nav shows `FAQ` link between `How It Works` and
      `Pricing`. Clicking takes you to `/help`.
- [ ] Landing hero shows the "Currently available in the United States
      only" chip below the existing top badge, with a tooltip "Why USA
      only? See the FAQ." and clicking takes you to `/help`.
- [ ] Footer shows `FAQ` link between `Pricing` and `Sign In`.

## In-app nav

- [ ] Desktop layout shows `Help & FAQ` link with a `HelpCircle` icon at the
      bottom of the left sidebar, above `Sign out`.
- [ ] Mobile drawer shows `Help & FAQ` link with a `HelpCircle` icon below
      `Profile`.

## Accessibility

- [ ] Search input has `<label class="sr-only">`.
- [ ] Accordion buttons have `aria-expanded`.
- [ ] Back-to-top button has `aria-label`.
- [ ] All interactive elements are keyboard-reachable (Tab order is
      sensible: search → sidebar → first accordion → next, etc.).
