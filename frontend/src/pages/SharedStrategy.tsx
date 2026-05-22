import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowLeft, AlertCircle, CheckCircle2, Sparkles } from 'lucide-react'
import { strategiesApi } from '../api/endpoints'
import { useAuthStore } from '../stores/authStore'
import ThetaLogo from '../components/ThetaLogo'

export default function SharedStrategy() {
  const { token } = useParams<{ token: string }>()
  const navigate = useNavigate()
  const { isAuthenticated } = useAuthStore()

  const { data, isLoading, error } = useQuery({
    queryKey: ['shared-strategy', token],
    queryFn: () => strategiesApi.previewShared(token!).then(r => r.data),
    enabled: !!token,
    retry: false,
  })

  const importMutation = useMutation({
    mutationFn: () => strategiesApi.importShared(token!),
    onSuccess: () => setTimeout(() => navigate('/app/strategies'), 1200),
  })

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950 px-4 sm:px-6 py-8">
      <div className="max-w-2xl mx-auto space-y-5">
        <Link to="/" className="inline-flex items-center gap-2 mb-2">
          <ThetaLogo size={32}/>
          <span className="text-base font-extrabold tracking-[0.18em] text-slate-900 dark:text-slate-100">THETA ALGOS</span>
        </Link>

        {isLoading && (
          <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-8 text-center text-sm text-slate-500">
            Loading shared strategy…
          </div>
        )}

        {error && (
          <div className="rounded-2xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-900/20 p-5 flex items-start gap-3">
            <AlertCircle size={18} className="text-red-600 dark:text-red-400 flex-shrink-0 mt-0.5"/>
            <div>
              <div className="font-bold text-red-900 dark:text-red-200 mb-1">Link not found</div>
              <p className="text-sm text-red-800 dark:text-red-300">This share link is invalid or has been revoked. Ask the sender for a new one.</p>
            </div>
          </div>
        )}

        {data && (
          <>
            {/* Sharer attribution banner */}
            <div className="rounded-2xl bg-gradient-to-br from-violet-600 to-indigo-700 text-white p-5">
              <div className="flex items-center gap-2 mb-2 text-xs opacity-80">
                <Sparkles size={14}/> Strategy shared with you
              </div>
              <h1 className="text-2xl font-extrabold mb-1">{data.name}</h1>
              {data.shared_by_username && (
                <div className="text-sm opacity-90">
                  From <strong>@{data.shared_by_username}</strong>
                </div>
              )}
            </div>

            {/* Strategy details grid */}
            <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-5">
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 text-sm">
                <div><div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Instruments</div><div className="font-semibold mt-1">{(data.instruments || []).join(', ') || '—'}</div></div>
                <div><div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Bias TF</div><div className="font-semibold mt-1">{(data.higher_timeframes || [])[0] || '—'}</div></div>
                <div><div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Setup TF</div><div className="font-semibold mt-1">{data.primary_timeframe}</div></div>
                <div><div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Entry TF</div><div className="font-semibold mt-1">{data.execution_timeframe}</div></div>
                <div><div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">R : R</div><div className="font-semibold mt-1">1 : {data.risk_reward_ratio}</div></div>
                <div><div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Stop Type</div><div className="font-semibold mt-1 capitalize">{data.stop_loss_type}</div></div>
              </div>
              {data.description && (
                <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-800">
                  <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold mb-1">Description</div>
                  <p className="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap leading-relaxed">{data.description}</p>
                </div>
              )}
            </div>

            {/* Yes/No confirmation OR Sign In prompt */}
            {!isAuthenticated ? (
              <div className="rounded-2xl border-2 border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/30 p-5 text-center">
                <div className="font-extrabold text-slate-900 dark:text-slate-100 mb-1">Sign in to add this strategy</div>
                <p className="text-xs text-slate-500 dark:text-slate-400 mb-3">You need a Theta Algos account to import @{data.shared_by_username || 'this user'}'s strategy. It's free.</p>
                <div className="flex flex-col sm:flex-row gap-2 justify-center">
                  <button onClick={() => navigate(`/login?returnTo=${encodeURIComponent(window.location.pathname)}`)}
                    className="bg-violet-600 hover:bg-violet-700 text-white font-bold px-5 py-2.5 rounded-lg text-sm">
                    Sign in
                  </button>
                  <button onClick={() => navigate(`/register?returnTo=${encodeURIComponent(window.location.pathname)}`)}
                    className="bg-white dark:bg-slate-900 border border-violet-300 dark:border-violet-700 text-violet-700 dark:text-violet-300 font-bold px-5 py-2.5 rounded-lg text-sm">
                    Create account
                  </button>
                </div>
              </div>
            ) : importMutation.isSuccess ? (
              <div className="rounded-2xl border-2 border-emerald-300 dark:border-emerald-700 bg-emerald-50 dark:bg-emerald-950/30 p-5 text-center">
                <CheckCircle2 size={32} className="mx-auto text-emerald-600 mb-2"/>
                <div className="font-extrabold text-emerald-900 dark:text-emerald-200">Added to your strategies</div>
                <p className="text-xs text-emerald-700 dark:text-emerald-300 mt-1">"{data.name}" was shared by @{data.shared_by_username || 'a trader'} — redirecting…</p>
              </div>
            ) : (
              <div className="rounded-2xl border-2 border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5">
                <div className="text-center">
                  <div className="font-extrabold text-slate-900 dark:text-slate-100 mb-1">Add this strategy to your account?</div>
                  <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">A copy will be placed in YOUR strategies library. You can edit, backtest, or delete it — the original stays untouched.</p>
                  <div className="flex gap-2 justify-center">
                    <button onClick={() => navigate('/app/strategies')}
                      disabled={importMutation.isPending}
                      className="bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-300 font-bold px-6 py-2.5 rounded-lg text-sm">
                      No, cancel
                    </button>
                    <button onClick={() => importMutation.mutate()}
                      disabled={importMutation.isPending}
                      className="bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white font-bold px-6 py-2.5 rounded-lg text-sm">
                      {importMutation.isPending ? 'Adding…' : 'Yes, add it'}
                    </button>
                  </div>
                  {importMutation.isError && (
                    <p className="text-xs text-red-600 mt-3">{(importMutation.error as any)?.response?.data?.detail || 'Could not add. Try again.'}</p>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
