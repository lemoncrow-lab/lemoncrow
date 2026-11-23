import express from "express";
import { removeUser, saveUser } from "@lib/users";
import { audit } from "./audit";

const app = express();
app.use(express.json());

app.post("/api/users", async (req, res) => {
  await saveUser(req.body.name);
  res.status(201).end();
});

app.delete("/api/users/:id", async (req, res) => {
  await removeUser(req.params.id);
  res.status(204).end();
});

app.get("/api/ping", (req, res) => {
  audit(req.query.who);
  res.send("pong");
});

export default app;
