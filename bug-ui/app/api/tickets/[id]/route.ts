import { NextRequest, NextResponse } from "next/server"
import { getTicket, updateTicket } from "@/lib/db"

const BUG_UI_TOKEN = process.env.BUG_UI_TOKEN ?? ""

export async function GET(_req: NextRequest, { params }: { params: { id: string } }) {
  const ticket = getTicket(params.id)
  if (!ticket) return NextResponse.json({ error: "not found" }, { status: 404 })
  return NextResponse.json(ticket)
}

// The brain calls this as its Worker progresses: investigating -> fix_opened/enhancement_opened -> done.
export async function PATCH(req: NextRequest, { params }: { params: { id: string } }) {
  const token = req.headers.get("x-bugui-token")
  if (BUG_UI_TOKEN && token !== BUG_UI_TOKEN) return NextResponse.json({ error: "forbidden" }, { status: 403 })
  const body = await req.json()
  const ticket = updateTicket(params.id, { status: body.status, pr_url: body.pr_url, comment: body.comment })
  if (!ticket) return NextResponse.json({ error: "not found" }, { status: 404 })
  return NextResponse.json(ticket)
}
