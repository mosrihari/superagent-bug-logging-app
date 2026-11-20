"use client"
import { useRouter } from "next/navigation"
import { useEffect, useState } from "react"

/** Reply box on a ticket card. While the ticket's PR is open, the reply goes to the same Worker; once
 *  it is merged, a new request here becomes a new ticket. The name is remembered in this browser. */
export default function FollowUp({ id, merged }: { id: string; merged: boolean }) {
  const router = useRouter()
  const [open, setOpen] = useState(false)
  const [text, setText] = useState("")
  const [name, setName] = useState("")
  const [busy, setBusy] = useState(false)
  useEffect(() => setName(localStorage.getItem("reporter") ?? ""), [])

  async function send(e: React.FormEvent) {
    e.preventDefault()
    if (!text.trim()) return
    setBusy(true)
    localStorage.setItem("reporter", name)
    await fetch(`/api/tickets/${id}/followups`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, reporter: name }),
    })
    setText(""); setBusy(false); setOpen(false)
    router.refresh()
  }

  if (!open) return <button className="followup-toggle" onClick={() => setOpen(true)}>Reply</button>
  return (
    <form className="followup" onSubmit={send}>
      <textarea value={text} onChange={e => setText(e.target.value)} rows={2} autoFocus required
        placeholder={merged ? "Merged. A new request here becomes a new ticket." : "Ask for more or different changes on this ticket"} />
      <div className="followup-row">
        <input value={name} onChange={e => setName(e.target.value)} placeholder="Your name" />
        <button type="button" onClick={() => setOpen(false)} className="secondary">Cancel</button>
        <button disabled={busy} type="submit">{busy ? "Sending…" : "Send"}</button>
      </div>
    </form>
  )
}
