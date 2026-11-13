import { NextRequest, NextResponse } from "next/server"
import { createTicket, listTickets } from "@/lib/db"

const BRAIN_URL = process.env.BRAIN_URL ?? ""
const BUG_UI_TOKEN = process.env.BUG_UI_TOKEN ?? ""

export async function GET() {
  return NextResponse.json(listTickets())
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  if (!body.title || !body.description || !body.type) {
    return NextResponse.json({ error: "title, description and type are required" }, { status: 400 })
  }
  const ticket = createTicket({
    type: body.type,
    title: body.title,
    description: body.description,
    source: body.source ?? "ui",
    reporter: body.reporter ?? "anonymous",
  })

  // Only notify the brain when a PERSON filed this through the web form. When the brain itself
  // created the ticket (a Telegram report, an Axiom alert), it already knows — notifying it back
  // would be an unnecessary round trip, not a bug, but worth avoiding.
  if (!body.fromBrain && BRAIN_URL) {
    fetch(`${BRAIN_URL}/webhooks/bugui`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-bugui-token": BUG_UI_TOKEN },
      body: JSON.stringify({ id: ticket.id, type: ticket.type, description: ticket.description, reporter: ticket.reporter }),
    }).catch(() => {}) // the brain may be restarting; the ticket is already saved regardless
  }

  return NextResponse.json(ticket, { status: 201 })
}
