"use client"
import { useRouter } from "next/navigation"
import { useState } from "react"

export default function NewTicket() {
  const router = useRouter()
  const [type, setType] = useState<"bug" | "enhancement">("bug")
  const [title, setTitle] = useState("")
  const [description, setDescription] = useState("")
  const [reporter, setReporter] = useState("")
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    await fetch("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, title, description, reporter: reporter || "anonymous", source: "ui" }),
    })
    router.push("/")
  }

  return (
    <form className="new-ticket" onSubmit={submit}>
      <h1>File a ticket</h1>
      <label>
        Kind
        <select value={type} onChange={e => setType(e.target.value as any)}>
          <option value="bug">Bug</option>
          <option value="enhancement">Enhancement</option>
        </select>
      </label>
      <label>
        Title
        <input value={title} onChange={e => setTitle(e.target.value)} required maxLength={120} />
      </label>
      <label>
        Description
        <textarea value={description} onChange={e => setDescription(e.target.value)} required rows={5} />
      </label>
      <label>
        Your name
        <input value={reporter} onChange={e => setReporter(e.target.value)} placeholder="optional" />
      </label>
      <button disabled={busy} type="submit">{busy ? "Filing…" : "File ticket"}</button>
    </form>
  )
}
