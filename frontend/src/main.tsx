import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { toCssVars } from './theme/tokens'
import './styles/base.css'
import './styles/components.css'

// Publish the palette to CSS before the first paint. base.css and
// components.css contain no colour literals at all, so without this the page
// renders unstyled for one frame -- and, more importantly, the shaders and the
// stylesheets would otherwise be two separate copies of the same decisions.
toCssVars()

const client = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
