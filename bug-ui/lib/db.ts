/**
 * The ticket store. SQLite on local disk, via Node's built-in node:sqlite (no native build step) —
 * fine for `next dev`, but Vercel's serverless filesystem does not persist between invocations, so this
 * file is a placeholder for local development only. Before deploying to Vercel, swap this module for a
 * real hosted database (Neon, Vercel Postgres, or Vercel KV), keeping the same five functions so
 * nothing else in the app has to change. See README.md's "Deploying" section.
 */
import { DatabaseSync } from "node:sqlite"
import path from "path"

const db = new DatabaseSync(path.join(process.cwd(), "tickets.db"))
db.exec("PRAGMA journal_mode = WAL")
db.exec(`
  CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('bug', 'enhancement')),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    source TEXT NOT NULL,
    reporter TEXT NOT NULL,
    pr_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    comments TEXT NOT NULL DEFAULT '[]'
  )
`)

export type Ticket = {
  id: string
  type: "bug" | "enhancement"
  title: string
  description: string
  status: string
  source: string
  reporter: string
  pr_url: string | null
  created_at: string
  updated_at: string
  comments: { at: string; text: string }[]
}

function row(r: any): Ticket {
  return { ...r, comments: JSON.parse(r.comments) }
}

export function listTickets(): Ticket[] {
  return db.prepare("SELECT * FROM tickets ORDER BY updated_at DESC").all().map(row)
}

export function getTicket(id: string): Ticket | undefined {
  const r = db.prepare("SELECT * FROM tickets WHERE id = ?").get(id)
  return r ? row(r as any) : undefined
}

export function createTicket(input: { type: "bug" | "enhancement"; title: string; description: string; source: string; reporter: string }): Ticket {
  const prefix = input.type === "bug" ? "BUG" : "ENH"
  const n = (db.prepare("SELECT COUNT(*) as n FROM tickets WHERE type = ?").get(input.type) as any).n + 1
  const id = `${prefix}-${n}`
  const at = new Date().toISOString()
  db.prepare(
    "INSERT INTO tickets (id, type, title, description, status, source, reporter, created_at, updated_at, comments) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, '[]')"
  ).run(id, input.type, input.title, input.description, input.source, input.reporter, at, at)
  return getTicket(id)!
}

export function updateTicket(id: string, fields: { status?: string; pr_url?: string; comment?: string }): Ticket | undefined {
  const existing = getTicket(id)
  if (!existing) return undefined
  const comments = fields.comment ? [...existing.comments, { at: new Date().toISOString(), text: fields.comment }] : existing.comments
  db.prepare("UPDATE tickets SET status = ?, pr_url = ?, comments = ?, updated_at = ? WHERE id = ?").run(
    fields.status ?? existing.status,
    fields.pr_url ?? existing.pr_url,
    JSON.stringify(comments),
    new Date().toISOString(),
    id
  )
  return getTicket(id)
}
