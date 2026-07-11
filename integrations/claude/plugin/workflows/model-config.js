import fs from "node:fs"
import path from "node:path"

const HOST_ROLE_IDS = ["code", "execute", "solve", "explore", "plan", "research", "review"]

function readSettings() {
  const workspaceRoot = process.env.CLAUDE_WORKSPACE_ROOT || process.cwd()
  const settingsPath = path.join(workspaceRoot, ".lemoncrow", "settings.json")
  try {
    const raw = fs.readFileSync(settingsPath, "utf-8")
    const parsed = JSON.parse(raw)
    return typeof parsed === "object" && parsed !== null ? parsed : {}
  } catch {
    return {}
  }
}

export function resolveClaudeRoleModel(roleId) {
  const settings = readSettings()
  const hosts = settings?.models?.hosts
  const runtimeRoles = settings?.models?.runtime?.roles
  const hostRoles = isLegacyAutoHostStub(hosts?.claude?.roles) ? undefined : hosts?.claude?.roles
  const defaultRoles = isLegacyAutoHostStub(hosts?.default?.roles) ? undefined : hosts?.default?.roles
  const candidate = roleModel(hostRoles, roleId) ?? roleModel(defaultRoles, roleId) ?? roleModel(runtimeRoles, roleId) ?? ""
  if (typeof candidate !== "string" || !candidate.trim() || candidate === "auto") {
    return undefined
  }
  return normalizeClaudeModelId(candidate.trim())
}

function roleModel(roles, roleId) {
  if (typeof roles !== "object" || roles === null) {
    return undefined
  }
  return roles[roleId] ?? roles["*"]
}

function isLegacyAutoHostStub(roles) {
  if (typeof roles !== "object" || roles === null) {
    return false
  }
  const keys = Object.keys(roles)
  return keys.length === HOST_ROLE_IDS.length && HOST_ROLE_IDS.every((roleId) => roles[roleId] === "auto")
}

function normalizeClaudeModelId(modelId) {
  return modelId.startsWith("claude-") ? modelId.replace(/(\d)\.(?=\d)/g, "$1-") : modelId
}
