import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../src/tokens/tokens.css'
import './gallery.css'
import { Gallery } from './Gallery'

const root = document.getElementById('root')
if (!root) throw new Error('Gallery root element is missing from index.html')

createRoot(root).render(
  <StrictMode>
    <Gallery />
  </StrictMode>,
)
