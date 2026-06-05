import { StrictMode, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Splash from './components/Splash.tsx'

// Stage 6 E1: Splash blocks App mount until the backend's
// /api/health answers. Gating at this level (rather than inside
// App) means App's useEffect hooks — which fire /api/config and
// /api/lists fetches on mount — don't run until the backend is
// actually up.
function Boot() {
  const [ready, setReady] = useState(false)
  if (!ready) return <Splash onReady={() => setReady(true)} />
  return <App />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Boot />
  </StrictMode>,
)
