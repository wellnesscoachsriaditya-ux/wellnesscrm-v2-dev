import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@wellnesscrm/design-system/tokens.css'
import { App } from './App'

const root = document.getElementById('root')
if (!root) throw new Error('Root element is missing from index.html')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
