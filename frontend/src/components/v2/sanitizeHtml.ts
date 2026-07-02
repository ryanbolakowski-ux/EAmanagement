/**
 * Conservative HTML sanitizer for rendering server-provided rich text
 * (strategy notes, announcements) via dangerouslySetInnerHTML.
 *
 * ── INTERIM SOLUTION ─────────────────────────────────────────────────────
 * Hand-rolled allowlist because new deps are frozen for the V2 track.
 * Once DOMPurify is approved as a dependency, replace the body of
 * sanitizeHtml() with DOMPurify.sanitize(dirty, {...}) and keep this
 * signature — callers shouldn't need to change.
 *
 * Strategy: parse with the browser's inert DOMParser (scripts never execute
 * during text/html parsing), then walk the detached tree:
 *   • tags NOT in ALLOWED_TAGS: dangerous ones (script/style/iframe/...)
 *     are removed subtree-and-all; unknown-but-benign wrappers are
 *     unwrapped so their text survives
 *   • attributes: dropped unless allowlisted for that tag, and every
 *     on* handler attribute is dropped unconditionally
 *   • href/src values are scheme-checked AFTER entity decoding, so tricks
 *     like "javascript&#58;" or "java\tscript:" are caught
 */

const ALLOWED_TAGS: ReadonlySet<string> = new Set([
  'a', 'abbr', 'b', 'blockquote', 'br', 'code', 'div', 'em',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'li', 'mark',
  'ol', 'p', 'pre', 's', 'small', 'span', 'strong', 'sub', 'sup',
  'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul',
])

/** Elements whose CONTENT is also dangerous — drop the whole subtree. */
const DROP_WITH_CHILDREN: ReadonlySet<string> = new Set([
  'script', 'style', 'iframe', 'frame', 'object', 'embed', 'form',
  'link', 'meta', 'base', 'noscript', 'svg', 'math', 'template',
  'video', 'audio', 'source',
])

/** Per-tag attribute allowlist; '*' applies to every allowed tag. */
const ALLOWED_ATTRS: Record<string, ReadonlySet<string>> = {
  '*': new Set(['title']),
  a: new Set(['href', 'title']),
  td: new Set(['colspan', 'rowspan']),
  th: new Set(['colspan', 'rowspan']),
}

const SAFE_SCHEME = /^(?:https?:|mailto:|tel:)/i
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:/i

function isSafeUrl(raw: string): boolean {
  // Strip the control/whitespace chars browsers tolerate inside schemes
  // ("java\tscript:" parses as javascript: — the check must too)
  const url = raw.replace(/[\u0000-\u0020]/g, '').toLowerCase()
  if (url === '') return false
  if (SAFE_SCHEME.test(url)) return true
  // No scheme at all → relative / fragment / query-only URLs are fine.
  // Anything with an unrecognized scheme (javascript:, data:, vbscript:,
  // blob:, ...) is rejected.
  return !HAS_SCHEME.test(url)
}

export function sanitizeHtml(dirty: string): string {
  if (!dirty) return ''
  // Non-browser environments (vitest without jsdom): strip every tag.
  // Over-aggressive but safe — this helper is for browser rendering.
  if (typeof window === 'undefined' || typeof DOMParser === 'undefined') {
    return dirty.replace(/<[^>]*>/g, '')
  }

  // DOMParser builds a DETACHED document: nothing executes while we work.
  const doc = new DOMParser().parseFromString(dirty, 'text/html')

  // Snapshot first — we mutate the tree while iterating.
  const elements = Array.from(doc.body.querySelectorAll('*'))
  for (const el of elements) {
    if (!el.isConnected) continue // ancestor was already dropped
    const tag = el.tagName.toLowerCase()

    if (DROP_WITH_CHILDREN.has(tag)) {
      el.remove()
      continue
    }
    if (!ALLOWED_TAGS.has(tag)) {
      // Benign-but-unknown wrapper (font, center, custom elements...):
      // keep the children, lose the element. The children are already in
      // the snapshot, so their own attribute pass still runs.
      el.replaceWith(...Array.from(el.childNodes))
      continue
    }

    // Attribute pass
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase()
      const allowed =
        ALLOWED_ATTRS[tag]?.has(name) || ALLOWED_ATTRS['*'].has(name)
      if (name.startsWith('on') || !allowed) {
        el.removeAttribute(attr.name)
        continue
      }
      if ((name === 'href' || name === 'src') && !isSafeUrl(attr.value)) {
        el.removeAttribute(attr.name)
      }
    }

    // Links that survived can't reach back into the opener
    if (tag === 'a' && el.hasAttribute('href')) {
      el.setAttribute('rel', 'noopener noreferrer')
    }
  }

  return doc.body.innerHTML
}

export default sanitizeHtml
