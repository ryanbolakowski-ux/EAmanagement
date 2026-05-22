import { Link } from "react-router-dom"
import { useEffect, useState } from "react"
import { Globe, ShieldAlert, Server } from "lucide-react"
import api from "../api/client"
import ThetaLogo from "../components/ThetaLogo"

type GeoStatus = {
  ip?: string; country?: string | null; allowed?: boolean
  is_vpn?: boolean; is_proxy?: boolean; is_tor?: boolean
  is_datacenter?: boolean; fraud_score?: number
}

export default function NotAvailable() {
  const [geo, setGeo] = useState<GeoStatus>({})
  useEffect(() => {
    api.get("/api/v1/geo/status").then(r => setGeo(r.data || {})).catch(() => {})
  }, [])

  const reason: { icon: any; title: string; body: string } = (() => {
    if (geo.is_vpn || geo.is_proxy || geo.is_tor) {
      const what = geo.is_tor ? "Tor network" : geo.is_vpn ? "VPN" : "proxy server"
      return {
        icon: ShieldAlert,
        title: `Please disable your ${what}`,
        body: `We detected a ${what} connection. US trading compliance requires us to verify your physical location, which we can't do through a ${what}. Disconnect it and reload this page — if you're a US resident on a residential connection, you'll be let through immediately.`,
      }
    }
    if (geo.is_datacenter) {
      return {
        icon: Server,
        title: "Datacenter IP detected",
        body: "Theta Algos cannot be accessed from datacenter or hosting IPs. Please connect from a residential or mobile network.",
      }
    }
    return {
      icon: Globe,
      title: "Theta Algos is not available in your country",
      body: `Theta Algos LLC is a US-based algorithmic-trading platform. We are licensed and regulated to operate only with United States residents. Access from ${geo.country || "your country"} is not currently supported.`,
    }
  })()
  const Icon = reason.icon

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950 flex flex-col items-center justify-center px-6">
      <div className="max-w-lg text-center">
        <Link to="/" className="inline-flex items-center gap-2 mb-8 justify-center">
          <ThetaLogo size={48} />
          <span className="text-lg font-extrabold tracking-[0.18em] text-slate-900 dark:text-slate-100">THETA ALGOS</span>
        </Link>
        <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-amber-100 dark:bg-amber-900/30 mb-5">
          <Icon size={32} className="text-amber-600 dark:text-amber-400" />
        </div>
        <h1 className="text-2xl md:text-3xl font-extrabold text-slate-900 dark:text-slate-100 mb-3">
          {reason.title}
        </h1>
        <p className="text-slate-600 dark:text-slate-400 mb-6 leading-relaxed">
          {reason.body}
        </p>
        <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-5 text-left text-sm text-slate-600 dark:text-slate-300 space-y-2">
          <p><strong>Why?</strong> US derivatives and securities regulations (CFTC, FINRA, SEC) require us to verify every user is physically located in the United States and to identify them before providing algorithmic-trading software.</p>
          <p>If you are a US resident and believe this is in error, please contact <a href="mailto:support@thetaalgos.com" className="text-blue-600 underline">support@thetaalgos.com</a>.</p>
        </div>
        <div className="mt-6 text-xs text-slate-400 dark:text-slate-500 grid grid-cols-2 gap-2 max-w-sm mx-auto">
          <div>Country: <span className="font-mono">{geo.country || "?"}</span></div>
          <div>IP: <span className="font-mono">{geo.ip || "?"}</span></div>
          {geo.is_vpn && <div className="col-span-2">⚠ VPN detected</div>}
          {geo.is_proxy && <div className="col-span-2">⚠ Proxy detected</div>}
          {geo.is_tor && <div className="col-span-2">⚠ Tor detected</div>}
          {geo.is_datacenter && <div className="col-span-2">⚠ Datacenter IP</div>}
          {geo.fraud_score !== undefined && <div className="col-span-2">Fraud score: {geo.fraud_score}/100</div>}
        </div>
      </div>
    </div>
  )
}
