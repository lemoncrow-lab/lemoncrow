import express from "express";
import { renameUser, saveUser } from "@lib/users";
import { clean } from "@lib/text";
import { audit } from "./audit";

const app = express();
app.use(express.json());

app.post("/api/users", async (req, res) => {
  await saveUser(clean(req.body.name));
  res.status(201).end();
});

app.put("/api/users/:id", async (req, res) => {
  await renameUser(req.params.id, "renamed");
  res.status(200).end();
});

app.get("/api/ping", (req, res) => {
  audit(req.query.who);
  res.send("pong");
});

export default app;
