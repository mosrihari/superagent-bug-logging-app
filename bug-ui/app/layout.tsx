import "./globals.css"

export const metadata = { title: "Bug & Enhancement Board" }

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <a href="/" className="brand">Bug &amp; Enhancement Board</a>
          <a href="/new" className="new-btn">+ New ticket</a>
        </header>
        <main>{children}</main>
      </body>
    </html>
  )
}
