import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './styles/index.css'
// V2 design language — additive layer loaded AFTER index.css so its tokens
// win inside V2 surfaces. Every selector is namespaced under .v2-root, so
// V1 screens are untouched (see styles/v2.css header for the ground rules).
import './styles/v2.css'
// Side-effect import: applies the saved theme class to <html> before first paint
import './stores/themeStore'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 30_000,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
