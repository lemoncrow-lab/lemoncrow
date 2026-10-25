// TypeScript counterpart of javascript.js: the same rules apply to `.ts` files. Nothing here runs.
import * as child_process from "child_process";
import axios from "axios";

export function run(name: string, db: { query: (sql: string, params?: unknown[]) => void }, source: string): void {
  // ruleid: lemoncrow.javascript.child-process-dynamic
  child_process.exec(`ls ${name}`);
  // ruleid: lemoncrow.javascript.sql-string-building
  db.query("SELECT * FROM users WHERE name = '" + name + "'");
  // ruleid: lemoncrow.javascript.eval-dynamic
  eval(source);
  // ok: lemoncrow.javascript.sql-string-building
  db.query("SELECT * FROM users WHERE name = $1", [name]);
}

export function proxy(req: { query: { url: string } }): void {
  const target: string = req.query.url;
  // ruleid: lemoncrow.javascript.ssrf-user-controlled-url
  axios.get(target);
}
