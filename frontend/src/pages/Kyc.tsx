import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { ShieldCheck, Loader2, CheckCircle2, AlertTriangle, XCircle, RefreshCw } from "lucide-react"
import api from "../api/client" /* kyc-frontend-patch-v2 */

const MIN_AGE = 18
// Latest acceptable DOB for an 18-year-old: today minus 18 years.
const _maxDob = (() => {
  const d = new Date(); d.setFullYear(d.getFullYear() - MIN_AGE)
  return d.toISOString().slice(0, 10)
})()
const _ageFromDob = (s: string): number | null => {
  if (!s) return null
  const [y, m, d] = s.split("-").map(Number)
  if (!y || !m || !d) return null
  const today = new Date()
  let age = today.getFullYear() - y
  const beforeBday = today.getMonth() + 1 < m || (today.getMonth() + 1 === m && today.getDate() < d)
  if (beforeBday) age -= 1
  return age
}

type Status = "not_started" | "pending" | "verified" | "failed" | "requires_input" | "canceled" | "manual_review" | "stub" | null

type KycStatusResponse = { status: Status; verification_url?: string | null; verified_at?: string | null }

export default function Kyc() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [firstName, setFirstName] = useState("")
  const [lastName, setLastName] = useState("")
  const [dob, setDob] = useState("")
  const [country, setCountry] = useState("US")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")

  // Poll while we're in a non-terminal state so the page self-updates as soon
  // as the Stripe Identity webhook fires (or as soon as the opportunistic
  // /status sync pulls the verified state from Stripe).
  const { data: kycData, refetch, isFetching } = useQuery<KycStatusResponse>({
    queryKey: ["kyc-status"],
    queryFn: async () => {
      try {
        const r = await api.get("/api/v1/kyc/status")
        console.debug("[kyc] status fetched:", r.data?.status)
        return r.data as KycStatusResponse
      } catch (e: any) {
        if (e?.response?.status === 401) { window.location.href = "/login" }
        throw e
      }
    },
    refetchInterval: (q) => {
      const s = (q.state.data as KycStatusResponse | undefined)?.status
      return s === "pending" || s === "requires_input" ? 10000 : false
    },
    refetchOnWindowFocus: true,
    retry: 1,
  })
  const status: Status = (kycData?.status as Status) || null
  const verificationUrl = kycData?.verification_url || null

  const startVerification = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!firstName || !lastName || !dob) {
      setError("Please fill in your legal name and date of birth as they appear on your ID.")
      return
    }
    const age = _ageFromDob(dob)
    if (age === null) {
      setError("Please enter a valid date of birth.")
      return
    }
    if (age < MIN_AGE) {
      setError(`You must be at least ${MIN_AGE} years old to use Theta Algos. US derivatives and securities regulations prohibit minors from trading on this platform.`)
      return
    }
    setLoading(true); setError("")
    try {
      const { data } = await api.post("/api/v1/kyc/start", {
        first_name: firstName, last_name: lastName, date_of_birth: dob, country_code: country,
      })
      // Trigger an immediate refetch so the UI catches state changes as soon
      // as they happen (e.g. manual_review fallback returned by the backend).
      qc.invalidateQueries({ queryKey: ["kyc-status"] })
      if (data?.redirect_url) {
        window.location.href = data.redirect_url
        return
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || "Could not start verification. Please try again.")
    } finally { setLoading(false) }
  }

  if (status === null) return <div className="min-h-screen flex items-center justify-center"><Loader2 className="animate-spin" /></div>

  if (status === "verified") {
    return (
      <div className="min-h-screen flex items-center justify-center px-6">
        <div className="max-w-md text-center">
          <CheckCircle2 size={56} className="mx-auto text-green-500 mb-4" />
          <h1 className="text-2xl font-bold mb-2 text-slate-900 dark:text-slate-100">Identity verified</h1>
          <p className="text-slate-600 dark:text-slate-400 mb-6">You can now access live trading and paid features.</p>
          <button onClick={() => navigate("/app")} className="bg-blue-600 hover:bg-blue-700 text-white font-semibold px-6 py-2.5 rounded-lg">
            Go to dashboard
          </button>
        </div>
      </div>
    )
  }

  if (status === "pending" || status === "stub" || status === "manual_review") {
    const isManual = status === "manual_review"
    return (
      <div className="min-h-screen flex items-center justify-center px-6">
        <div className="max-w-md text-center">
          <Loader2 size={56} className="mx-auto text-blue-500 animate-spin mb-4" />
          <h1 className="text-2xl font-bold mb-2 text-slate-900 dark:text-slate-100">
            {isManual ? "Manual review pending" : "Verification in progress"}
          </h1>
          <p className="text-slate-600 dark:text-slate-400 mb-6">
            {isManual
              ? "Your information has been submitted. Our team will review and approve your identity within 1 business day."
              : "We are reviewing your ID. This usually takes under 60 seconds. You will receive an email when it is complete."}
          </p>
          <div className="flex items-center justify-center gap-3">
            <button onClick={() => navigate("/app")} className="text-blue-600 underline">Back to dashboard</button>
            <button
              type="button"
              onClick={() => refetch()}
              disabled={isFetching}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 border border-slate-300 dark:border-slate-700 rounded-md text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50"
            >
              <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
              Refresh status
            </button>
          </div>
        </div>
      </div>
    )
  }

  if (status === "failed" || status === "canceled") {
    const wasCanceled = status === "canceled"
    return (
      <div className="min-h-screen flex items-center justify-center px-6">
        <div className="max-w-md text-center">
          <XCircle size={56} className="mx-auto text-red-500 mb-4" />
          <h1 className="text-2xl font-bold mb-2 text-slate-900 dark:text-slate-100">
            {wasCanceled ? "Verification canceled" : "Verification did not pass"}
          </h1>
          <p className="text-slate-600 dark:text-slate-400 mb-6">
            {wasCanceled
              ? "You exited the Stripe Identity flow before completing it. You can start again at any time."
              : "Stripe Identity could not confirm your ID. Common causes: blurry photo, mismatched name, or a non-US document. Please try again with a clear, well-lit photo of your government-issued US ID."}
          </p>
          <div className="flex items-center justify-center gap-3">
            <button
              type="button"
              onClick={() => {
                // Drop the cached status so the form re-renders, allowing retry.
                qc.setQueryData(["kyc-status"], { status: "not_started" })
              }}
              className="bg-blue-600 hover:bg-blue-700 text-white font-semibold px-6 py-2.5 rounded-lg"
            >
              Try again
            </button>
            <button onClick={() => navigate("/app")} className="text-blue-600 underline">Back to dashboard</button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950 px-6 py-10 flex flex-col items-center">
      <div className="max-w-md w-full">
        <div className="flex items-center gap-3 mb-2">
          <ShieldCheck size={28} className="text-blue-600" />
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100">Verify your identity</h1>
        </div>
        <p className="text-sm text-slate-600 dark:text-slate-400 mb-6 leading-relaxed">
          US trading regulations (CFTC / SEC / FINRA) require Theta Algos to verify the identity of every user
          before enabling live broker connectivity. We use Stripe Identity (SOC 2 Type II certified) to scan
          your government-issued ID and take a selfie. The whole process takes about 90 seconds.
        </p>

        {status === "requires_input" && (
          <div className="bg-amber-50 border border-amber-200 text-amber-800 px-4 py-3 rounded-lg mb-4">
            <div className="flex items-start gap-2 mb-2">
              <AlertTriangle size={18} className="mt-0.5 flex-shrink-0" />
              <span className="text-sm">Your previous attempt was incomplete. You can continue where you left off, or start over below.</span>
            </div>
            {verificationUrl && (
              <a
                href={verificationUrl}
                className="inline-block bg-amber-600 hover:bg-amber-700 text-white text-sm font-semibold px-4 py-1.5 rounded-md"
              >
                Continue verification
              </a>
            )}
          </div>
        )}

        <form onSubmit={startVerification} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1.5">Legal first name</label>
              <input value={firstName} onChange={e => setFirstName(e.target.value)} required
                className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1.5">Legal last name</label>
              <input value={lastName} onChange={e => setLastName(e.target.value)} required
                className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1.5">Date of birth</label>
            <input type="date" value={dob} onChange={e => setDob(e.target.value)} required max={_maxDob}
              className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 rounded-lg px-3 py-2 text-sm" />
            <p className="text-xs text-slate-400 mt-1">Must match the date on your government ID. You must be at least {MIN_AGE} years old to use Theta Algos.</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1.5">Country of residence</label>
            <select value={country} onChange={e => setCountry(e.target.value)}
              className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 rounded-lg px-3 py-2 text-sm">
              <option value="US">United States</option>
            </select>
            <p className="text-xs text-slate-400 mt-1">Theta Algos is currently available to US residents only.</p>
          </div>

          {error && <div className="bg-red-50 border border-red-200 text-red-700 text-sm px-3 py-2 rounded-lg">{error}</div>}

          <button type="submit" disabled={loading}
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-semibold py-2.5 rounded-lg text-sm">
            {loading ? "Opening Stripe Identity..." : "Continue to ID verification"}
          </button>

          <p className="text-[11px] text-slate-400 text-center leading-relaxed">
            By continuing you agree to Stripe Identity processing your ID and selfie photo. We never store
            your raw ID images.
          </p>
        </form>
      </div>
    </div>
  )
}
