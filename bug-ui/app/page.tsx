import { listTickets } from "@/lib/db"

export const dynamic = "force-dynamic"

const STATUS_LABEL: Record<string, string> = {
  open: "Open",
  investigating: "Investigating",
  fix_opened: "Fix opened",
  enhancement_opened: "Enhancement opened",
  in_review: "In review",
  done: "Done",
  not_confirmed: "Could not confirm",
}

export default function Board() {
  const tickets = listTickets()
  const bugs = tickets.filter(t => t.type === "bug")
  const enhancements = tickets.filter(t => t.type === "enhancement")
  return (
    <div className="board">
      <Column title="Bugs" tickets={bugs} />
      <Column title="Enhancements" tickets={enhancements} />
    </div>
  )
}

function Column({ title, tickets }: { title: string; tickets: ReturnType<typeof listTickets> }) {
  return (
    <section className="column">
      <h2>{title} <span className="count">{tickets.length}</span></h2>
      {tickets.length === 0 && <p className="empty">Nothing here yet.</p>}
      {tickets.map(t => (
        <article key={t.id} className={`card status-${t.status}`}>
          <div className="card-head">
            <span className="ticket-id">{t.id}</span>
            <span className="status">{STATUS_LABEL[t.status] ?? t.status}</span>
          </div>
          <h3>{t.title}</h3>
          <p className="desc">{t.description}</p>
          <div className="meta">
            <span>reported by {t.reporter}</span>
            <span>via {t.source}</span>
          </div>
          {t.pr_url && <a className="pr-link" href={t.pr_url} target="_blank">View pull request →</a>}
          {t.comments.length > 0 && (
            <ul className="comments">
              {t.comments.map((c, i) => <li key={i}>{c.text}</li>)}
            </ul>
          )}
        </article>
      ))}
    </section>
  )
}
