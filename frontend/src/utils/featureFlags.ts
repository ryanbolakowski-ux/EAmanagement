/**
 * Frontend feature flags.
 *
 * Read from Vite env vars at build time (VITE_*).  All flags default to OFF
 * so a missing env var is the safe choice in production.
 *
 * Pair each flag with a matching backend env var so the UI and the server
 * cannot disagree.  For ENABLE_AI_CHAT, the backend reads the same name.
 */

const _bool = (raw: string | undefined, fallback = false): boolean => {
  if (raw === undefined || raw === null) return fallback
  return String(raw).toLowerCase() === 'true'
}

/**
 * AI chat bubble (Theta Assistant).
 *
 * When false, the ChatBubble component is unmounted at the layout level AND
 * the /support/chat/status polling call is skipped inside the bubble.  This
 * means a user with devtools open cannot force the panel open and trigger
 * any Anthropic-API-backed request.  Re-enable by setting
 *   VITE_ENABLE_AI_CHAT=true  (Vercel)
 *   ENABLE_AI_CHAT=true       (backend .env)
 * and redeploying both sides.
 */
export const ENABLE_AI_CHAT = _bool(
  (import.meta as any).env?.VITE_ENABLE_AI_CHAT,
  false,
)
