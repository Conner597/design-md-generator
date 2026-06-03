import { useState } from 'react'

export default function App() {
  const [firmName, setFirmName] = useState('')
  const [firmUrl, setFirmUrl] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!firmName.trim() || !firmUrl.trim()) {
      setStatus('Enter both a firm name and a URL.')
      return
    }
    setBusy(true)
    setStatus('Scraping the site and building DESIGN.md…')
    try {
      const res = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ firm_name: firmName, firm_url: firmUrl }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || `Request failed (${res.status})`)
      }
      const { filename, content } = await res.json()
      const blob = new Blob([content], { type: 'text/markdown' })
      const href = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = href
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(href)
      setStatus(`Downloaded ${filename}`)
    } catch (err) {
      setStatus(`Error: ${err.message}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="wrap">
      <h1>Design MD Generator</h1>
      <p className="sub">
        Enter a firm name and homepage URL. The tool scrapes the site, reverse-engineers
        the design tokens and logo, and downloads a DESIGN.md.
      </p>
      <form onSubmit={handleSubmit}>
        <label>
          Firm name
          <input
            value={firmName}
            onChange={(e) => setFirmName(e.target.value)}
            placeholder="Acme Advisors"
          />
        </label>
        <label>
          Firm URL
          <input
            value={firmUrl}
            onChange={(e) => setFirmUrl(e.target.value)}
            placeholder="https://acme.com"
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? 'Working…' : 'Generate'}
        </button>
      </form>
      {status && <p className="status">{status}</p>}
    </main>
  )
}
