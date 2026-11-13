/** @type {import('next').NextConfig} */
module.exports = {
  // better-sqlite3 is a native module; keep it out of the server bundle so Next.js requires it at runtime.
  serverExternalPackages: ["better-sqlite3"],
}
