// ─────────────────────────────────────────────────────────────────────────────
// Plan access & automation (Phase G)
//
// Mirrors the backend contract for GET /api/v1/account-signals/my-access and the
// broker-account list. Kept in its own module so components and the useMyAccess
// hook share one source of truth.
// ─────────────────────────────────────────────────────────────────────────────

export type AutomationStatus =
  | 'not_eligible'        // not tier_5
  | 'agreement_required'  // tier_5 but fully_automated_trading agreement not accepted
  | 'pending'             // agreement accepted, trading not yet enabled (needs verify + enable)
  | 'enabled'             // automation live (trading_enabled true)
  | 'disabled'            // eligible + agreed but trading turned off

export interface MyAccessAgreements {
  fully_automated_trading: boolean
  signals_disclosure_v2: boolean
}

export interface MyAccess {
  tier: string                       // 'free_trial'|'tier_2'|'tier_3'|'tier_4'|'tier_5' (maybe 'tier_1')
  fully_automated: boolean           // tier_5 only
  gets_signals: boolean              // tier_2/3/4
  requires_manual_approval: boolean  // tier_4
  can_place_on_approval: boolean     // tier_4 (place trade when user approves a signal)
  automation_status: AutomationStatus
  agreements: MyAccessAgreements
  has_broker_account: boolean
}

// Lite shape of BrokerAccountResponse — only the fields the activation flow
// needs. The index signature keeps the full server payload accessible (other
// pages read fields like is_demo / account_name off the same list response)
// without re-declaring the entire BrokerAccountResponse here.
export interface BrokerAccountLite {
  id: string
  trading_enabled: boolean
  sandbox_mode: boolean
  // Remaining BrokerAccountResponse fields (label, account_name, broker, …) are
  // reachable via the index signature as `any`, so other pages that read them
  // off this same list response keep compiling unchanged.
  [key: string]: any
}
