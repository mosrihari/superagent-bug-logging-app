import { NextRequest, NextResponse } from "next/server"
import { getTicket, updateTicket } from "@/lib/db"

const BRAIN_URL = process.env.BRAIN_URL ?? ""
const BUG_UI_TOKEN = process.env.BUG_UI_TOKEN ?? ""

// A person replies on a ticket's card. Saved on the card straight away, then handed to the brain, which
// decides: while the ticket's PR is open it goes to the same Worker; after the merge it is new work.
export async function POST(req: NextRequest, { params }: { params: { id: string } }) {
  const body = await req.json()
  const text = String(body.text ?? "").trim()
  const reporter = String(body.reporter ?? "").trim() || "anonymous"
  if (!text) return NextResponse.json({ error: "text is required" }, { status: 400 })
  if (!getTicket(params.id)) return NextResponse.json({ error: "not found" }, { status: 404 })
  const ticket = updateTicket(params.id, { comment: `Follow-up from ${reporter}: ${text}` })
  if (BRAIN_URL) {
    fetch(`${BRAIN_URL}/webhooks/bugui/followup`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-bugui-token": BUG_UI_TOKEN },
      body: JSON.stringify({ id: params.id, text, reporter }),
    }).catch(() => {}) // the brain may be restarting; the follow-up is already on the card
  }
  return NextResponse.json(ticket, { status: 201 })
}
